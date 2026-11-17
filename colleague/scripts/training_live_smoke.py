#!/usr/bin/env python3
"""Opt-in, real-client smoke test using only a synthetic, read-only MCP tool.

No authentication files are inspected or copied. No global configuration or trust
is changed. Platform CLIs use their own existing authentication and may persist
this new synthetic session in their normal history directories. Never run this
from CI without explicitly authorizing model usage.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

BASE = Path(__file__).resolve().parents[1]
HOOKS = BASE / "hooks"
SERVER = "nous_fixture"
FINAL = "NOUS_LIVE_OK beta=37"
STOP_FINAL = "NOUS_STOP_OK"
STOP_PROMPT = "Synthetic Stop-hook integration test. Do not use tools. Reply with exactly NOUS_STOP_OK."
PROMPT = ('Synthetic integration test. Call the MCP tool lookup exactly once with '
          '{"key":"beta"}. Do not use any other tools. Use its result, then reply '
          'with exactly: ' + FINAL)
TOOL = {"name": "lookup", "description": "Read a fixed synthetic in-memory lookup table; no files, commands, or network.",
        "inputSchema": {"type": "object", "properties": {"key": {"type": "string", "enum": ["alpha", "beta"]}},
                        "required": ["key"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False}}


def write_json(path, value):
    path = Path(path)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def toml_value(value):
    """Encode a bounded CLI value; special map keys belong on the RHS.

    Codex splits the left side of -c at every dot and does not TOML-unquote it.
    JSON objects are not TOML inline tables, so json.dumps is insufficient here.
    """
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return "{" + ",".join(toml_value(key) + "=" + toml_value(item)
                              for key, item in value.items()) + "}"
    raise ValueError("unsupported_cli_config_value")


def codex_assignment(path, value):
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*", path):
        raise ValueError("special_cli_key_requires_rhs_table")
    return path + "=" + toml_value(value)


def stop_config(root, trust=None):
    """The same -c values are used by inventory and exec; no persistent writes."""
    config = {"analytics.enabled": False, "features.hooks": True, "features.plugins": False,
              "features.apps": False, "features.shell_tool": False, "features.memories": False,
              "features.multi_agent": False, "skills.include_instructions": False,
              "memories.use_memories": False, "memories.generate_memories": False,
              "project_doc_max_bytes": 0, "project_root_markers": [], "mcp_servers": {},
              "web_search": "disabled", "model_reasoning_effort": "low", "history.persistence": "none",
              "log_dir": str(root / "private" / "codex-logs"), "sqlite_home": str(root / "private" / "codex-state"),
              "features.rollout_budget.enabled": True, "features.rollout_budget.limit_tokens": 12000,
              "features.rollout_budget.reminder_at_remaining_tokens": [3000],
              "projects": {str(root / "project"): {"trust_level": "trusted"}}}
    if trust is not None:
        config["hooks.state"] = {trust["key"]: {"trusted_hash": trust["currentHash"]}}
    return [item for key, value in config.items() for item in ("-c", codex_assignment(key, value))]


def prepare_stop_fixture(root):
    settings = root / "project" / ".codex"
    settings.mkdir(mode=0o700)
    (settings / "config.toml").write_text("# Synthetic Stop hook fixture only.\n")
    (settings / "config.toml").chmod(0o600)
    command = shlex.join([str(Path(sys.executable).resolve()), "-B", str(Path(__file__).resolve()),
                          "--observe-stop", str(root)])
    hook_file = settings / "hooks.json"
    write_json(hook_file, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 20}]}]}})
    return {"command": command, "hook_file": str(hook_file),
            "sha256": hashlib.sha256(hook_file.read_bytes()).hexdigest()}


def summarize_hook_inventory(result, root, fixture):
    """Reject missing/malformed schemas; count disabled and all other sources too."""
    safe = {"status": "inventory_schema_unrecognized", "matched": False}
    if not isinstance(result, dict) or set(result) != {"data"} or not isinstance(result["data"], list) or len(result["data"]) != 1:
        return safe, None
    entry = result["data"][0]
    if (not isinstance(entry, dict) or not {"cwd", "hooks", "errors", "warnings"} <= set(entry)
            or not isinstance(entry["cwd"], str) or not all(isinstance(entry[k], list) for k in ("hooks", "errors", "warnings"))):
        return safe, None
    safe.update(status="inventory_received", cwd_matches_requested=Path(entry["cwd"]).resolve() == root / "project",
                total_hooks=len(entry["hooks"]), errors_count=len(entry["errors"]), warnings_count=len(entry["warnings"]))
    if len(entry["hooks"]) != 1:
        return safe, None
    hook = entry["hooks"][0]
    required = {"currentHash", "displayOrder", "enabled", "eventName", "isManaged", "key", "source", "sourcePath",
                "timeoutSec", "trustStatus", "handlerType", "command"}
    if (not isinstance(hook, dict) or not required <= set(hook)
            or not all(isinstance(hook[k], str) and hook[k] for k in ("key", "currentHash", "sourcePath", "command"))
            or type(hook["enabled"]) is not bool or type(hook["isManaged"]) is not bool
            or hook["trustStatus"] not in ("untrusted", "trusted", "modified", "managed")):
        safe["status"] = "inventory_schema_unrecognized"
        return safe, None
    safe.update(trust_status=hook["trustStatus"], enabled=hook["enabled"], managed=hook["isManaged"],
                only_expected_fixture=(hook["source"] == "project" and hook["sourcePath"] == fixture["hook_file"]
                    and hook["command"] == fixture["command"] and hook["handlerType"] == "command"
                    and hook["eventName"] == "stop" and hook["timeoutSec"] == 20))
    safe["matched"] = bool(safe["cwd_matches_requested"] and not entry["errors"] and not entry["warnings"]
                           and safe["only_expected_fixture"] and hook["enabled"] and not hook["isManaged"])
    return safe, hook if safe["matched"] else None


def hook_inventory(executable, root, fixture, trust=None):
    """Bounded initialize + hooks/list only. Raw responses and stderr never persist."""
    argv = [executable, "app-server", "--stdio", *stop_config(root, trust)]
    process = subprocess.Popen(argv, cwd=root / "project", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffer, diagnostic = b"", b""
    safe, hook = {"status": "inventory_timeout", "matched": False}, None
    def send(message):
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "nous_stop_fixture", "version": "1.0"},
                                                           "capabilities": {"experimentalApi": True}}})
        deadline, done = time.monotonic() + 20, False
        while time.monotonic() < deadline and not done:
            for key, _ in selector.select(.1):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    diagnostic = (diagnostic + chunk)[-32768:]
                    continue
                buffer += chunk
                if len(buffer) > 1024 * 1024:
                    safe["status"], done = "inventory_response_too_large", True
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("id") in (1, 2) and "error" in message:
                        safe["status"], done = "inventory_rpc_error", True
                        break
                    if message.get("id") == 1:
                        send({"method": "initialized", "params": {}})
                        send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(root / "project")]}})
                    elif message.get("id") == 2:
                        safe, hook = summarize_hook_inventory(message.get("result"), root, fixture)
                        done = True
                        break
            if process.poll() is not None and not selector.get_map():
                safe["status"] = "inventory_server_exited"
                break
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    safe["diagnostic_categories"] = diagnostic_codes(diagnostic.decode("utf-8", errors="replace"))
    if safe["diagnostic_categories"]:
        safe["matched"], hook = False, None
    return safe, hook


def verify_stop_trust(executable, root, fixture, report):
    """One exact handler in every stage; no model execution or config writes."""
    original = None
    for stage, use_trust, expected in (("initial", False, "untrusted"), ("temporary", True, "trusted"),
                                      ("reverted", False, "untrusted")):
        safe, hook = hook_inventory(executable, root, fixture, original if use_trust else None)
        report.setdefault("inventory_stages", []).append({"stage": stage, **safe})
        if (not hook or hook["trustStatus"] != expected
                or (original is not None and any(hook[k] != original[k] for k in ("key", "currentHash")))
                or hashlib.sha256(Path(fixture["hook_file"]).read_bytes()).hexdigest() != fixture["sha256"]):
            report["status"] = "single_hook_trust_not_confirmed"
            return None
        original = original or hook
    report["temporary_exact_trust_verified"] = True
    return original


def observe_stop(root):
    """Trusted fixture wrapper: record receipt, then pass bounded input to the real hook."""
    root = root.resolve()
    payload = sys.stdin.buffer.read(65537)
    event = None
    try:
        event = json.loads(payload) if len(payload) <= 65536 else None
        valid = (isinstance(event, dict) and event.get("hook_event_name") == "Stop"
                 and isinstance(event.get("cwd"), str) and Path(event["cwd"]).resolve() == root / "project")
    except (ValueError, TypeError, OSError):
        valid = False
    audit = {"received": True, "fixture_event_valid": valid, "collector_invoked": False,
             "session_id_present": isinstance(event, dict) and isinstance(event.get("session_id"), str) and bool(event["session_id"]),
             "transcript_path_present": isinstance(event, dict) and isinstance(event.get("transcript_path"), str) and bool(event["transcript_path"])}
    if valid:
        env = dict(os.environ, NOUS_HOME=str(root / "nous-data"), NOUS_LEDGER_HOME=str(root / "nous-data" / "ledger"))
        try:
            outcome = subprocess.run([str(Path(sys.executable).resolve()), "-B", str(HOOKS / "nous_training_hook.py"),
                                      "--agent", "codex", "--managed-marker", "nous-training-v1"],
                                     input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=15)
            audit.update(collector_invoked=True, collector_returncode=outcome.returncode,
                         diagnostic_categories=diagnostic_codes(outcome.stderr.decode("utf-8", errors="replace")),
                         collector_static_codes=[code for code in ("invalid_hook_input", "incomplete_hook_input", "collection_failed")
                                                 if ("nous-training: " + code).encode() in outcome.stderr])
        except Exception as exc:
            audit.update(collector_error_type=type(exc).__name__)
    write_json(root / "audit" / "stop-received.json", audit)
    return 0


def stop_capture_summary(root, expected_session, *, agent="codex", expected_final=STOP_FINAL,
                         require_receipt=True, forbid_tools=True):
    """Read only this test's own cache; never import/recover to manufacture hook success."""
    from nous_training_export import record_digest
    from nous_training import project_id, _hash
    from nous_training_health import read_health
    pid = project_id(str(root / "project"))
    health = read_health(root / "nous-data", pid)
    health_row = health.get("projects", {}).get(pid, {}).get(agent, {})
    paths = list((root / "nous-data" / "training" / "records").glob("*.json"))
    receipt = root / "audit" / "stop-received.json"
    audit = json.loads(receipt.read_text()) if receipt.is_file() else {}
    result = {"hook_receipt": audit, "canonical_count": len(paths), "manual_import_used": False,
              "review_evidence_added": False, "stop_hook_verified": False, "health_status": health.get("status"),
              "health": {key: health_row.get(key) for key in ("attempt_count", "captured_count", "failed_count", "pending_count", "last_status", "last_error_code")}}
    if len(paths) == 1:
        record = json.loads(paths[0].read_text())
        final = record.get("messages", [{}])[-1]
        result.update(record_schema=record.get("schema"), record_digest=record_digest(record),
                      canonical_final_matches=final.get("role") == "assistant" and final.get("content") == [{"type": "text", "text": expected_final}],
                      evidence_count=len(record.get("evidence", [])), canonical_quality=record.get("quality", {}))
        # Match build_record's source-session binding without reading other sessions.
        expected_sid = hashlib.sha256(str(expected_session).encode()).hexdigest()[:24] if expected_session else None
        result["session_matches"] = bool(expected_sid and record.get("provenance", {}).get("session_id") == expected_sid)
        result["project_matches"] = (record.get("provenance", {}).get("project_id") == pid
                                     and record.get("agent", {}).get("id") == agent)
        result["project_opt_in"] = record.get("provenance", {}).get("authorization") == "project_opt_in"
        transcript = locate_transcript(agent, expected_session)
        result["source_transcript_matches"] = bool(transcript and record.get("provenance", {}).get("source_transcript_id")
                                                    == _hash(["transcript", os.path.realpath(transcript)])[:24])
        result["health_matches_record"] = bool(health.get("status") == "ok" and health_row.get("captured_count", 0) >= 1
                                               and health_row.get("failed_count") == 0 and health_row.get("pending_count") == 0
                                               and health_row.get("last_status") == "collected"
                                               and health_row.get("last_record_id") == record.get("id"))
        result["canonical_tool_blocks"] = sum(1 for message in record.get("messages", [])
                                               for block in message.get("content", [])
                                               if block.get("type") in ("tool_use", "tool_result"))
        result["stop_hook_verified"] = bool((not require_receipt or (audit.get("fixture_event_valid") and audit.get("collector_invoked")))
                                            and result["canonical_final_matches"] and result["session_matches"]
                                            and result["project_matches"] and result["source_transcript_matches"]
                                            and result["health_matches_record"] and result["project_opt_in"]
                                            and result["evidence_count"] == 0
                                            and (not forbid_tools or result["canonical_tool_blocks"] == 0))
    return result


