#!/usr/bin/env python3
"""Opt-in real CLI -> scripted loopback provider -> real stdio MCP recovery test.

No paid/real model is used. Raw request bodies and CLI output stay in RAM. The
only persisted request data passes through the separately authorized observer.
"""
import argparse
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "hooks"))
from nous_training_transport import MAX_BODY_BYTES, _json, decode_response
from training_live_smoke import codex_assignment
import training_request_smoke as single

SERVER = "nous_fixture"
MODEL = single.MODEL
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
DUMMY_KEY = "nous-local-fixture-not-a-real-api-key"
FINAL = '{"answer":37}'
PROMPT = ('Synthetic integration test. Use only the provided lookup tool with {"key":"beta"}. '
          'Its first call returns a controlled transient error. Retry once with the same input, '
          'then return exactly {"answer":37}. Do not inspect files, run commands, or use other tools.')
FAILURE = {"error": "fixture_transient", "retryable": True}
SUCCESS = {"key": "beta", "value": 37}
ORACLE = {"schema": "nous.oracle/1", "scope": "final_json_task",
          "assertions": [{"kind": "exact_json", "expected": {"answer": 37}}]}
TOOL = {"name": "lookup", "description": "Read the synthetic beta=37 table. The first call fails transiently; retry once. No files, network, or shell.",
        "inputSchema": {"type": "object", "properties": {"key": {"type": "string", "enum": ["beta"]}},
                        "required": ["key"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}
TIMEOUT_SECONDS = 90
MAX_REQUESTS = 3
MAX_CLI_BYTES = 2 * 1024 * 1024
MAX_MCP_LINE = 256 * 1024
DIAGNOSTICS = {"request_capture_unsupported", "response_capture_unsupported", "response_capture_rejected", "episode_capture_failed"}
FIXTURE_CODES = {"fixture_request_limit", "fixture_request_mismatch", "fixture_catalog_invalid", "fixture_catalog_not_isolated",
                 "fixture_contract_not_matched", "fixture_contract_not_function", "fixture_contract_changed",
                 "fixture_input_not_list", "fixture_history_count_mismatch", "fixture_history_content_mismatch", "fixture_unexpected_native_tool_call",
                 "fixture_call_identity_mismatch", "fixture_call_name_mismatch", "fixture_call_namespace_mismatch", "fixture_call_arguments_mismatch",
                 "fixture_result_identity_mismatch", "fixture_result_error_flag_mismatch", "fixture_result_content_mismatch"}
RESOURCE_BUILTINS = frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})
KNOWN_BUILTINS = RESOURCE_BUILTINS | {"update_plan", "request_user_input", "exec_command", "write_stdin", "view_image",
                                    "spawn_agent", "send_input", "wait", "close_agent", "resume_agent", "apply_patch", "exec", "tool_search"}
EPISODE_CODES = frozenset("""
episode_structure_limit episode_invalid_json episode_size_limit episode_invalid_fields episode_invalid_quality
episode_quality_incomplete episode_invalid_privacy episode_privacy_not_checked episode_record_required episode_target_invalid
episode_request_context_changed episode_history_list_required episode_history_prefix_changed
episode_observed_response_missing_or_changed episode_unobserved_context_append episode_tool_results_missing_or_duplicate
episode_unbound_context_extension episode_supervision_policy_required episode_schema_or_provider_invalid episode_step_limit
episode_source_identity_missing episode_step_order_invalid episode_cross_source_step episode_invalid_source_digest
episode_duplicate_snapshot episode_exchange_schema_or_provider_mismatch episode_native_exchange_invalid episode_content_digest_mismatch
episode_duplicate_response_identity episode_initial_tool_history episode_initial_request_digest_mismatch episode_continues_after_final
episode_response_model_changed episode_reused_call_id episode_final_response_missing episode_message_projection_mismatch
episode_invalid_step_evidence episode_invalid_record episode_step_review_missing_or_negative episode_sample_invalid
""".split())
REQUEST_CODES = single.REQUEST_ERROR_CODES | frozenset("""
invalid_request_tool_arguments request_function_contract_missing_or_ambiguous request_tool_arguments_mismatch
invalid_request_tool_linkage request_tool_call_limit request_tool_results_missing request_tool_batch_interleaved
request_tool_result_identity_mismatch request_tool_response_required request_tool_choice_violation request_parallel_tool_violation
unsupported_request_tool_output request_tool_validation_limit request_tool_schema_execution_unsupported
unsupported_request_schema duplicate_request_json_key request_tool_exchange_required request_tool_exchange_requires_episode
""".split())
OBSERVER_CODES = frozenset("""
unsupported_request_provider invalid_observer_option invalid_oracle_registration request_session_required
episode_handle_invalid episode_handle_consumed episode_step_in_flight episode_step_limit oracle_request_binding_mismatch
episode_ticket_invalid_or_consumed episode_empty request_record_already_exists request_bytes_required_or_limit_exceeded request_invalid_json
""".split())
CONTEXT_FIELDS = frozenset("""
model instructions system tools tool_choice parallel_tool_calls stream store reasoning thinking text include metadata client_metadata
prompt_cache_key prompt_cache_options previous_response_id service_tier max_tokens max_output_tokens temperature top_p top_k
stop_sequences cache_control container context_management mcp_servers output_config stream_options truncation background
access_programs request_country
""".split())


