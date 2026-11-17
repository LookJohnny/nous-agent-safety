"""Read Codex protocol events without pretending custom tools are functions.

This parser is an IN-MEMORY, UNSCRUBBED intermediate representation. Its result
can contain private message text, tool arguments and tool descriptions. A caller
must obtain collection authorization and scrub the entire result before any
persistence. It must not log this result or bypass the collector's privacy gate.

Only known event shapes are copied. Hidden reasoning/analysis and identity-bearing
session metadata are excluded. Unknown visible shapes are not copied and make
quality incomplete. ``seq`` is the original physical JSONL line number; omitted
lines leave gaps. Runtime snapshots are audit context, not fabricated model input.
The parser never executes code, resolves hashes, fetches URLs, or reads tool paths.
"""
import copy
import hashlib
import json
import re
from nous_transcript import TranscriptReadError, _check_bytes, _iter_lines, read_transcript_bytes


SCHEMA = "nous.codex.events/1"
ADAPTER_VERSION = "codex-events-1.1"
SUPPORTED_ADAPTER_VERSIONS = {"codex-events-1", ADAPTER_VERSION}
# This is a source-format observation, not proof of the producer's identity.
# Other versions retain the generic unknown-field gate until separately checked.
OBSERVED_RUNTIME_VERSION = "0.169.0-alpha.11.1"
_U64_MAX = (1 << 64) - 1
_OBSERVED_HISTORY_METADATA = {
    "history_sequence", "sampled_through_sequence", "client_authored",
    "harness_authored_configuration", "fallback_token_limit_override",
    "user_input_order", "retained_source", "mcp_attribution",
}
MAX_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
KNOWN_EXEC_GRAMMAR = (
    "\nstart: pragma_source | plain_source\n"
    "pragma_source: PRAGMA_LINE NEWLINE SOURCE\n"
    "plain_source: SOURCE\n\n"
    "PRAGMA_LINE: /[ \\t]*\\/\\/ @exec:[^\\r\\n]*/\n"
    "NEWLINE: /\\r?\\n/\nSOURCE: /[\\s\\S]+/\n"
)
KNOWN_EXEC_GRAMMAR_SHA256 = hashlib.sha256(KNOWN_EXEC_GRAMMAR.encode()).hexdigest()
_HIDDEN = {"reasoning", "analysis", "thinking", "redacted_thinking",
           "reasoning_text", "summary_text", "encrypted_reasoning"}
