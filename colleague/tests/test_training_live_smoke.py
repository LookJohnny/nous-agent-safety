"""No platform CLI or model calls: validate only the synthetic live-test verifier."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "hooks"))
spec = importlib.util.spec_from_file_location("training_live_smoke", BASE / "scripts" / "training_live_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def record():
    return {"messages": [
        {"role": "user", "content": [{"type": "text", "text": smoke.PROMPT}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-one", "name": "fixture_lookup", "input": {"key": "beta"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-one", "is_error": False,
                                       "content": json.dumps({"key": "beta", "value": 37})}]},
        {"role": "assistant", "content": [{"type": "text", "text": smoke.FINAL}]},
    ], "tools": [], "quality": {"complete": True, "issues": []}, "evidence": []}


def inventory(root, fixture, trust="untrusted"):
    return {"data": [{"cwd": str(root / "project"), "errors": [], "warnings": [], "hooks": [{
        "currentHash": "a" * 64, "displayOrder": 0, "enabled": True, "eventName": "stop", "isManaged": False,
        "key": "project./synthetic/.codex/hooks.json:Stop:0:0", "source": "project", "sourcePath": fixture["hook_file"],
        "timeoutSec": 20, "trustStatus": trust, "handlerType": "command", "command": fixture["command"]}]}]}


class LiveFixtureVerifier(unittest.TestCase):
    def test_cli_special_keys_are_preserved_in_rhs_inline_tables(self):
        project = '/synthetic/project.with.dots/测试 "quoted" \\path'
        hook = 'project./synthetic/.codex/hooks.json:Stop:0:0'
        assignments = [smoke.codex_assignment("projects", {project: {"trust_level": "trusted"}}),
                       smoke.codex_assignment("hooks.state", {hook: {"trusted_hash": "a" * 64}})]
        parsed = tomllib.loads("\n".join(assignments))
        self.assertEqual(parsed["projects"], {project: {"trust_level": "trusted"}})
        self.assertEqual(parsed["hooks"]["state"], {hook: {"trusted_hash": "a" * 64}})
        for lhs in ('projects."/tmp/toy".trust_level', 'hooks.state."key.with.dot".trusted_hash'):
            with self.assertRaisesRegex(ValueError, "special_cli_key"):
                smoke.codex_assignment(lhs, "not-used")

    def test_cli_encoder_rejects_unsupported_values_instead_of_string_fallback(self):
        for value in (None, 1.5, 2 ** 63, {1: "bad-key"}, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises((ValueError, UnicodeError)):
                smoke.codex_assignment("synthetic", value)

    def test_codex_command_uses_valid_toml_and_only_fixture_mcp(self):
        with tempfile.TemporaryDirectory() as temp:
            args, sid = smoke.command("codex", Path(temp), "/synthetic/codex")
        values = [args[i + 1] for i, arg in enumerate(args) if arg == "-c"]
        parsed = tomllib.loads("\n".join(values))
        self.assertIsNone(sid)
        self.assertIn("--ignore-user-config", args)
        self.assertEqual(set(parsed["mcp_servers"]), {smoke.SERVER})
        self.assertEqual(parsed["mcp_servers"][smoke.SERVER]["enabled_tools"], ["lookup"])
        self.assertFalse(parsed["features"]["hooks"])
        self.assertEqual(parsed["features"]["rollout_budget"]["limit_tokens"], 12000)

    def test_stop_config_scope_and_exact_trust_are_shared_values(self):
        root = Path('/synthetic/with.dots/"quoted"')
        trust = {"key": "project.with.dots", "currentHash": "a" * 64}
        args = smoke.stop_config(root, trust)
        parsed = tomllib.loads("\n".join(args[1::2]))
        self.assertEqual(parsed["projects"], {str(root / "project"): {"trust_level": "trusted"}})
        self.assertEqual(parsed["hooks"]["state"], {trust["key"]: {"trusted_hash": trust["currentHash"]}})
        self.assertEqual(parsed["mcp_servers"], {})
        self.assertEqual(parsed["project_root_markers"], [])
        for key in ("plugins", "apps", "shell_tool", "memories", "multi_agent"):
            self.assertFalse(parsed["features"][key])

    def test_inventory_counts_disabled_other_hooks_and_rejects_unknown_shape(self):
        root = Path('/synthetic')
        fixture = {"hook_file": "/synthetic/project/.codex/hooks.json", "command": "synthetic-only"}
        valid = inventory(root, fixture)
        safe, hook = smoke.summarize_hook_inventory(valid, root, fixture)
        self.assertTrue(safe["matched"])
        self.assertIsNotNone(hook)
        wrongs = [{}, {"data": []}, copy.deepcopy(valid), copy.deepcopy(valid), copy.deepcopy(valid), copy.deepcopy(valid)]
        wrongs[2]["data"][0]["hooks"].append({**valid["data"][0]["hooks"][0], "enabled": False})
        wrongs[3]["data"][0]["hooks"][0]["isManaged"] = True
        wrongs[4]["data"][0]["hooks"][0]["command"] = "different"
        wrongs[5]["data"][0]["warnings"].append({"message": "never copy warning content"})
        for value in wrongs:
            safe, hook = smoke.summarize_hook_inventory(value, root, fixture)
            self.assertFalse(safe["matched"])
            self.assertIsNone(hook)
            self.assertNotIn("warning content", json.dumps(safe))

    def test_trust_cycle_never_writes_trust_and_detects_changed_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "project").mkdir()
            fixture = smoke.prepare_stop_fixture(root)
            seq = [smoke.summarize_hook_inventory(inventory(root, fixture, status), root, fixture)
                   for status in ("untrusted", "trusted", "untrusted")]
            before = Path(fixture["hook_file"]).read_bytes()
            report = {}
            with mock.patch.object(smoke, "hook_inventory", side_effect=seq) as call:
                trusted = smoke.verify_stop_trust("synthetic", root, fixture, report)
            self.assertIsNotNone(trusted)
            self.assertTrue(report["temporary_exact_trust_verified"])
            self.assertIsNone(call.call_args_list[0].args[3])
            self.assertEqual(call.call_args_list[1].args[3]["key"], trusted["key"])
            self.assertIsNone(call.call_args_list[2].args[3])
            self.assertEqual(before, Path(fixture["hook_file"]).read_bytes())
            seq[1] = copy.deepcopy(seq[1])
            seq[1][1]["currentHash"] = "b" * 64
            with mock.patch.object(smoke, "hook_inventory", side_effect=seq):
                self.assertIsNone(smoke.verify_stop_trust("synthetic", root, fixture, {}))

    def test_runtime_inventory_failure_prevents_project_enable_and_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "new"
            args = SimpleNamespace(agent="codex", out=str(root), run=True, probe=False, report_owned=False)
            trusted = {"key": "synthetic", "currentHash": "a" * 64}
            with mock.patch.object(smoke.shutil, "which", return_value="/synthetic/codex"), \
                 mock.patch.object(smoke, "verify_stop_trust", side_effect=lambda e, r, f, report: report.update(inventory_stages=[]) or trusted), \
                 mock.patch.object(smoke, "hook_inventory", return_value=({"matched": False}, None)), \
                 mock.patch.object(smoke.subprocess, "Popen") as launch:
                self.assertEqual(smoke.run_stop(args), 1)
            launch.assert_not_called()
            self.assertFalse((root / "nous-data").exists())
            report = json.loads((root / "report.json").read_text())
            self.assertFalse(report["model_run_attempted"])
            self.assertEqual(report["status"], "runtime_inventory_mismatch")

    def test_stop_verifier_requires_receipt_same_session_and_exact_cached_final(self):
        from nous_training import project_id, _hash
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "audit").mkdir()
            cached = root / "nous-data" / "training" / "records"
            cached.mkdir(parents=True)
            sid = "01234567-1234-1234-1234-0123456789ab"
            pid = project_id(str(root / "project"))
            transcript = root / "synthetic-transcript.jsonl"
            value = {"schema": "nous.trajectory/1", "id": "t_" + "a" * 32, "agent": {"id": "codex"},
                     "messages": [{"role": "assistant", "content": [{"type": "text", "text": smoke.STOP_FINAL}]}],
                     "evidence": [], "quality": {"complete": False, "issues": ["synthetic_missing_context"]},
                     "provenance": {"session_id": hashlib.sha256(sid.encode()).hexdigest()[:24], "project_id": pid,
                                    "authorization": "project_opt_in",
                                    "source_transcript_id": _hash(["transcript", str(transcript)])[:24]}}
            health = {"status": "ok", "projects": {pid: {"codex": {"captured_count": 1, "failed_count": 0,
                      "pending_count": 0, "last_status": "collected", "last_record_id": value["id"]}}}}
            smoke.write_json(cached / "one.json", value)
            with mock.patch("nous_training_export.record_digest", return_value="safe-digest"), \
                 mock.patch("nous_training_health.read_health", return_value=health), \
                 mock.patch.object(smoke, "locate_transcript", return_value=transcript):
                self.assertFalse(smoke.stop_capture_summary(root, sid)["stop_hook_verified"])
                smoke.write_json(root / "audit" / "stop-received.json", {"fixture_event_valid": True, "collector_invoked": True})
                result = smoke.stop_capture_summary(root, sid)
                self.assertTrue(result["stop_hook_verified"])
                self.assertFalse(result["manual_import_used"])
                self.assertFalse(smoke.stop_capture_summary(root, "other-session")["stop_hook_verified"])
                value["provenance"]["authorization"] = "one_shot_import"
                smoke.write_json(cached / "one.json", value)
                self.assertFalse(smoke.stop_capture_summary(root, sid)["stop_hook_verified"])
                value["provenance"]["authorization"] = "project_opt_in"
                value["evidence"] = [{"kind": "verifier", "result": "passed"}]
                smoke.write_json(cached / "one.json", value)
                self.assertFalse(smoke.stop_capture_summary(root, sid)["stop_hook_verified"])
                value["evidence"] = []
                smoke.write_json(cached / "one.json", value)
                health["projects"][pid]["codex"]["captured_count"] = 0
                self.assertFalse(smoke.stop_capture_summary(root, sid)["stop_hook_verified"])
                health["projects"][pid]["codex"]["captured_count"] = 1
                value["provenance"]["project_id"] = "p_" + "0" * 24
                smoke.write_json(cached / "one.json", value)
                self.assertFalse(smoke.stop_capture_summary(root, sid)["stop_hook_verified"])
                value["provenance"]["project_id"] = pid
                value["messages"][-1]["content"][0]["text"] = "wrong"
                smoke.write_json(cached / "one.json", value)
                self.assertFalse(smoke.stop_capture_summary(root, sid)["stop_hook_verified"])

    def test_safe_final_parser_accepts_stop_profile_but_not_lookup_profile(self):
        text = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": smoke.STOP_FINAL}})
        self.assertTrue(smoke.safe_events(text, expected_final=smoke.STOP_FINAL)[1])
        self.assertFalse(smoke.safe_events(text)[1])

    def test_stop_wrapper_rejects_invalid_or_wrong_scope_input_without_collector(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "audit").mkdir()
            for payload in (b"not-json", b"x" * 65537,
                            json.dumps({"hook_event_name": "Stop", "cwd": "/different/synthetic-project", "session_id": "private-value"}).encode()):
                with mock.patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload))), \
                     mock.patch.object(smoke.subprocess, "run") as invoke:
                    self.assertEqual(smoke.observe_stop(root), 0)
                invoke.assert_not_called()
                saved = json.loads((root / "audit" / "stop-received.json").read_text())
                self.assertFalse(saved["fixture_event_valid"])
                self.assertNotIn("private-value", json.dumps(saved))

    def test_valid_record_has_digest_bound_verifier_not_human_review(self):
        from nous_training_export import record_digest
        value = record()
        self.assertTrue(smoke.add_fixture_evidence(value))
        self.assertEqual(value["evidence"], [{"kind": "verifier", "result": "passed", "verifier": "live-fixture-v1",
                                             "record_digest": record_digest(value)}])

    def test_wrong_missing_error_and_unrelated_results_never_get_evidence(self):
        wrong = record()
        wrong["messages"][2]["content"][0]["content"] = '{"key":"beta","value":38}'
        missing = record()
        del missing["messages"][2]
        error = record()
        error["messages"][2]["content"][0]["is_error"] = True
        unrelated = record()
        unrelated["messages"][2]["content"][0]["tool_use_id"] = "different-call"
        final = record()
        final["messages"][-1]["content"][0]["text"] = "unverified answer"
        wrong_input = record()
        wrong_input["messages"][1]["content"][0]["input"] = {"key": "alpha"}
        partial = record()
        partial["quality"]["complete"] = False
        extra = record()
        extra["messages"].insert(2, copy.deepcopy(extra["messages"][1]))
        custom = record()
        custom["messages"][1]["content"][0]["format"] = "custom"
        no_id = record()
        no_id["messages"][1]["content"][0].pop("id")
        no_id["messages"][2]["content"][0].pop("tool_use_id")
        for value in (wrong, missing, error, unrelated, final, wrong_input, partial, extra, custom, no_id):
            with self.subTest(value=value):
                self.assertFalse(smoke.add_fixture_evidence(value))
                self.assertEqual(value["evidence"], [])

    def test_mcp_text_wrapper_success_and_nested_error(self):
        value = record()
        payload = {"content": [{"type": "text", "text": json.dumps({"key": "beta", "value": 37})}], "isError": False}
        value["messages"][2]["content"][0]["content"] = json.dumps(payload)
        self.assertTrue(smoke.canonical_fixture_passes(value))
        payload["isError"] = True
        value["messages"][2]["content"][0]["content"] = json.dumps(payload)
        self.assertFalse(smoke.add_fixture_evidence(value))

    def test_diagnostics_do_not_copy_secret_or_configuration_text(self):
        text = 'Invalid API key synthetic-secret@example.invalid; EPERM Operation not permitted; config=private'
        self.assertEqual(smoke.diagnostic_codes(text), ["auth_rejected", "permission_denied"])

    def test_unexpected_failure_keeps_static_report_without_exception_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            smoke.write_json(root / "plan.json", {"agent": "claude", "model_run_attempted": False})
            smoke.write_json(root / "report.json", {"agent": "claude", "model_run_attempted": True, "stage": "client_launch"})
            def fail(args):
                args.report_owned = True
                raise RuntimeError("private-token-content")
            with mock.patch.object(sys, "argv", ["training_live_smoke.py", "--agent", "claude", "--out", temp]), \
                 mock.patch.object(smoke, "run", side_effect=fail), \
                 mock.patch("sys.stderr"):
                self.assertEqual(smoke.main(), 1)
            result = json.loads((root / "report.json").read_text())
            self.assertTrue(result["model_run_attempted"])
            self.assertEqual(result["stage"], "client_launch")
            self.assertFalse(result["passed"])
            self.assertNotIn("private-token-content", json.dumps(result))

    def test_refusing_existing_output_does_not_overwrite_earlier_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            smoke.write_json(root / "plan.json", {"synthetic": True})
            smoke.write_json(root / "report.json", {"passed": True})
            before = (root / "report.json").read_bytes()
            with mock.patch.object(sys, "argv", ["training_live_smoke.py", "--agent", "claude", "--out", temp]), mock.patch("sys.stderr"):
                self.assertEqual(smoke.main(), 1)
            self.assertEqual((root / "report.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
