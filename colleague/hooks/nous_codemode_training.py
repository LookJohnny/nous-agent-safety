"""Observed Codex event serialization for supervised next-assistant training.

This is a versioned local representation, not a platform's wire/chat template.
Only visible native payloads enter the strings. Physical source positions and
runtime audit events remain in canonical records and evidence digests. This
module validates structure and quality; the enclosing exporter must additionally
verify outcome and independent content-review evidence before emitting samples.
"""
import hashlib
import json
import re

from nous_codemode_protocol import KNOWN_EXEC_GRAMMAR_SHA256, ProtocolError, validate_protocol_shape
from nous_training_schema import SchemaError, validate_schema, validate_value

PROFILE = "codemode-events-v1"
SERIALIZATION = "nous.observed-visible-events/1"
HEADER = {"schema": SERIALIZATION, "representation": "observed-visible-events"}
MAX_EVENTS = 4096
MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
MAX_TARGETS = 128
MAX_SAMPLE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,199}$")
_CALLS = {"custom_tool_call", "function_call"}
_OUTPUTS = {"custom_tool_call_output", "function_call_output"}
_AUDIT = {"runtime_tool_execution", "token_usage_record"}
_PHASES = {"commentary", "partial_answer", "final_answer", "final"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


class CodeModeError(ValueError):
    """Static reason code; never embeds source content."""


def _json(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
        text.encode("utf-8")
        return text
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CodeModeError("codemode_invalid_json") from None


def _loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CodeModeError("codemode_duplicate_json_key")
            result[key] = value
        return result
    def constant(_value):
        raise CodeModeError("codemode_invalid_json")
    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        _json(result).encode("utf-8")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, CodeModeError):
            raise
        raise CodeModeError("codemode_invalid_json") from None


def _keys(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or set(value) - set(required) - set(optional)):
        raise CodeModeError("codemode_invalid_fields")


def _string(value, *, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise CodeModeError("codemode_invalid_string")


def _blocks(blocks):
    if not isinstance(blocks, list) or not blocks:
        raise CodeModeError("codemode_empty_content")
    for block in blocks:
        if not isinstance(block, dict):
            raise CodeModeError("codemode_invalid_content")
        kind = block.get("type")
        key = "refusal" if kind == "refusal" else "text"
        if not isinstance(kind, str) or kind not in {"input_text", "output_text", "text", "refusal"}:
            raise CodeModeError("codemode_unsupported_content")
        _keys(block, {"type", key}, {"annotations", "logprobs"})
        if any(block[field] != [] for field in ("annotations", "logprobs") if field in block):
            raise CodeModeError("codemode_unsupported_content_metadata")
        _string(block[key], nonempty=False)


def _contract(item, catalog, prefix=(), depth=0):
    if depth > 4 or not isinstance(item, dict):
        raise CodeModeError("codemode_invalid_contract")
    kind, name = item.get("type"), item.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise CodeModeError("codemode_invalid_contract_name")
    if "description" in item:
        _string(item["description"], nonempty=False)
    path = (*prefix, name)
    if kind == "namespace":
        _keys(item, {"type", "name", "tools"}, {"description"})
        if not isinstance(item["tools"], list) or not item["tools"]:
            raise CodeModeError("codemode_empty_catalog")
        for child in item["tools"]:
            _contract(child, catalog, path, depth + 1)
        return
    full_name = ".".join(path)
    if full_name in catalog:
        raise CodeModeError("codemode_duplicate_contract")
    if kind == "custom":
        _keys(item, {"type", "name", "format"}, {"description"})
        form = item["format"]
        if form == {"type": "text"}:
            pass
        elif (isinstance(form, dict) and set(form) == {"type", "syntax", "definition"}
              and form.get("type") == "grammar" and form.get("syntax") == "lark"
              and isinstance(form.get("definition"), str)
              and hashlib.sha256(form["definition"].encode()).hexdigest() == KNOWN_EXEC_GRAMMAR_SHA256):
            pass
        else:
            raise CodeModeError("codemode_unsupported_grammar")
    elif kind == "function":
        _keys(item, {"type", "name", "parameters"}, {"description", "strict"})
        if "strict" in item and type(item["strict"]) is not bool:
            raise CodeModeError("codemode_invalid_function_strict")
        try:
            validate_schema(item["parameters"])
        except SchemaError as error:
            raise CodeModeError("codemode_" + str(error)) from None
    else:
        raise CodeModeError("codemode_unsupported_contract")
    catalog[full_name] = item


def _resolve(name, catalog):
    found = [item for full, item in catalog.items() if full == name or item["name"] == name]
    if len(found) != 1:
        raise CodeModeError("codemode_tool_contract_ambiguous" if found else "codemode_tool_contract_missing")
    return found[0]


def _assistant(payload):
    kind = payload.get("type")
    return isinstance(kind, str) and (kind in _CALLS or (kind == "message" and payload.get("role") == "assistant"))


def _validate_payloads(payloads, *, complete):
    catalog, pending, calls, ids = {}, {}, set(), set()
    base, user, last = False, False, None
    for payload in payloads:
        if not isinstance(payload, dict):
            raise CodeModeError("codemode_invalid_event")
        kind = payload.get("type")
        if not isinstance(kind, str):
            raise CodeModeError("codemode_invalid_event_type")
        for field in ("id", "call_id"):
            if field in payload:
                _string(payload[field])
        if "id" in payload:
            if payload["id"] in ids:
                raise CodeModeError("codemode_duplicate_item_id")
            ids.add(payload["id"])
        if "status" in payload and payload["status"] != "completed":
            raise CodeModeError("codemode_incomplete_status")
        if kind == "base_instructions":
            _keys(payload, {"type", "text", "source"})
            _string(payload["text"])
            if payload["source"] not in {"session_meta.base_instructions", "session_meta.base_instructions.text"}:
                raise CodeModeError("codemode_invalid_base_source")
            if user or base:
                raise CodeModeError("codemode_late_base_instructions")
            base = True
        elif kind == "configuration_update":
            _keys(payload, {"type", "reasoning"})
            _keys(payload["reasoning"], {"effort"})
            if not isinstance(payload["reasoning"]["effort"], str) or payload["reasoning"]["effort"] not in _EFFORTS:
                raise CodeModeError("codemode_unsupported_configuration")
        elif kind == "additional_tools":
            _keys(payload, {"type", "role", "tools"}, {"id"})
            if payload["role"] != "developer" or not isinstance(payload["tools"], list) or not payload["tools"]:
                raise CodeModeError("codemode_invalid_catalog")
            for item in payload["tools"]:
                _contract(item, catalog)
        elif kind == "message":
            _keys(payload, {"type", "role", "content"}, {"id", "status", "phase", "channel"})
            if not isinstance(payload["role"], str) or payload["role"] not in {"system", "developer", "user", "assistant"}:
                raise CodeModeError("codemode_invalid_role")
            _blocks(payload["content"])
            for field in ("phase", "channel"):
                if field in payload and (not isinstance(payload[field], str) or payload[field] not in _PHASES):
                    raise CodeModeError("codemode_unsupported_phase")
            if payload["role"] == "system":
                if user:
                    raise CodeModeError("codemode_late_system_context")
                base = True
            if payload["role"] == "user":
                if pending:
                    raise CodeModeError("codemode_pending_tool_call")
                user = True
            if payload["role"] == "assistant":
                if not base or not user:
                    raise CodeModeError("codemode_context_missing")
                if pending:
                    raise CodeModeError("codemode_pending_tool_call")
            last = payload
        elif kind in _CALLS:
            key = "input" if kind == "custom_tool_call" else "arguments"
            _keys(payload, {"type", "call_id", "name", key}, {"id", "status"})
            _string(payload["name"])
            _string(payload[key])
            if not base or not user:
                raise CodeModeError("codemode_context_missing")
            if payload["call_id"] in calls:
                raise CodeModeError("codemode_duplicate_call")
            contract = _resolve(payload["name"], catalog)
            if contract["type"] != ("custom" if kind == "custom_tool_call" else "function"):
                raise CodeModeError("codemode_tool_type_mismatch")
            if kind == "function_call":
                arguments = _loads(payload["arguments"])
                if not isinstance(arguments, dict):
                    raise CodeModeError("codemode_function_arguments_not_object")
                try:
                    validate_value(arguments, contract["parameters"])
                except SchemaError as error:
                    raise CodeModeError("codemode_" + str(error)) from None
            calls.add(payload["call_id"])
            pending[payload["call_id"]] = kind
            last = payload
        elif kind in _OUTPUTS:
            _keys(payload, {"type", "call_id", "output"}, {"id", "is_error"})
            if "is_error" in payload and type(payload["is_error"]) is not bool:
                raise CodeModeError("codemode_invalid_error_flag")
            if isinstance(payload["output"], list):
                _blocks(payload["output"])
            elif not isinstance(payload["output"], str):
                raise CodeModeError("codemode_invalid_output")
            if pending.pop(payload["call_id"], None) != kind.removesuffix("_output"):
                raise CodeModeError("codemode_unmatched_tool_output")
            last = payload
        else:
            raise CodeModeError("codemode_unsupported_event")
    if not base or not user:
        raise CodeModeError("codemode_context_missing")
    if complete:
        if pending:
            raise CodeModeError("codemode_pending_tool_call")
        if (not last or last.get("type") != "message" or last.get("role") != "assistant"
                or last.get("phase") in {"commentary", "partial_answer"}
                or last.get("channel") in {"commentary", "partial_answer"}):
            raise CodeModeError("codemode_final_response_missing")


def _quality(quality):
    if (not isinstance(quality, dict) or quality.get("complete") is not True
            or quality.get("issues") != [] or quality.get("missing") != []):
        raise CodeModeError("codemode_quality_incomplete")


def _events(record, require_quality, *, complete=True):
    if not isinstance(record, dict) or record.get("schema") != "nous.trajectory/2":
        raise CodeModeError("codemode_schema_required")
    privacy = record.get("privacy", {})
    if not isinstance(privacy, dict) or privacy.get("scrubbed") is not True:
        raise CodeModeError("codemode_privacy_not_scrubbed")
    protocol = record.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("schema") != "nous.codex.events/1":
        raise CodeModeError("codemode_protocol_required")
    if require_quality:
        _quality(record.get("quality"))
        _quality(protocol.get("quality"))
    events = protocol.get("events")
    if not isinstance(events, list) or not events:
        raise CodeModeError("codemode_events_empty")
    if len(events) > MAX_EVENTS or len(_json(protocol).encode()) > MAX_PROTOCOL_BYTES:
        raise CodeModeError("codemode_protocol_size_limit")
    try:
        validate_protocol_shape(protocol)
    except ProtocolError:
        raise CodeModeError("codemode_invalid_protocol_shape") from None
    visible, positions, last_seq = [], [], 0
    for i, event in enumerate(events):
        _keys(event, {"seq", "type", "payload"}, {"model_visible"})
        if type(event["seq"]) is not int or event["seq"] <= last_seq:
            raise CodeModeError("codemode_event_order")
        last_seq = event["seq"]
        if not isinstance(event["payload"], dict):
            raise CodeModeError("codemode_invalid_event")
        if event["type"] in {"world_state", "turn_context"}:
            raise CodeModeError("codemode_unresolved_runtime_context")
        if event["type"] in _AUDIT:
            if event.get("model_visible", False) is not False:
                raise CodeModeError("codemode_runtime_visibility")
            continue
        if event.get("model_visible", True) is not True:
            raise CodeModeError("codemode_hidden_visible_event")
        payload = event["payload"]
        if event["type"] == "base_instructions":
            if "type" in payload:
                raise CodeModeError("codemode_invalid_base_event")
            payload = {"type": "base_instructions", **payload}
        elif event["type"] != payload.get("type"):
            raise CodeModeError("codemode_event_type_mismatch")
        visible.append(payload)
        positions.append(i)
    _json(protocol).encode("utf-8")
    _validate_payloads(visible, complete=complete)
    return events, visible, positions


def _serialize(payloads):
    return "".join(_json(item) + "\n" for item in payloads)


def samples(record, require_quality=True):
    """Return [(pure training fields, canonical event-index range), ...]."""
    if not require_quality:
        return negative_samples(record)
    events, visible, positions = _events(record, require_quality)
    result, total_bytes = [], 0
    i = 0
    while i < len(visible):
        if not _assistant(visible[i]):
            i += 1
            continue
        start = i
        i += 1
        while (i < len(visible) and _assistant(visible[i])
               and positions[i] == positions[i - 1] + 1):
            i += 1
        sample = {"prompt": _serialize([HEADER, *visible[:start]]),
                  "completion": _serialize(visible[start:i])}
        target = {"start": positions[start], "end": positions[i - 1] + 1}
        size = len(sample["prompt"].encode()) + len(sample["completion"].encode())
        total_bytes += size
        if size > MAX_SAMPLE_BYTES or total_bytes > MAX_TOTAL_BYTES or len(result) >= MAX_TARGETS:
            raise CodeModeError("codemode_training_size_limit")
        validate_sample(sample)
        result.append((sample, target))
    if not result:
        raise CodeModeError("codemode_no_assistant_target")
    return result


def negative_samples(record):
    """Conflict keys from legal observed prefixes; NEVER positive eligibility.

    A rejected run's later missing result, incomplete final, or unsupported suffix
    cannot erase a preceding identical assistant target. Prefixes are validated
    in their original order; after an invalid prefix no later target is used.
    The caller must bind an actual negative annotation to the full record digest.
    """
    if not isinstance(record, dict) or not isinstance(record.get("protocol"), dict):
        raise CodeModeError("codemode_protocol_required")
    protocol = record["protocol"]
    events = protocol.get("events")
    if not isinstance(events, list) or not events:
        raise CodeModeError("codemode_events_empty")
    if len(events) > MAX_EVENTS or len(_json(protocol).encode()) > MAX_PROTOCOL_BYTES:
        raise CodeModeError("codemode_protocol_size_limit")
    result, total_bytes, index = [], 0, 0
    while index < len(events):
        event = events[index]
        target_event = (isinstance(event, dict) and isinstance(event.get("payload"), dict)
                        and _assistant(event["payload"]))
        if not target_event:
            index += 1
            continue
        start = index
        index += 1
        while (index < len(events) and isinstance(events[index], dict)
               and isinstance(events[index].get("payload"), dict) and _assistant(events[index]["payload"])):
            index += 1
        prefix_record = {**record, "protocol": {**protocol, "events": events[:index]}}
        try:
            _prefix, visible, positions = _events(prefix_record, False, complete=False)
            split = positions.index(start)
            sample = {"prompt": _serialize([HEADER, *visible[:split]]),
                      "completion": _serialize(visible[split:])}
            validate_sample(sample)
        except CodeModeError:
            break
        size = len(sample["prompt"].encode()) + len(sample["completion"].encode())
        total_bytes += size
        if size > MAX_SAMPLE_BYTES or total_bytes > MAX_TOTAL_BYTES or len(result) >= MAX_TARGETS:
            raise CodeModeError("codemode_training_size_limit")
        result.append((sample, {"start": start, "end": index}))
    return result


def sample_for(record, target, require_quality=True):
    """Recompute an exact emitted target; do not permit arbitrary partial ranges."""
    _keys(target, {"start", "end"})
    if any(type(target[key]) is not int for key in target) or target["start"] < 0 or target["end"] <= target["start"]:
        raise CodeModeError("codemode_invalid_target")
    for sample, position in samples(record, require_quality=require_quality):
        if position == target:
            return sample
    raise CodeModeError("codemode_target_not_found")


def validate_sample(sample):
    """Validate standalone training fields; source/evidence binding is external."""
    _keys(sample, {"prompt", "completion"})
    _json(sample)
    if any(not isinstance(sample[key], str) for key in sample):
        raise CodeModeError("codemode_invalid_string")
    if sum(len(sample[key].encode("utf-8")) for key in sample) > MAX_SAMPLE_BYTES:
        raise CodeModeError("codemode_training_size_limit")
    parts = []
    for key in ("prompt", "completion"):
        text = sample[key]
        _string(text)
        if not text.endswith("\n"):
            raise CodeModeError("codemode_serialization_boundary")
        rows = [_loads(line) for line in text.split("\n")[:-1]]
        if _serialize(rows) != text:
            raise CodeModeError("codemode_noncanonical_serialization")
        parts.append(rows)
    prompt, completion = parts
    if not prompt or prompt[0] != HEADER or not completion:
        raise CodeModeError("codemode_serialization_header")
    if any(not isinstance(item, dict) or not _assistant(item) for item in completion):
        raise CodeModeError("codemode_nonassistant_target")
    _validate_payloads(prompt[1:] + completion, complete=False)
    return True
