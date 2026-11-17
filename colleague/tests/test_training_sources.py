"""Preference source binding and complete lineage reconstruction, synthetic only."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_training_export as export
from nous_training_validate import validate_dataset
from tests.test_training_export import record, review, text, tool_record, preference
from tests.test_codemode_training import record as codemode_record


def bound_pair(source, *, label="pair", schema=None):
    pair = {"id": label, "prompt": copy.deepcopy(source["messages"][:-1]),
            "chosen": [text("assistant", "Synthetic improved response")],
            "rejected": [text("assistant", "Synthetic rejected response")],
            "tools": copy.deepcopy(source.get("tools", [])),
            "provenance": {"source_record_id": source["id"], "project_id": source["provenance"]["project_id"]},
            "privacy": {"scrubbed": True, "scrubber_version": "synthetic"}, "evidence": []}
    if schema is not None:
        pair["schema"] = schema
    sign(pair)
    return pair


def sign(pair):
    pair["evidence"] = [{"kind": "human_preference", "result": "chosen", "reviewer": "synthetic",
                         "record_digest": export.preference_digest(pair)}]


def approved_codemode(label="code-a"):
    item = codemode_record()
    item["id"] = label
    item["messages"] = [text("user", "Synthetic code task"), text("assistant", "Synthetic complete answer")]
    item["provenance"] = {"project_id": label, "session_id": label}
    digest = export.record_digest(item)
    item["evidence"] = [{"kind": kind, "result": "passed", "verifier": "synthetic", "record_digest": digest}
                        for kind in ("verifier", "content_verifier")]
    return item


class TrainingSources(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nous-source-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset"

    def make(self, records=(), pairs=(), profile="trl"):
        manifest = export.export_dataset(records, self.dataset, preferences=pairs, profile=profile)
        self.assertTrue(validate_dataset(self.dataset)["valid"], validate_dataset(self.dataset))
        return manifest

    def manifest(self):
        return json.loads((self.dataset / "manifest.json").read_text())

    def save_manifest(self, value):
        (self.dataset / "manifest.json").write_text(json.dumps(value))

    def replace(self, name, values):
        raw = "".join(json.dumps(value) + "\n" for value in values).encode()
        (self.dataset / name).write_bytes(raw)
        manifest = self.manifest()
        manifest["files"][name] = {"records": len(values), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.save_manifest(manifest)

    def codes(self):
        return {e["code"] for e in validate_dataset(self.dataset)["errors"]}

    def test_new_implicit_schema_binds_source_and_project(self):
        source = record()
        pair = bound_pair(source)
        original = export.preference_digest(pair)
        for field in ("source_record_id", "project_id"):
            changed = copy.deepcopy(pair)
            changed["provenance"][field] += "-different"
            self.assertNotEqual(export.preference_digest(changed), original)
        manifest = self.make([source], [pair])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 1)
        saved = json.loads((self.dataset / "preferences.jsonl").read_text())
        self.assertEqual(saved["schema"], "nous.preference/2")
        self.assertEqual(export.preference_digest(saved), original)

    def test_explicit_v1_historical_digest_and_valid_source_remain_compatible(self):
        source = record()
        pair = bound_pair(source, schema="nous.preference/1")
        legacy = export._digest({key: pair.get(key, [] if key == "tools" else None)
                                 for key in ("prompt", "chosen", "rejected", "tools")})
        self.assertEqual(export.preference_digest(pair), legacy)
        self.assertEqual(self.make([source], [pair])["files"]["dpo.jsonl"]["records"], 1)
        self.assertEqual(json.loads((self.dataset / "preferences.jsonl").read_text())["schema"], "nous.preference/1")

    def test_standalone_preferences_remain_low_level_api_compatible(self):
        first, second = preference(), preference()
        second.update(id="legacy", schema="nous.preference/1")
        sign(second)
        self.assertEqual(self.make(pairs=[first, second])["files"]["dpo.jsonl"]["records"], 1)

    def test_wrong_source_context_is_not_accepted_even_with_fresh_evidence(self):
        a, b = record("a"), record("b")
        b["messages"][0] = text("user", "A different synthetic task")
        review(b)
        pair = bound_pair(b)
        pair["provenance"]["source_record_id"] = a["id"]
        sign(pair)
        manifest = self.make([a, b], [pair])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        self.assertIn("dpo:preference_source_context_mismatch", manifest["rejections"])

    def test_legacy_v1_source_still_gets_semantic_validation(self):
        source = record()
        pair = bound_pair(source, schema="nous.preference/1")
        pair["prompt"] = [text("user", "Unrelated legacy task")]
        sign(pair)
        self.assertEqual(self.make([source], [pair])["files"]["dpo.jsonl"]["records"], 0)

    def test_changing_to_matching_source_still_invalidates_v2_annotation(self):
        first, second = record("a"), record("b")
        pair = bound_pair(first)
        pair["provenance"]["source_record_id"] = second["id"]
        manifest = self.make([first, second], [pair])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        self.assertIn("dpo:explicit_preference_missing", manifest["rejections"])

    def test_full_prompt_prefix_is_required_not_just_last_question(self):
        source = record()
        source["messages"].insert(0, text("system", "Synthetic instruction A"))
        review(source)
        pair = bound_pair(source)
        pair["prompt"][0] = text("system", "Synthetic instruction B")
        sign(pair)
        self.assertEqual(self.make([source], [pair])["files"]["dpo.jsonl"]["records"], 0)

    def test_source_message_ids_and_timestamps_are_not_prompt_content(self):
        source = record()
        source["messages"][0].update(id="source-id", ts="2026-11-16")
        review(source)
        pair = bound_pair(source)
        pair["prompt"][0].update(id="annotation-copy", ts="2026-11-17")
        sign(pair)
        self.assertEqual(self.make([source], [pair])["files"]["dpo.jsonl"]["records"], 1)

    def test_tool_contract_must_match_even_without_a_call_in_prompt(self):
        source = record()
        pair = bound_pair(source)
        pair["tools"] = tool_record()["tools"]
        sign(pair)
        manifest = self.make([source], [pair])
        self.assertIn("dpo:preference_source_context_mismatch", manifest["rejections"])

    def test_source_context_does_not_equate_boolean_and_numeric_arguments(self):
        source = tool_record()
        source["tools"][0]["function"]["parameters"]["properties"]["path"] = {"anyOf": [{"type": "boolean"}, {"type": "integer"}]}
        for index, block in enumerate(source["messages"][1]["content"]):
            block["input"]["path"] = index == 0
        review(source)
        pair = bound_pair(source)
        pair["prompt"][1]["content"][0]["input"]["path"] = 1
        sign(pair)
        self.assertFalse(export.preference_source_matches(pair, source))
        manifest = self.make([source], [pair])
        self.assertIn("dpo:preference_source_context_mismatch", manifest["rejections"])

    def test_project_identity_must_match_source(self):
        source = record()
        pair = bound_pair(source)
        pair["provenance"]["project_id"] = "another-project"
        sign(pair)
        self.assertEqual(self.make([source], [pair])["files"]["dpo.jsonl"]["records"], 0)

    def test_v2_ordered_source_cannot_be_flattened_for_dpo(self):
        source = approved_codemode()
        pair = bound_pair(source)
        manifest = self.make([source], [pair])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        self.assertIn("dpo:preference_ordered_source_not_supported", manifest["rejections"])

    def test_clipped_sft_dedup_origin_is_detected(self):
        first, second = record("a"), record("b")
        manifest = self.make([first, second])
        manifest["lineage"] = [entry for entry in manifest["lineage"] if not (entry["format"] == "sft" and entry["id"] == "b")]
        self.save_manifest(manifest)
        self.assertIn("training_origin_lineage_missing", self.codes())

    def test_clipped_dpo_dedup_origin_is_detected(self):
        first, second = record("a"), record("b")
        manifest = self.make([first, second], [bound_pair(first, label="pa"), bound_pair(second, label="pb")])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 1)
        manifest["lineage"] = [entry for entry in manifest["lineage"] if not (entry["format"] == "dpo" and entry["id"] == "pb")]
        self.save_manifest(manifest)
        self.assertIn("training_origin_lineage_missing", self.codes())

    def test_each_codemode_origin_and_target_must_be_present(self):
        first, second = approved_codemode(), approved_codemode("code-b")
        manifest = self.make([first, second], profile="codemode-events-v1")
        entries = [entry for entry in manifest["lineage"] if entry["format"] == "sft"]
        self.assertEqual(len(entries), 4)
        selected = next(entry for entry in entries if entry["id"] == "code-b")
        manifest["lineage"].remove(selected)
        self.save_manifest(manifest)
        self.assertIn("training_origin_lineage_missing", self.codes())

    def test_missing_eligible_training_row_is_detected_even_after_rehash(self):
        self.make([record()])
        self.replace("sft.jsonl", [])
        manifest = self.manifest()
        manifest["lineage"] = [entry for entry in manifest["lineage"] if entry["format"] != "sft"]
        self.save_manifest(manifest)
        self.assertIn("eligible_training_sample_missing", self.codes())

    def test_nontrainable_and_conflicting_canonical_sources_do_not_require_lineage(self):
        a, b, partial = record("a"), record("b"), record("partial", reviewed=False)
        b["evidence"][0]["result"] = "rejected"
        manifest = self.make([a, b, partial])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_rehashed_wrong_source_sidecar_is_rejected_offline(self):
        a, b = record("a"), record("b")
        b["messages"][0] = text("user", "Unrelated task B")
        review(b)
        self.make([a, b], [bound_pair(a)])
        pair = json.loads((self.dataset / "preferences.jsonl").read_text())
        pair["provenance"]["source_record_id"] = "b"
        sign(pair)  # Even a fresh annotation cannot prove this false source claim.
        self.replace("preferences.jsonl", [pair])
        manifest = self.manifest()
        for entry in manifest["lineage"]:
            if entry["format"] in ("preference", "dpo"):
                entry.update(record_digest=export.preference_digest(pair),
                             provenance=export._lineage(pair, "", 1, "preference", [])["provenance"])
        self.save_manifest(manifest)
        self.assertIn("preference_source_context_mismatch", self.codes())

    def test_new_source_transcript_alias_survives_lineage(self):
        source = record()
        source["provenance"]["source_transcript_id"] = "same-source-file"
        manifest = self.make([source])
        self.assertTrue(all(entry["provenance"].get("source_transcript_id") == "same-source-file" for entry in manifest["lineage"]))

    def test_v2_source_group_identity_is_bound_to_review(self):
        source = approved_codemode()
        original = export.record_digest(source)
        for field in ("project_id", "session_id", "session_group_id", "source_transcript_id"):
            with self.subTest(field=field):
                changed = copy.deepcopy(source)
                changed["provenance"][field] = "changed-identity"
                self.assertNotEqual(export.record_digest(changed), original)
                out = self.root / field
                manifest = export.export_dataset([changed], out, profile="codemode-events-v1")
                self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
                self.assertTrue(validate_dataset(out)["valid"])

    def test_v2_capture_timestamp_is_not_part_of_review_digest(self):
        source = approved_codemode()
        original = export.record_digest(source)
        source["provenance"]["captured_at"] = 1234567890
        self.assertEqual(export.record_digest(source), original)
        self.assertEqual(self.make([source], profile="codemode-events-v1")["files"]["sft.jsonl"]["records"], 2)

    def test_v1_historical_source_identity_digest_stays_unchanged(self):
        source = record()
        original = export.record_digest(source)
        source["provenance"].update(project_id="changed", session_group_id="new", source_transcript_id="new-file")
        self.assertEqual(export.record_digest(source), original)


if __name__ == "__main__":
    unittest.main()
