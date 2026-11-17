"""Fail-closed privacy boundary for the observable Codex event protocol.

Callbacks are the collector's existing text/value scrubber and stable identifier
mapper. This module does not persist data, execute custom code, or claim that
unobserved file access was verified. Every custom call needs separate,
digest-bound content review before training.
"""
import copy
import json
import math
import os
import re

import nous_privacy


class ProtocolPrivacyError(ValueError):
    """Static diagnostic only; never includes a source value."""


_HIDDEN = {"reasoning", "analysis", "thinking", "redacted_thinking",
           "reasoning_text", "summary_text", "encrypted_reasoning"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_PHASES = {"commentary", "partial_answer", "final_answer", "final"}
_STATUS = {"completed", "in_progress", "incomplete"}
_USAGE = {"input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens"}
_SENSITIVE = re.compile(r"(?i)^(password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|private[_-]?key|phone|mobile|telephone|email|id[_-]?card|ssn|card[_-]?number)$")
_STATIC = re.compile(r"^[a-z][a-z0-9_]*$")
_HASH = re.compile(r"^[0-9a-f]{40,64}$")
_OMITTED = "[excluded file content omitted]"
_SCHEMA_KEYS = {"type", "properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
                "required", "additionalProperties", "items", "enum", "const", "minimum", "maximum",
                "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern",
                "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties", "anyOf", "oneOf",
                "allOf", "title", "description", "default", "examples", "$comment", "$id", "$schema", "$ref",
                "$anchor", "deprecated", "readOnly", "writeOnly", "format", "contains", "not", "if", "then",
                "else", "propertyNames", "unevaluatedProperties", "unevaluatedItems", "prefixItems",
                "minContains", "maxContains", "dependentRequired", "contentEncoding", "contentMediaType", "encrypted"}


def _fail(reason="invalid_protocol_privacy_shape"):
    raise ProtocolPrivacyError(reason)


def _shape(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        _fail()


def _json_value(value, depth=0):
    if depth > 64:
        _fail("protocol_privacy_depth_limit")
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    _fail("invalid_protocol_privacy_value")


def _strict_loads(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                _fail("invalid_protocol_arguments")
            result[key] = value
        return result
    try:
        result = json.loads(text, object_pairs_hook=pairs,
                            parse_constant=lambda _: _fail("invalid_protocol_arguments"))
        _json_value(result)
        return result
    except (ValueError, TypeError, RecursionError):
        _fail("invalid_protocol_arguments")


def _omit_calls(protocol, call_ids):
    """Clear already-normalized calls across native events and runtime audits.

    This accepts a previously scrubbed protocol. A blocked outer call also masks
    every audit body observed while it was active, so an audit cannot retain a
    second copy of content removed from the model-visible result.
    """
    _json_value(protocol)
    if not isinstance(call_ids, (set, frozenset, list, tuple)) or any(
            not isinstance(value, str) or not value for value in call_ids):
        _fail("invalid_excluded_call_ids")
    result = copy.deepcopy(protocol)
    excluded = set(call_ids)
    if not excluded:
        return result
    if not isinstance(result, dict) or not isinstance(result.get("events"), list):
        _fail()
    active = set()
    # First discover parents of explicitly blocked child IDs. Earlier sibling
    # audits are covered in the second pass once the full parent set is known.
    for event in result["events"]:
        payload = event["payload"]
        kind = event["type"]
        if kind == "custom_tool_call":
            active.add(payload["call_id"])
        elif kind == "custom_tool_call_output":
            active.discard(payload["call_id"])
        elif kind == "runtime_tool_execution" and payload["id"] in excluded:
            excluded.update(active)
    active.clear()
    affected = False
    for event in result["events"]:
        payload = event["payload"]
        kind = event["type"]
        call_id = payload.get("call_id")
        if kind == "custom_tool_call":
            active.add(call_id)
        if kind == "runtime_tool_execution" and (payload["id"] in excluded or active & excluded):
            prior = payload.get("result", {})
            payload["arguments"] = {"omitted": "excluded_file"}
            payload["result"] = {"content": [{"type": "text", "text": _OMITTED}]}
            if "isError" in prior:
                payload["result"]["isError"] = prior["isError"]
            affected = True
        elif call_id in excluded:
            if kind == "custom_tool_call":
                payload["input"] = _OMITTED
                affected = True
            elif kind == "function_call":
                payload["arguments"] = '{"omitted":"excluded_file"}'
                affected = True
            elif kind in {"custom_tool_call_output", "function_call_output"}:
                payload["output"] = _OMITTED
                affected = True
        if kind == "custom_tool_call_output":
            active.discard(call_id)
    if affected:
        quality = result["quality"]
        quality["complete"] = False
        quality["issues"] = sorted(set(quality["issues"]) | {"excluded_file_content"})
    return result


def omit_calls(protocol, call_ids):
    """Mask excluded normalized call IDs in an already-scrubbed protocol."""
    try:
        return _omit_calls(protocol, call_ids)
    except (TypeError, KeyError, IndexError, RecursionError, OverflowError):
        _fail()


def _scrub_protocol(protocol, *, clean, identifier, project=""):
    """Return (safe protocol, static issues, excluded normalized IDs, review flag).

    Known hidden events/blocks are removed. Unknown event or protocol fields are
    rejected rather than saved with a misleading ``scrubbed`` marker. Runtime
    MCP calls are audit observations: an excluded child taints every custom call
    active at its position, even when the child has a different call ID.
    """
    _json_value(protocol)
    _shape(protocol, {"schema", "adapter_version", "events", "quality"},
           {"schema", "adapter_version", "events", "quality"})
    if protocol["schema"] != "nous.codex.events/1" or protocol["adapter_version"] not in {"codex-events-1", "codex-events-1.1"}:
        _fail("unsupported_protocol_privacy_version")
    _shape(protocol["quality"], {"complete", "missing", "issues"}, {"complete", "missing", "issues"})
    quality = protocol["quality"]
    if type(quality["complete"]) is not bool or any(
            not isinstance(quality[k], list) or any(not isinstance(s, str) or not _STATIC.fullmatch(s) for s in quality[k])
            for k in ("missing", "issues")) or not isinstance(protocol["events"], list):
        _fail()
    result = {k: copy.deepcopy(protocol[k]) for k in ("schema", "adapter_version", "quality")}
    result["events"] = []
    issues, excluded, active = set(), set(), set()
    requires_review = False

    def ident(value, *, contract=False):
        if not isinstance(value, str) or not value:
            _fail()
        mapped = identifier(value)
        if not isinstance(mapped, str) or not mapped:
            _fail("invalid_protocol_identifier")
        if mapped != value:
            issues.add("contract_redacted" if contract else "redacted_metadata")
        return mapped

    def text(value):
        if not isinstance(value, str):
            _fail()
        out = clean(value)
        if not isinstance(out, str):
            _fail("invalid_protocol_scrubber_result")
        return out

    def ids(value, out):
        for key in ("id", "call_id"):
            if key in value:
                out[key] = ident(value[key])
        if "status" in value:
            if value["status"] not in _STATUS:
                _fail()
            out["status"] = value["status"]

    def blocks(value, *, mcp=False):
        if not isinstance(value, list):
            _fail()
        out = []
        for block in value:
            if not isinstance(block, dict):
                _fail()
            kind = block.get("type")
            if kind in _HIDDEN:
                continue
            field = "refusal" if kind == "refusal" and not mcp else "text"
            kinds = {"text"} if mcp else {"text", "input_text", "output_text", "refusal"}
            if kind not in kinds:
                _fail("unsupported_protocol_content")
            metadata_fields = {"annotations", "logprobs"} if field == "text" else set()
            _shape(block, {"type", field} | metadata_fields, {"type", field})
            selected = {"type": kind, field: text(block[field])}
            for metadata in ("annotations", "logprobs"):
                if metadata in block:
                    if block[metadata] != []:
                        _fail("unsupported_protocol_content_metadata")
                    selected[metadata] = []
            out.append(selected)
        return out

    def schema(value, sensitive=False):
        # JSON Schema property names are structure, not secret-value fields.
        # Defaults/examples/enums/consts under sensitive properties are data.
        if isinstance(value, bool):
            return value
        if not isinstance(value, dict) or set(value) - _SCHEMA_KEYS:
            _fail("invalid_protocol_tool_schema")
        out = {}
        for key, child in value.items():
            string_keys = {"title", "description", "$comment", "$id", "$schema", "$ref", "$anchor",
                           "pattern", "format", "contentEncoding", "contentMediaType"}
            number_keys = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
            integer_keys = {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties",
                            "minContains", "maxContains"}
            if (key in string_keys and not isinstance(child, str)
                    or key in number_keys and (type(child) not in (int, float) or (key == "multipleOf" and child <= 0))
                    or key in integer_keys and (type(child) is not int or child < 0)
                    or key in {"uniqueItems", "deprecated", "readOnly", "writeOnly", "encrypted"} and type(child) is not bool
                    or key in {"enum", "examples"} and not isinstance(child, list)
                    or key == "enum" and not child
                    or key == "required" and (not isinstance(child, list) or any(not isinstance(v, str) for v in child))):
                _fail("invalid_protocol_tool_schema")
            if key == "type":
                kinds = child if isinstance(child, list) else [child]
                if (not kinds or any(not isinstance(v, str) or v not in
                                    {"object", "array", "string", "number", "integer", "boolean", "null"} for v in kinds)
                        or len(set(kinds)) != len(kinds)):
                    _fail("invalid_protocol_tool_schema")
            if key == "dependentRequired" and (not isinstance(child, dict) or any(
                    not isinstance(v, list) or any(not isinstance(s, str) for s in v) for v in child.values())):
                _fail("invalid_protocol_tool_schema")
            if sensitive and key in {"default", "examples", "enum", "const"}:
                issues.add("contract_redacted")
                continue
            if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}:
                if not isinstance(child, dict):
                    _fail("invalid_protocol_tool_schema")
                mapped = {}
                for name, sub in child.items():
                    new_name = text(name)
                    if new_name in mapped:
                        _fail("redacted_key_collision")
                    mapped[new_name] = schema(sub, sensitive or (key == "properties" and bool(_SENSITIVE.fullmatch(name))))
                out[key] = mapped
            elif key in {"additionalProperties", "contains", "not", "if", "then", "else", "propertyNames", "unevaluatedProperties", "unevaluatedItems"} or (key == "items" and not isinstance(child, list)):
                out[key] = schema(child, sensitive)
            elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} or (key == "items" and isinstance(child, list)):
                if not isinstance(child, list):
                    _fail("invalid_protocol_tool_schema")
                out[key] = [schema(v, sensitive) for v in child]
            elif key in {"default", "examples", "enum", "const"}:
                out[key] = clean(child)
            else:
                out[text(key)] = clean(child, False)
        if out != value:
            issues.add("contract_redacted")
        return out

    def contract(value, depth=0):
        if depth > 4 or not isinstance(value, dict):
            _fail("invalid_protocol_tool_contract")
        kind = value.get("type")
        extra = {"namespace": {"tools"}, "custom": {"format"}, "function": {"parameters", "strict"}}
        if kind not in extra:
            _fail("unsupported_protocol_tool_contract")
        _shape(value, {"type", "name", "description"} | extra[kind], {"type", "name"})
        out = {"type": kind, "name": ident(value["name"], contract=True)}
        if "description" in value:
            out["description"] = text(value["description"])
        if kind == "namespace":
            if not isinstance(value.get("tools"), list):
                _fail()
            out["tools"] = [contract(v, depth + 1) for v in value["tools"]]
        elif kind == "function":
            if "parameters" not in value:
                _fail()
            out["parameters"] = schema(value["parameters"])
            if "strict" in value:
                if type(value["strict"]) is not bool:
                    _fail()
                out["strict"] = value["strict"]
        else:
            form = value.get("format")
            if not isinstance(form, dict):
                _fail()
            if form.get("type") == "text":
                _shape(form, {"type"}, {"type"})
                out["format"] = {"type": "text"}
            elif form.get("type") == "grammar":
                _shape(form, {"type", "syntax", "definition"}, {"type", "syntax", "definition"})
                if form["syntax"] not in {"lark", "regex"}:
                    _fail()
                out["format"] = {"type": "grammar", "syntax": form["syntax"], "definition": text(form["definition"])}
            else:
                _fail()
        if out != value:
            issues.add("contract_redacted")
        return out

    def file_excluded(arguments):
        paths = [v for k, v in arguments.items() if k in {"file_path", "path", "filename", "target_file"} and isinstance(v, str)] if isinstance(arguments, dict) else []
        reasons = [nous_privacy.excluded(os.path.join(project, p) if not os.path.isabs(p) else p,
                                        project, strict=True) if project else "privacy_exclusion_unverified"
                   for p in paths]
        if "privacy_exclusion_unverified" in reasons:
            issues.add("privacy_exclusion_unverified")
        # Explicit forbidden names can be rejected even without a structured path;
        # their absence does not prove that arbitrary code avoids private files.
        tokens = re.findall(r"[^\s\"'\\]+", json.dumps(arguments, ensure_ascii=False))
        hit = any(reasons) or any(nous_privacy.excluded_name(p) for p in paths + tokens)
        if hit:
            issues.add("excluded_file_content")
        return hit

    def world(value):
        _shape(value, {"full", "state"}, {"full", "state"})
        if type(value["full"]) is not bool or not isinstance(value["state"], dict):
            _fail()
        booleans = {"apps_instructions", "environments_instructions", "git_attribution", "plugins_instructions"}
        hashes = {"model_catalog", "multi_agent_usage_hint", "context_window_guidance"}
        objects = {"model": {"model", "instructions"}, "collaboration_mode": {"mode", "model", "instructions"},
                   "multi_agent_mode": {"mode", "usage_hint_hash"}, "permissions": {"instructions"}}
        out = {"full": value["full"], "state": {}}
        for key, item in value["state"].items():
            if key in booleans:
                if type(item) is not bool:
                    _fail()
                out["state"][key] = item
            elif key in hashes:
                if not isinstance(item, str) or (item and not _HASH.fullmatch(item)):
                    _fail()
                out["state"][key] = text(item)
            elif key in {"tools", "top_level_tools"}:
                if not isinstance(item, dict) or any(not isinstance(v, str) or (v and not _HASH.fullmatch(v)) for v in item.values()):
                    _fail()
                mapped = {}
                for name, digest in item.items():
                    normalized = ident(name, contract=True)
                    if normalized in mapped:
                        _fail("redacted_key_collision")
                    mapped[normalized] = text(digest)
                out["state"][key] = mapped
            elif key in {"skills", "host_skills", "realtime"}:
                field = "active" if key == "realtime" else "includeInstructions"
                _shape(item, {field}, {field})
                if type(item[field]) is not bool:
                    _fail()
                out["state"][key] = {field: item[field]}
            elif key in objects:
                _shape(item, objects[key])
                out["state"][key] = {k: text(v) for k, v in item.items()}
            else:
                _fail("unsupported_protocol_world_state")
        return out

    last_seq = 0
    for event in protocol["events"]:
        _shape(event, {"seq", "type", "payload", "model_visible"}, {"seq", "type", "payload"})
        seq, kind, payload = event["seq"], event["type"], event["payload"]
        if type(seq) is not int or seq <= last_seq or not isinstance(kind, str) or not isinstance(payload, dict):
            _fail()
        last_seq = seq
        if kind in _HIDDEN or payload.get("channel") in _HIDDEN or payload.get("phase") in _HIDDEN:
            continue
        safe = {"seq": seq, "type": kind}
        if "model_visible" in event:
            if kind != "runtime_tool_execution" or event["model_visible"] is not False:
                _fail()
            safe["model_visible"] = False
        if kind == "message":
            _shape(payload, {"type", "id", "status", "role", "content", "phase", "channel"}, {"type", "role", "content"})
            if payload["role"] not in {"system", "developer", "user", "assistant", "tool"}:
                _fail()
            out = {"type": kind, "role": payload["role"], "content": blocks(payload["content"])}
            ids(payload, out)
            for key in ("phase", "channel"):
                if key in payload:
                    if payload[key] not in _PHASES:
                        _fail()
                    out[key] = payload[key]
            if not out["content"]:
                continue
        elif kind == "additional_tools":
            _shape(payload, {"type", "id", "role", "tools"}, {"type", "role", "tools"})
            if payload["role"] != "developer" or not isinstance(payload["tools"], list):
                _fail()
            out = {"type": kind, "role": "developer", "tools": [contract(t) for t in payload["tools"]]}
            ids(payload, out)
        elif kind == "configuration_update":
            _shape(payload, {"type", "reasoning"}, {"type", "reasoning"})
            _shape(payload["reasoning"], {"effort"}, {"effort"})
            if payload["reasoning"]["effort"] not in _EFFORTS:
                _fail()
            out = copy.deepcopy(payload)
        elif kind in {"custom_tool_call", "function_call"}:
            field = "input" if kind == "custom_tool_call" else "arguments"
            _shape(payload, {"type", "id", "status", "call_id", "name", field}, {"type", "call_id", "name", field})
            if not isinstance(payload[field], str):
                _fail()
            out = {"type": kind, "name": ident(payload["name"], contract=True)}
            ids(payload, out)
            raw = payload[field] if field == "input" else _strict_loads(payload[field])
            if field == "arguments" and not isinstance(raw, dict):
                _fail("invalid_protocol_arguments")
            if file_excluded(raw):
                excluded.add(out["call_id"])
            if field == "input":
                requires_review = True
                active.add(out["call_id"])
                out[field] = text(raw)
            else:
                scrubbed = clean(raw)
                out[field] = payload[field] if scrubbed == raw else json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        elif kind in {"custom_tool_call_output", "function_call_output"}:
            _shape(payload, {"type", "id", "call_id", "output"}, {"type", "call_id", "output"})
            out = {"type": kind, "output": text(payload["output"]) if isinstance(payload["output"], str) else blocks(payload["output"])}
            ids(payload, out)
            active.discard(out["call_id"])
        elif kind == "runtime_tool_execution":
            if event.get("model_visible") is not False:
                _fail()
            _shape(payload, {"type", "id", "server", "tool", "arguments", "readOnlyHint", "status", "result"},
                   {"type", "id", "server", "tool", "arguments", "status"})
            if (payload["type"] != "McpToolCall" or not isinstance(payload["arguments"], dict)
                    or payload["status"] not in {"completed", "failed", "in_progress"}):
                _fail()
            out = {"type": "McpToolCall", "server": ident(payload["server"], contract=True),
                   "tool": ident(payload["tool"], contract=True), "arguments": clean(payload["arguments"]),
                   "id": ident(payload["id"]), "status": payload["status"]}
            if "readOnlyHint" in payload:
                if type(payload["readOnlyHint"]) is not bool:
                    _fail()
                out["readOnlyHint"] = payload["readOnlyHint"]
            r = payload.get("result", {})
            if "result" in payload:
                _shape(r, {"content", "isError", "structuredContent"})
                out["result"] = {}
                if "content" in r:
                    out["result"]["content"] = blocks(r["content"])
                if "isError" in r:
                    if type(r["isError"]) is not bool:
                        _fail()
                    out["result"]["isError"] = r["isError"]
                if "structuredContent" in r:
                    if not isinstance(r["structuredContent"], dict):
                        _fail()
                    out["result"]["structuredContent"] = clean(r["structuredContent"])
            if file_excluded(payload["arguments"]):
                excluded.add(out["id"])
                excluded.update(active)
                out["arguments"] = {"omitted": "excluded_file"}
                out["result"] = {"content": [{"type": "text", "text": _OMITTED}]}
                if "isError" in r:
                    out["result"]["isError"] = r["isError"]
        elif kind == "base_instructions":
            _shape(payload, {"text", "source"}, {"text", "source"})
            if payload["source"] not in {"session_meta.base_instructions", "session_meta.base_instructions.text"}:
                _fail()
            out = {"text": text(payload["text"]), "source": payload["source"]}
        elif kind == "world_state":
            out = world(payload)
        elif kind == "turn_context":
            _shape(payload, {"model", "effort"})
            out = {key: text(value) for key, value in payload.items()}
        elif kind == "token_usage_record":
            _shape(payload, {"usage", "turn_token_usage", "thread_token_usage"})
            out = {}
            for key, value in payload.items():
                _shape(value, _USAGE)
                if any(type(v) is not int or v < 0 for v in value.values()):
                    _fail()
                out[key] = dict(value)
        else:
            _fail("unsupported_protocol_event")
        if "type" in payload and kind != "runtime_tool_execution" and payload["type"] != kind:
            _fail()
        safe["payload"] = out
        result["events"].append(safe)

    # A later child audit can taint earlier outer input and sibling audit bodies.
    result = omit_calls(result, excluded)
    if issues:
        result["quality"]["complete"] = False
        result["quality"]["issues"] = sorted(set(result["quality"]["issues"]) | issues)
    _json_value(result)
    return result, sorted(issues), excluded, requires_review


def scrub_protocol(protocol, *, clean, identifier, project=""):
    """Return safe protocol, static issues, normalized excluded IDs, review flag."""
    try:
        return _scrub_protocol(protocol, clean=clean, identifier=identifier, project=project)
    except (TypeError, KeyError, IndexError, RecursionError, OverflowError):
        _fail()