def safe_capture_error(error):
    """Accept only exact owned error classes and hardcoded codes; no repr/str fallback.

    Looking up already-loaded modules avoids importing the collector before the
    runner binds its isolated NOUS_HOME (the MCP subprocess never imports it).
    """
    for module, name, allowed in (("nous_training_episode", "EpisodeError", EPISODE_CODES),
                                  ("nous_training_request", "RequestError", REQUEST_CODES),
                                  ("nous_training_observer", "ObserverError", OBSERVER_CODES)):
        imported = sys.modules.get(module)
        if imported is not None and type(error) is getattr(imported, name, None):
            args = error.args
            if len(args) == 1 and type(args[0]) is str and args[0] in allowed:
                return args[0]
    return "capture_error"


def catalog_summary(request, agent):
    report = {"types": {kind: 0 for kind in ("function", "custom", "namespace", "web_search", "local_shell", "unknown")},
              "known_builtins": {name: False for name in sorted(KNOWN_BUILTINS)}, "fixture_count": 0, "structure_limit": False}
    def walk(tools, depth=0):
        if not isinstance(tools, list) or depth > 4 or len(tools) > 4096:
            report["structure_limit"] = True
            return
        for tool in tools:
            if not isinstance(tool, dict):
                report["types"]["unknown"] += 1
                continue
            kind = "function" if agent == "claude" and "input_schema" in tool else tool.get("type")
            kind = kind if type(kind) is str and kind in report["types"] else "unknown"
            report["types"][kind] += 1
            name = tool.get("name")
            if type(name) is str and name in KNOWN_BUILTINS:
                report["known_builtins"][name] = True
            description = tool.get("description")
            schema = tool.get("input_schema" if agent == "claude" else "parameters")
            if schema == TOOL["inputSchema"] and type(description) is str and TOOL["description"] in description:
                report["fixture_count"] += 1
            if kind == "namespace":
                walk(tool.get("tools"), depth + 1)
    walk(request.get("tools"))
    return report


