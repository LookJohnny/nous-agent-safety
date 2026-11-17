"""Collector/exporter integration for ordered CodeMode data; synthetic only."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "hooks"))
import nous_ledger
import nous_training as training
import nous_training_export as exporter
from nous_training_validate import validate_dataset


class CodeModeIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nous-codemode-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home_patch = mock.patch.object(training, "HOME", self.root / "home")
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.key_patch = mock.patch.object(nous_ledger, "_key", return_value=b"synthetic-codemode-privacy-key-32!")
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.transcript = self.root / "fixture.jsonl"
        self.transcript.write_bytes((BASE / "examples/training/codemode.jsonl").read_bytes())

    def collect(self):
        result = training.collect(self.transcript, str(self.project), agent="codex", session="fixture", manual=True)
        self.path = training._root() / "records" / (result["id"] + ".json")
        return json.loads(self.path.read_text())

    def attest(self, record, *, content=True, outcome=True):
        result = copy.deepcopy(record)
        for kind in (["verifier"] if outcome else []) + (["content_verifier"] if content else []):
            result["evidence"].append({"kind": kind, "result": "passed", "verifier": "handwritten-fixture-check",
                                       "record_digest": exporter.record_digest(result)})
        return result

    def export(self, records, name="export", profile="codemode-events-v1"):
        out = self.root / name
        manifest = exporter.export_dataset(records, str(out), profile=profile)
        self.assertTrue(validate_dataset(out)["valid"], validate_dataset(out))
        return out, manifest

    def test_collect_and_export_multiple_assistant_targets_with_bound_context(self):
        record = self.collect()
        self.assertEqual(record["schema"], "nous.trajectory/2")
        self.assertEqual(record["quality"], {"complete": True, "issues": [], "missing": []})
        self.assertTrue(record["privacy"]["custom_content_review_required"])
        self.assertTrue(record["provenance"]["session_group_id"])
        self.assertTrue(record["provenance"]["source_transcript_id"])
        out, manifest = self.export([self.attest(record)])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 2)
        samples = [json.loads(line) for line in (out / "sft.jsonl").read_text().splitlines()]
        self.assertIn('"type":"custom_tool_call"', samples[0]["completion"])
        self.assertIn('"type":"custom_tool_call_output"', samples[1]["prompt"])
        self.assertNotIn('"type":"custom_tool_call_output"', samples[0]["completion"])
        self.assertNotIn('"type":"McpToolCall"', samples[1]["prompt"])
        targets = [entry["target"] for entry in manifest["lineage"] if entry["format"] == "sft"]
        self.assertEqual(len(targets), 2)
        self.assertNotEqual(targets[0], targets[1])

    def test_task_outcome_and_content_clearance_are_independent(self):
        record = self.collect()
        for index, (content, outcome, reason) in enumerate([
            (False, True, "custom_content_review_missing"),
            (True, False, "verified_outcome_missing"),
        ]):
            _, manifest = self.export([self.attest(record, content=content, outcome=outcome)], name="missing-" + str(index))
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
            self.assertIn("sft:" + reason, manifest["rejections"])

    def test_native_context_mutation_cannot_reuse_old_review(self):
        original = self.attest(self.collect())
        changed = copy.deepcopy(original)
        changed["protocol"]["events"][0]["payload"]["text"] += " Changed source instructions."
        self.assertEqual(changed["messages"], original["messages"])
        self.assertNotEqual(exporter.record_digest(changed), exporter.record_digest(original))
        _, manifest = self.export([changed])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_reimport_changed_protocol_clears_evidence_without_backfilling_tools(self):
        original = self.attest(self.collect())
        original["tools"] = [{"type": "function", "function": {"name": "stale", "parameters": {"type": "object"}}}]
        training._atomic(self.path, original)
        rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        rows[0]["payload"]["base_instructions"]["text"] += " New context."
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        changed = self.collect()
        self.assertEqual(changed["evidence"], [])
        self.assertNotIn("tools", changed)

    def test_legacy_profile_cannot_reinterpret_native_custom_events(self):
        record = self.attest(self.collect())
        _, manifest = self.export([record], profile="trl")
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("sft:protocol_requires_codemode_profile", manifest["rejections"])

    def test_old_schema_rejects_unbound_protocol_extension(self):
        record = self.attest(self.collect())
        record["schema"] = "nous.trajectory/1"
        with self.assertRaisesRegex(training.TrainingError, "unbound_protocol_extension"):
            training.scrub_record(record)
        _, manifest = self.export([record], profile="trl")
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 0)
        self.assertIn("canonical:unbound_protocol_extension", manifest["rejections"])

    def test_validator_recomputes_explicit_target_boundaries(self):
        out, manifest = self.export([self.attest(self.collect())])
        entry = next(entry for entry in manifest["lineage"] if entry["format"] == "sft")
        entry["target"]["start"] += 1
        (out / "manifest.json").write_text(json.dumps(manifest))
        self.assertFalse(validate_dataset(out)["valid"])

    def test_negative_label_blocks_equal_training_rows_from_another_snapshot(self):
        accepted = self.attest(self.collect())
        rejected = copy.deepcopy(accepted)
        rejected["id"] = "another-snapshot"
        rejected["messages"][0]["ts"] = "2026-11-16T01:00:00Z"
        rejected["evidence"] = [{"kind": "human_review", "result": "rejected", "reviewer": "fixture-reviewer",
                                 "record_digest": exporter.record_digest(rejected)}]
        self.assertNotEqual(exporter.record_digest(accepted), exporter.record_digest(rejected))
        _, manifest = self.export([accepted, rejected])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("sft:cross_record_evidence_conflict", manifest["rejections"])

    def test_review_content_is_persistent_and_rejected_content_blocks_training(self):
        record = self.collect()
        training.review(record["id"], "fixture-reviewer", "accepted")
        training.review(record["id"], "fixture-reviewer", "cleared", purpose="content")
        m = training.export(str(self.root / "allowed"), profile="codemode-events-v1")
        self.assertEqual(m["files"]["sft.jsonl"]["records"], 2)
        training.review(record["id"], "fixture-reviewer", "rejected", purpose="content")
        m = training.export(str(self.root / "blocked"), profile="codemode-events-v1")
        self.assertEqual(m["files"]["sft.jsonl"]["records"], 0)

    def test_incomplete_negative_snapshot_still_blocks_observed_matching_prefix(self):
        accepted = self.attest(self.collect())
        rejected = copy.deepcopy(accepted)
        rejected["id"] = "incomplete-rejected"
        rejected["protocol"]["events"][-1]["payload"]["phase"] = "commentary"
        for quality in [rejected["quality"], rejected["protocol"]["quality"]]:
            quality.update(complete=False, issues=["incomplete_final"])
        rejected["evidence"] = [{"kind": "human_review", "result": "rejected", "reviewer": "fixture-reviewer",
                                 "record_digest": exporter.record_digest(rejected)}]
        out, manifest = self.export([accepted, rejected])
        # The first target is identical. A different, later final answer remains
        # a distinct observation; only the matching rejected target is blocked.
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 1)
        sample = json.loads((out / "sft.jsonl").read_text())
        self.assertNotIn('"type":"custom_tool_call"', sample["completion"])

    def test_legacy_tools_are_not_backfilled_after_message_content_changes(self):
        self.transcript.write_bytes((BASE / "examples/training/claude.jsonl").read_bytes())
        context = json.loads((BASE / "examples/training/tools.json").read_text())
        first = training.collect(self.transcript, str(self.project), agent="claude", session="legacy", context=context, manual=True)
        path = training._root() / "records" / (first["id"] + ".json")
        self.assertTrue(json.loads(path.read_text())["tools"])
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "assistant", "uuid": "different-final",
                                  "message": {"role": "assistant", "content": "A changed source snapshot."}}) + "\n")
        training.collect(self.transcript, str(self.project), agent="claude", session="legacy", manual=True)
        changed = json.loads(path.read_text())
        self.assertNotIn("tools", changed)
        self.assertIn("tool_definitions", changed["quality"]["missing"])

    def test_private_filename_omission_clears_both_protocol_and_legacy_view(self):
        rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        for row in rows:
            payload = row.get("payload", {})
            if payload.get("type") == "custom_tool_call":
                payload["input"] = 'text(await tools.read_file({path:".env"}));'
            elif payload.get("type") == "custom_tool_call_output":
                payload["output"] = "SYNTHETIC_PRIVATE_FILE_BODY"
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        record = self.collect()
        self.assertFalse(record["quality"]["complete"])
        self.assertNotIn("SYNTHETIC_PRIVATE_FILE_BODY", json.dumps(record))

    def test_native_encrypted_schema_annotation_is_preserved_but_not_trainable(self):
        rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        def annotate(tools):
            for tool in tools:
                if tool["type"] == "namespace":
                    annotate(tool["tools"])
                elif tool["type"] == "function":
                    tool["parameters"]["properties"]["key"]["encrypted"] = True
        annotate(rows[1]["payload"]["tools"])
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        record = self.collect()
        self.assertIn('"encrypted": true', json.dumps(record["protocol"]))
        _, manifest = self.export([self.attest(record)])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_hook_and_default_import_share_group_across_changed_snapshots(self):
        rows = [{"type": "user", "message": {"role": "user", "content": "First task"}},
                {"type": "assistant", "message": {"role": "assistant", "content": "First answer"}}]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        training.set_project(str(self.project), True)
        first = training.collect(self.transcript, str(self.project), agent="claude", session="vendor-id")
        original = json.loads((training._root() / "records" / (first["id"] + ".json")).read_text())
        rows.extend([{"type": "user", "message": {"role": "user", "content": "Followup task"}},
                     {"type": "assistant", "message": {"role": "assistant", "content": "Followup answer"}}])
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        second = training.collect(self.transcript, str(self.project), agent="claude", manual=True)
        changed = json.loads((training._root() / "records" / (second["id"] + ".json")).read_text())
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(original["provenance"]["session_group_id"], changed["provenance"]["session_group_id"])
        self.assertEqual(original["provenance"]["source_transcript_id"], changed["provenance"]["source_transcript_id"])
        from nous_training_split import split_dataset
        for record in (original, changed):
            training.review(record["id"], "fixture-reviewer", "accepted")
        training.export(str(self.root / "source"))
        split = split_dataset(self.root / "source", self.root / "split", group_by="session", eval_ratio=.5, seed="0")
        # A whole component must be on one side; no split may contain one snapshot each.
        train = (self.root / "split/train/sft.jsonl").read_text().splitlines()
        evaluation = (self.root / "split/eval/sft.jsonl").read_text().splitlines()
        self.assertEqual(sorted([len(train), len(evaluation)]), [0, 2])

    def test_native_session_identity_links_transcript_copies(self):
        rows = [{"type": role, "sessionId": "native-fixture-session", "message": {"role": role, "content": role + " text"}}
                for role in ("user", "assistant")]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        copy_path = self.root / "copied.jsonl"
        copy_path.write_bytes(self.transcript.read_bytes())
        records = [training.build_record(path, str(self.project), agent="claude") for path in (self.transcript, copy_path)]
        self.assertEqual(records[0]["provenance"]["session_group_id"], records[1]["provenance"]["session_group_id"])
        self.assertNotEqual(records[0]["provenance"]["source_transcript_id"], records[1]["provenance"]["source_transcript_id"])


if __name__ == "__main__":
    unittest.main()
