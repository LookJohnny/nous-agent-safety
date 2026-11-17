"""Only synthetic exported datasets; reports must never include source values."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
import nous_training_audit as audit
import nous_training_export as export
from nous_training_validate import validate_dataset
from tests.test_training_export import record, review, preference, text
from tests.test_codemode_training import record as codemode_record
from tests.test_training_request import record as native_request_record
from tests.test_training_episode import record as native_episode_record, review as review_episode_step


def request_record(provider="openai-responses", *, outcome=True, content=True):
    value = native_request_record(provider)
    value.update(id="synthetic-request", agent={"id": provider}, evidence=[])
    value["provenance"].update(source="request_observer", observation_scope="client_request_response",
        project_id="synthetic-project", session_id="synthetic-session", session_group_id="synthetic-session",
        request_sha256="1" * 64, response_sha256="2" * 64, synthetic=True)
    value["privacy"]["content_review_required"] = True
    digest = export.record_digest(value)
    if outcome:
        value["evidence"].append({"kind": "human_review", "result": "accepted", "reviewer": "PRIVATE_REVIEWER", "record_digest": digest})
    if content:
        value["evidence"].append({"kind": "content_review", "result": "cleared", "reviewer": "PRIVATE_REVIEWER", "record_digest": digest})
    return value


def reviewed_episode(*, outcome=True, content=True):
    value = native_episode_record()
    value["agent"] = {"id": "openai-responses"}
    value["provenance"]["synthetic"] = True
    bound = export.record_digest(value)
    if outcome:
        value["evidence"].append({"kind": "human_review", "result": "accepted", "reviewer": "PRIVATE_ACTOR", "record_digest": bound})
    if content:
        value["evidence"].append({"kind": "content_review", "result": "cleared", "reviewer": "PRIVATE_ACTOR", "record_digest": bound})
    return value


class TrainingAudit(unittest.TestCase):

    def test_historical_source_categories_are_counted_without_claiming_eligibility(self):
        records=[]
        sources=['historical_transcript','historical_repair','derived_standalone_text_task']
        for source in sources:
            value=record(source,reviewed=False)
            value['provenance']['source']=source
            value['quality']={'complete':False,'issues':['historical_workspace_unavailable'],
                              'missing':['historical_workspace','history_ancestry','history_replay_semantics']}
            records.append(value)
        self.make(records);report=self.report()
        self.assertTrue(report['structural_valid'])
        self.assertEqual(report['distributions']['canonical_sources'],{s:1 for s in sources})
        self.assertEqual(report['quality']['trajectory_issue_records'],{'historical_workspace_unavailable':3})
        self.assertEqual(report['counts']['sft'],0)
        self.assertEqual(report['production_readiness'],'unverified')

    def test_history_gap_and_source_whitelists_reject_unrecognized_suffixes(self):
        value=record(reviewed=False)
        value['provenance']['source']='historical_transcript_PRIVATE_SOURCE'
        value['quality']={'complete':False,'issues':['history_nonlegacy_mode','history_branch_PRIVATE_SECRET'],
                          'missing':['history_replay_semantics','historical_workspace_PRIVATE_PATH']}
        self.make([value]);report=self.report()
        self.assertEqual(report['distributions']['canonical_sources'],{'unknown':1})
        self.assertEqual(report['quality']['trajectory_issue_records'],{'history_nonlegacy_mode':1,'unknown':1})
        self.assertNotIn('PRIVATE_',json.dumps(report))
        self.assertEqual(report['counts']['sft'],0)

    def test_episode_categories_and_step_reviews_do_not_claim_all_decisions_accepted(self):
        value = reviewed_episode(); review_episode_step(value, 0)
        self.make([value], profile=export.EPISODE_PROFILE)
        report = self.report()
        self.assertTrue(report["structural_valid"], report)
        self.assertEqual(report["counts"]["sft"], 2)
        self.assertEqual(report["distributions"]["canonical_schemas"], {"nous.trajectory/4": 1})
        self.assertEqual(report["distributions"]["canonical_sources"], {"request_episode_observer": 1})
        self.assertEqual(report["evidence"]["episode_steps_by_bound_review"], {"accepted": 1, "unreviewed": 2})
        self.assertEqual(report["production_readiness"], "unverified")
        self.assertNotIn("PRIVATE_ACTOR", json.dumps(report))

    def test_episode_incomplete_step_is_a_specific_candidate_rejection(self):
        value = reviewed_episode(outcome=False, content=False)
        value["episode"]["steps"][0]["quality"].update(complete=False, issues=["request_tool_content_redacted"])
        self.make([value], profile=export.EPISODE_PROFILE)
        report = self.report()
        self.assertTrue(report["structural_valid"], report)
        self.assertEqual(report["counts"]["sft"], 0)
        self.assertEqual(report["rejections"]["by_reason"], {"episode_quality_incomplete": 1})
        self.assertEqual(report["evidence"]["episode_steps_by_declared_quality"], {"complete": 2, "incomplete": 1})

    def test_request_providers_and_source_are_static_known_categories(self):
        records = [request_record(provider) for provider in ("openai-responses", "anthropic-messages")]
        records[1]["id"] = "another-request"
        self.make(records, profile=export.REQUEST_PROFILE)
        report = self.report()
        self.assertTrue(report["structural_valid"], report)
        self.assertEqual(report["counts"]["sft"], 2)
        self.assertEqual(report["distributions"]["trajectory_agents"], {"anthropic-messages": 1, "openai-responses": 1})
        self.assertEqual(report["distributions"]["canonical_sources"], {"request_observer": 2})
        self.assertEqual(report["distributions"]["canonical_schemas"], {"nous.trajectory/3": 2})
        self.assertEqual(report["production_readiness"], "unverified")
        self.assertEqual(report["evidence"]["training_rows_by_qualifying_kind"], {"content_review": 2, "human_review": 2})
        self.assertNotIn("PRIVATE_REVIEWER", json.dumps(report))

    def test_request_dedup_counts_all_sources_and_projects(self):
        first, second = request_record(), request_record()
        second["id"] = "alias"
        second["provenance"].update(project_id="another-project", session_group_id="another-session", session_id="another-session")
        for annotation in second["evidence"]:
            annotation["record_digest"] = export.record_digest(second)
        self.make([first, second], profile=export.REQUEST_PROFILE)
        report = self.report()
        self.assertTrue(report["structural_valid"], report)
        self.assertEqual(report["counts"]["sft"], 1)
        self.assertEqual(report["coverage"]["distinct_source_projects"], 2)
        self.assertEqual(report["coverage"]["distinct_source_trajectories"], 2)
        self.assertEqual(report["coverage"]["training_rows_with_multiple_origins"], 1)

    def test_request_incomplete_candidates_have_specific_static_rejection(self):
        value = request_record(outcome=False, content=False)
        value["quality"].update(complete=False, issues=["contract_redacted"])
        self.make([value], profile=export.REQUEST_PROFILE)
        report = self.report()
        self.assertTrue(report["structural_valid"], report)
        self.assertEqual(report["counts"]["sft"], 0)
        self.assertEqual(report["rejections"]["by_reason"], {"request_quality_incomplete": 1})
        self.assertEqual(report["quality"]["trajectory_issue_records"], {"contract_redacted": 1})

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nous-audit-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "dataset"

    def make(self, records=None, pairs=(), profile="trl"):
        return export.export_dataset([record()] if records is None else records,
                                     self.dataset, preferences=pairs, profile=profile)

    def report(self):
        return audit.audit_dataset(self.dataset)

    def manifest(self):
        return json.loads((self.dataset / "manifest.json").read_text())

    def write_manifest(self, value):
        (self.dataset / "manifest.json").write_text(json.dumps(value))

    def replace(self, filename, records):
        raw = "".join(json.dumps(row) + "\n" for row in records).encode()
        (self.dataset / filename).write_bytes(raw)
        manifest = self.manifest()
        manifest["files"][filename] = {"records": len(records), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.write_manifest(manifest)

    def pair(self, source, *, marker=None):
        value = preference()
        value["prompt"] = copy.deepcopy(source["messages"][:-1])
        value["provenance"] = {"source_record_id": source["id"]}
        if marker is not None:
            value["provenance"]["synthetic"] = marker
        value["evidence"][0]["record_digest"] = export.preference_digest(value)
        return value

    def test_valid_nonempty_reports_counts_without_production_claims(self):
        self.make()
        report = self.report()
        self.assertEqual(report["status"], "structurally_valid_with_training_rows")
        self.assertTrue(report["structural_valid"])
        self.assertTrue(report["statistics_available"])
        self.assertTrue(report["training_rows_present"])
        self.assertEqual(report["counts"], {"trajectories": 1, "preferences": 0, "sft": 1, "dpo": 0, "rejected": 0})
        for field in ("production_readiness", "model_effectiveness", "rights_and_consent"):
            self.assertEqual(report[field], "unverified")
        self.assertNotIn("score", report)
        self.assertEqual(report["coverage"]["distinct_source_session_groups"], 1)

    def test_zero_training_rows_remain_structurally_valid_with_actions(self):
        self.make([record(reviewed=False)])
        report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertFalse(report["training_rows_present"])
        self.assertIn("no_training_rows", report["reasons"])
        self.assertIn("inspect_rejections_and_bound_evidence", report["reasons"])
        self.assertEqual(report["rejections"]["by_reason"], {"verified_outcome_missing": 1})
        self.assertIsNone(report["synthetic"]["all_origins_explicit_synthetic_fraction"])

    def test_empty_dataset_requests_explicit_authorized_collection(self):
        self.make([])
        report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertIn("collect_or_import_explicitly_authorized_sources", report["reasons"])
        self.assertEqual(report["counts"]["trajectories"], 0)

    def test_tampered_training_rows_never_produce_aggregate_statistics(self):
        self.make()
        with (self.dataset / "sft.jsonl").open("a") as handle:
            handle.write('{"PRIVATE_BODY":"PRIVATE_VALUE"}\n')
        report = self.report()
        self.assertFalse(report["structural_valid"])
        self.assertFalse(report["statistics_available"])
        self.assertNotIn("counts", report)
        self.assertTrue({"file_sha256_mismatch", "file_byte_count_mismatch"} & {error["code"] for error in report["validation_errors"]})
        self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_rehashed_tampering_still_fails_lineage_validation(self):
        self.make()
        rows = [json.loads(line) for line in (self.dataset / "sft.jsonl").read_text().splitlines()]
        rows[0]["messages"][-1]["content"] = "PRIVATE_REPLACEMENT"
        self.replace("sft.jsonl", rows)
        report = self.report()
        self.assertFalse(report["structural_valid"])
        self.assertIn("lineage_sample_mismatch", {error["code"] for error in report["validation_errors"]})
        self.assertNotIn("PRIVATE_REPLACEMENT", json.dumps(report))

    def test_unknown_validation_codes_and_locations_are_not_echoed(self):
        self.make()
        with mock.patch.object(audit, "validate_dataset", return_value={"valid": False, "errors": [
                {"code": "private_customer_name", "file": "/private/source", "line": "PRIVATE_LINE"}]}):
            report = self.report()
        self.assertEqual(report["validation_errors"], [{"code": "validation_error", "count": 1}])
        self.assertNotIn("private_", json.dumps(report))
        self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_unknown_source_agent_quality_and_evidence_values_are_bucketed(self):
        value = record()
        value["agent"]["id"] = "PRIVATE_AGENT"
        value["provenance"].update(source="PRIVATE_SOURCE", project_id="PRIVATE_PROJECT", session_id="PRIVATE_SESSION")
        value["quality"]["issues"] = ["PRIVATE_ISSUE", "PRIVATE_ISSUE"]
        value["quality"]["missing"].append("PRIVATE_CONTEXT")
        value["evidence"].append({"kind": "PRIVATE_KIND", "result": "PRIVATE_RESULT", "reviewer": "PRIVATE_REVIEWER"})
        self.make([value])
        report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertEqual(report["distributions"]["trajectory_agents"], {"unknown": 1})
        self.assertEqual(report["distributions"]["canonical_sources"], {"unknown": 1})
        self.assertEqual(report["quality"]["trajectory_issue_records"], {"unknown": 1})
        self.assertEqual(report["evidence"]["canonical_annotations_by_kind"]["unknown"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_unknown_rejection_reason_is_not_echoed_even_when_validated(self):
        self.make([record(reviewed=False)])
        rows = [json.loads(line) for line in (self.dataset / "rejected.jsonl").read_text().splitlines()]
        rows[0]["reason"] = "private_customer_contract"
        self.replace("rejected.jsonl", rows)
        manifest = self.manifest()
        manifest["rejections"] = {"sft:private_customer_contract": 1}
        self.write_manifest(manifest)
        self.assertTrue(validate_dataset(self.dataset)["valid"])
        report = self.report()
        self.assertEqual(report["rejections"]["by_reason"], {"unknown": 1})
        self.assertNotIn("private_customer_contract", json.dumps(report))

    def test_actual_rows_do_not_trust_unvalidated_input_counters(self):
        self.make()
        manifest = self.manifest()
        manifest["counts"] = {"input_trajectories": 99999999, "input_preferences": 99999999}
        self.write_manifest(manifest)
        report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertEqual(report["counts"]["trajectories"], 1)
        self.assertNotIn("99999999", json.dumps(report))

    def test_deduplicated_mixed_lineage_and_dpo_count_all_actual_origins(self):
        first, second = record("a"), record("b")
        first["provenance"].update(project_id="project-a", session_group_id="group-a")
        second["provenance"].update(project_id="project-b", session_group_id="group-b")
        self.make([first, second], [self.pair(first)])
        report = self.report()
        self.assertEqual(report["counts"]["sft"], 1)
        self.assertEqual(report["counts"]["dpo"], 1)
        coverage = report["coverage"]
        self.assertEqual(coverage["distinct_source_projects"], 2)
        self.assertEqual(coverage["distinct_source_session_groups"], 2)
        self.assertEqual(coverage["distinct_source_trajectories"], 2)
        self.assertEqual(coverage["training_rows_with_multiple_origins"], 1)
        self.assertEqual(coverage["additional_origin_links_after_deduplication"], 1)
        self.assertEqual(coverage["training_rows_sharing_source_session"], 2)
        self.assertEqual(coverage["repeated_session_row_links"], 1)
        self.assertEqual(coverage["training_rows_with_unresolved_sources"], 0)

    def test_unbound_dpo_does_not_invent_source_coverage(self):
        value = preference()
        value["provenance"] = {"project_id": "PRIVATE_PROJECT", "session_id": "PRIVATE_SESSION", "synthetic": True}
        value["evidence"][0]["record_digest"] = export.preference_digest(value)
        self.make([], [value])
        report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertEqual(report["coverage"]["distinct_source_projects"], 0)
        self.assertEqual(report["coverage"]["training_rows_with_unresolved_sources"], 1)
        self.assertEqual(report["coverage"]["training_rows_with_incomplete_source_identity"], 1)
        self.assertEqual(report["synthetic"]["training_rows_by_origin_markers"], {"unknown_or_partially_marked": 1})

    def test_session_aliases_and_missing_identity_are_reported_separately(self):
        first, second, third = record("a"), record("b"), record("c")
        for value in (first, second):
            value["provenance"]["source_transcript_id"] = "shared-path-identity"
        third["provenance"].pop("project_id")
        self.make([first, second, third])
        report = self.report()
        self.assertEqual(report["coverage"]["distinct_source_projects"], 1)
        self.assertEqual(report["coverage"]["distinct_source_session_groups"], 1)
        self.assertEqual(report["coverage"]["training_rows_with_incomplete_source_identity"], 1)

    def test_synthetic_ratios_use_only_exact_explicit_markers(self):
        values = []
        for index, marker in enumerate((True, False, "true", None)):
            value = record(str(index))
            value["messages"][-1] = text("assistant", "unique synthetic fixture answer " + str(index))
            if marker is not None:
                value["provenance"]["synthetic"] = marker
            values.append(review(value))
        self.make(values)
        manifest = self.manifest()
        manifest["synthetic"] = True  # A dataset label is not a per-origin annotation.
        self.write_manifest(manifest)
        report = self.report()
        summary = report["synthetic"]
        self.assertEqual(summary["canonical_by_explicit_marker"]["trajectories"],
                         {"synthetic": 1, "non_synthetic": 1, "unknown": 2})
        self.assertEqual(summary["all_origins_explicit_synthetic_fraction"], 0.25)
        self.assertEqual(summary["training_rows_by_origin_markers"],
                         {"all_origins_explicit_synthetic": 1, "all_origins_explicit_non_synthetic": 1,
                          "unknown_or_partially_marked": 2})

    def test_dedup_mixed_synthetic_origin_is_not_reported_as_all_synthetic(self):
        first, second = record("a"), record("b")
        first["provenance"]["synthetic"] = True
        second["provenance"]["synthetic"] = False
        self.make([first, second])
        report = self.report()
        self.assertEqual(report["synthetic"]["training_rows_by_origin_markers"], {"mixed_explicit": 1})
        self.assertEqual(report["synthetic"]["all_origins_explicit_synthetic_fraction"], 0)
        self.assertEqual(report["synthetic"]["training_rows_with_any_explicit_synthetic_origin"], 1)

    def test_canonical_evidence_and_qualifying_training_evidence_are_distinct(self):
        value = record()
        value["evidence"].append({"kind": "verifier", "result": "passed", "verifier": "PRIVATE_VERIFIER",
                                  "record_digest": export.record_digest(value)})
        value["evidence"].append({"kind": "human_review", "result": "accepted", "reviewer": "PRIVATE_REVIEWER",
                                  "record_digest": "0" * 64})
        self.make([value])
        evidence = self.report()["evidence"]
        self.assertEqual(evidence["canonical_annotations_by_kind"], {"human_review": 2, "verifier": 1})
        self.assertEqual(evidence["digest_bound_annotations_by_kind"], {"human_review": 1, "verifier": 1})
        self.assertEqual(evidence["training_rows_by_qualifying_kind"], {"human_review": 1, "verifier": 1})
        self.assertNotIn("PRIVATE_", json.dumps(evidence))

    def test_codemode_multiple_targets_count_rows_not_duplicate_sessions(self):
        value = codemode_record()
        value["messages"] = [text("user", "synthetic task"), text("assistant", "synthetic reply")]
        value["provenance"] = {"project_id": "synthetic-project", "session_group_id": "synthetic-group",
                               "session_id": "snapshot", "synthetic": True}
        digest = export.record_digest(value)
        value["evidence"] = [{"kind": "human_review", "result": "accepted", "reviewer": "test", "record_digest": digest},
                             {"kind": "content_review", "result": "cleared", "reviewer": "test", "record_digest": digest}]
        self.make([value], profile=export.CODEMODE_PROFILE)
        report = self.report()
        self.assertTrue(report["structural_valid"], report)
        self.assertEqual(report["counts"]["sft"], 2)
        self.assertEqual(report["coverage"]["distinct_source_session_groups"], 1)
        self.assertEqual(report["coverage"]["repeated_session_row_links"], 1)
        self.assertEqual(report["evidence"]["training_rows_by_qualifying_kind"], {"human_review": 2, "content_review": 2})

    def test_source_artifact_change_after_manifest_anchor_is_detected(self):
        self.make()
        original = audit._open_regular
        def changed(path):
            if Path(path) == self.dataset / "sft.jsonl":
                with (self.dataset / "sft.jsonl").open("ab") as handle:
                    handle.write(b" \n")
            return original(path)
        with mock.patch.object(audit, "_open_regular", side_effect=changed):
            report = self.report()
        self.assertFalse(report["structural_valid"])
        self.assertEqual(report["reasons"], ["dataset_changed_during_audit"])

    def test_even_valid_manifest_replacement_after_anchor_is_detected(self):
        self.make()
        original = audit._snapshot
        def changed(source, snapshot, raw):
            manifest = self.manifest()
            manifest["created_at"] = "new valid manifest"
            self.write_manifest(manifest)
            return original(source, snapshot, raw)
        with mock.patch.object(audit, "_snapshot", side_effect=changed):
            report = self.report()
        self.assertEqual(report["reasons"], ["dataset_changed_during_audit"])
        self.assertTrue(validate_dataset(self.dataset)["valid"])

    def test_statistics_read_only_verified_snapshot_after_original_changes(self):
        self.make()
        original = audit._aggregate
        def changed(snapshot, manifest):
            (self.dataset / "sft.jsonl").write_text("PRIVATE_REPLACEMENT")
            return original(snapshot, manifest)
        with mock.patch.object(audit, "_aggregate", side_effect=changed):
            report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertEqual(report["counts"]["sft"], 1)
        self.assertNotIn("PRIVATE_REPLACEMENT", json.dumps(report))

    def test_snapshot_permissions_and_source_bytes_unchanged(self):
        self.make()
        before = {p.name: p.read_bytes() for p in self.dataset.iterdir()}
        original = audit._aggregate
        def checked(snapshot, manifest):
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in snapshot.iterdir()))
            return original(snapshot, manifest)
        with mock.patch.object(audit, "_aggregate", side_effect=checked):
            self.assertTrue(self.report()["structural_valid"])
        self.assertEqual({p.name: p.read_bytes() for p in self.dataset.iterdir()}, before)

    def test_validator_only_receives_the_private_bounded_snapshot(self):
        self.make()
        def checked(path):
            self.assertNotEqual(Path(path), self.dataset)
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o700)
            return validate_dataset(path)
        with mock.patch.object(audit, "validate_dataset", side_effect=checked) as validator:
            self.assertTrue(self.report()["structural_valid"])
        self.assertEqual(validator.call_count, 1)

    def test_manifest_and_declared_total_limits_precede_validator(self):
        self.make()
        with mock.patch.object(audit, "MAX_MANIFEST_BYTES", 16), mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_manifest_too_large"])
        manifest = self.manifest()
        manifest["files"]["sft.jsonl"]["bytes"] = audit.MAX_DATASET_BYTES + 1
        self.write_manifest(manifest)
        with mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_dataset_too_large"])

    def test_actual_file_growth_over_total_limit_precedes_validator(self):
        self.make()
        total = sum(p.stat().st_size for p in self.dataset.iterdir())
        with (self.dataset / "trajectories.jsonl").open("ab") as handle:
            handle.write(b"x" * total)
        with mock.patch.object(audit, "MAX_DATASET_BYTES", total + 1), \
             mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_dataset_too_large"])

    def test_single_line_limit_never_silently_truncates_or_validates(self):
        self.make()
        with mock.patch.object(audit, "MAX_LINE_BYTES", 32), mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_line_too_large"])
        self.assertFalse(report["statistics_available"])

    def test_declared_record_limit_precedes_copy_and_validator(self):
        self.make()
        manifest = self.manifest()
        manifest["files"]["sft.jsonl"]["records"] = audit.MAX_RECORDS + 1
        self.write_manifest(manifest)
        with mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_too_many_records"])

    def test_empty_line_cpu_attack_hits_actual_total_record_limit(self):
        self.make()
        raw = b"\n" * 4
        (self.dataset / "sft.jsonl").write_bytes(raw)
        manifest = self.manifest()
        manifest["files"]["sft.jsonl"] = {"records": 1, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.write_manifest(manifest)
        with mock.patch.object(audit, "MAX_RECORDS", 3), mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_too_many_records"])

    def test_fifo_replacement_is_nonblocking_and_never_reaches_validator(self):
        self.make()
        (self.dataset / "sft.jsonl").unlink()
        os.mkfifo(self.dataset / "sft.jsonl")
        with mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["reasons"], ["audit_source_unavailable"])

    def test_invalid_manifest_file_fields_rejected_before_copy(self):
        self.make()
        manifest = self.manifest()
        manifest["files"]["sft.jsonl"]["bytes"] = "PRIVATE_PATH"
        self.write_manifest(manifest)
        with mock.patch.object(audit, "validate_dataset") as validator:
            report = self.report()
            validator.assert_not_called()
        self.assertEqual(report["validation_errors"], [{"code": "invalid_file_manifest", "count": 1}])
        self.assertNotIn("PRIVATE_PATH", json.dumps(report))

    def test_missing_or_symlink_artifact_never_reads_untrusted_content(self):
        self.assertFalse(self.report()["structural_valid"])
        self.assertFalse(self.dataset.exists())
        self.make()
        target = self.root / "private"
        target.write_text("PRIVATE_CONTENT")
        (self.dataset / "sft.jsonl").unlink()
        (self.dataset / "sft.jsonl").symlink_to(target)
        report = self.report()
        self.assertFalse(report["structural_valid"])
        self.assertNotIn("PRIVATE_CONTENT", json.dumps(report))

    def test_os_and_parser_errors_return_only_static_diagnostics(self):
        self.make()
        with mock.patch.object(audit, "_snapshot", side_effect=OSError("PRIVATE_SOURCE_PATH")):
            report = self.report()
        self.assertEqual(report["reasons"], ["audit_failed"])
        self.assertNotIn("PRIVATE_SOURCE_PATH", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