def run_stop(args):
    if args.agent != "codex":
        raise ValueError("stop_profile_requires_codex")
    root = Path(args.out).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    args.report_owned = True
    for name in ("project", "audit", "private"):
        (root / name).mkdir(mode=0o700)
    fixture = prepare_stop_fixture(root)
    report = {"agent": "codex", "profile": "stop", "synthetic": True, "status": "prepared", "passed": False,
              "model_run_attempted": False, "global_config_modified_by_harness": False, "persistent_trust_written": False,
              "global_bypass_used": False, "credentials_read_by_harness": False, "timeout_seconds": 90,
              "token_budget": 12000, "verified_usd_ceiling": False,
              "loader_flag_difference": "exec_ignores_user_toml_inventory_does_not",
              "source_inventory_is_snapshot": True}
    write_json(root / "plan.json", report)
    write_json(root / "private" / "fixture.json", fixture)
    executable = shutil.which(args.agent)
    if not executable:
        raise ValueError("client_not_found")
    if not args.run and not args.probe:
        print(json.dumps(report))
        return 0
    trusted = verify_stop_trust(executable, root, fixture, report)
    if trusted is None:
        write_json(root / "report.json", report)
        print(json.dumps(report))
        return 1
    command = [executable, "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
               "-C", str(root / "project"), "-s", "read-only", "--json", *stop_config(root, trusted), STOP_PROMPT]
    write_json(root / "private" / "command-review.json", {"command": command})
    if not args.run:
        report["status"] = "probe_passed_model_not_started"
        write_json(root / "report.json", report)
        print(json.dumps(report))
        return 0
    # Repeat the exact runtime override inventory immediately before starting the client.
    safe, live = hook_inventory(executable, root, fixture, trusted)
    report["inventory_stages"].append({"stage": "before_model", **safe})
    if (not live or live["trustStatus"] != "trusted" or any(live[k] != trusted[k] for k in ("key", "currentHash"))
            or hashlib.sha256(Path(fixture["hook_file"]).read_bytes()).hexdigest() != fixture["sha256"]):
        report["status"] = "runtime_inventory_mismatch"
        write_json(root / "report.json", report)
        return 1
    os.environ["NOUS_HOME"] = str(root / "nous-data")
    os.environ["NOUS_LEDGER_HOME"] = str(root / "nous-data" / "ledger")
    sys.path.insert(0, str(HOOKS))
    import nous_training as training
    training.set_project(str(root / "project"), True)
    report.update(status="client_launch", model_run_attempted=True)
    write_json(root / "report.json", report)
    process = subprocess.Popen(command, cwd=root / "project", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        report["timed_out"] = True
    sid, final, counts = safe_events(stdout, expected_final=STOP_FINAL)
    if sid:
        write_json(root / "private" / "session.json", {"session_id": sid})
    report.update(status="client_finished", returncode=process.returncode, expected_final_seen=final, event_counts=counts,
                  elapsed_seconds=round(time.monotonic() - started, 2), diagnostic_categories=diagnostic_codes(stdout + "\n" + stderr))
    write_json(root / "report.json", report)
    report.update(stop_capture_summary(root, sid))
    if report.get("session_matches") and report.get("project_matches") and report.get("source_transcript_matches"):
        profile = "codemode-events-v1" if report.get("record_schema") == "nous.trajectory/2" else "trl"
        manifest = training.export(str(root / "dataset"), profile=profile)
        from nous_training_validate import validate_dataset
        from nous_training_audit import audit_dataset
        report.update(export_profile=profile, validation=validate_dataset(root / "dataset"),
                      audit=audit_dataset(root / "dataset"),
                      canonical_records=manifest["files"]["trajectories.jsonl"]["records"],
                      sft_records=manifest["files"]["sft.jsonl"]["records"], rejections=manifest["rejections"])
    report["stop_passed"] = bool(process.returncode == 0 and final and report["stop_hook_verified"] and not report.get("timed_out"))
    report["candidate_pipeline_passed"] = bool(report.get("validation", {}).get("valid") and report.get("audit", {}).get("structural_valid"))
    report["strict_sft_ready"] = bool(report["candidate_pipeline_passed"] and report.get("sft_records", 0) > 0)
    report["passed"] = report["stop_passed"] and report["candidate_pipeline_passed"]
    write_json(root / "report.json", report)
    print(json.dumps(report))
    return 0 if report["passed"] else 1


def serve(audit):
    """MCP stdio server. Only the harness audit files are written, never tool-selected paths."""
    calls = 0
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method, params = request.get("method"), request.get("params", {})
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-11-25"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER, "version": "1.0.0"}}
            elif method == "tools/list":
                result = {"tools": [TOOL]}
                # This is the exact result actually returned to the platform's discovery request.
                write_json(audit / "tools-list.json", result)
            elif method == "tools/call":
                calls += 1
                name, args = params.get("name"), params.get("arguments")
                valid = calls == 1 and name == "lookup" and args == {"key": "beta"}
                # Do not log unexpected arguments, since a misconfigured client might send private input.
                write_json(audit / "mcp-call.json", {"count": calls, "name_matches": name == "lookup", "arguments_match": args == {"key": "beta"}, "valid": valid})
                result = {"content": [{"type": "text", "text": json.dumps({"key": "beta", "value": 37}) if valid else "Synthetic fixture rejected this call."}], "isError": not valid}
            elif method in ("ping", "resources/list", "prompts/list"):
                result = {} if method == "ping" else {"resources" if method == "resources/list" else "prompts": []}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unsupported fixture method"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        except (ValueError, TypeError, KeyError):
            return 2
    return 0


def command(agent, root, executable):
    project, home, audit = root / "project", root / "nous-data", root / "audit"
    mcp = {"command": sys.executable, "args": [str(Path(__file__).resolve()), "--serve", str(audit)]}
    if agent == "claude":
        hook = shlex.join(["/usr/bin/env", "NOUS_HOME=" + str(home), "NOUS_LEDGER_HOME=" + str(home / "ledger"),
                           sys.executable, str(HOOKS / "nous_training_hook.py"), "--agent", "claude", "--managed-marker", "nous-training-v1"])
        settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hook, "timeout": 20}]}]},
                    "autoMemoryEnabled": False, "disableClaudeAiConnectors": True}
        write_json(root / "claude-settings.json", settings)
        write_json(root / "mcp.json", {"mcpServers": {SERVER: mcp}})
        sid = str(uuid.uuid4())
        args = [executable, "-p", "--restricted", "--setting-sources", "", "--settings", str(root / "claude-settings.json"),
                "--strict-mcp-config", "--mcp-config", str(root / "mcp.json"), "--tools", "", "--allowedTools", "mcp__nous_fixture__lookup",
                "--permission-mode", "dontAsk", "--permission-prompts", "none", "--disable-slash-commands", "--no-chrome",
                "--system-prompt", "You run one synthetic MCP smoke test. Follow the user's exact task and do not inspect files or environment.",
                "--session-id", sid, "--effort", "low", "--max-turns", "3", "--max-budget-usd", "0.75",
                "--output-format", "stream-json", "--verbose", PROMPT]
        return args, sid
    config = {"features.hooks": False, "features.apps": False, "features.shell_tool": False,
              "features.memories": False, "features.multi_agent": False, "skills.include_instructions": False,
              "memories.use_memories": False, "memories.generate_memories": False,
              "project_doc_max_bytes": 0, "web_search": "disabled", "model_reasoning_effort": "low",
              "history.persistence": "none", "log_dir": str(root / "codex-logs"), "sqlite_home": str(root / "codex-state"),
              "features.rollout_budget.enabled": True, "features.rollout_budget.limit_tokens": 12000,
              "features.rollout_budget.reminder_at_remaining_tokens": [3000],
              "mcp_servers." + SERVER: {**mcp, "enabled_tools": ["lookup"], "required": True,
                                        "default_tools_approval_mode": "approve", "startup_timeout_sec": 10,
                                        "tool_timeout_sec": 10}}
    args = [executable, "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "-C", str(project), "-s", "read-only", "--json"]
    # A complete RHS table preserves arbitrary server/path/trust keys exactly.
    server = config.pop("mcp_servers." + SERVER)
    config["mcp_servers"] = {SERVER: server}
    for key, value in config.items():
        args += ["-c", codex_assignment(key, value)]
    args += [PROMPT]
    return args, None


def safe_events(stdout, expected_final=FINAL):
    """Return only new-session metadata and known synthetic final-text status."""
    sid, final, counts = None, False, {}
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if isinstance(kind, str) and re.fullmatch(r"[A-Za-z_.]+", kind):
            counts[kind] = counts.get(kind, 0) + 1
        candidate = row.get("thread_id") if kind == "thread.started" else row.get("session_id")
        if isinstance(candidate, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", candidate):
            sid = candidate
        item = row.get("item", {})
        if isinstance(item, dict) and item.get("type") == "agent_message":
            final = final or (isinstance(item.get("text"), str) and item["text"].strip() == expected_final)
        final = final or (kind == "result" and isinstance(row.get("result"), str) and row["result"].strip() == expected_final)
    return sid, final, counts


def diagnostic_codes(text):
    """Content-free categories; never persist the matched source text."""
    patterns = {
        "auth_missing": r"not logged|not authenticated|login required|please.{0,20}/login|no api key|missing.{0,20}api.key",
        "auth_rejected": r"invalid.{0,20}api.key|unauthorized|authentication.{0,20}(failed|error)|\b401\b|token.{0,20}expired",
        "network_failed": r"ECONN|ENOTFOUND|fetch failed|network.{0,20}(error|failed)|unable to connect|connection.{0,20}(failed|refused)",
        "permission_denied": r"EPERM|EACCES|operation not permitted|permission denied",
        "untrusted": r"untrusted|project.{0,20}trust|MCP.{0,20}trust",
        "invalid_config": r"unknown.{0,20}field|unknown.{0,20}feature|invalid.{0,20}config|failed to load.{0,20}config",
        "budget_or_quota": r"budget|token limit|rate.limit|quota|credit balance",
        "sandbox_denied": r"sandbox.{0,30}(denied|failed|error)|seatbelt.{0,30}(denied|failed|error)",
    }
    return sorted(code for code, pattern in patterns.items() if re.search(pattern, text, re.I))


def canonical_fixture_passes(record):
    """Bind success to the stored observation, not merely the client's stdout."""
    messages = record.get("messages", [])
    if not messages or messages[-1].get("role") != "assistant":
        return False
    blocks = messages[-1].get("content", [])
    if not isinstance(blocks, list) or blocks != [{"type": "text", "text": FINAL}]:
        return False
    calls, results = [], []
    for message in messages:
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if block.get("type") == "tool_use":
                calls.append(block)
            elif block.get("type") == "tool_result":
                results.append(block)
    if (len(calls) != 1 or len(results) != 1 or calls[0].get("input") != {"key": "beta"}
            or calls[0].get("format", "function") != "function"
            or not isinstance(calls[0].get("id"), str) or not calls[0]["id"]):
        return False
    result = results[0]
    if result.get("tool_use_id") != calls[0].get("id") or result.get("is_error") is True:
        return False
    value = result.get("content")
    # Accept the observed platform's JSON text or MCP text-content wrapper only.
    for _ in range(4):
        if value == {"key": "beta", "value": 37}:
            return True
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return False
        elif isinstance(value, dict) and "content" in value and value.get("isError") is not True:
            value = value["content"]
        elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and value[0].get("type") == "text":
            value = value[0].get("text")
        else:
            return False
    return value == {"key": "beta", "value": 37}


def add_fixture_evidence(record):
    if (record.get("quality", {}).get("complete") is not True or record.get("quality", {}).get("issues")
            or not canonical_fixture_passes(record)):
        return False
    from nous_training_export import record_digest
    record.setdefault("evidence", []).append({"kind": "verifier", "result": "passed", "verifier": "live-fixture-v1",
                                             "record_digest": record_digest(record)})
    return True


def locate_transcript(agent, sid):
    """Inspect filenames only, then return only this newly generated session."""
    if not sid:
        return None
    if agent == "claude":
        directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        paths = list(directory.glob("*/" + sid + ".jsonl")) if directory.is_dir() else []
    else:
        directory = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        paths = list(directory.glob("**/*" + sid + ".jsonl")) if directory.is_dir() else []
    return paths[0] if len(paths) == 1 else None


def finalize(root, report):
    """Offline processing of this run only; no platform CLI or model invocation."""
    if report.get("synthetic") is not True or report.get("cwd") != str(root / "project"):
        raise ValueError("not_a_fixture_run")
    os.environ["NOUS_HOME"] = str(root / "nous-data")
    os.environ["NOUS_LEDGER_HOME"] = str(root / "nous-data" / "ledger")
    sys.path.insert(0, str(HOOKS))
    import nous_training as training
    agent, sid = report["agent"], report.get("session_id")
    wire = report.get("mcp_call", {})
    report["mcp_tools_list_received"] = (root / "audit" / "tools-list.json").is_file()
    report["client_mcp_passed"] = bool(report.get("returncode") == 0 and wire.get("valid") and report.get("expected_final_seen"))
    transcript = locate_transcript(agent, sid)
    report["transcript_found"] = transcript is not None
    report["schema_supplied_via_context"] = False
    report["native_tool_schemas_supported"] = False
    report["canonical_verifier_passed"] = False
    if transcript and report["client_mcp_passed"]:
        from nous_transcript import parse_transcript
        parsed = parse_transcript(transcript, agent=agent)
        report["adapter_quality"] = parsed["quality"]
        calls = [block for message in parsed["messages"] for block in message["content"] if block.get("type") == "tool_use"]
        context = None
        if (len(calls) == 1 and calls[0].get("format", "function") == "function"
                and calls[0].get("input") == {"key": "beta"} and isinstance(calls[0].get("name"), str)):
            catalog = json.loads((root / "audit" / "tools-list.json").read_text())
            original = catalog["tools"][0]
            actual_name = calls[0]["name"]
            context = {"tools": [{"type": "function", "function": {"name": actual_name,
                       "description": original["description"], "parameters": original["inputSchema"]}}]}
            write_json(root / "context.json", context)
            report["observed_tool_name"] = actual_name
            report["schema_supplied_via_context"] = True
        else:
            # Do not replace a real custom exec/Code Mode call with its nested MCP call.
            report["adapter_gap"] = "expected_one_direct_function_mcp_call"
        result = training.collect(transcript, str(root / "project"), agent=agent, session=sid, context=context, manual=True)
        report["import"] = result
        report["missing_context"] = result.get("quality", {}).get("missing", [])
        record_path = training._root() / "records" / (result["id"] + ".json")
        record = json.loads(record_path.read_text())
        record["provenance"].update(source="live_synthetic_fixture", synthetic=True)
        if context is not None and record.get("schema") != "nous.trajectory/2":
            report["canonical_verifier_passed"] = add_fixture_evidence(record)
        training._atomic(record_path, record)
        report["export_profile"] = "codemode-events-v1" if record.get("schema") == "nous.trajectory/2" else "trl"
        manifest = training.export(str(root / "dataset"), profile=report["export_profile"])
        from nous_training_validate import validate_dataset
        report["validation"] = validate_dataset(root / "dataset")
        report["canonical_records"] = manifest["files"]["trajectories.jsonl"]["records"]
        report["sft_records"] = manifest["files"]["sft.jsonl"]["records"]
        report["rejections"] = manifest["rejections"]
    report["candidate_pipeline_passed"] = bool(report.get("validation", {}).get("valid") and report.get("canonical_records", 0) > 0)
    report["passed"] = bool(report.get("validation", {}).get("valid") and report.get("sft_records", 0) > 0)
    report["manual_import_pipeline_passed"] = report["passed"]
    report["stop_hook_verified"] = report.get("stop_captured_before_import", False)
    report["stage"] = "finalized"
    write_json(root / "report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "command"}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def run(args):
    if getattr(args, "profile", "mcp") == "stop":
        return run_stop(args)
    root = Path(args.out).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    args.report_owned = True
    for name in ("project", "audit"):
        (root / name).mkdir(mode=0o700)
    executable = shutil.which(args.agent)
    if not executable:
        raise ValueError("client_not_found")
    cmd, sid = command(args.agent, root, executable)
    report = {"agent": args.agent, "synthetic": True, "cwd": str(root / "project"),
              "hooks_enabled_for_test": args.agent == "claude", "model_run_attempted": False,
              "global_config_modified_by_harness": False, "credentials_read_by_harness": False,
              "budget": "Claude estimated USD 0.75, may overshoot; Codex experimental 12000-token budget, no verified USD ceiling",
              "timeout_seconds": 90, "command": cmd}
    write_json(root / "plan.json", report)
    if not args.run:
        print(json.dumps({"plan": str(root / "plan.json"), "model_run_attempted": False}))
        return 0
    os.environ["NOUS_HOME"] = str(root / "nous-data")
    os.environ["NOUS_LEDGER_HOME"] = str(root / "nous-data" / "ledger")
    sys.path.insert(0, str(HOOKS))
    import nous_training as training
    if args.agent == "claude":
        training.set_project(str(root / "project"), True)
    report["model_run_attempted"] = True
    report["stage"] = "client_launch"
    write_json(root / "report.json", report)
    started = time.monotonic()
    process = subprocess.Popen(cmd, cwd=root / "project", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        report["timed_out"] = True
    observed_sid, final, counts = safe_events(stdout)
    sid = observed_sid or sid
    report.update(returncode=process.returncode, elapsed_seconds=round(time.monotonic() - started, 2),
                  session_id=sid, expected_final_seen=final, event_counts=counts,
                  stderr_present=bool(stderr), stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest())
    # No raw CLI output is saved: initialization/diagnostic events may contain user configuration.
    report["diagnostic_categories"] = diagnostic_codes(stderr + "\n" + stdout)
    report["result_subtypes"] = []
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "result":
            subtype = row.get("subtype")
            if isinstance(subtype, str) and re.fullmatch(r"[a-z_]{1,60}", subtype):
                report["result_subtypes"].append(subtype)
    capture = stop_capture_summary(root, sid, agent="claude", expected_final=FINAL,
                                   require_receipt=False, forbid_tools=False) if args.agent == "claude" else {}
    report["capture_before_import"] = capture
    report["stop_captured_before_import"] = capture.get("stop_hook_verified", False)
    wire_path = root / "audit" / "mcp-call.json"
    wire = json.loads(wire_path.read_text()) if wire_path.exists() else {}
    report["mcp_call"] = wire
    transcript = locate_transcript(args.agent, sid)
    report["transcript_found"] = transcript is not None
    report["stage"] = "client_finished"
    write_json(root / "report.json", report)
    return finalize(root, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude", "codex"))
    parser.add_argument("--out", help="new directory under a dedicated scratch location")
    parser.add_argument("--run", action="store_true", help="authorize one real-client invocation and model usage")
    parser.add_argument("--profile", choices=("mcp", "stop"), default="mcp", help="MCP import test or Codex autonomous Stop test")
    parser.add_argument("--probe", action="store_true", help="Stop profile: read-only hook inventory and temporary trust; no model")
    parser.add_argument("--finalize-existing", type=Path, help="offline finalize this harness's existing report; never call a model")
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--observe-stop", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.report_owned = False
    if args.observe_stop:
        return observe_stop(args.observe_stop)
    if args.serve:
        return serve(args.serve)
    if args.finalize_existing:
        args.out = str(args.finalize_existing.resolve())
    elif not args.agent or not args.out:
        parser.error("--agent and --out are required")
    if args.probe and (args.profile != "stop" or args.run):
        parser.error("--probe requires --profile stop without --run")
    try:
        if args.finalize_existing:
            root = args.finalize_existing.resolve()
            args.report_owned = True
            return finalize(root, json.loads((root / "report.json").read_text()))
        return run(args)
    except Exception as exc:
        # The exception may include private paths or client details.
        report = {"error": "live_smoke_failed", "type": type(exc).__name__, "passed": False}
        if args.report_owned and args.out and Path(args.out).is_dir() and (Path(args.out) / "plan.json").is_file():
            prior = Path(args.out) / "report.json"
            if not prior.is_file():
                prior = Path(args.out) / "plan.json"
            report = {**json.loads(prior.read_text()), **report}
            write_json(Path(args.out) / "report.json", report)
        print(json.dumps({k: v for k, v in report.items() if k != "command"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
