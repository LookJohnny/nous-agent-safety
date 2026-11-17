"""Offline integrity/schema/provenance validation with synthetic data only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import nous_training_export as export
from nous_training_validate import validate_dataset
from tests.test_training_export import record, tool_record, review, preference


class DatasetValidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "dataset"

    def tearDown(self):
        self.temp.cleanup()

    def make(self, *, records=None, preferences=None, profile="trl"):
        return export.export_dataset([record(), tool_record()] if records is None else records,
                                     self.folder, preferences=[preference()] if preferences is None else preferences,
                                     profile=profile)

    def manifest(self):
        return json.loads((self.folder / "manifest.json").read_text())

    def write_manifest(self, manifest):
        (self.folder / "manifest.json").write_text(json.dumps(manifest))

    def rows(self, name):
        return [json.loads(x) for x in (self.folder / name).read_text().splitlines()]

    def replace(self, name, rows, *, rehash=True):
        raw = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows).encode()
        (self.folder / name).write_bytes(raw)
        if rehash:
            manifest = self.manifest()
            manifest["files"][name] = {"records": len(rows), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            self.write_manifest(manifest)

    def codes(self):
        return {error["code"] for error in validate_dataset(self.folder)["errors"]}

    def test_complete_plain_tools_and_preferences_validate_for_both_profiles(self):
        for profile in ("trl", "openai"):
            with self.subTest(profile=profile):
                self.folder = Path(self.temp.name) / profile
                self.make(profile=profile)
                result = validate_dataset(self.folder)
                self.assertTrue(result["valid"], result)
                self.assertEqual(result["counters"], {"trajectories": 2, "preferences": 1, "sft": 2,
                                                       "dpo": 1, "rejected": 0, "errors": 0})

    def test_missing_manifest_and_unsupported_legacy_manifest(self):
        self.assertEqual(self.codes(), {"complete_manifest_missing"})
        self.make()
        manifest = self.manifest()
        manifest["schema"] = "nous.training.manifest/1"
        self.write_manifest(manifest)
        self.assertEqual(self.codes(), {"unsupported_manifest_schema"})

    def test_manifest_cannot_claim_a_different_argument_encoding(self):
        self.make(profile="trl")
        manifest = self.manifest()
        manifest["formats"]["tool_arguments"] = "json-string"
        self.write_manifest(manifest)
        self.assertIn("manifest_format_mismatch", self.codes())

    def test_hash_and_counts_detect_tampering(self):
        self.make()
        with (self.folder / "sft.jsonl").open("a") as handle:
            handle.write("{}\n")
        codes = self.codes()
        self.assertTrue({"file_sha256_mismatch", "file_line_count_mismatch", "file_byte_count_mismatch"} <= codes)

    def test_rehashed_invalid_role_and_extra_training_fields_still_fail(self):
        self.make()
        rows = self.rows("sft.jsonl")
        rows[0]["messages"][0]["role"] = "unknown"
        rows[1]["unexpected"] = "SECRET-NEVER-IN-ERRORS"
        self.replace("sft.jsonl", rows)
        result = validate_dataset(self.folder)
        self.assertFalse(result["valid"])
        self.assertNotIn("SECRET-NEVER-IN-ERRORS", json.dumps(result))
        self.assertTrue({"invalid_training_message", "invalid_training_fields"} <= {e["code"] for e in result["errors"]})

    def test_duplicate_json_keys_and_non_json_constants_are_rejected(self):
        self.make()
        (self.folder / "sft.jsonl").write_text('{"messages":[],"messages":[]}\n{"messages":NaN}\n')
        self.assertTrue({"duplicate_json_key", "invalid_json_constant"} <= self.codes())

    def test_rehashed_invalid_arguments_and_tool_result_links_are_rejected(self):
        self.make(records=[tool_record()], preferences=[])
        rows = self.rows("sft.jsonl")
        rows[0]["messages"][1]["tool_calls"][0]["function"]["arguments"] = {"path": 123}
        self.replace("sft.jsonl", rows)
        self.assertIn("tool_argument_schema_mismatch", self.codes())
        rows[0]["messages"][1]["tool_calls"][0]["function"]["arguments"] = {"path": "a.py"}
        rows[0]["messages"][2]["tool_call_id"] = "unknown-call"
        self.replace("sft.jsonl", rows)
        self.assertIn("orphan_or_duplicate_tool_result", self.codes())

    def test_removed_evidence_is_detected_even_when_file_hash_is_updated(self):
        self.make(records=[record()], preferences=[])
        rows = self.rows("trajectories.jsonl")
        rows[0]["evidence"] = []
        self.replace("trajectories.jsonl", rows)
        self.assertIn("verified_outcome_missing", self.codes())

    def test_preference_evidence_digest_recomputed_from_sidecar(self):
        pair = preference()
        pair["prompt"][0].update(id="id-preserved-only-in-sidecar", ts="2026-11-16")
        pair["evidence"][0]["record_digest"] = export.preference_digest(pair)
        self.make(records=[], preferences=[pair])
        self.assertTrue(validate_dataset(self.folder)["valid"])
        rows = self.rows("preferences.jsonl")
        rows[0]["evidence"][0]["record_digest"] = "0" * 64
        self.replace("preferences.jsonl", rows)
        self.assertIn("explicit_preference_missing", self.codes())

    def test_lineage_missing_and_changed_sample_digest(self):
        self.make(records=[record()], preferences=[])
        manifest = self.manifest()
        entry = next(x for x in manifest["lineage"] if x["format"] == "sft")
        entry["sample_digest"] = "0" * 64
        self.write_manifest(manifest)
        self.assertIn("lineage_sample_mismatch", self.codes())
        manifest["lineage"] = [x for x in manifest["lineage"] if x["format"] != "sft"]
        self.write_manifest(manifest)
        self.assertIn("training_lineage_missing", self.codes())

    def test_dedup_keeps_both_source_aliases_and_validates(self):
        first, second = record("one"), record("two")
        second["messages"][0]["id"] = "different-id"
        review(second)
        manifest = self.make(records=[first, second], preferences=[])
        aliases = [x for x in manifest["lineage"] if x["format"] == "sft"]
        self.assertEqual(len(aliases), 2)
        self.assertEqual({x["line"] for x in aliases}, {1})
        self.assertEqual({x["source_line"] for x in aliases}, {1, 2})
        self.assertTrue(validate_dataset(self.folder)["valid"])

    def test_canonical_rejections_remain_a_valid_auditable_dataset(self):
        first = record()
        second = copy.deepcopy(first)
        second["evidence"][0]["result"] = "rejected"
        self.make(records=[first, second], preferences=[])
        result = validate_dataset(self.folder)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["counters"]["sft"], 0)

    def test_unlinked_reverse_preference_does_not_poison_valid_pair(self):
        good, unlinked = preference(), preference()
        unlinked["id"] = "unlinked"
        unlinked["chosen"], unlinked["rejected"] = unlinked["rejected"], unlinked["chosen"]
        unlinked["provenance"] = {"source_record_id": "no-such-source"}
        unlinked["evidence"][0]["record_digest"] = export.preference_digest(unlinked)
        self.make(records=[], preferences=[good, unlinked])
        result = validate_dataset(self.folder)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["counters"]["dpo"], 1)

    def test_offline_validation_detects_negative_alias_even_with_distinct_canonical_digest(self):
        positive, negative = record("one"), record("two")
        negative["messages"][0]["id"] = "different-source-id"
        review(negative)
        negative["evidence"][0]["result"] = "rejected"
        negative["quality"]["complete"] = False
        self.make(records=[positive], preferences=[])
        rows = self.rows("trajectories.jsonl") + [negative]
        self.replace("trajectories.jsonl", rows)
        manifest = self.manifest()
        manifest["lineage"].append(export._lineage(negative, export.record_digest(negative), 2, "trajectory", []))
        self.write_manifest(manifest)
        self.assertIn("cross_record_evidence_conflict", self.codes())

    def test_unknown_manifest_filename_and_symlink_are_not_followed(self):
        self.make()
        manifest = self.manifest()
        manifest["files"]["../../private"] = manifest["files"]["sft.jsonl"]
        self.write_manifest(manifest)
        self.assertEqual(self.codes(), {"invalid_manifest_files"})
        del manifest["files"]["../../private"]
        self.write_manifest(manifest)
        (self.folder / "sft.jsonl").unlink()
        (self.folder / "sft.jsonl").symlink_to(Path(self.temp.name) / "not-a-dataset")
        self.assertIn("artifact_missing_or_symlink", self.codes())

    def test_cli_returns_nonzero_on_invalid_and_zero_on_valid(self):
        self.make()
        script = Path(__file__).parent.parent / "hooks" / "nous_training_validate.py"
        passed = subprocess.run([sys.executable, str(script), str(self.folder)], text=True, capture_output=True)
        self.assertEqual(passed.returncode, 0, passed.stdout)
        (self.folder / "manifest.json").unlink()
        failed = subprocess.run([sys.executable, str(script), str(self.folder)], text=True, capture_output=True)
        self.assertEqual(failed.returncode, 1)
        self.assertFalse(json.loads(failed.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
