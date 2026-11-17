"""Only synthetic temporary projects and in-memory native exchanges."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_training as training
import nous_training_request as request
import nous_training_request_privacy as privacy
from nous_training_episode import episode_digest
from tests.test_training_request import tool_exchange, closed_tool_exchange
from tests.test_training_episode import record as episode_record, refresh


def with_arguments(arguments):
    value = tool_exchange()
    value["request"]["tools"][0]["parameters"] = {"type": "object"}
    value["response"]["output"][0]["arguments"] = json.dumps(arguments)
    request.validate_exchange(value)
    return value


class RequestPrivacy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nous-request-privacy-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        patch = mock.patch("nous_ledger._key", return_value=b"synthetic-test-privacy-key-32byte!")
        patch.start(); self.addCleanup(patch.stop)

    def scrub(self, value, *, project=None):
        return privacy.scrub_tools(value, clean=lambda x, _: copy.deepcopy(x),
                                   project=str(self.project) if project is None else project)

    def native_scrub(self, value):
        return training.scrub_record({"schema": "nous.request-step/1", "exchange": value,
            "quality": {"complete": True, "issues": [], "missing": []}}, str(self.project))

    def git(self):
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True)

    def rejected(self, value, code, *, project=None):
        with self.assertRaisesRegex(privacy.RequestPrivacyError, "^" + code + "$"):
            self.scrub(value, project=project)

    def test_unchecked_episode_is_not_promoted_without_project_checks(self):
        value = episode_record()
        value["privacy"]["tool_history_checked"] = False
        for step in value["episode"]["steps"]:
            step["privacy"]["tool_history_checked"] = False
        before = copy.deepcopy(value)
        with self.assertRaisesRegex(training.TrainingError, "^request_file_exclusion_unverified$"):
            training.scrub_record(value)
        self.assertEqual(value, before)

    def test_checked_episode_rescrub_keeps_digest_and_existing_evidence(self):
        value = episode_record()
        bound = episode_digest(value)
        value["evidence"] = [{"kind": "human_review", "result": "accepted", "reviewer": "synthetic",
                               "record_digest": bound}]
        safe = training.scrub_record(value)
        self.assertEqual(episode_digest(safe), bound)
        self.assertEqual(safe["evidence"][0]["record_digest"], bound)
        self.assertEqual(episode_digest(training.scrub_record(safe)), bound)

    def test_opaque_namespace_cannot_hide_behind_safe_leaf_name(self):
        value = tool_exchange(); tool = value["request"]["tools"][0]
        value["request"]["tools"] = [{"type": "namespace", "name": "shell", "tools": [tool]}]
        value["response"]["output"][0]["namespace"] = "shell"
        request.validate_exchange(value)
        self.rejected(value, "request_opaque_tool_execution")

    def test_opaque_nested_command_is_rejected_without_execution(self):
        self.rejected(with_arguments({"settings": [{"command": "synthetic-do-not-run"}]}), "request_opaque_tool_execution")

    def test_anthropic_absent_result_content_is_preserved_as_absent(self):
        value = closed_tool_exchange("anthropic-messages")
        value["request"]["messages"][-1]["content"][0].pop("content")
        safe = self.native_scrub(value)
        self.assertNotIn("content", safe["exchange"]["request"]["messages"][-1]["content"][0])
        request.validate_exchange(safe["exchange"])

    def test_nested_path_arrays_require_verified_project_git_policy(self):
        value = with_arguments({"settings": [{"paths": ["first.txt", "second.txt"]}]})
        for name in ("first.txt", "second.txt"):
            (self.project / name).write_text("synthetic only")
        with mock.patch.object(privacy.subprocess, "run", return_value=mock.Mock(returncode=1)) as check:
            self.scrub(value)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.kwargs["timeout"], 3)
        self.assertEqual(set(check.call_args.kwargs["input"].split(b"\0")[:-1]),
                         {str(path).encode() for name in ("first.txt", "second.txt")
                          for path in (self.project / name, (self.project / name).resolve())})
        self.rejected(value, "request_file_exclusion_unverified", project="")

    def test_gitignored_file_rejects_whole_exchange(self):
        self.git()
        (self.project / ".gitignore").write_text("ignored.txt\n")
        (self.project / "ignored.txt").write_text("synthetic only")
        self.rejected(with_arguments({"path": "ignored.txt"}), "request_excluded_tool_content")

    def test_unavailable_git_is_not_a_verified_file_check(self):
        (self.project / "public.txt").write_text("synthetic only")
        with mock.patch.object(privacy.nous_privacy.subprocess, "run", side_effect=FileNotFoundError("PRIVATE_STDERR")):
            self.rejected(with_arguments({"path": "public.txt"}), "request_file_exclusion_unverified")

    def test_symlink_escape_is_rejected_before_git_or_body_inspection(self):
        outside = self.root / "outside.txt"; outside.write_text("synthetic only")
        (self.project / "alias.txt").symlink_to(outside)
        with mock.patch.object(privacy.os, "open", side_effect=AssertionError("must not read outside")):
            self.rejected(with_arguments({"path": "alias.txt"}), "request_external_file_tool")

    def test_fifo_is_rejected_without_attempting_a_blocking_open(self):
        os.mkfifo(self.project / "pipe")
        with mock.patch.object(privacy.os, "open", side_effect=AssertionError("must not open FIFO")):
            self.rejected(with_arguments({"path": "pipe"}), "request_excluded_tool_content")

    def test_explicit_null_namespace_preserves_absent_namespace_semantics(self):
        value = tool_exchange()
        value["response"]["output"][0]["namespace"] = None
        request.validate_exchange(value)
        self.assertIsNone(self.scrub(value)["response"]["output"][0]["namespace"])

    def test_symlink_to_ignored_internal_target_cannot_bypass_gitignore(self):
        self.git()
        (self.project / ".gitignore").write_text("ignored.txt\n")
        target = self.project / "ignored.txt"; target.write_text("synthetic only")
        (self.project / "alias.txt").symlink_to(target)
        self.rejected(with_arguments({"path": "alias.txt"}), "request_excluded_tool_content")

    def test_short_structured_json_secrets_are_masked_in_string_and_text_array_results(self):
        for provider in request.PROVIDERS:
            for as_array in (False, True):
                value = closed_tool_exchange(provider)
                body = '{"password":"tiny-secret","nested":{"api_key":"small-key"}}'
                if provider == "openai-responses":
                    value["request"]["input"][-1]["output"] = [{"type": "input_text", "text": body}] if as_array else body
                else:
                    value["request"]["messages"][-1]["content"][0]["content"] = [{"type": "text", "text": body}] if as_array else body
                safe = self.native_scrub(value)
                serialized = json.dumps(safe)
                self.assertNotIn("tiny-secret", serialized)
                self.assertNotIn("small-key", serialized)
                self.assertFalse(safe["quality"]["complete"])
                self.assertIn("request_tool_content_redacted", safe["quality"]["issues"])
                self.assertEqual(safe["messages"], request.request_messages(safe["exchange"]))

    def test_short_structured_argument_secrets_clear_pending_and_historical_copies(self):
        value = with_arguments({"password": "tiny-secret"})
        safe = self.native_scrub(value)
        self.assertNotIn("tiny-secret", json.dumps(safe))
        self.assertFalse(safe["quality"]["complete"])
        self.assertEqual(safe["messages"], request.request_messages(safe["exchange"]))


if __name__ == "__main__":
    unittest.main()
