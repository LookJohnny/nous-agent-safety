"""Synthetic request -> pure verification -> strict export/validate/split checks."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
import nous_training_export as export
from nous_training_request import request_messages
from nous_training_split import split_dataset
from nous_training_validate import validate_dataset
from nous_training_verify import evaluate_record
from tests.test_training_export import preference
from tests.test_training_verify import record as base_record


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def source(label="one", *, project="p1", session=None, shared=None,
           result=4, content=True, outcome=True, scope="final_json_task"):
    value = base_record(canonical({"answer": result}))
    value["id"] = label
    exchange = value["exchange"]
    exchange["request"]["input"] = "Return a JSON result for synthetic task " + (shared or label)
    exchange["response"]["id"] = "response-" + label
    value["messages"] = request_messages(exchange)
    raw = canonical(exchange["request"]).encode()
    request_sha = hashlib.sha256(raw).hexdigest()
    value["provenance"].update(
        source="synthetic-request-fixture", synthetic=True, project_id=project,
        session_id=session or label, session_group_id=session or label,
        request_sha256=request_sha, request_content_sha256=request_sha,
        response_sha256=hashlib.sha256(canonical(exchange["response"]).encode()).hexdigest(),
        observation_scope="request", authorization="synthetic-explicit")
    value["evidence"] = []
    digest = export.record_digest(value)
    if outcome:
        spec = {"schema": "nous.oracle/1", "scope": scope, "request_sha256": request_sha,
                "assertions": [{"kind": "exact_json", "expected": {"answer": 4}}]}
        report = evaluate_record(value, spec, request_sha256=request_sha)
        if report["evidence_candidate"]:
            value["evidence"].append(report["evidence_candidate"])
    if content:
        value["evidence"].append({"kind": "content_review", "result": "cleared",
                                  "reviewer": "synthetic-only", "record_digest": digest})
    return value


class RequestPipeline(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nous-request-pipeline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "dataset"

    def make(self, records, *, profile=export.REQUEST_PROFILE, preferences=()):
        report = export.export_dataset(records, self.dataset, profile=profile, preferences=preferences)
        validation = validate_dataset(self.dataset)
        self.assertTrue(validation["valid"], validation)
        return report

    def rows(self, name):
        return [json.loads(row) for row in (self.dataset / name).read_text().splitlines()]

    def rejection_codes(self):
        return {row["reason"] for row in self.rows("rejected.jsonl")}

    @staticmethod
    def origin_parts(report):
        result = {}
        for row in report["line_provenance"]:
            for origin in row["origins"]:
                result.setdefault(origin["id"], set()).add(row["file"].split("/")[0])
        return result

    def test_exact_verifier_plus_independent_content_review_exports_one_row(self):
        manifest = self.make([source()])
        self.assertEqual(manifest["profile"], export.REQUEST_PROFILE)
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        self.assertEqual(manifest["files"]["rejected.jsonl"]["records"], 0)
        row = self.rows("sft.jsonl")[0]
        self.assertEqual(set(row), {"prompt", "completion"})
        prompt, completion = json.loads(row["prompt"]), json.loads(row["completion"])
        self.assertEqual(prompt["schema"], "nous.request-sft/1")
        self.assertNotIn("response", prompt)
        self.assertNotIn("response-one", row["completion"])
        self.assertEqual(completion["response"]["output"][0]["content"][0]["text"], '{"answer":4}')

    def test_outcome_match_does_not_replace_content_review(self):
        manifest = self.make([source(content=False)])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("custom_content_review_missing", self.rejection_codes())

    def test_format_match_does_not_replace_outcome_review(self):
        manifest = self.make([source(scope="final_json_format")])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("verified_outcome_missing", self.rejection_codes())

    def test_wrong_result_retains_negative_and_is_not_trainable(self):
        manifest = self.make([source(result=5)])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("negative_or_conflicting_evidence", self.rejection_codes())
        evidence = self.rows("trajectories.jsonl")[0]["evidence"]
        self.assertIn("failed", {item["result"] for item in evidence})

    def test_unreviewed_candidate_does_not_gain_outcome(self):
        manifest = self.make([source(outcome=False)])
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("verified_outcome_missing", self.rejection_codes())

    def test_stale_evidence_is_not_resigned_after_request_change(self):
        value = source()
        original = export.record_digest(value)
        value["exchange"]["request"]["input"] += " changed"
        raw = canonical(value["exchange"]["request"]).encode()
        value["provenance"].update(request_sha256=hashlib.sha256(raw).hexdigest(),
                                    request_content_sha256=hashlib.sha256(raw).hexdigest())
        value["messages"] = request_messages(value["exchange"])
        self.assertNotEqual(export.record_digest(value), original)
        manifest = self.make([value])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertTrue(all(item["record_digest"] == original for item in self.rows("trajectories.jsonl")[0]["evidence"]))

    def test_bound_source_and_context_changes_invalidate_digest(self):
        value = source()
        original = export.record_digest(value)
        for name in ("request_sha256", "response_sha256", "request_content_sha256", "project_id", "session_group_id"):
            changed = copy.deepcopy(value)
            changed["provenance"][name] = "different"
            self.assertNotEqual(export.record_digest(changed), original, name)
        changed = copy.deepcopy(value)
        changed["provenance"]["captured_at"] = "2099-01-01"
        self.assertEqual(export.record_digest(changed), original)

    def test_old_profile_does_not_flatten_native_exchange(self):
        manifest = self.make([source()], profile="trl")
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("exchange_requires_request_profile", self.rejection_codes())

    def test_dpo_is_explicitly_unsupported_for_request_profile(self):
        manifest = self.make([source()], preferences=[preference()])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        self.assertIn("request_preferences_not_supported", self.rejection_codes())

    def test_dedup_preserves_both_origins_and_connects_their_projects(self):
        records = [source("shared-a", project="pa", shared="same"), source("shared-b", project="pb", shared="same"),
                   source("unique-a", project="pa"), source("unique-b", project="pb")]
        manifest = self.make(records)
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 4)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 3)
        report = split_dataset(self.dataset, self.root / "split", eval_ratio=0.5)
        self.assertEqual(len(report["components"]), 1)
        shared = next(row for row in report["line_provenance"] if len(row["origins"]) == 2)
        self.assertEqual({row["id"] for row in shared["origins"]}, {"shared-a", "shared-b"})
        self.assertEqual(len({row["file"].split("/")[0] for row in report["line_provenance"]}), 1)

    def test_request_snapshots_in_same_session_cannot_cross_split(self):
        self.make([source("first", session="same-session"), source("later", session="same-session"),
                   source("other", session="other-session")])
        report = split_dataset(self.dataset, self.root / "split", group_by="session", eval_ratio=0.5)
        parts = self.origin_parts(report)
        self.assertEqual(parts["first"], parts["later"])
        self.assertEqual(len(report["components"]), 2)

    def test_actual_partition_is_byte_exact_and_keeps_project_groups(self):
        self.make([source(str(index), project="project-" + str(index // 2)) for index in range(24)])
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.dataset.iterdir()}
        output = self.root / "split"
        report = split_dataset(self.dataset, output, eval_ratio=0.5, seed="synthetic-request-check")
        self.assertEqual(report["profile"], export.REQUEST_PROFILE)
        self.assertTrue(report["complete"])
        self.assertEqual(sum(report["counts"][part]["sft"] for part in ("train", "eval")), 24)
        source_rows = (self.dataset / "sft.jsonl").read_bytes().splitlines(keepends=True)
        for line in report["line_provenance"]:
            actual = (output / line["file"]).read_bytes().splitlines(keepends=True)[line["line"] - 1]
            self.assertEqual(actual, source_rows[line["input_line"] - 1])
        parts = self.origin_parts(report)
        for index in range(0, 24, 2):
            self.assertEqual(parts[str(index)], parts[str(index + 1)])
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.dataset.iterdir()})

    def test_conflicting_negative_alias_blocks_equal_training_text(self):
        accepted = source("accepted", project="pa", shared="same")
        rejected = source("rejected", project="pb", shared="same")
        rejected["evidence"].append({"kind": "human_review", "result": "rejected", "reviewer": "synthetic",
                                     "record_digest": export.record_digest(rejected)})
        manifest = self.make([accepted, rejected])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 2)


if __name__ == "__main__":
    unittest.main()
