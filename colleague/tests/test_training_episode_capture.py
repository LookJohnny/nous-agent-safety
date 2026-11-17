"""Capture, privacy, authorization, and selective labels from observed bytes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from tests.test_training_episode import record as fixture
from tests.test_training_observer import encoded, oracle
import nous_ledger
import nous_training as training
import nous_training_episode_capture as capture
from nous_training_episode import episode_digest, validate_episode, samples
from nous_training_validate import validate_dataset

class Capture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nous-episode-capture-test-")
        self.addCleanup(tmp.cleanup); self.root = Path(tmp.name)
        self.project = self.root / "project"; self.project.mkdir(); self.home = self.root / "home"
        for patch in (mock.patch.object(training, "HOME", self.home), mock.patch.object(nous_ledger, "_key", return_value=b"synthetic-training-key-32-bytes!!")):
            patch.start(); self.addCleanup(patch.stop)
    def begin(self, provider="openai-responses", **options):
        return capture.begin_episode(provider, project=str(self.project), session="synthetic-session", oracle=oracle(), synthetic=True, **options)
    def step(self, handle, exchange):
        ticket = capture.begin_step(handle, encoded(exchange["request"]))
        return capture.finish_step(handle, ticket, encoded(exchange["response"]))
    def read(self, report):
        return json.loads((self.home / "training/records" / (report["id"] + ".json")).read_text())
    def complete(self, provider="openai-responses", **options):
        handle = self.begin(provider, manual=True)
        for step in fixture(provider, **options)["episode"]["steps"]:
            self.assertEqual(self.step(handle, step["exchange"])["status"], "captured")
        report = capture.finish_episode(handle)
        return report, self.read(report)
    def test_default_off_no_parse_or_write(self):
        self.assertIsNone(self.begin()); self.assertIsNone(capture.begin_step(None, b"invalid"))
        self.assertEqual(capture.finish_episode(None), {"status": "disabled"}); self.assertFalse(self.home.exists())
    def test_three_steps_both_providers_parallel_errors_oracle(self):
        for provider in ("openai-responses", "anthropic-messages"):
            report, value = self.complete(provider, parallel=True, error=True)
            self.assertEqual(report["steps"], 3); self.assertEqual(report["verification"]["status"], "matched")
            validate_episode(value); self.assertEqual([t for _, t in samples(value)], [{"step": 2}]); self.assertEqual(value["step_evidence"], [])
    def test_content_and_step_reviews_then_export(self):
        report, value = self.complete()
        training.export(self.root / "none", profile="request-episode-sft-v1")
        self.assertEqual((self.root / "none/sft.jsonl").read_text(), "")
        training.review(report["id"], "synthetic-reviewer", "cleared", purpose="content")
        training.review_step(report["id"], 0, "synthetic-reviewer", "accepted")
        training.review_step(report["id"], 1, "synthetic-reviewer", "rejected")
        training.export(self.root / "accepted", profile="request-episode-sft-v1")
        self.assertTrue(validate_dataset(self.root / "accepted")["valid"])
        self.assertEqual(len((self.root / "accepted/sft.jsonl").read_text().splitlines()), 2)
        value = self.read(report); self.assertEqual(value["evidence"][0]["record_digest"], episode_digest(value)); self.assertEqual(len(value["step_evidence"]), 2)
    def test_revoke_and_regrant_cannot_commit(self):
        training.set_project(str(self.project), True); handle = self.begin()
        self.step(handle, fixture()["episode"]["steps"][0]["exchange"])
        training.set_project(str(self.project), False); training.set_project(str(self.project), True)
        self.assertEqual(capture.finish_episode(handle), {"status": "disabled"}); self.assertFalse((self.home / "training/records").exists())
    def test_global_revoke_cancels_manual_inflight(self):
        handle = self.begin(manual=True); ex = fixture()["episode"]["steps"][0]["exchange"]
        ticket = capture.begin_step(handle, encoded(ex["request"])); training.revoke_all()
        self.assertEqual(capture.finish_step(handle, ticket, encoded(ex["response"])), {"status": "disabled"})
    def test_one_pending_one_use_ticket(self):
        handle = self.begin(manual=True); ex = fixture()["episode"]["steps"][0]["exchange"]
        ticket = capture.begin_step(handle, encoded(ex["request"]))
        with self.assertRaisesRegex(ValueError, "episode_step_in_flight"): capture.begin_step(handle, encoded(ex["request"]))
        with self.assertRaisesRegex(ValueError, "episode_step_in_flight"): capture.finish_episode(handle)
        capture.finish_step(handle, ticket, encoded(ex["response"]))
        with self.assertRaisesRegex(ValueError, "episode_ticket_invalid_or_consumed"): capture.finish_step(handle, ticket, encoded(ex["response"]))
    def test_ticket_cannot_move_handles(self):
        first, second = self.begin(manual=True), self.begin(manual=True); ex = fixture()["episode"]["steps"][0]["exchange"]
        ticket = capture.begin_step(first, encoded(ex["request"]))
        with self.assertRaisesRegex(ValueError, "episode_ticket_invalid_or_consumed"): capture.finish_step(second, ticket, encoded(ex["response"]))
    def test_response_failure_closes_without_partial_write(self):
        handle = self.begin(manual=True); ex = fixture()["episode"]["steps"][0]["exchange"]
        ticket = capture.begin_step(handle, encoded(ex["request"]))
        with self.assertRaises(ValueError): capture.finish_step(handle, ticket, b"invalid")
        with self.assertRaisesRegex(ValueError, "episode_handle_consumed"): capture.finish_episode(handle)
        self.assertFalse((self.home / "training/records").exists())
    def test_missing_middle_is_not_reconstructed(self):
        handle = self.begin(manual=True); steps = fixture()["episode"]["steps"]
        self.step(handle, steps[0]["exchange"])
        with self.assertRaises(ValueError): self.step(handle, steps[2]["exchange"])
        self.assertFalse((self.home / "training/records").exists())
    def test_unfinished_tool_response_is_candidate_only(self):
        handle = self.begin(manual=True); self.step(handle, fixture()["episode"]["steps"][0]["exchange"])
        report = capture.finish_episode(handle)
        self.assertFalse(report["quality"]["complete"]); self.assertNotEqual(report["verification"]["status"], "matched"); self.assertEqual(self.read(report)["evidence"], [])
    def test_oracle_and_raw_bytes_never_persisted(self):
        report, value = self.complete(); raw = json.dumps(value)
        for text in ('"expected"', '"oracle_template"', '"request_bytes"'): self.assertNotIn(text, raw)
    def test_project_replaced_or_home_changed(self):
        handle = self.begin(manual=True); self.step(handle, fixture()["episode"]["steps"][0]["exchange"])
        self.project.rename(self.root / "previous"); self.project.mkdir()
        with self.assertRaisesRegex(training.TrainingError, "project_changed"): capture.finish_episode(handle)
        handle = self.begin(manual=True)
        with mock.patch.object(training, "HOME", self.root / "other"):
            self.assertIsNone(capture.begin_step(handle, b"invalid"))
    def test_opaque_execution_rejected_before_write(self):
        handle = self.begin(manual=True); ex = fixture()["episode"]["steps"][0]["exchange"]
        ex["request"]["tools"][0]["name"] = "execute"; ex["response"]["output"][0]["name"] = "execute"
        with self.assertRaisesRegex(ValueError, "request_opaque_tool_execution"): self.step(handle, ex)
        self.assertFalse((self.home / "training/records").exists())
    def test_short_structured_secrets_scrubbed_and_ineligible(self):
        value = fixture("anthropic-messages")
        for step in value["episode"]["steps"]:
            ex = step["exchange"]; ex["request"]["tools"][0]["input_schema"]["properties"]["password"] = {"type": "string"}
            contents = [m["content"] for m in ex["request"]["messages"]] + [ex["response"]["content"]]
            for content in contents:
                if isinstance(content, list):
                    for block in content:
                        if block["type"] == "tool_use": block["input"]["password"] = "tiny-secret"
                        if block["type"] == "tool_result": block["content"] = '{"password":"tiny-result"}'
        handle = self.begin("anthropic-messages", manual=True)
        for step in value["episode"]["steps"]: self.step(handle, step["exchange"])
        report = capture.finish_episode(handle); raw = json.dumps(self.read(report))
        self.assertNotIn("tiny-secret", raw); self.assertNotIn("tiny-result", raw)
        self.assertFalse(report["quality"]["complete"]); self.assertNotEqual(report["verification"]["status"], "matched")

    def test_revoke_between_final_scrub_and_commit(self):
        handle = self.begin(manual=True)
        for step in fixture()["episode"]["steps"]: self.step(handle, step["exchange"])
        original = training.scrub_record
        def revoke_after_scrub(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if value.get("schema") == "nous.trajectory/4": training.revoke_all()
            return result
        with mock.patch.object(training, "scrub_record", side_effect=revoke_after_scrub):
            self.assertEqual(capture.finish_episode(handle), {"status": "disabled"})
        self.assertFalse((self.home / "training/records").exists())

    def test_oracle_template_frozen_before_any_dispatch(self):
        spec = oracle()
        handle = capture.begin_episode("openai-responses", project=str(self.project), session="frozen",
                                       oracle=spec, manual=True, synthetic=True)
        spec["assertions"][0]["expected"]["answer"] = 999
        for step in fixture()["episode"]["steps"]: self.step(handle, step["exchange"])
        report = capture.finish_episode(handle)
        self.assertEqual(report["verification"]["status"], "matched")
        with self.assertRaisesRegex(ValueError, "episode_handle_consumed"): capture.finish_episode(handle)

    def test_rescrub_never_resigns_modified_content(self):
        report, value = self.complete()
        value["episode"]["steps"][0]["exchange"]["request"]["input"][0]["content"] = "changed task"
        with self.assertRaisesRegex(ValueError, "episode_content_digest_mismatch"):
            training.scrub_record(value)


if __name__ == "__main__": unittest.main()