def context_changes(before, after, agent):
    """Booleans/counts only. Unknown field names and all values remain in RAM."""
    history_key = "messages" if agent == "claude" else "input"
    report = {"has_previous": before is not None, "changed_fields": {key: False for key in sorted(CONTEXT_FIELDS)},
              "unknown_fields_present": len(set(after) - CONTEXT_FIELDS - {history_key}), "unknown_context_changed": False,
              "metadata": {}, "history_prefix_equal": None, "history_prefix_without_cache_equal": None,
              "history_prefix_without_transport_metadata_equal": None, "history_prefix_text_alias_equal": None,
              "history_prefix_text_alias_without_cache_equal": None, "prefix_positions": [], "prefix_positions_truncated": False}
    if before is None:
        return report
    missing = object()
    report["changed_fields"] = {key: before.get(key, missing) != after.get(key, missing) for key in sorted(CONTEXT_FIELDS)}
    report["unknown_context_changed"] = ({k: v for k, v in before.items() if k not in CONTEXT_FIELDS and k != history_key}
                                         != {k: v for k, v in after.items() if k not in CONTEXT_FIELDS and k != history_key})
    for field, known in (("metadata", {"user_id"}), ("client_metadata", set())):
        left, right = before.get(field), after.get(field)
        if isinstance(left, dict) and isinstance(right, dict):
            report["metadata"][field] = {"known_fields_changed": {key: left.get(key, missing) != right.get(key, missing) for key in sorted(known)},
                                          "entry_count_changed": len(left) != len(right), "keys_changed": set(left) != set(right),
                                          "unknown_values_changed": {k: v for k, v in left.items() if k not in known} != {k: v for k, v in right.items() if k not in known}}
    left, right = before.get(history_key), after.get(history_key)
    def without(value, omitted):
        if isinstance(value, dict):
            return {key: copy.deepcopy(child) if key == "input" and value.get("type") == "tool_use" else without(child, omitted)
                    for key, child in value.items() if key not in omitted}
        if isinstance(value, list):
            return [without(child, omitted) for child in value]
        return value
    if isinstance(left, list) and isinstance(right, list):
        prefix = right[:len(left)]
        report["history_prefix_equal"] = left == prefix
        # These are diagnostic comparisons only. The actual bytes are unchanged.
        report["history_prefix_without_cache_equal"] = without(left, {"cache_control"}) == without(prefix, {"cache_control"})
        def visible(items):
            result = without(items, {"cache_control"})
            if agent == "codex":
                result = [{key: value for key, value in item.items() if key not in {"id", "status"}}
                          if isinstance(item, dict) and item.get("type") in {"message", "function_call"} else item for item in result]
            return result
        report["history_prefix_without_transport_metadata_equal"] = visible(left) == visible(prefix)
        def aliases(items):
            result = copy.deepcopy(items)
            if agent == "claude":
                for item in result:
                    if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
                        item["content"] = [{"type": "text", "text": item["content"]}]
            return result
        report["history_prefix_text_alias_equal"] = aliases(left) == aliases(prefix)
        report["history_prefix_text_alias_without_cache_equal"] = aliases(without(left, {"cache_control"})) == aliases(without(prefix, {"cache_control"}))
        def shape(content):
            kind = "string" if isinstance(content, str) else "list" if isinstance(content, list) else "other"
            blocks = [{"type": "text", "text": content}] if kind == "string" else content if kind == "list" else []
            types = [part.get("type") if isinstance(part, dict) else "unknown" for part in blocks[:16]]
            types = [value if type(value) is str and value in {"text", "input_text", "output_text", "tool_use", "tool_result"} else "unknown" for value in types]
            lengths = [len(part["text"]) if isinstance(part, dict) and isinstance(part.get("text"), str) else None for part in blocks[:16]]
            return {"container": kind, "blocks": len(blocks), "block_types": types, "text_lengths": lengths, "truncated": len(blocks) > 16}
        def plain(content):
            if isinstance(content, str):
                return content
            if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                    and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)
                    and not set(content[0]) - {"type", "text", "cache_control"}):
                return content[0]["text"]
            return None
        for index, (old, new) in enumerate(zip(left[:32], prefix[:32])):
            old = old if isinstance(old, dict) else {}
            new = new if isinstance(new, dict) else {}
            a, b = plain(old.get("content")), plain(new.get("content"))
            equal = None if a is None or b is None else hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()
            report["prefix_positions"].append({"position": index, "role_equal": old.get("role") == new.get("role"),
                                                "before": shape(old.get("content")), "after": shape(new.get("content")),
                                                "plain_text_hash_equal": equal})
        report["prefix_positions_truncated"] = len(left) > 32
    return report


def atomic_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fixture-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def serve_mcp(source, target, save):
    """The only side effect is fixed-schema fixture audit counters via save()."""
    audit = {"initializations": 0, "tools_lists": 0, "calls": 0, "transient_errors": 0,
             "successes": 0, "unexpected_calls": 0, "invalid_requests": 0}
    while True:
        line = source.readline(MAX_MCP_LINE + 1)
        if not line:
            return 0
        try:
            if len(line) > MAX_MCP_LINE:
                raise ValueError("mcp_limit")
            value = _json(line)
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise ValueError("invalid_mcp")
            if "id" not in value:
                continue
            ident = value["id"]
            if type(ident) not in (int, str) or (isinstance(ident, str) and len(ident) > 128):
                raise ValueError("invalid_mcp")
            method, params = value.get("method"), value.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("invalid_mcp")
            if method == "initialize":
                audit["initializations"] += 1
                version = params.get("protocolVersion")
                if version not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
                    raise ValueError("unsupported_mcp_version")
                result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER, "version": "2.0.0"}}
            elif method == "tools/list":
                audit["tools_lists"] += 1
                result = {"tools": [copy.deepcopy(TOOL)]}
                save("tools-list.json", result)
            elif method == "tools/call":
                audit["calls"] += 1
                valid = params.get("name") == "lookup" and params.get("arguments") == {"key": "beta"} and audit["calls"] <= 2
                if not valid:
                    audit["unexpected_calls"] += 1
                    payload, failed = {"error": "fixture_refused"}, True
                elif audit["calls"] == 1:
                    audit["transient_errors"] += 1
                    payload, failed = FAILURE, True
                else:
                    audit["successes"] += 1
                    payload, failed = SUCCESS, False
                result = {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}], "isError": failed}
            elif method in {"ping", "resources/list", "prompts/list"}:
                result = {} if method == "ping" else {"resources" if method == "resources/list" else "prompts": []}
            else:
                save("mcp-audit.json", audit)
                target.write(json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Unsupported fixture method"}}) + "\n")
                target.flush()
                continue
            save("mcp-audit.json", audit)
            target.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}, separators=(",", ":")) + "\n")
            target.flush()
        except Exception:
            audit["invalid_requests"] += 1
            save("mcp-audit.json", audit)
            return 2


