"""Synthetic end-to-end collection, revocation and privacy regression tests."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import nous_ledger
import nous_mask
import nous_privacy
import nous_training as training
from nous_training_export import preference_digest, record_digest


def message(role, text, ident):
    return {"type": role, "uuid": ident, "message": {
        "role": role, "id": ident, "content": text}}


class Collection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nous-training-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir()
        home_patch = mock.patch.object(training, "HOME", self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        key_patch = mock.patch.object(nous_ledger, "_key", return_value=b"synthetic-training-key-32-bytes!!")
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.transcript = self.write_transcript("initial")

    def write_transcript(self, name, prompt="请回答这个问题", reply="问题已回答", rows=None):
        path = self.root / (name + ".jsonl")
        rows = rows or [message("user", prompt, name + "-u"), message("assistant", reply, name + "-a")]
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def stored(self, ident):
        return json.loads((self.home / "training" / "records" / (ident + ".json")).read_text())

    def import_record(self, transcript=None, **kwargs):
        return training.collect(transcript or self.transcript, str(self.project), manual=True, **kwargs)

    def output(self, name):
        return str(self.root / name)

    def preference(self, source_id, chosen="较好的答案"):
        source = self.stored(source_id)
        pref = {"id": "pair-1", "prompt": source["messages"][:-1],
                "chosen": [{"role": "assistant", "content": chosen}],
                "rejected": [{"role": "assistant", "content": "较差的答案"}],
                "provenance": {"source_record_id": source_id}}
        pref["evidence"] = [{"kind": "human_preference", "result": "chosen", "reviewer": "r1",
                             "record_digest": preference_digest(pref)}]
        return pref

    def test_default_off_never_opens_transcript(self):
        with mock.patch.object(training, "build_record", side_effect=AssertionError("must not read")):
            self.assertEqual(training.collect("missing.jsonl", str(self.project)), {"status": "disabled"})
            self.assertIsNone(training.capture_stop({"event": "Stop", "agent": "claude"},
                {"cwd": str(self.project), "transcript_path": "missing.jsonl"}))
        self.assertFalse((self.home / "training").exists())

    def test_import_review_export_and_replay_keep_evidence(self):
        result = self.import_record()
        ident = result["id"]
        training.review(ident, "reviewer-1", "accepted")
        digest = record_digest(self.stored(ident))
        replay = self.import_record()
        self.assertEqual(replay["id"], ident)
        self.assertEqual(record_digest(self.stored(ident)), digest)
        self.assertEqual(len(self.stored(ident)["evidence"]), 1)
        manifest = training.export(self.output("dataset"))
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 1)
        sample = json.loads((self.root / "dataset" / "sft.jsonl").read_text())
        self.assertEqual(sample["messages"][-1]["content"], "问题已回答")
        self.assertEqual((self.home / "training" / "records" / (ident + ".json")).stat().st_mode & 0o777, 0o600)

    def test_content_change_invalidates_previous_review(self):
        result = self.import_record()
        training.review(result["id"], "reviewer-1", "accepted")
        self.write_transcript("initial", reply="回答已经改变")
        self.import_record()
        self.assertEqual(self.stored(result["id"])["evidence"], [])
        manifest = training.export(self.output("unreviewed"))
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_mask_failure_never_writes_content_or_import_permission(self):
        with mock.patch.object(nous_mask, "mask", side_effect=RuntimeError("synthetic-sensitive-content")):
            with self.assertRaises(RuntimeError):
                self.import_record()
        self.assertFalse((self.home / "training" / "records").exists())
        self.assertEqual(training._config()["projects"], {})
        self.assertTrue(all(p.stat().st_size == 0 for p in (self.home / "training" / "locks").glob("*.lock")))
        training.set_project(str(self.project), True)
        with mock.patch.object(nous_mask, "mask", side_effect=RuntimeError("synthetic-sensitive-content")):
            result = training.capture_stop({"event": "Stop", "agent": "claude"},
                {"cwd": str(self.project), "transcript_path": str(self.transcript), "session_id": "s1"})
        self.assertEqual(result, {"status": "collection_failed"})
        self.assertFalse((self.home / "training" / "records").exists())

    def test_dynamic_keys_and_sensitive_values_are_scrubbed_idempotently(self):
        record = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
                    "name": "lookup", "input": {"person@private-company.invalid": "13800138000", "password": "secret"}}]}],
                  "quality": {"complete": True, "issues": []}, "tools": [{"type": "function", "function": {
                    "name": "lookup", "parameters": {"type": "object", "properties": {"password": {"type": "string"}}}}}]}
        first = training.scrub_record(record)
        second = training.scrub_record(first)
        text = json.dumps(first)
        self.assertNotIn("person@private-company.invalid", text)
        self.assertNotIn("13800138000", text)
        self.assertEqual(record_digest(first), record_digest(second))
        self.assertEqual(first["tools"][0]["function"]["parameters"]["properties"]["password"], {"type": "string"})

    def test_redacted_dynamic_key_collision_fails_closed(self):
        record = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
                    "name": "lookup", "input": {"keyA": "one", "keyB": "two"}}]}],
                  "quality": {"complete": True, "issues": []}}
        original = nous_mask.mask
        with mock.patch.object(nous_mask, "mask", side_effect=lambda text, **kw:
                               "same-key" if text in ("keyA", "keyB") else original(text, **kw)):
            with self.assertRaisesRegex(ValueError, "redacted_key_collision"):
                training.scrub_record(record)

    def file_fixture(self, name="private-file"):
        path = self.project / "private_notes.txt"
        path.write_text("SYNTHETIC_PRIVATE_FILE_CONTENT")
        (self.project / ".gitignore").write_text("private_notes.txt\n")
        rows = [message("user", "synthetic inspect", "u1"),
                message("assistant", [{"type": "tool_use", "id": "c1", "name": "Read",
                    "input": {"file_path": str(path), "content": "SYNTHETIC_PRIVATE_TOOL_INPUT"}}], "a1"),
                message("user", [{"type": "tool_result", "tool_use_id": "c1",
                    "content": "SYNTHETIC_PRIVATE_FILE_CONTENT"}], "u2"),
                message("assistant", "synthetic completed response", "a2")]
        return self.write_transcript(name, rows=rows), path

    def assert_file_excluded(self, result, *, unverified):
        record = self.stored(result["id"])
        raw = json.dumps(record)
        self.assertFalse(record["quality"]["complete"])
        self.assertIn("excluded_file_content", record["quality"]["issues"])
        self.assertEqual("privacy_exclusion_unverified" in record["quality"]["issues"], unverified)
        for marker in ("SYNTHETIC_PRIVATE_FILE_CONTENT", "SYNTHETIC_PRIVATE_TOOL_INPUT", "SYNTHETIC_PRIVATE_STDERR"):
            self.assertNotIn(marker, raw)
        self.assertEqual(record["messages"][1]["content"][0]["input"], {"omitted": "excluded_file"})
        self.assertEqual(record["messages"][2]["content"][0]["content"], "[excluded file content omitted]")

    def test_missing_git_excludes_file_input_and_result_before_persistence(self):
        transcript, _ = self.file_fixture()
        with mock.patch.object(nous_privacy.subprocess, "run", side_effect=FileNotFoundError("SYNTHETIC_PRIVATE_STDERR")):
            result = self.import_record(transcript)
        self.assert_file_excluded(result, unverified=True)

    def test_git_timeout_excludes_file_input_and_result_without_exposing_stderr(self):
        transcript, _ = self.file_fixture()
        timeout = subprocess.TimeoutExpired(["git"], 3, stderr=b"SYNTHETIC_PRIVATE_STDERR")
        with mock.patch.object(nous_privacy.subprocess, "run", side_effect=timeout):
            result = self.import_record(transcript)
        self.assert_file_excluded(result, unverified=True)

    def test_git_error_status_including_non_repository_fails_closed(self):
        transcript, _ = self.file_fixture()
        for status in (2, 128, -9, None, True):
            with self.subTest(status=status), mock.patch.object(nous_privacy.subprocess, "run", return_value=
                    subprocess.CompletedProcess(["git"], status, stderr=b"SYNTHETIC_PRIVATE_STDERR")):
                result = self.import_record(transcript)
            self.assert_file_excluded(result, unverified=True)

    def test_confirmed_gitignored_file_remains_excluded_without_unverified_reason(self):
        transcript, path = self.file_fixture()
        with mock.patch.object(nous_privacy.subprocess, "run", return_value=subprocess.CompletedProcess(["git"], 0)) as git:
            result = self.import_record(transcript)
        git.assert_called_once_with(["git", "-C", os.path.realpath(self.project), "check-ignore", "-q", str(path)],
                                    capture_output=True, timeout=3)
        self.assert_file_excluded(result, unverified=False)

    def test_confirmed_not_ignored_file_preserves_visible_tool_data(self):
        transcript, _ = self.file_fixture()
        with mock.patch.object(nous_privacy.subprocess, "run", return_value=subprocess.CompletedProcess(["git"], 1)):
            result = self.import_record(transcript)
        record = self.stored(result["id"])
        self.assertTrue(record["quality"]["complete"])
        self.assertIn("SYNTHETIC_PRIVATE_FILE_CONTENT", json.dumps(record))

    def test_legacy_exclusion_callers_keep_best_effort_git_behavior(self):
        _, path = self.file_fixture()
        with mock.patch.object(nous_privacy.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(nous_privacy.excluded(path, str(self.project)))
            self.assertEqual(nous_privacy.excluded(path, str(self.project), strict=True), "privacy_exclusion_unverified")

    def tool_role_record(self, call_id, content):
        path = self.project / "private_notes.txt"
        path.write_text("SYNTHETIC_PRIVATE_FILE_CONTENT")
        return {"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": "Read",
                "input": {"file_path": str(path), "content": "SYNTHETIC_PRIVATE_TOOL_INPUT"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": content}],
            "quality": {"complete": True, "issues": []}}

    def test_role_tool_result_is_removed_for_every_content_shape_when_file_is_unverified(self):
        forms = ["SYNTHETIC_PRIVATE_FILE_CONTENT", [{"type": "text", "text": "SYNTHETIC_PRIVATE_FILE_CONTENT"}],
                 {"nested": "SYNTHETIC_PRIVATE_FILE_CONTENT"}, ["SYNTHETIC_PRIVATE_FILE_CONTENT"], None, False, 42]
        for content in forms:
            with self.subTest(content_type=type(content).__name__), \
                 mock.patch.object(nous_privacy.subprocess, "run", side_effect=FileNotFoundError()):
                record = training.scrub_record(self.tool_role_record("c1", content), str(self.project))
            self.assertFalse(record["quality"]["complete"])
            self.assertIn("privacy_exclusion_unverified", record["quality"]["issues"])
            self.assertEqual(record["messages"][1]["content"], "[excluded file content omitted]")
            self.assertEqual(record["messages"][1]["tool_call_id"], "c1")
            self.assertNotIn("SYNTHETIC_PRIVATE_FILE_CONTENT", json.dumps(record))
            self.assertNotIn("SYNTHETIC_PRIVATE_TOOL_INPUT", json.dumps(record))

    def test_role_tool_sensitive_call_id_is_normalized_before_exclusion_matching(self):
        call_id = "call@private-company.invalid"
        with mock.patch.object(nous_privacy.subprocess, "run", side_effect=FileNotFoundError()):
            record = training.scrub_record(self.tool_role_record(call_id, "SYNTHETIC_PRIVATE_FILE_CONTENT"), str(self.project))
        call, result = record["messages"]
        self.assertTrue(call["content"][0]["id"].startswith("redacted_"))
        self.assertEqual(call["content"][0]["id"], result["tool_call_id"])
        self.assertEqual(result["content"], "[excluded file content omitted]")
        self.assertIn("redacted_metadata", record["quality"]["issues"])
        self.assertIn("privacy_exclusion_unverified", record["quality"]["issues"])
        for value in (call_id, "SYNTHETIC_PRIVATE_FILE_CONTENT", "SYNTHETIC_PRIVATE_TOOL_INPUT"):
            self.assertNotIn(value, json.dumps(record))

    def test_numeric_pii_fields_are_masked_without_changing_tool_schema(self):
        record = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
                    "name": "lookup", "input": {"phone": 13800138000, "card_number": 4111111111111111,
                                                      "count": 3}}]}],
                  "quality": {"complete": True, "issues": []}, "tools": [{"type": "function", "function": {
                    "name": "lookup", "parameters": {"type": "object", "properties": {"phone": {"type": "integer"}}}}}]}
        first = training.scrub_record(record)
        args = first["messages"][0]["content"][0]["input"]
        self.assertRegex(args["phone"], r"^<PII_[0-9a-f]{4}>$")
        self.assertRegex(args["card_number"], r"^<PII_[0-9a-f]{4}>$")
        self.assertEqual(args["count"], 3)
        self.assertEqual(first["tools"], record["tools"])
        self.assertFalse(first["quality"]["complete"])
        self.assertIn("pii_type_changed", first["quality"]["issues"])
        second = training.scrub_record(first)
        self.assertEqual(record_digest(first), record_digest(second))
        self.assertFalse(second["quality"]["complete"])
        self.assertIn("pii_type_changed", second["quality"]["issues"])

    def test_private_metadata_is_redacted_with_tool_linkage_and_sft_exclusion(self):
        private_uuid = "uuid@private-company.invalid"
        private_parent = "parent@private-company.invalid"
        private_call = "call@private-company.invalid"
        private_tool = "tool@private-company.invalid"
        private_stamp = "sk-" + "simTEST1234567890abcdEFGH"
        rows = [message("user", "读取代码", private_uuid),
                message("assistant", [{"type": "tool_use", "id": private_call, "name": private_tool,
                                         "input": {"file_path": "app.py"}}], "a1"),
                message("user", [{"type": "tool_result", "tool_use_id": private_call,
                                    "content": "print(1)"}], "u2"),
                message("assistant", "读取完成", "a2")]
        rows[0]["timestamp"] = private_stamp
        rows[1]["parentUuid"] = private_parent
        context = {"tools": [{"type": "function", "function": {"name": private_tool, "parameters": {
            "type": "object", "properties": {"file_path": {"type": "string"}}}}}]}
        result = self.import_record(self.write_transcript("private-metadata", rows=rows), context=context)
        record = self.stored(result["id"])
        raw = json.dumps(record)
        private_values = (private_uuid, private_parent, private_call, private_tool, private_stamp)
        for value in private_values:
            self.assertNotIn(value, raw)
        messages = record["messages"]
        self.assertEqual(messages[1]["content"][0]["id"], messages[2]["content"][0]["tool_use_id"])
        self.assertTrue(messages[1]["content"][0]["id"].startswith("redacted_"))
        self.assertNotIn("ts", messages[0])
        self.assertFalse(record["quality"]["complete"])
        self.assertIn("redacted_metadata", record["quality"]["issues"])
        training.review(result["id"], "r1", "accepted")
        manifest = training.export(self.output("metadata-dataset"))
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        for path in (self.root / "metadata-dataset").iterdir():
            text = path.read_text()
            for value in private_values:
                self.assertNotIn(value, text)

    def test_one_shot_import_does_not_reauthorize_disabled_sibling_records(self):
        training.set_project(str(self.project), True)
        old = training.collect(self.transcript, str(self.project), session="older")
        training.set_project(str(self.project), False)
        new_path = self.write_transcript("new", prompt="只导入这一份", reply="新回答")
        new = self.import_record(new_path, session="newer")
        self.assertFalse(training._allowed_record(self.stored(old["id"])))
        self.assertTrue(training._allowed_record(self.stored(new["id"])))
        self.assertFalse(training._allowed(training.project_id(self.project), "capture"))
        manifest = training.export(self.output("scoped"))
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        training.set_project(str(self.project), False)
        self.assertFalse(training._allowed_record(self.stored(new["id"])))

    def test_revocation_during_parse_prevents_write(self):
        training.set_project(str(self.project), True)
        build = training.build_record
        def revoke_after_read(*args, **kwargs):
            record = build(*args, **kwargs)
            training.set_project(str(self.project), False)
            return record
        with mock.patch.object(training, "build_record", side_effect=revoke_after_read):
            self.assertEqual(training.collect(self.transcript, str(self.project)), {"status": "disabled"})
        self.assertFalse((self.home / "training" / "records").exists())

    def test_in_flight_manual_import_cannot_undo_a_later_revocation(self):
        build = training.build_record
        for revoke in (lambda: training.revoke_all(purge=True), lambda: training.set_project(str(self.project), False)):
            def revoke_after_read(*args, **kwargs):
                record = build(*args, **kwargs)
                revoke()
                return record
            with mock.patch.object(training, "build_record", side_effect=revoke_after_read):
                self.assertEqual(self.import_record(), {"status": "disabled"})
            self.assertEqual(training.list_records()["records"], [])
        # A later, explicitly requested import may grant this record again.
        result = self.import_record()
        self.assertTrue(training._allowed_record(self.stored(result["id"])))

    def test_existing_consent_revoke_stops_local_export_and_delete_purges(self):
        import nous_consent
        import nous_spool
        ident = self.import_record()["id"]
        with mock.patch.object(nous_consent, "HOME", str(self.home)), \
             mock.patch.object(nous_consent, "STORE", str(self.home / "consent.json")), \
             mock.patch.object(nous_consent, "_tell_server"), mock.patch.object(nous_spool, "purge"):
            nous_consent.revoke()
            self.assertFalse(training._allowed_record(self.stored(ident)))
            self.assertEqual(training.export(self.output("revoked"))["files"]["trajectories.jsonl"]["records"], 0)
            nous_consent.revoke("user_delete", delete=True)
        self.assertEqual(list((self.home / "training" / "records").glob("*.json")), [])

    def test_preference_requires_current_source_authorization(self):
        ident = self.import_record()["id"]
        pref = self.preference(ident)
        manifest = training.export(self.output("pairs"), preferences=[pref])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 1)
        training.set_project(str(self.project), False)
        with self.assertRaisesRegex(ValueError, "preference_project_disabled"):
            training.export(self.output("disabled-pairs"), preferences=[pref])
        self.assertFalse((self.root / "disabled-pairs" / "manifest.json").exists())

    def test_preference_masking_does_not_resign_evidence(self):
        ident = self.import_record()["id"]
        email = "private@private-company.invalid"
        pref = self.preference(ident, chosen=email)
        manifest = training.export(self.output("scrubbed-pairs"), preferences=[pref])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        for path in (self.root / "scrubbed-pairs").iterdir():
            self.assertNotIn(email, path.read_text())

    def test_preferences_cannot_self_attest_privacy_without_a_source(self):
        source = self.import_record()
        pref = self.preference(source["id"])
        (self.home / "training" / "records" / (source["id"] + ".json")).unlink()
        pref["privacy"] = {"scrubbed": True, "scrubber_version": "fake"}
        with self.assertRaises((ValueError, FileNotFoundError)):
            training.export(self.output("untrusted"), preferences=[pref])
        self.assertFalse((self.root / "untrusted" / "manifest.json").exists())

    def test_stop_flush_gap_stays_out_of_sft_even_after_review(self):
        training.set_project(str(self.project), True)
        result = training.capture_stop({"event": "Stop", "agent": "claude"},
            {"cwd": str(self.project), "transcript_path": str(self.transcript), "session_id": "s1",
             "last_assistant_message": "尚未写入日志的新回答"})
        self.assertFalse(result["quality"]["complete"])
        self.assertIn("stop_reply_not_flushed", result["quality"]["issues"])
        training.review(result["id"], "reviewer-1", "accepted")
        self.assertEqual(training.export(self.output("partial"))["files"]["sft.jsonl"]["records"], 0)

    def test_stop_recovers_a_real_delayed_flush_without_inventing_text(self):
        training.set_project(str(self.project), True)
        expected = "已完整写入最终回答"
        with mock.patch.object(training.time, "sleep", side_effect=lambda _: self.write_transcript("initial", reply=expected)) as sleep:
            result = training.capture_stop({"event": "Stop", "agent": "claude"},
                {"cwd": str(self.project), "transcript_path": str(self.transcript), "session_id": "late-flush",
                 "last_assistant_message": expected})
        sleep.assert_called_once_with(.1)
        self.assertTrue(result["quality"]["complete"])
        self.assertEqual(self.stored(result["id"])["messages"][-1]["content"][0]["text"], expected)

    def test_tool_context_and_review_survive_same_session_stop_replay(self):
        # This test concerns review replay, with a confirmed shareable file. Git
        # failure/ignored cases are independently checked above, without relying
        # on the developer machine's executable or global Git configuration.
        git = mock.patch.object(nous_privacy.subprocess, "run", return_value=subprocess.CompletedProcess(["git"], 1))
        git.start()
        self.addCleanup(git.stop)
        rows = [message("user", "读取代码", "u1"),
                message("assistant", [{"type": "tool_use", "id": "c1", "name": "Read", "input": {"file_path": "app.py"}}], "a1"),
                message("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "print(1)"}], "u2"),
                message("assistant", "读取完成", "a2")]
        path = self.write_transcript("tools", rows=rows)
        context = {"tools": [{"type": "function", "function": {"name": "Read", "parameters": {
            "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}}]}
        result = self.import_record(path, session="tools", context=context)
        training.review(result["id"], "r1", "accepted")
        training.set_project(str(self.project), True)
        training.collect(path, str(self.project), session="tools")
        stored = self.stored(result["id"])
        self.assertEqual(stored["tools"], context["tools"])
        self.assertEqual(len(stored["evidence"]), 1)
        self.assertEqual(training.export(self.output("tool-dataset"))["files"]["sft.jsonl"]["records"], 1)

    def test_cli_import_review_export_uses_only_isolated_home(self):
        env = dict(os.environ, NOUS_HOME=str(self.home), NOUS_LEDGER_HOME=str(self.home / "ledger"),
                   NOUS_MANAGED=str(self.home / "managed.json"), PYTHONDONTWRITEBYTECODE="1")
        def cli(*args):
            proc = subprocess.run([sys.executable, "-B", str(HOOKS / "nous_training.py"), *args],
                                  capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        record = cli("import", "--transcript", str(self.transcript), "--project", str(self.project))
        cli("review", "--id", record["id"], "--reviewer", "r1", "--result", "accepted")
        manifest = cli("export", "--out", self.output("cli-dataset"))
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 1)

    def test_concurrent_cli_imports_keep_every_record_and_authorization(self):
        env = dict(os.environ, NOUS_HOME=str(self.home), NOUS_LEDGER_HOME=str(self.home / "ledger"),
                   NOUS_MANAGED=str(self.home / "managed.json"), PYTHONDONTWRITEBYTECODE="1")
        def cli_import(path):
            proc = subprocess.run([sys.executable, "-B", str(HOOKS / "nous_training.py"), "import",
                                   "--transcript", str(path), "--project", str(self.project)],
                                  capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)["id"]
        # Initialize the actual isolated pseudonym key before concurrent workers.
        seed = cli_import(self.transcript)
        paths = [self.write_transcript("parallel-" + str(i), prompt="并发任务" + str(i)) for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            ids = list(workers.map(cli_import, paths))
        self.assertEqual(len(set(ids + [seed])), 9)
        records = list((self.home / "training" / "records").glob("*.json"))
        self.assertEqual(len(records), 9)
        for path in records:
            self.assertTrue(training._allowed_record(json.loads(path.read_text())))
        self.assertEqual(len(training._config()["projects"][training.project_id(self.project)]["imports"]), 9)

    def test_same_session_old_capture_cannot_overwrite_newer_messages(self):
        import threading
        entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        build = training.build_record
        calls = []

        def slow_first(*args, **kwargs):
            record = build(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            else:
                second_entered.set()
            return record

        with mock.patch.object(training, "build_record", side_effect=slow_first), ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(self.import_record, session="overlap")
            try:
                self.assertTrue(entered.wait(5))
                with self.transcript.open("a") as out:
                    out.write(json.dumps(message("user", "第二轮问题", "u2")) + "\n")
                    out.write(json.dumps(message("assistant", "第二轮回答", "a2")) + "\n")
                new = pool.submit(self.import_record, session="overlap")
                self.assertFalse(second_entered.wait(.1))
            finally:
                release.set()
            first, latest = old.result(timeout=5), new.result(timeout=5)
        self.assertEqual(first["id"], latest["id"])
        self.assertEqual(len(self.stored(first["id"])["messages"]), 4)

    def test_inventory_has_review_paths_but_no_message_bodies(self):
        ident = self.import_record()["id"]
        result = training.list_records(str(self.project))
        self.assertEqual(result["records"][0]["id"], ident)
        self.assertTrue(Path(result["records"][0]["path"]).is_file())
        self.assertNotIn("请回答这个问题", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