_ROLES = {"system", "developer", "user", "assistant", "tool"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_STATUS = {"completed", "in_progress", "incomplete"}
_PHASES = {"commentary", "partial_answer", "final_answer", "final"}
_USAGE_KEYS = {"input_tokens", "cached_input_tokens", "cache_write_input_tokens",
               "output_tokens", "reasoning_output_tokens", "total_tokens"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,199}$")
_HASH = re.compile(r"^[0-9a-f]{40,64}$")
_OMIT = {"internal_chat_message_metadata_passthrough", "encrypted_content"}


class DuplicateKey(ValueError):
    pass


class ProtocolError(ValueError):
    """Static, safe-to-log protocol validation error (never includes input)."""


def _loads(text):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKey("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("non-finite JSON number")

    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)


class _Parser:
    def __init__(self):
        self.events = []
        self.issues = []
        self.missing = ["system_prompt", "tool_definitions"]
        self.calls = {}
        self.results = set()
        self.item_ids = set()
        self.mirror_ids = set()
        self.native_user_text = set()
        self.mirror_user_text = set()
        self.last_visible = None
        self.runtime_version = None
        self.last_ordinal = None
        self.session_metadata_seen = False

    def issue(self, issue, missing=None):
        if issue not in self.issues:
            self.issues.append(issue)
        if missing and missing not in self.missing:
            self.missing.append(missing)

    def found(self, key):
        if key in self.missing:
            self.missing.remove(key)

    def add(self, seq, kind, payload):
        self.events.append({"seq": seq, "type": kind, "payload": payload})

    def fields(self, value, allowed, issue="unsupported_event_fields"):
        if set(value) - set(allowed) - _OMIT:
            self.issue(issue, "visible_content")

    def blocks(self, value):
        if not isinstance(value, list):
            self.issue("invalid_content", "visible_content")
            return []
        out = []
        for block in value:
            if not isinstance(block, dict):
                self.issue("unsupported_content", "visible_content")
                continue
            kind = block.get("type")
            if kind in _HIDDEN:
                continue
            if kind in {"input_text", "output_text", "text"} and isinstance(block.get("text"), str):
                self.fields(block, {"type", "text", "annotations", "logprobs"}, "unsupported_content_fields")
                selected = {"type": kind, "text": block["text"]}
                for key in ("annotations", "logprobs"):
                    if key in block:
                        if block[key] == []:
                            selected[key] = []
                        else:
                            self.issue("unsupported_content_fields", "visible_content")
                out.append(selected)
            elif kind == "refusal" and isinstance(block.get("refusal"), str):
                self.fields(block, {"type", "refusal"}, "unsupported_content_fields")
                out.append({"type": kind, "refusal": block["refusal"]})
            else:
                self.issue("unsupported_content", "visible_content")
        return out

    def identity(self, value, result):
        # IDs remain exact here so call/result mappings survive; the collector
        # performs consistent privacy-safe ID replacement before persistence.
        for key in ("id", "call_id"):
            if key in value:
                if isinstance(value[key], str) and value[key]:
                    result[key] = value[key]
                else:
                    self.issue("invalid_item_identity", "event_identity")
        if "status" in value:
            if value["status"] in _STATUS:
                result["status"] = value["status"]
            else:
                self.issue("unknown_item_status", "visible_content")
        if "id" in result:
            if result["id"] in self.item_ids:
                self.issue("duplicate_item_id", "event_identity")
            self.item_ids.add(result["id"])

    def contract(self, value, depth=0):
        if not isinstance(value, dict) or depth > 4:
            self.issue("invalid_tool_contract", "tool_definitions")
            return None
        kind, name = value.get("type"), value.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            self.issue("invalid_tool_contract", "tool_definitions")
            return None
        result = {"type": kind, "name": name}
        if "description" in value:
            if isinstance(value["description"], str):
                result["description"] = value["description"]
            else:
                self.issue("invalid_tool_contract", "tool_definitions")
        if kind == "namespace":
            self.fields(value, {"type", "name", "description", "tools"}, "unsupported_tool_contract_fields")
            if not isinstance(value.get("tools"), list):
                self.issue("invalid_tool_contract", "tool_definitions")
                return None
            result["tools"] = [t for x in value["tools"] if (t := self.contract(x, depth + 1))]
            names = [t["name"] for t in result["tools"]]
            if len(names) != len(set(names)):
                self.issue("duplicate_tool_contract", "tool_definitions")
        elif kind == "custom":
            self.fields(value, {"type", "name", "description", "format"}, "unsupported_tool_contract_fields")
            form = value.get("format", {"type": "text"})
            if not isinstance(form, dict):
                self.issue("invalid_custom_format", "tool_definitions")
                return None
            if form.get("type") == "text" and set(form) == {"type"}:
                result["format"] = {"type": "text"}
            elif (form.get("type") == "grammar" and form.get("syntax") in {"lark", "regex"}
                  and isinstance(form.get("definition"), str) and form["definition"]
                  and set(form) == {"type", "syntax", "definition"}):
                result["format"] = copy.deepcopy(form)
            else:
                self.issue("invalid_custom_format", "tool_definitions")
                return None
        elif kind == "function":
            self.fields(value, {"type", "name", "description", "parameters", "strict"}, "unsupported_tool_contract_fields")
            if not isinstance(value.get("parameters"), dict):
                self.issue("invalid_function_parameters", "tool_definitions")
                return None
            result["parameters"] = copy.deepcopy(value["parameters"])
            if "strict" in value:
                if type(value["strict"]) is bool:
                    result["strict"] = value["strict"]
                else:
                    self.issue("invalid_function_strict", "tool_definitions")
        else:
            self.issue("unsupported_tool_contract", "tool_definitions")
            return None
        return result

    def response(self, seq, value):
        kind = value.get("type")
        if kind in _HIDDEN or value.get("channel") in _HIDDEN or value.get("phase") in _HIDDEN:
            return
        if kind == "message":
            self.fields(value, {"type", "id", "status", "role", "content", "phase", "channel"})
            if value.get("role") not in _ROLES:
                self.issue("unknown_role", "visible_content")
                return
            blocks = self.blocks(value.get("content"))
            if not blocks:
                return
            result = {"type": kind, "role": value["role"], "content": blocks}
            self.identity(value, result)
            for key in ("phase", "channel"):
                if key in value:
                    if isinstance(value[key], str) and value[key] in _PHASES:
                        result[key] = value[key]
                    else:
                        self.issue("unknown_message_phase", "visible_content")
            if value["role"] == "system":
                self.found("system_prompt")
            if value["role"] == "user":
                self.native_user_text.add(tuple(x["text"] for x in blocks if "text" in x))
            self.add(seq, kind, result)
            self.last_visible = result
        elif kind == "additional_tools":
            self.fields(value, {"type", "id", "role", "tools"})
            if value.get("role") != "developer" or not isinstance(value.get("tools"), list):
                self.issue("invalid_additional_tools", "tool_definitions")
                return
            contracts = [t for x in value["tools"] if (t := self.contract(x))]
            names = [t["name"] for t in contracts]
            if len(names) != len(set(names)):
                self.issue("duplicate_tool_contract", "tool_definitions")
            result = {"type": kind, "role": "developer", "tools": contracts}
            self.identity(value, result)
            self.add(seq, kind, result)
            if contracts:
                self.found("tool_definitions")
        elif kind == "configuration_update":
            self.fields(value, {"type", "reasoning"}, "unsupported_configuration_update")
            reasoning = value.get("reasoning")
            if not isinstance(reasoning, dict) or set(reasoning) != {"effort"} or reasoning.get("effort") not in _EFFORTS:
                self.issue("unsupported_configuration_update", "runtime_configuration")
                return
            self.add(seq, kind, {"type": kind, "reasoning": {"effort": reasoning["effort"]}})
        elif kind in {"custom_tool_call", "function_call"}:
            argument = "input" if kind == "custom_tool_call" else "arguments"
            self.fields(value, {"type", "id", "status", "call_id", "name", argument})
            if (not isinstance(value.get("name"), str) or not value["name"] or not isinstance(value.get(argument), str)
                    or not isinstance(value.get("call_id"), str) or not value["call_id"]):
                self.issue("invalid_tool_call", "tool_execution")
                return
            result = {"type": kind, "name": value["name"], argument: value[argument]}
            self.identity(value, result)
            call_id = result.get("call_id")
            if not call_id or call_id in self.calls:
                self.issue("invalid_call_identity", "tool_execution")
            else:
                self.calls[call_id] = kind
            if kind == "function_call":
                try:
                    if not isinstance(_loads(value[argument]), dict):
                        raise ValueError()
                except (ValueError, RecursionError):
                    self.issue("invalid_function_arguments", "tool_execution")
            self.add(seq, kind, result)
            self.last_visible = result
        elif kind in {"custom_tool_call_output", "function_call_output"}:
            self.fields(value, {"type", "id", "call_id", "output"})
            if not isinstance(value.get("call_id"), str) or not value["call_id"]:
                self.issue("invalid_call_identity", "tool_execution")
                return
            output = value.get("output")
            if isinstance(output, list):
                output = self.blocks(output)
            elif not isinstance(output, str):
                self.issue("invalid_tool_output", "tool_execution")
                return
            result = {"type": kind, "output": output}
            self.identity(value, result)
            call_id = result.get("call_id")
            if (not call_id or self.calls.get(call_id) != kind.removesuffix("_output")
                    or call_id in self.results):
                self.issue("unmatched_tool_output", "tool_execution")
            if call_id:
                self.results.add(call_id)
            self.add(seq, kind, result)
            self.last_visible = result
        else:
            self.issue("unknown_response_item", "visible_content")

    def usage(self, seq, value):
        result = {}
        self.fields(value, {"thread_id", "turn_id", "session_id", "root_turn_id", "response_id",
                            "usage", "turn_token_usage", "thread_token_usage"}, "unknown_usage_fields")
        for key in ("usage", "turn_token_usage", "thread_token_usage"):
            if key not in value:
                continue
            item = value[key]
            if not isinstance(item, dict):
                self.issue("invalid_usage")
                continue
            if set(item) - _USAGE_KEYS:
                self.issue("unknown_usage_fields")
            result[key] = {k: v for k, v in item.items() if k in _USAGE_KEYS and type(v) is int and v >= 0}
            if len(result[key]) != len(item):
                self.issue("invalid_usage")
        self.add(seq, "token_usage_record", result)

    def world(self, seq, value):
        # These are opaque runtime references, NOT replacement prompt text. No
        # environment paths, dates, user IDs or arbitrary state bodies are copied.
        self.issue("unresolved_world_state", "runtime_context_semantics")
        result = {"full": value.get("full") if type(value.get("full")) is bool else None, "state": {}}
        state = value.get("state")
        if not isinstance(state, dict) or result["full"] is None:
            self.issue("invalid_world_state", "runtime_context")
            return
        self.fields(value, {"full", "state"}, "unsupported_world_state_fields")
        booleans = {"apps_instructions", "environments_instructions", "git_attribution", "plugins_instructions"}
        hashes = {"model_catalog", "multi_agent_usage_hint", "context_window_guidance"}
        objects = {"model": {"model", "instructions"}, "collaboration_mode": {"mode", "model", "instructions"},
                   "multi_agent_mode": {"mode", "usage_hint_hash"}, "permissions": {"instructions"}}
        known_omitted = {"agents_md", "environments", "managed_developer_instructions", "persistent_mode"}
        for key, item in state.items():
            if key in booleans and type(item) is bool:
                result["state"][key] = item
            elif key in hashes and isinstance(item, str) and (not item or _HASH.fullmatch(item)):
                result["state"][key] = item
            elif key in {"tools", "top_level_tools"} and isinstance(item, dict):
                result["state"][key] = {k: v for k, v in item.items()
                                        if _NAME.fullmatch(k) and isinstance(v, str) and (not v or _HASH.fullmatch(v))}
                if len(result["state"][key]) != len(item):
                    self.issue("unsupported_world_state_fields", "runtime_context")
            elif key in {"skills", "host_skills"} and isinstance(item, dict) and type(item.get("includeInstructions")) is bool:
                result["state"][key] = {"includeInstructions": item["includeInstructions"]}
                if set(item) != {"includeInstructions"}:
                    self.issue("unsupported_world_state_fields", "runtime_context")
            elif key == "realtime" and isinstance(item, dict) and type(item.get("active")) is bool:
                result["state"][key] = {"active": item["active"]}
            elif key in objects and isinstance(item, dict):
                selected = {}
                for field in objects[key]:
                    val = item.get(field)
                    if isinstance(val, str) and ((_HASH.fullmatch(val) if field in {"instructions", "usage_hint_hash"}
                                                 else _NAME.fullmatch(val))):
                        selected[field] = val
                result["state"][key] = selected
            elif key not in known_omitted:
                self.issue("unsupported_world_state_fields", "runtime_context")
        self.add(seq, "world_state", result)

    def record_envelope(self, row):
        """Check known storage ordering, without converting host state to input.

        The pinned public source calls ordinal an optional u64 storage sequence.
        History metadata contains sampling/retention state and is deliberately
        not dismissed as telemetry: only its static diagnostic is retained.
        """
        kind, payload = row.get("type"), row["payload"]
        if kind == "session_meta":
            version = payload.get("cli_version")
            if self.session_metadata_seen and version != self.runtime_version:
                self.issue("runtime_version_changed", "source_format")
            self.session_metadata_seen = True
            self.runtime_version = version if isinstance(version, str) else None
        allowed = {"type", "timestamp", "payload"}
        if self.runtime_version == OBSERVED_RUNTIME_VERSION:
            allowed.update({"ordinal", "metadata"})
            if "ordinal" in row:
                value = row["ordinal"]
                if type(value) is not int or not 0 <= value <= _U64_MAX:
                    self.issue("invalid_record_ordinal", "source_order")
                elif self.last_ordinal is not None and value <= self.last_ordinal:
                    self.issue("record_ordinal_order_violation", "source_order")
                else:
                    self.last_ordinal = value
            if "metadata" in row:
                metadata = row["metadata"]
                if kind != "response_item" or not isinstance(metadata, dict):
                    self.issue("invalid_history_metadata", "sampling_context")
                elif metadata:
                    # Even the known names affect sampling, retention, or
                    # execution attribution. They are never silently ignored.
                    self.issue("unresolved_history_metadata", "sampling_context")
                    if set(metadata) - _OBSERVED_HISTORY_METADATA:
                        self.issue("unsupported_history_metadata_fields", "sampling_context")
        self.fields(row, allowed, "unsupported_record_fields")

    def row(self, seq, row):
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            self.issue("invalid_record", "visible_content")
            return
        kind, payload = row.get("type"), row["payload"]
        self.record_envelope(row)
        if kind == "response_item":
            self.response(seq, payload)
        elif kind == "session_meta":
            base = payload.get("base_instructions")
            if isinstance(base, str):
                text, source = base, "session_meta.base_instructions"
            elif isinstance(base, dict) and isinstance(base.get("text"), str):
                text, source = base["text"], "session_meta.base_instructions.text"
            else:
                text, source = None, None
            if text:
                self.add(seq, "base_instructions", {"text": text, "source": source})
                self.found("system_prompt")
        elif kind == "world_state":
            self.world(seq, payload)
        elif kind == "token_usage_record":
            self.usage(seq, payload)
        elif kind == "turn_context":
            # This runtime record may carry permissions and context hashes. It is
            # not harmless telemetry; only model/effort are retained as hints.
            selected = {k: v for k, v in payload.items() if k in {"model", "effort"}
                        and isinstance(v, str) and _NAME.fullmatch(v)}
            self.add(seq, "turn_context", selected)
            self.issue("unresolved_turn_context", "runtime_context_semantics")
        elif kind == "event_msg":
            self.event_message(seq, payload)
        else:
            self.issue("unknown_record_type", "visible_content")

    def event_message(self, seq, payload):
        kind = payload.get("type")
        if kind in {"agent_reasoning", "agent_reasoning_delta", "agent_reasoning_raw_content",
                    "agent_reasoning_raw_content_delta"}:
            return
        if kind in {"task_started", "task_complete", "token_count"}:
            # Lifecycle/counter mirrors contain no unique training target. In
            # particular task_complete.last_agent_message is not re-injected.
            return
        if kind == "item_completed":
            item = payload.get("item", {})
            if not isinstance(item, dict):
                self.issue("invalid_completed_item", "visible_content")
                return
            if item.get("type") in {"UserMessage", "AgentMessage", "Reasoning"}:
                # These are presentation mirrors of native response items.
                if item.get("type") == "UserMessage":
                    # UI user IDs may differ from wire message IDs. Compare only
                    # the known plaintext mirror; never synthesize model input.
                    content = item.get("content")
                    if (isinstance(content, list) and content and all(isinstance(x, dict)
                            and x.get("type") == "text" and isinstance(x.get("text"), str)
                            and set(x) <= {"type", "text", "text_elements"}
                            and x.get("text_elements", []) == [] for x in content)):
                        self.mirror_user_text.add(tuple(x["text"] for x in content))
                    else:
                        self.issue("unsupported_completion_mirror", "visible_content")
                elif item.get("type") == "AgentMessage":
                    if isinstance(item.get("id"), str):
                        self.mirror_ids.add(item["id"])
                    else:
                        self.issue("invalid_completed_item", "visible_content")
                return
            if item.get("type") == "McpToolCall":
                self.runtime_execution(seq, item)
                return
            self.issue("unsupported_runtime_execution", "runtime_execution")
            return
        if kind == "thread_settings_applied":
            self.issue("unresolved_thread_settings", "runtime_context_semantics")
            return
        self.issue("unknown_event_message", "visible_content")

    def runtime_execution(self, seq, item):
        self.fields(item, {"type", "id", "server", "tool", "arguments", "readOnlyHint",
                           "status", "result", "duration"}, "unsupported_runtime_execution_fields")
        if (item.get("type") != "McpToolCall"
                or any(not isinstance(item.get(k), str) or not item[k] for k in ("id", "server", "tool", "status"))
                or item.get("status") not in {"completed", "failed", "in_progress"}
                or not isinstance(item.get("arguments"), dict)):
            self.issue("invalid_runtime_execution", "runtime_execution")
            return
        result = {k: copy.deepcopy(item[k]) for k in ("type", "id", "server", "tool", "arguments", "status")}
        if "readOnlyHint" in item:
            if type(item["readOnlyHint"]) is bool:
                result["readOnlyHint"] = item["readOnlyHint"]
            else:
                self.issue("invalid_runtime_execution", "runtime_execution")
        output = item.get("result")
        if isinstance(output, dict):
            self.fields(output, {"content", "isError", "structuredContent"}, "unsupported_runtime_result_fields")
            selected = {}
            if "content" in output:
                selected["content"] = self.blocks(output["content"])
            if "isError" in output:
                if type(output["isError"]) is bool:
                    selected["isError"] = output["isError"]
                else:
                    self.issue("invalid_runtime_result", "runtime_execution")
            if "structuredContent" in output:
                if isinstance(output["structuredContent"], dict):
                    selected["structuredContent"] = copy.deepcopy(output["structuredContent"])
                else:
                    self.issue("invalid_runtime_result", "runtime_execution")
            result["result"] = selected
        elif output is not None or item["status"] == "completed":
            self.issue("invalid_runtime_result", "runtime_execution")
        self.events.append({"seq": seq, "type": "runtime_tool_execution", "model_visible": False, "payload": result})

    def finish(self):
        if set(self.calls) - self.results:
            self.issue("pending_tool_calls", "tool_execution")
        if self.mirror_ids - self.item_ids or self.mirror_user_text - self.native_user_text:
            self.issue("unmatched_completion_mirror", "visible_content")
        last = self.last_visible or {}
        if (last.get("type") != "message" or last.get("role") != "assistant"
                or last.get("phase") in {"commentary", "partial_answer"}
                or last.get("channel") in {"commentary", "partial_answer"}
                or last.get("status") in {"in_progress", "incomplete"}):
            self.issue("incomplete_final", "terminal_response")
        return {"schema": SCHEMA, "events": self.events,
                "quality": {"complete": not self.issues, "missing": self.missing, "issues": self.issues},
                "adapter_version": ADAPTER_VERSION}


def _read_failure(parser, error):
    code = str(error)
    if code in {"transcript_too_large", "transcript_line_too_large", "transcript_too_many_lines"}:
        parser.issue("protocol_size_limit", "visible_content")
    parser.issue(code, "visible_content")
    return parser.finish()


def parse_protocol_bytes(data):
    """Parse the same bounded immutable snapshot used by the message adapter.

    All byte/line limits are checked before parsing. An oversized suffix cannot
    leave a valid-looking prefix in the returned observation. No source is opened.
    """
    parser = _Parser()
    try:
        _check_bytes(data, max_bytes=MAX_BYTES, max_line_bytes=MAX_LINE_BYTES)
    except TranscriptReadError as error:
        return _read_failure(parser, error)
    for seq, line in enumerate(_iter_lines(data), 1):
        if not line.strip():
            continue
        try:
            parser.row(seq, _loads(line.decode("utf-8")))
        except DuplicateKey:
            parser.issue("duplicate_json_key", "visible_content")
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            parser.issue("malformed_record", "visible_content")
    return parser.finish()


def parse_protocol(path):
    """Read a stable regular file with static failures; never persist raw bytes."""
    try:
        data = read_transcript_bytes(path, max_bytes=MAX_BYTES, max_line_bytes=MAX_LINE_BYTES)
    except TranscriptReadError as error:
        return _read_failure(_Parser(), error)
    return parse_protocol_bytes(data)


def validate_protocol_shape(protocol):
    """Validate a closed protocol envelope; incomplete candidates are allowed.

    This checks structure, not collection consent, privacy, semantic correctness,
    grammar compatibility or training eligibility. ProtocolError has static text.
    """
    try:
        _validate_protocol_shape(protocol)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise ProtocolError("invalid_protocol_shape") from None


def _validate_protocol_shape(protocol):
    if (not isinstance(protocol, dict)
            or set(protocol) != {"schema", "adapter_version", "events", "quality"}
            or protocol["schema"] != SCHEMA or protocol["adapter_version"] not in SUPPORTED_ADAPTER_VERSIONS
            or not isinstance(protocol["events"], list)):
        raise ValueError()
    # JSON round-trip rejects Python-only values, non-finite floats and nested
    # non-string keys, without serializing any value to disk or an error message.
    if _loads(json.dumps(protocol, ensure_ascii=False, allow_nan=False)) != protocol:
        raise ValueError()
    quality = protocol["quality"]
    if not isinstance(quality, dict) or set(quality) != {"complete", "missing", "issues"} or type(quality["complete"]) is not bool:
        raise ValueError()
    for field in ("missing", "issues"):
        values = quality[field]
        if (not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", v) for v in values)
                or len(set(values)) != len(values)):
            raise ValueError()
    if quality["complete"] and quality["issues"]:
        raise ValueError()
    parser, last = _Parser(), 0
    native = {"message", "additional_tools", "configuration_update", "custom_tool_call", "custom_tool_call_output",
              "function_call", "function_call_output"}
    for event in protocol["events"]:
        if not isinstance(event, dict):
            raise ValueError()
        kind, seq, payload = event.get("type"), event.get("seq"), event.get("payload")
        fields = {"seq", "type", "payload", "model_visible"} if kind == "runtime_tool_execution" else {"seq", "type", "payload"}
        if (set(event) != fields or type(seq) is not int or seq <= last or not isinstance(payload, dict)):
            raise ValueError()
        last, before = seq, len(parser.events)
        if kind in native:
            if payload.get("type") != kind:
                raise ValueError()
            parser.response(seq, payload)
        elif kind == "base_instructions":
            if (set(payload) != {"text", "source"} or not isinstance(payload["text"], str) or not payload["text"]
                    or payload["source"] not in {"session_meta.base_instructions", "session_meta.base_instructions.text"}):
                raise ValueError()
            parser.add(seq, kind, copy.deepcopy(payload))
        elif kind == "world_state":
            parser.world(seq, payload)
        elif kind == "turn_context":
            if set(payload) - {"model", "effort"} or any(not isinstance(v, str) or not _NAME.fullmatch(v) for v in payload.values()):
                raise ValueError()
            parser.add(seq, kind, copy.deepcopy(payload))
        elif kind == "token_usage_record":
            parser.usage(seq, payload)
        elif kind == "runtime_tool_execution":
            if event["model_visible"] is not False:
                raise ValueError()
            parser.runtime_execution(seq, payload)
        else:
            raise ValueError()
        if len(parser.events) != before + 1 or parser.events[-1] != event:
            raise ValueError()