def mcp_config(root):
    return {"command": sys.executable, "args": [str(Path(__file__).resolve()), "--serve-mcp", "--audit", str(Path(root) / "audit")]}


def command(agent, root, port, executable):
    root = Path(root)
    if agent == "codex":
        args = single.command(root, port, executable)
        args[-1] = PROMPT
        index = args.index("mcp_servers={}")
        del args[index - 1:index + 1]
        overrides = {"mcp_servers": {SERVER: {**mcp_config(root), "enabled_tools": ["lookup"], "required": True,
                                            "default_tools_approval_mode": "approve", "startup_timeout_sec": 10, "tool_timeout_sec": 10}},
                     "projects": {str(root / "project"): {"trust_level": "trusted"}}}
        return args[:-1] + [part for key, value in overrides.items() for part in ("-c", codex_assignment(key, value))] + [PROMPT]
    if agent != "claude":
        raise ValueError("unsupported_fixture_agent")
    return [str(executable), "-p", "--bare", "--restricted", "--setting-sources", "", "--settings", str(root / "private" / "claude-settings.json"),
            "--strict-mcp-config", "--mcp-config", str(root / "private" / "mcp.json"), "--tools", "",
            "--allowedTools", "mcp__" + SERVER + "__lookup", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--no-session-persistence", "--disable-slash-commands", "--no-chrome", "--model", CLAUDE_MODEL,
            "--system-prompt", "Complete the synthetic MCP recovery task. Use only the provided lookup tool.",
            "--system-prompt-snapshot", "off", "--max-budget-usd", "0.75", "--output-format", "stream-json", "--verbose", PROMPT]


