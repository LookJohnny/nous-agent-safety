#!/usr/bin/env python3
"""Opt-in real CLI / fake local provider smoke test. Default is a plan only.

The fixture neither forwards traffic nor runs a model. Original API request and
CLI output remain in RAM. Only the authorized observer may store scrubbed data.
Never use this harness to claim a real model answered the fixture correctly.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "hooks"))
from nous_training_transport import MAX_BODY_BYTES, _json, decode_response
from nous_training_request import RequestError
from training_live_smoke import codex_assignment

MODEL = "nous-request-fixture"
PROMPT = 'Synthetic transport check: return only the JSON object {"answer":4}. Do not call tools.'
FINAL = '{"answer":4}'
CONTENT_TYPE = "text/event-stream"
CLI = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
TIMEOUT_SECONDS = 90
MAX_CLI_BYTES = 2 * 1024 * 1024
ORACLE = {"schema": "nous.oracle/1", "scope": "final_json_task",
          "assertions": [{"kind": "exact_json", "expected": {"answer": 4}}]}
REQUEST_ERROR_CODES = frozenset("""
unsupported_request_field unsupported_request_setting unsupported_request_include
unsupported_request_reasoning_field unsupported_request_reasoning_summary
unsupported_request_input_item unsupported_request_tool unsupported_request_tool_schema
unsupported_request_custom_format unsupported_request_content unsupported_request_content_metadata
unsupported_request_tool_choice unsupported_request_provider unsupported_request_role
request_server_context_unsupported request_background_unsupported request_truncation_unsupported
request_tool_history_not_supported request_tool_contract_changed request_assistant_prefill_unsupported
request_final_text_required request_response_incomplete request_transport_mismatch
request_input_missing request_input_incomplete request_user_input_missing
request_size_limit request_item_limit request_structure_too_deep
hidden_reasoning_unsupported invalid_request_body invalid_request_json invalid_request_exchange
invalid_request_tool_catalog invalid_request_tool_schema invalid_request_content invalid_request_input
invalid_request_boolean invalid_request_number invalid_request_string invalid_request_metadata invalid_request_client_metadata
unsupported_response_field unsupported_response_output unsupported_response_usage unsupported_response_context
request_unbound_context_extension request_content_digest_mismatch request_message_projection_mismatch
request_quality_incomplete request_privacy_not_scrubbed
""".split())


def safe_capture_error(error):
    """Only adapter-owned static errors; never stringify arbitrary exceptions."""
    return str(error) if type(error) is RequestError and str(error) in REQUEST_ERROR_CODES else "capture_error"


def response_bytes():
    item = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": FINAL, "annotations": []}]}
    common = {"id": "resp_fixture", "object": "response", "model": MODEL}
    events = [{"type": "response.created", "response": {**common, "status": "in_progress", "output": []}},
              {"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": {**common, "status": "completed", "output": [item],
                                                           "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}]
    return b"".join(b"event: " + event["type"].encode() + b"\ndata: " +
                    json.dumps(event, separators=(",", ":")).encode() + b"\n\n" for event in events)


def model_catalog():
    return {"models": [{"slug": MODEL, "display_name": "Synthetic local fixture", "base_instructions": "Follow the synthetic user's exact JSON task.",
                        "supported_reasoning_levels": [], "shell_type": "disabled", "visibility": "list", "supported_in_api": True,
                        "priority": 0, "support_verbosity": False, "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "experimental_supported_tools": [], "use_responses_lite": False, "tool_mode": "direct", "node_repl_disabled": True}]}


def command(root, port, executable=CLI):
    root = Path(root)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid_fixture_port")
    provider = {"name": "Local synthetic fixture", "base_url": f"http://127.0.0.1:{port}/v1", "wire_api": "responses",
                "requires_openai_auth": False, "supports_websockets": False, "request_max_retries": 0,
                "stream_max_retries": 0, "stream_idle_timeout_ms": 10000}
    config = {"model_provider": "nous_fixture", "model_providers": {"nous_fixture": provider}, "model": MODEL,
              "model_catalog_json": str(root / "private" / "model-catalog.json"),
              "analytics.enabled": False, "features.hooks": False, "features.plugins": False, "features.apps": False,
              "features.shell_tool": False, "features.memories": False, "features.multi_agent": False,
              "features.enable_request_compression": False, "skills.include_instructions": False,
              "memories.use_memories": False, "memories.generate_memories": False,
              "mcp_servers": {}, "project_doc_max_bytes": 0, "project_root_markers": [], "web_search": "disabled",
              "history.persistence": "none", "log_dir": str(root / "private" / "codex-logs"),
              "sqlite_home": str(root / "private" / "codex-state"), "features.rollout_budget.enabled": True,
              "features.rollout_budget.limit_tokens": 12000, "features.rollout_budget.reminder_at_remaining_tokens": [3000]}
    args = [str(executable), "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral",
            "-C", str(root / "project"), "-s", "read-only", "--json"]
    for key, value in config.items():
        args += ["-c", codex_assignment(key, value)]
    return args + [PROMPT]


def safe_request_shape(raw):
    """Closed field names and counts; never copy unknown keys, names or values."""
    value = _json(raw)
    if not isinstance(value, dict):
        raise ValueError("fixture_request_not_object")
    fields = {"model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "stream", "store",
              "reasoning", "text", "include", "previous_response_id", "metadata", "prompt_cache_key", "service_tier",
              "max_output_tokens", "temperature", "top_p", "truncation", "stream_options",
              "client_metadata", "prompt_cache_options", "access_programs", "request_country"}
    result = {"bytes": len(raw), "field_count": len(value), "unknown_field_count": len(set(value) - fields),
              "fields_present": {key: key in value for key in sorted(fields)}, "input_items": 0, "tools": 0,
              "input_type_counts": {}, "tool_type_counts": {}, "fixture_prompt_present": False}
    # Fixed names from the pinned native request struct. These report settings,
    # never unknown names, metadata values, opaque identifiers or body text.
    include = value.get("include")
    selectors = {"reasoning.encrypted_content", "output.encrypted_content"}
    result["include_shape"] = {"is_list": isinstance(include, list),
        "items": len(include) if isinstance(include, list) else 0,
        "known_selectors_present": {key: isinstance(include, list) and key in include for key in sorted(selectors)},
        "unknown_items": sum(not isinstance(item, str) or item not in selectors for item in include) if isinstance(include, list) else 0}
    reasoning = value.get("reasoning")
    known_reasoning = {"effort", "summary", "context"}
    result["reasoning_shape"] = {"is_object": isinstance(reasoning, dict),
        "fields_present": {key: isinstance(reasoning, dict) and key in reasoning for key in sorted(known_reasoning)},
        "unknown_fields": len(set(reasoning) - known_reasoning) if isinstance(reasoning, dict) else 0}
    for key, choices in (("effort", {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
                         ("summary", {"auto", "concise", "detailed", "none"}),
                         ("context", {"auto", "current_turn", "all_turns"})):
        item = reasoning.get(key) if isinstance(reasoning, dict) else None
        result["reasoning_shape"][key] = ("absent_or_null" if item is None else item
            if isinstance(item, str) and item in choices else "unknown")
    metadata = value.get("client_metadata")
    result["client_metadata_shape"] = {"is_object": isinstance(metadata, dict),
        "entries": len(metadata) if isinstance(metadata, dict) else 0,
        "max_key_characters": max((len(k) for k in metadata if isinstance(k, str)), default=0) if isinstance(metadata, dict) else 0,
        "max_value_characters": max((len(v) for v in metadata.values() if isinstance(v, str)), default=0) if isinstance(metadata, dict) else 0,
        "string_map": isinstance(metadata, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())}
    items = value.get("input")
    if isinstance(items, list):
        result["input_items"] = len(items)
        known = {"message", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "reasoning"}
        for item in items:
            kind = item.get("type", "message") if isinstance(item, dict) else "unknown"
            kind = kind if isinstance(kind, str) and kind in known else "unknown"
            result["input_type_counts"][kind] = result["input_type_counts"].get(kind, 0) + 1
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content")
                texts = [content] if isinstance(content, str) else [part.get("text") for part in content if isinstance(part, dict)] if isinstance(content, list) else []
                result["fixture_prompt_present"] |= PROMPT in texts
    elif isinstance(items, str):
        result["fixture_prompt_present"] = items == PROMPT
    if isinstance(value.get("tools"), list):
        result["tools"] = len(value["tools"])
        for tool in value["tools"]:
            kind = tool.get("type") if isinstance(tool, dict) else "unknown"
            kind = kind if isinstance(kind, str) and kind in {"function", "custom", "namespace", "web_search", "local_shell"} else "unknown"
            result["tool_type_counts"][kind] = result["tool_type_counts"].get(kind, 0) + 1
    if value.get("model") != MODEL or value.get("stream") is not True or not result["fixture_prompt_present"]:
        raise ValueError("fixture_request_not_matched")
    return result


class ObserverBridge:
    def __init__(self, project):
        # Import only after the runner set an isolated NOUS_HOME and ledger.
        from nous_training_observer import begin_request, finish_request
        self.begin_request, self.finish_request = begin_request, finish_request
        self.project = str(project)
        self.oracle_snapshot = json.dumps(ORACLE, sort_keys=True).encode()

    def begin(self, raw):
        return self.begin_request("openai-responses", raw, project=self.project, session="synthetic-loopback-fixture",
                                  oracle=json.loads(self.oracle_snapshot), synthetic=True, manual=True,
                                  oracle_registration="before_response")

    def finish(self, ticket, body):
        value = self.finish_request(ticket, body, content_type=CONTENT_TYPE)
        verification = value.get("verification") or {}
        known_codes = {"all_assertions_matched", "result_assertion_mismatch", "request_redacted_or_changed",
                       "response_not_unchanged", "request_exchange_invalid", "verification_internal_error"}
        return {"collected": value.get("status") == "collected", "schema_v3": value.get("schema") == "nous.trajectory/3",
                "objective_result_matched": verification.get("status") == "matched",
                "verification_status": verification.get("status") if verification.get("status") in
                    {"matched", "mismatched", "indeterminate"} else "unknown",
                "verification_codes": sorted({code if code in known_codes else "other_verification_code"
                                               for code in verification.get("codes", [])}),
                "oracle_registered_before_response": verification.get("oracle_registration") == "before_response",
                "content_review_required": value.get("content_review_required") is True}


class FixtureState:
    def __init__(self, bridge):
        self.bridge, self.lock = bridge, threading.Lock()
        self.body = response_bytes()
        decode_response("openai-responses", self.body, CONTENT_TYPE)
        self.report = {"requests": 0, "response_sent": False, "capture_started_before_response": False,
                       "capture": {"collected": False}, "diagnostics": [], "request_shape": None,
                       "request_capture_code": None, "response_capture_code": None}

    def diagnostic(self, code):
        with self.lock:
            if code not in self.report["diagnostics"]:
                self.report["diagnostics"].append(code)


def handler_class(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reject(self, code, diagnostic):
            state.diagnostic(diagnostic)
            self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_GET(self):
            self.reject(405, "unexpected_http_method")

        do_PUT = do_GET
        do_DELETE = do_GET
        do_PATCH = do_GET
        do_HEAD = do_GET

        def do_POST(self):
            try:
                with state.lock:
                    state.report["requests"] += 1
                    count = state.report["requests"]
                if count != 1:
                    return self.reject(409, "more_than_one_request")
                if self.path != "/v1/responses":
                    return self.reject(404, "unexpected_request_path")
                if self.headers.get("Authorization") is not None or self.headers.get("x-api-key") is not None:
                    return self.reject(400, "unexpected_auth_header")
                if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Content-Encoding", "identity").lower() != "identity":
                    return self.reject(400, "unsupported_request_encoding")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_BODY_BYTES:
                    return self.reject(413, "request_length_rejected")
                if self.headers.get("Content-Type", "").lower() not in ("application/json", "application/json; charset=utf-8"):
                    return self.reject(415, "unexpected_request_content_type")
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    return self.reject(400, "partial_request_body")
                try:
                    shape = safe_request_shape(raw)
                except Exception:
                    return self.reject(400, "fixture_request_shape_rejected")
                state.report["request_shape"] = shape
                ticket = None
                try:
                    ticket = state.bridge.begin(raw)
                    state.report["capture_started_before_response"] = ticket is not None
                except Exception as error:
                    state.report["request_capture_code"] = safe_capture_error(error)
                    state.diagnostic("request_capture_unsupported")
                # The oracle and immutable request binding exist before any SSE bytes.
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE)
                self.send_header("Content-Length", str(len(state.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(state.body)
                self.wfile.flush()
                state.report["response_sent"] = True
                if ticket is not None:
                    try:
                        state.report["capture"] = state.bridge.finish(ticket, state.body)
                    except Exception as error:
                        state.report["response_capture_code"] = safe_capture_error(error)
                        state.diagnostic("response_capture_unsupported")
                self.close_connection = True
            except Exception:
                state.diagnostic("fixture_handler_failed")
                self.close_connection = True
    return Handler


def child_environment():
    env = os.environ.copy()
    for key in list(env):
        if key.upper().endswith("_PROXY") or key.upper() in {"ALL_PROXY", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY"}:
            env.pop(key, None)
    env["RUST_LOG"] = "off"
    return env


def run_cli(args, cwd):
    """Bound both elapsed time and pipes without storing stdout/stderr files."""
    process = subprocess.Popen(args, cwd=cwd, env=child_environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
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
    readers = [threading.Thread(target=pump, args=(stream, dest), daemon=True)
               for stream, dest in zip((process.stdout, process.stderr), chunks)]
    for reader in readers:
        reader.start()
    deadline, timed_out = time.monotonic() + TIMEOUT_SECONDS, False
    while process.poll() is None:
        if overflow.is_set() or time.monotonic() >= deadline:
            timed_out = not overflow.is_set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            break
        time.sleep(0.02)
    process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=2)
    final_matches, completed = 0, False
    for line in bytes(chunks[0]).splitlines():
        try:
            value = _json(line)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        item = value.get("item")
        if value.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text") == FINAL:
            final_matches += 1
        completed |= value.get("type") == "turn.completed"
    return {"exit_code": process.returncode, "timed_out": timed_out, "output_limit_hit": overflow.is_set(),
            "stdout_bytes": len(chunks[0]), "stderr_bytes": len(chunks[1]), "exact_final_messages": final_matches,
            "turn_completed": completed}


def write_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def run(root, executable):
    root = Path(root)
    if not root.is_absolute() or root.exists() or root.is_symlink():
        raise ValueError("fresh_absolute_output_directory_required")
    root.mkdir(mode=0o700)
    for relative in ("project", "private", "private/data", "private/data/ledger", "private/codex-state", "private/codex-logs"):
        (root / relative).mkdir(mode=0o700)
    os.environ["NOUS_HOME"] = str(root / "private" / "data")
    os.environ["NOUS_LEDGER_HOME"] = str(root / "private" / "data" / "ledger")
    write_json(root / "private" / "model-catalog.json", model_catalog())
    report = {"schema": "nous.request-smoke/1", "real_cli": True, "real_model": False, "synthetic_provider": True,
              "raw_request_saved": False, "global_config_modified": False, "strict_sft_ready": False,
              "observation_point": "local_fixture_server", "oracle_template_defined_before_client": True,
              "fixture_transport_passed": False, "diagnostics": []}
    server = None
    try:
        state = FixtureState(ObserverBridge(root / "project"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(state))
        server.daemon_threads = False
        # Avoid HTTPServer's default exception printer exposing request paths.
        server.handle_error = lambda *_args: state.diagnostic("fixture_server_failed")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        report["client"] = run_cli(command(root, server.server_port, executable), root / "project")
        server.shutdown()
        server.server_close()
        server = None
        report["fixture"] = state.report
        client = report["client"]
        report["fixture_transport_passed"] = (client["exit_code"] == 0 and not client["timed_out"] and not client["output_limit_hit"]
                                               and client["exact_final_messages"] == 1 and client["turn_completed"]
                                               and state.report["requests"] == 1 and state.report["response_sent"]
                                               and not any(code not in {"request_capture_unsupported", "response_capture_unsupported"}
                                                           for code in state.report["diagnostics"]))
        from nous_training import export
        from nous_training_validate import validate_dataset
        export(root / "dataset", profile="request-sft-v1")
        validation = validate_dataset(root / "dataset")
        report["dataset"] = {"valid": validation["valid"], "counts": validation["counters"]}
        report["request_capture_passed"] = (report["fixture_transport_passed"] and state.report["capture_started_before_response"]
                                             and state.report["capture"].get("collected") is True and validation["valid"]
                                             and validation["counters"]["trajectories"] == 1)
        # The objective result check does not substitute for content/privacy review.
    except Exception:
        report["diagnostics"].append("fixture_runner_failed")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        write_json(root / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--codex", default=CLI)
    parser.add_argument("--run", action="store_true", help="Explicitly run one real CLI against a synthetic local provider")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"status": "plan_only", "real_model": False, "requires_explicit_run": True, "max_requests": 1,
                          "timeout_seconds": TIMEOUT_SECONDS, "raw_request_saved": False}))
        return 0
    if args.out is None:
        parser.error("--out must name a new absolute directory")
    try:
        report = run(args.out, args.codex)
    except Exception:
        print(json.dumps({"status": "not_started", "diagnostic": "fixture_preparation_failed"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("fixture_transport_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