def child_environment(agent, root, port):
    # Preserve HOME/CODEX_HOME, but inherit no auth/provider/proxy/debug variables.
    allowed = {"HOME", "CODEX_HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["RUST_LOG"] = "off"
    if agent == "claude":
        env.update(CLAUDE_CONFIG_DIR=str(Path(root) / "private" / "claude-config"),
                   ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY=DUMMY_KEY,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
                   CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1", CLAUDE_CODE_DISABLE_THINKING="1",
                   ENABLE_TOOL_SEARCH="false", API_TIMEOUT_MS="15000", CLAUDE_CODE_AUTO_MODE_SERVER="0")
    return env


def fixture_contract(request, agent):
    leaves = []
    def visit(tools, namespace=()):
        if not isinstance(tools, list) or len(namespace) > 4:
            raise ValueError("fixture_catalog_invalid")
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("fixture_catalog_invalid")
            name = tool.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
                raise ValueError("fixture_catalog_invalid")
            if tool.get("type") == "namespace":
                visit(tool.get("tools"), namespace + (name,))
            else:
                leaves.append((namespace, tool))
    visit(request.get("tools"))
    matches, seen = [], set()
    for namespace, tool in leaves:
        identity = namespace + (tool["name"],)
        if identity in seen:
            raise ValueError("fixture_catalog_not_isolated")
        seen.add(identity)
        schema = tool.get("input_schema") if agent == "claude" else tool.get("parameters")
        description = tool.get("description")
        if not isinstance(schema, dict) or (agent == "codex" and tool.get("type") != "function"):
            raise ValueError("fixture_contract_not_function")
        if schema == TOOL["inputSchema"] and type(description) is str and TOOL["description"] in description:
            matches.append((namespace, tool))
        elif agent != "codex":
            raise ValueError("fixture_catalog_not_isolated")
    if len(matches) != 1:
        raise ValueError("fixture_catalog_not_isolated")
    namespace, tool = matches[0]
    if agent == "codex" and tool.get("type") != "function":
        raise ValueError("fixture_contract_not_function")
    return {"name": tool["name"], "namespace": ".".join(namespace) or None}


def payload_envelope(value, expected, agent=None):
    """Match the complete fixed fixture JSON, optionally in Codex's exact wrapper.

    The timing header is native MCP rendering (pinned tools/context.rs), not
    arbitrary leading text. It is never removed from captured request bytes.
    """
    if isinstance(value, str):
        try:
            if _json(value) == expected:
                return "plain_json"
        except Exception:
            pass
    if isinstance(value, list):
        if (len(value) == 1 and isinstance(value[0], dict) and set(value[0]) == {"type", "text"}
                and value[0].get("type") in {"text", "input_text"} and isinstance(value[0].get("text"), str)
                and payload_envelope(value[0]["text"], expected)):
            return "single_text_json"
    if agent != "codex" or expected not in (FAILURE, SUCCESS):
        return None
    def header(text):
        match = re.fullmatch(r"Wall time: ([0-9]{1,3}\.[0-9]{4}) seconds\nOutput:", text) if isinstance(text, str) else None
        return match is not None and 0 <= float(match[1]) <= TIMEOUT_SECONDS
    if isinstance(value, str):
        first, separator, rest = value.partition("\nOutput:\n")
        if separator and header(first + "\nOutput:") and payload_envelope(rest, expected) == "plain_json":
            return "codex_wall_time_text"
    if (isinstance(value, list) and len(value) == 2
            and all(isinstance(part, dict) and set(part) == {"type", "text"} and part.get("type") == "input_text" for part in value)
            and header(value[0]["text"]) and payload_envelope(value[1]["text"], expected) == "plain_json"):
        return "codex_wall_time_items"
    return None


def contains_payload(value, expected, agent=None):
    return payload_envelope(value, expected, agent) is not None


def inspect_history(request, agent, contract, step):
    """Verify actual native call/result replay, never reconstruct missing input."""
    items = request.get("messages" if agent == "claude" else "input")
    if not isinstance(items, list):
        raise ValueError("fixture_input_not_list")
    calls, results, prompt_seen = [], [], False
    for item in items:
        if not isinstance(item, dict):
            continue
        if agent == "codex":
            if item.get("type") == "function_call":
                calls.append(item)
            elif item.get("type") == "function_call_output":
                results.append(item)
            elif item.get("type") in {"custom_tool_call", "custom_tool_call_output", "local_shell_call", "web_search_call"}:
                raise ValueError("fixture_unexpected_native_tool_call")
        content = item.get("content", [])
        if isinstance(content, str):
            prompt_seen |= item.get("role") == "user" and content == PROMPT
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            prompt_seen |= item.get("role") == "user" and part.get("text") == PROMPT
            if agent == "claude" and part.get("type") == "tool_use":
                calls.append(part)
            elif agent == "claude" and part.get("type") == "tool_result":
                results.append(part)
    if not prompt_seen or len(calls) != step or len(results) != step:
        raise ValueError("fixture_history_count_mismatch")
    envelopes = []
    for index, (call, result) in enumerate(zip(calls, results)):
        ident = ("toolu_fixture_" if agent == "claude" else "call_fixture_") + str(index + 1)
        if agent == "claude":
            checks = {"fixture_call_identity_mismatch": call.get("id") == ident,
                      "fixture_call_arguments_mismatch": call.get("input") == {"key": "beta"},
                      "fixture_result_identity_mismatch": result.get("tool_use_id") == ident,
                      "fixture_result_error_flag_mismatch": result.get("is_error", False) is (index == 0)}
            output = result.get("content")
        else:
            try:
                arguments = _json(call.get("arguments"))
            except Exception:
                arguments = None
            checks = {"fixture_call_identity_mismatch": call.get("call_id") == ident,
                      "fixture_call_arguments_mismatch": arguments == {"key": "beta"},
                      "fixture_result_identity_mismatch": result.get("call_id") == ident,
                      "fixture_call_namespace_mismatch": call.get("namespace") == contract["namespace"]}
            output = result.get("output")
        checks["fixture_call_name_mismatch"] = call.get("name") == contract["name"]
        for code, valid in checks.items():
            if not valid:
                raise ValueError(code)
        envelope = payload_envelope(output, FAILURE if index == 0 else SUCCESS, agent)
        if envelope is None:
            raise ValueError("fixture_result_content_mismatch")
        envelopes.append(envelope)
    return {"native_calls": len(calls), "native_results": len(results), "prompt_matched": prompt_seen,
            "error_replayed": step >= 1, "success_replayed": step >= 2,
            "result_envelopes": envelopes,
            "builtin_calls": sum(call.get("name") in RESOURCE_BUILTINS for call in calls)}


def sse(events):
    return b"".join(b"event: " + value["type"].encode() + b"\ndata: " +
                    json.dumps(value, separators=(",", ":")).encode() + b"\n\n" for value in events)


def response_bytes(agent, step, contract):
    if agent == "codex":
        if step < 2:
            item = {"type": "function_call", "id": "fc_fixture_" + str(step + 1), "call_id": "call_fixture_" + str(step + 1),
                    "name": contract["name"], "arguments": '{"key":"beta"}', "status": "completed"}
            if contract["namespace"] is not None:
                item["namespace"] = contract["namespace"]
        else:
            item = {"type": "message", "id": "msg_fixture_final", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": FINAL, "annotations": []}]}
        common = {"id": "resp_fixture_" + str(step + 1), "object": "response", "model": MODEL}
        return sse([{"type": "response.created", "response": {**common, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {**common, "status": "completed", "output": [item],
                                                                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}])
    block = ({"type": "tool_use", "id": "toolu_fixture_" + str(step + 1), "name": contract["name"], "input": {}}
             if step < 2 else {"type": "text", "text": ""})
    delta = {"type": "input_json_delta", "partial_json": '{"key":"beta"}'} if step < 2 else {"type": "text_delta", "text": FINAL}
    return sse([{"type": "message_start", "message": {"id": "msg_fixture_" + str(step + 1), "type": "message", "role": "assistant",
                                                        "model": CLAUDE_MODEL, "content": [], "stop_reason": None, "stop_sequence": None,
                                                        "usage": {"input_tokens": 1, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta}, {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if step < 2 else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"}])


class EpisodeBridge:
    def __init__(self, agent, project):
        from nous_training_episode_capture import begin_episode, begin_step, finish_step, finish_episode
        self.begin_step, self.finish_step, self.finish_episode = begin_step, finish_step, finish_episode
        self.handle = begin_episode("anthropic-messages" if agent == "claude" else "openai-responses", project=str(project),
                                    session="synthetic-multiturn-fixture", oracle=copy.deepcopy(ORACLE), manual=True, synthetic=True,
                                    oracle_registration="before_response")

    def begin(self, raw):
        return self.begin_step(self.handle, raw)

    def finish(self, ticket, body):
        return self.finish_step(self.handle, ticket, body, content_type="text/event-stream")

    def complete(self):
        return self.finish_episode(self.handle)


class FixtureState:
    def __init__(self, agent, bridge):
        self.agent, self.bridge, self.lock = agent, bridge, threading.Lock()
        self.contract, self.bodies, self.previous_request = None, [], None
        self.report = {"requests": 0, "responses_sent": 0, "step_observations": 0,
                       "histories": [], "diagnostics": [], "step_capture_statuses": [], "actual_native_catalog_shape_valid": False,
                       "catalogs": [], "context_changes": [], "capture_errors": [],
                       "policy": {"fixture_calls_only": True, "declared_context_preserved": True,
                                  "response_builtin_calls": 0}}

    def diagnostic(self, code):
        if code not in self.report["diagnostics"]:
            self.report["diagnostics"].append(code)

    def capture_error(self, stage, step, error):
        self.report["capture_errors"].append({"stage": stage, "step": step, "code": safe_capture_error(error)})

    def prepare(self, raw):
        step = len(self.bodies)
        if step >= MAX_REQUESTS:
            raise ValueError("fixture_request_limit")
        self.report["actual_native_catalog_shape_valid"] = False
        request = _json(raw)
        expected_model = CLAUDE_MODEL if self.agent == "claude" else MODEL
        if not isinstance(request, dict) or request.get("model") != expected_model or request.get("stream") is not True:
            raise ValueError("fixture_request_mismatch")
        self.report["catalogs"].append(catalog_summary(request, self.agent))
        self.report["context_changes"].append(context_changes(self.previous_request, request, self.agent))
        self.previous_request = request
        contract = fixture_contract(request, self.agent)
        if self.contract is not None and contract != self.contract:
            raise ValueError("fixture_contract_changed")
        self.contract = contract
        self.report["actual_native_catalog_shape_valid"] = True
        self.report["histories"].append(inspect_history(request, self.agent, contract, step))
        body = response_bytes(self.agent, step, contract)
        decode_response("anthropic-messages" if self.agent == "claude" else "openai-responses", body, "text/event-stream")
        ticket = None
        try:
            ticket = self.bridge.begin(raw)
        except Exception as error:
            self.diagnostic("request_capture_unsupported")
            self.capture_error("begin", step, error)
        self.bodies.append(body)
        return ticket, body


def handler_class(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def reject(self, code):
            state.diagnostic(code)
            self.close_connection = True
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        def do_GET(self):
            self.reject("unexpected_http_method")
        do_HEAD = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET
        def do_POST(self):
            timer = None
            try:
                # Requests are serialized with the capture steps, including response flush.
                with state.lock:
                    state.report["requests"] += 1
                    if state.report["requests"] > MAX_REQUESTS:
                        return self.reject("more_than_three_requests")
                    expected = "/v1/messages" if state.agent == "claude" else "/v1/responses"
                    # Anthropic SDK may use its documented beta=true endpoint selector.
                    paths = {expected, expected + "?beta=true"} if state.agent == "claude" else {expected}
                    if self.path not in paths:
                        return self.reject("unexpected_request_path")
                    auth, key = self.headers.get_all("Authorization", []), self.headers.get_all("x-api-key", [])
                    if auth or (key != [DUMMY_KEY] if state.agent == "claude" else bool(key)):
                        return self.reject("unexpected_auth_header")
                    if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Content-Encoding", "identity").lower() != "identity":
                        return self.reject("unsupported_request_encoding")
                    lengths = self.headers.get_all("Content-Length", [])
                    if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_BODY_BYTES:
                        return self.reject("request_length_rejected")
                    if self.headers.get("Content-Type", "").lower() not in {"application/json", "application/json; charset=utf-8"}:
                        return self.reject("unexpected_request_content_type")
                    def expire():
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    timer = threading.Timer(15, expire)
                    timer.daemon = True
                    timer.start()
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        return self.reject("partial_request_body")
                    try:
                        ticket, body = state.prepare(raw)
                    except Exception as error:
                        code = str(error) if type(error) is ValueError and str(error) in FIXTURE_CODES else "fixture_native_history_or_catalog_rejected"
                        return self.reject(code)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    state.report["responses_sent"] += 1
                    if ticket is not None:
                        try:
                            outcome = state.bridge.finish(ticket, body)
                            status = outcome.get("status") if isinstance(outcome, dict) else None
                            status = status if status in {"captured", "disabled", "rejected"} else "unknown"
                            state.report["step_capture_statuses"].append(status)
                            if status == "captured":
                                state.report["step_observations"] += 1
                            else:
                                state.diagnostic("response_capture_rejected")
                        except Exception as error:
                            state.diagnostic("response_capture_unsupported")
                            state.capture_error("finish", len(state.bodies) - 1, error)
                    self.close_connection = True
            except Exception:
                state.diagnostic("fixture_handler_failed")
                self.close_connection = True
            finally:
                if timer is not None:
                    timer.cancel()
    return Handler


def run_cli(args, env, cwd, agent):
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    chunks, overflow = [bytearray(), bytearray()], threading.Event()
    def pump(stream, dest):
        while True:
            block = stream.read(65536)
            if not block:
                break
            if len(dest) + len(block) > MAX_CLI_BYTES:
                overflow.set()
                break
            dest.extend(block)
        stream.close()
    readers = [threading.Thread(target=pump, args=pair, daemon=True) for pair in zip((process.stdout, process.stderr), chunks)]
    for reader in readers:
        reader.start()
    deadline, timeout = time.monotonic() + TIMEOUT_SECONDS, False
    while process.poll() is None:
        if overflow.is_set() or time.monotonic() >= deadline:
            timeout = not overflow.is_set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            break
        time.sleep(0.02)
    process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=2)
    final, completed = False, False
    for line in bytes(chunks[0]).splitlines():
        try:
            value = _json(line)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        if agent == "claude":
            if value.get("type") == "result":
                final |= value.get("result") == FINAL and value.get("is_error") is False
                completed |= value.get("subtype") == "success"
        else:
            item = value.get("item")
            final |= value.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text") == FINAL
            completed |= value.get("type") == "turn.completed"
    return {"exit_code": process.returncode, "timed_out": timeout, "output_limit_hit": overflow.is_set(), "exact_final": final,
            "turn_completed": completed, "stdout_bytes": len(chunks[0]), "stderr_bytes": len(chunks[1])}


def audit_summary(root):
    try:
        value = _json((Path(root) / "audit" / "mcp-audit.json").read_bytes())
        expected = {"initializations", "tools_lists", "calls", "transient_errors", "successes", "unexpected_calls", "invalid_requests"}
        if not isinstance(value, dict) or set(value) != expected or any(type(n) is not int or not 0 <= n <= 100 for n in value.values()):
            raise ValueError("invalid_audit")
        valid = value["initializations"] >= 1 and value["tools_lists"] >= 1 and value["calls"] == 2 and value["transient_errors"] == 1 and value["successes"] == 1 and value["unexpected_calls"] == value["invalid_requests"] == 0
        return {"valid": valid, **value}
    except Exception:
        return {"valid": False}


def run(agent, root, executable):
    root = Path(root)
    if not root.is_absolute() or root.exists() or root.is_symlink():
        raise ValueError("fresh_absolute_output_directory_required")
    root.mkdir(mode=0o700)
    for relative in ("project", "audit", "private", "private/data", "private/data/ledger", "private/codex-state", "private/codex-logs", "private/claude-config"):
        (root / relative).mkdir(mode=0o700)
    os.environ["NOUS_HOME"], os.environ["NOUS_LEDGER_HOME"] = str(root / "private/data"), str(root / "private/data/ledger")
    atomic_json(root / "private/model-catalog.json", single.model_catalog())
    atomic_json(root / "private/mcp.json", {"mcpServers": {SERVER: mcp_config(root)}})
    atomic_json(root / "private/claude-settings.json", {"disableAllHooks": True})
    report = {"schema": "nous.multiturn-smoke/1", "agent": agent, "real_cli": True, "real_model": False, "scripted_provider": True,
              "raw_requests_saved": False, "observation_point": "local_fixture_server", "oracle_registration": "before_response",
              "oracle_template_defined_before_client": True, "global_config_modified": False, "strict_sft_ready": False,
              "fixture_transport_passed": False, "mcp_recovery_passed": False, "episode_capture_passed": False, "diagnostics": []}
    server = None
    try:
        bridge = EpisodeBridge(agent, root / "project")
        state = FixtureState(agent, bridge)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(state))
        server.daemon_threads = False
        server.handle_error = lambda *_args: state.diagnostic("fixture_server_failed")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        args = command(agent, root, server.server_port, executable)
        atomic_json(root / "private/command-review.json", {"argv": args, "environment_names": sorted(child_environment(agent, root, server.server_port))})
        report["client"] = run_cli(args, child_environment(agent, root, server.server_port), root / "project", agent)
        server.shutdown()
        server.server_close()
        server = None
        report["fixture"], report["mcp"] = state.report, audit_summary(root)
        client = report["client"]
        report["fixture_transport_passed"] = (client["exit_code"] == 0 and client["exact_final"] and client["turn_completed"]
                                               and not client["timed_out"] and not client["output_limit_hit"]
                                               and state.report["requests"] == state.report["responses_sent"] == MAX_REQUESTS
                                               and not any(code not in DIAGNOSTICS for code in state.report["diagnostics"]))
        report["mcp_recovery_passed"] = report["fixture_transport_passed"] and report["mcp"]["valid"] and state.report["histories"][-1]["success_replayed"]
        try:
            outcome = bridge.complete()
            quality = outcome.get("quality") if isinstance(outcome.get("quality"), dict) else {}
            steps = outcome.get("steps")
            report["episode"] = {"captured": outcome.get("status") == "captured", "complete": quality.get("complete") is True,
                                  "steps": steps if type(steps) is int and 0 <= steps <= MAX_REQUESTS else None,
                                  "content_review_required": True}
        except Exception as error:
            state.diagnostic("episode_capture_failed")
            state.capture_error("episode", len(state.bodies), error)
        from nous_training import export
        from nous_training_validate import validate_dataset
        export(root / "dataset", profile="request-episode-sft-v1")
        validation = validate_dataset(root / "dataset")
        report["dataset"] = {"valid": validation["valid"], "counts": validation["counters"]}
        report["episode_capture_passed"] = bool(report["mcp_recovery_passed"] and report.get("episode", {}).get("captured")
                                                  and report["episode"].get("complete") and report["episode"].get("steps") == MAX_REQUESTS
                                                  and state.report["step_observations"] == MAX_REQUESTS
                                                  and validation["valid"] and validation["counters"]["trajectories"] == 1)
    except Exception:
        report["diagnostics"].append("fixture_runner_failed")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        atomic_json(root / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("codex", "claude"), default="codex")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--executable")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--serve-mcp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--audit", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve_mcp:
        if args.audit is None or not args.audit.is_absolute() or not args.audit.is_dir():
            return 2
        return serve_mcp(sys.stdin.buffer, sys.stdout, lambda name, value: atomic_json(args.audit / name, value))
    if not args.run:
        print(json.dumps({"status": "plan_only", "agent": args.agent, "real_model": False, "max_requests": MAX_REQUESTS,
                          "max_mcp_calls": 2, "timeout_seconds": TIMEOUT_SECONDS, "requires_explicit_run": True}))
        return 0
    if args.out is None:
        parser.error("--out must name a new absolute directory")
    executable = args.executable or (single.CLI if args.agent == "codex" else str(Path.home() / ".local/bin/claude"))
    try:
        report = run(args.agent, args.out, executable)
    except Exception:
        print(json.dumps({"status": "not_started", "diagnostic": "fixture_preparation_failed"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["mcp_recovery_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
