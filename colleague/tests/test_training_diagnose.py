"""Synthetic, read-only diagnosis: full visible gaps, exact evidence and privacy."""
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
import nous_training_diagnose as diagnosis
import nous_training_export as export
from nous_training_validate import validate_dataset
from tests.test_training_export import record, review, text, tool_record, preference
from tests.test_codemode_training import record as codemode_record
from tests.test_training_audit import request_record, reviewed_episode, review_episode_step


def ordered(*, outcome=False, content=False):
    value = codemode_record()
    value.update(agent={"id": "codex"}, messages=[text("user", "synthetic task"), text("assistant", "synthetic result")],
                 provenance={"project_id": "synthetic-project", "session_id": "synthetic-session"}, evidence=[])
    digest = export.record_digest(value)
    if outcome:
        value["evidence"].append({"kind": "human_review", "result": "accepted", "reviewer": "private-reviewer", "record_digest": digest})
    if content:
        value["evidence"].append({"kind": "content_review", "result": "cleared", "reviewer": "private-reviewer", "record_digest": digest})
    return value


class TrainingDiagnose(unittest.TestCase):

    def test_history_workspace_and_payload_gap_have_specific_materials_not_unknown(self):
        value = record(reviewed=False)
        value['provenance']['source'] = 'historical_transcript'
        value['quality'] = {'complete':False,
            'issues':['historical_workspace_unavailable','historical_tool_payloads_omitted'],
            'missing':['system_prompt','tool_definitions','historical_workspace','request_context']}
        before = copy.deepcopy(value)
        self.make([value]); item = self.one()
        self.assertNotIn('unknown', item['known_blocker_codes'])
        self.assertIn('historical_workspace', item['quality']['blocking_missing'])
        actions = {a['action']:a for a in item['required_actions']}
        self.assertIn('recover_historical_workspace', actions)
        self.assertIn('recover_original_source', actions)
        self.assertIn('capture_request_context', actions)
        self.assertNotIn('manual_inspection', actions)
        self.assertIn('historical_file_exclusion_and_privacy_context', actions['recover_historical_workspace']['required_materials'])
        self.assertIn('verified_privacy_context_before_reinspection', actions['recover_original_source']['required_materials'])
        self.assertEqual(item['evidence']['outcome']['status'], 'missing')
        self.assertEqual(item['existing_export']['sft_rows_from_this_source'],0)
        self.assertEqual(value,before)
        for action in actions.values():
            self.assertEqual(action['result'],'reinspection_required_no_automatic_eligibility')

    def test_historical_state_remains_unverified_after_current_privacy_relocation(self):
        value = record(reviewed=False)
        value['provenance']['source'] = 'historical_repair'
        value['quality'] = {'complete':False,
            'issues':['historical_workspace_state_unverified'],
            'missing':['historical_workspace_state']}
        self.make([value]); item = self.one()
        self.assertTrue({'historical_workspace_state_unverified','historical_workspace_state'} <= set(item['known_blocker_codes']))
        self.assertNotIn('unknown', item['known_blocker_codes'])
        actions = {a['action']:a for a in item['required_actions']}
        self.assertIn('verify_historical_workspace_state', actions)
        self.assertNotIn('recover_historical_workspace', actions)
        self.assertNotIn('manual_inspection', actions)
        action = actions['verify_historical_workspace_state']
        self.assertIn('original_workspace_snapshot_at_session_time', action['required_materials'])
        self.assertIn('historical_tracked_untracked_and_ignore_state', action['required_materials'])
        self.assertEqual(action['result'], 'reinspection_required_no_automatic_eligibility')
        self.assertFalse(item['quality']['declared']['complete'])
        self.assertEqual(item['existing_export']['sft_rows_from_this_source'], 0)
        self.assertEqual(item['evidence']['outcome']['status'], 'missing')

    def test_history_ancestry_details_require_verified_edges_not_guessed_concatenation(self):
        value = record(reviewed=False)
        issues = ['history_inherited_context','history_branch','history_missing_parent',
                  'history_parent_order','history_parent_chain_unavailable','history_parent_cycle']
        value['quality'] = {'complete':False,'issues':issues,'missing':['history_ancestry','history_branch_context']}
        self.make([value]); item=self.one()
        self.assertTrue(set(issues) <= set(item['known_blocker_codes']))
        self.assertNotIn('unknown',item['known_blocker_codes'])
        actions={a['action']:a for a in item['required_actions']}
        self.assertIn('recover_history_ancestry',actions)
        self.assertNotIn('manual_inspection',actions)
        self.assertIn('verified_native_branch_edges_and_order',actions['recover_history_ancestry']['required_materials'])
        self.assertEqual(item['existing_export']['status'],'not_included')

    def test_history_nonlegacy_replay_and_offline_native_projection_remain_blocked(self):
        value=record(reviewed=False)
        value['quality']={'complete':False,'issues':['history_nonlegacy_mode','historical_native_protocol_unavailable'],
                          'missing':['history_replay_semantics']}
        self.make([value]);item=self.one()
        self.assertTrue({'history_nonlegacy_mode','history_replay_semantics','historical_native_protocol_unavailable'} <= set(item['known_blocker_codes']))
        self.assertTrue({'capture_request_context','implement_runtime_adapter','recover_original_source','verify_task_outcome'} <= self.actions(item))
        self.assertNotIn('manual_inspection',self.actions(item))
        self.assertEqual(item['existing_export']['sft_rows_from_this_source'],0)

    def test_native_unknown_fields_are_specific_adapter_gaps_on_both_quality_views(self):
        value=ordered(outcome=False,content=False)
        issues=['unsupported_record_fields','unsupported_event_fields','unsupported_runtime_execution_fields',
                'unsupported_runtime_result_fields','unsupported_tool_contract_fields']
        value['quality']={'complete':False,'issues':issues[:2],'missing':[]}
        value['protocol']['quality']={'complete':False,'issues':issues[2:],'missing':[]}
        self.make([value],profile=export.CODEMODE_PROFILE);item=self.one()
        self.assertTrue(set(issues) <= set(item['known_blocker_codes']))
        self.assertNotIn('unknown',item['known_blocker_codes'])
        self.assertIn('implement_runtime_adapter',self.actions(item))
        self.assertNotIn('manual_inspection',self.actions(item))
        self.assertEqual(item['evidence']['content']['status'],'missing')
        self.assertEqual(item['existing_export']['sft_rows_from_this_source'],0)

    def test_history_registry_does_not_accept_private_suffixes_or_emit_them_in_actions(self):
        value=record(reviewed=False)
        private='history_ancestry_PRIVATE_SECRET_PATH'
        value['quality']={'complete':False,'issues':['historical_workspace_unavailable',private],
                          'missing':['historical_workspace','history_replay_semantics_PRIVATE_VALUE']}
        self.make([value]);report=self.report();item=report['records'][0]
        self.assertIn('historical_workspace_unavailable',item['known_blocker_codes'])
        self.assertIn('unknown',item['known_blocker_codes'])
        self.assertIn('manual_inspection',self.actions(item))
        self.assertIn('recover_historical_workspace',self.actions(item))
        self.assertNotIn('PRIVATE_',json.dumps(report))

    def test_history_recovery_actions_never_override_negative_evidence(self):
        value=record()
        value['quality']={'complete':False,'issues':['historical_tool_payloads_omitted','history_branch'],
                          'missing':['historical_workspace','history_branch_context']}
        value['evidence'][0]['result']='rejected'
        self.make([value]);item=self.one()
        self.assertTrue(item['blocked_by_negative_or_reported_conflict'])
        self.assertIn('resolve_negative_or_conflicting_evidence',self.actions(item))
        self.assertNotIn('verify_task_outcome',self.actions(item))
        self.assertNotIn('review_exact_scrubbed_content',self.actions(item))
        self.assertIn('recover_original_source',self.actions(item))
        self.assertEqual(item['existing_export']['sft_rows_from_this_source'],0)

    def test_history_source_recognition_does_not_rewrite_positive_evidence_or_quality(self):
        value=record()
        value['provenance']['source']='historical_repair'
        value['quality']={'complete':False,'issues':['history_nonlegacy_mode'],'missing':['history_replay_semantics']}
        self.make([value])
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.dataset.iterdir()}
        report=self.report();item=report['records'][0]
        self.assertEqual(item['evidence']['outcome']['status'],'satisfied')
        self.assertFalse(item['quality']['declared']['complete'])
        self.assertEqual(report['counts']['sft'],0)
        self.assertTrue(report['training_eligibility_not_reassessed'])
        self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.dataset.iterdir()})

    def test_episode_final_pass_does_not_label_unreviewed_tool_steps(self):
        self.make([reviewed_episode()], profile=export.EPISODE_PROFILE)
        item = self.one()
        self.assertEqual(item["schema"], "nous.trajectory/4")
        self.assertEqual(item["known_blocker_codes"], [])
        self.assertTrue(item["evidence"]["content"]["required_for_episode_training"])
        self.assertEqual(item["existing_export"]["sft_rows_from_this_source"], 1)
        steps = item["episode"]["steps"]
        self.assertEqual([s["bound_review_status"] for s in steps], ["unreviewed"] * 3)
        self.assertEqual([s["sft_rows_from_step"] for s in steps], [0, 0, 1])
        self.assertEqual(steps[0]["action_if_supervising_this_step"], "independent_step_review_required")
        self.assertEqual(steps[-1]["action_if_supervising_this_step"], "none")
        self.assertNotIn("PRIVATE_", json.dumps(item))
        self.assertNotIn("call-", json.dumps(item))

    def test_episode_negative_step_preserves_final_eligibility_and_negative_label(self):
        value = reviewed_episode(); review_episode_step(value, 0, "rejected")
        self.make([value], profile=export.EPISODE_PROFILE)
        item = self.one()
        self.assertFalse(item["blocked_by_negative_or_reported_conflict"])
        self.assertEqual(item["episode"]["steps"][0]["bound_review_status"], "negative")
        self.assertEqual(item["episode"]["steps"][0]["action_if_supervising_this_step"], "preserve_negative_step_review")
        self.assertEqual(item["episode"]["steps"][-1]["sft_rows_from_step"], 1)

    def test_episode_has_no_missing_context_exemption_in_any_step(self):
        value = reviewed_episode(outcome=False, content=False)
        value["episode"]["steps"][1]["quality"]["missing"] = ["system_prompt", "PRIVATE_GAP"]
        self.make([value], profile=export.EPISODE_PROFILE)
        item = self.one()
        self.assertIn("episode_quality_incomplete", item["known_blocker_codes"])
        self.assertEqual(item["quality"]["blocking_missing"], ["system_prompt", "unknown"])
        self.assertEqual(item["quality"]["informational_missing"], [])
        self.assertNotIn("PRIVATE_GAP", json.dumps(item))

    def test_episode_dual_reviews_remain_independent(self):
        value = reviewed_episode(outcome=False, content=True)
        self.make([value], profile=export.EPISODE_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["content"]["status"], "satisfied")
        self.assertEqual(item["evidence"]["outcome"]["status"], "missing")
        self.assertEqual(self.actions(item), {"verify_task_outcome"})

    def test_episode_deduplicated_targets_attribute_both_canonical_origins(self):
        first, second = reviewed_episode(), reviewed_episode()
        second["id"] = "another-episode"
        for origin in [second["provenance"]] + [s["provenance"] for s in second["episode"]["steps"]]:
            origin["project_id"] = "another-project"
        for annotation in second["evidence"]:
            annotation["record_digest"] = export.record_digest(second)
        review_episode_step(first, 0); review_episode_step(second, 0)
        self.make([first, second], profile=export.EPISODE_PROFILE)
        report = self.report()
        self.assertTrue(report["diagnosis_available"], report)
        self.assertEqual(report["counts"]["sft"], 2)
        self.assertEqual(report["aggregate"]["coverage"]["distinct_source_projects"], 2)
        for item in report["records"]:
            self.assertEqual([s["sft_rows_from_step"] for s in item["episode"]["steps"]], [1, 0, 1])

    def test_request_complete_sample_reports_dual_bound_reviews(self):
        self.make([request_record()], profile=export.REQUEST_PROFILE)
        item = self.one()
        self.assertEqual(item["schema"], "nous.trajectory/3")
        self.assertEqual(item["agent"], "openai-responses")
        self.assertEqual(item["existing_export"]["status"], "included")
        self.assertEqual(item["known_blocker_codes"], [])
        self.assertEqual(item["required_actions"], [])
        self.assertTrue(item["evidence"]["content"]["required_for_request_training"])
        self.assertFalse(item["evidence"]["content"]["required_for_ordered_event_training"])
        self.assertEqual(item["evidence"]["content"]["status"], "satisfied")
        self.assertNotIn("PRIVATE_REVIEWER", json.dumps(item))

    def test_request_context_has_no_v1_missing_exemptions(self):
        value = request_record(outcome=False, content=False)
        value["quality"]["missing"] = ["system_prompt", "tool_definitions"]
        self.make([value], profile=export.REQUEST_PROFILE)
        item = self.one()
        self.assertEqual(item["quality"]["blocking_missing"], ["system_prompt", "tool_definitions"])
        self.assertEqual(item["quality"]["informational_missing"], [])
        self.assertIn("request_quality_incomplete", item["known_blocker_codes"])
        self.assertIn("capture_request_context", self.actions(item))
        self.assertNotIn("supply_observed_tool_contracts", self.actions(item))
        self.assertEqual(item["existing_export"]["status"], "not_included")

    def test_request_content_review_is_independent_of_outcome(self):
        for outcome, content, expected in ((False, False, {"verify_task_outcome", "review_exact_scrubbed_content"}),
                                           (True, False, {"review_exact_scrubbed_content"}),
                                           (False, True, {"verify_task_outcome"})):
            value = request_record(outcome=outcome, content=content)
            facts = diagnosis._evidence_facts(value, export.record_digest(value))
            self.assertEqual(facts["outcome"]["status"], "satisfied" if outcome else "missing")
            self.assertEqual(facts["content"]["status"], "satisfied" if content else "missing")
            actions = diagnosis._actions(value, diagnosis._quality_facts(value), facts, [], False)
            self.assertEqual({item["action"] for item in actions}, expected)
            if not content:
                materials = {m for item in actions for m in item["required_materials"]}
                self.assertIn("inspection_of_exact_scrubbed_native_request_and_response", materials)

    def test_request_negative_content_blocks_without_acceptance_hint(self):
        value = request_record()
        value["evidence"][-1]["result"] = "rejected"
        self.make([value], profile=export.REQUEST_PROFILE)
        item = self.one()
        self.assertTrue(item["blocked_by_negative_or_reported_conflict"])
        self.assertEqual(item["evidence"]["outcome"]["status"], "blocked_negative_or_conflicting")
        self.assertEqual(item["evidence"]["content"]["status"], "blocked_negative_or_conflicting")
        self.assertEqual(self.actions(item), {"resolve_negative_or_conflicting_evidence"})

    def test_request_cross_record_conflict_is_not_hidden_by_positive_review(self):
        first, second = request_record(), request_record()
        second["id"] = "alias"
        second["provenance"]["request_sha256"] = "3" * 64
        for entry in second["evidence"]:
            entry["record_digest"] = export.record_digest(second)
        second["evidence"][0]["result"] = "rejected"
        self.make([first, second], profile=export.REQUEST_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["outcome"]["status"], "satisfied")
        self.assertTrue(item["blocked_by_negative_or_reported_conflict"])
        self.assertIn("cross_record_evidence_conflict", item["reported_blocker_codes"])
        self.assertEqual(self.actions(item), {"resolve_negative_or_conflicting_evidence"})

    def test_request_unknown_gaps_stay_static_and_failclosed(self):
        value = request_record(outcome=False, content=False)
        value["quality"].update(complete=False, issues=["PRIVATE_CONTEXT"], missing=["PRIVATE_SOURCE"])
        self.make([value], profile=export.REQUEST_PROFILE)
        item = self.one()
        self.assertEqual(item["quality"]["issues"], ["unknown"])
        self.assertEqual(item["quality"]["blocking_missing"], ["unknown"])
        self.assertIn("request_quality_incomplete", item["known_blocker_codes"])
        self.assertNotIn("PRIVATE_", json.dumps(item))

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nous-diagnosis-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = self.root / "dataset"

    def make(self, records=None, pairs=(), profile="trl"):
        return export.export_dataset([record()] if records is None else records, self.dataset, preferences=pairs, profile=profile)

    def report(self):
        return diagnosis.diagnose_dataset(self.dataset)

    def one(self):
        report = self.report()
        self.assertTrue(report["diagnosis_available"], report)
        return report["records"][0]

    @staticmethod
    def actions(item):
        return {action["action"] for action in item["required_actions"]}

    def manifest(self):
        return json.loads((self.dataset / "manifest.json").read_text())

    def write_manifest(self, value):
        (self.dataset / "manifest.json").write_text(json.dumps(value))

    def replace(self, name, rows):
        raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
        (self.dataset / name).write_bytes(raw)
        manifest = self.manifest()
        manifest["files"][name] = {"records": len(rows), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.write_manifest(manifest)

    def test_valid_existing_v1_export_is_described_without_new_ready_claim(self):
        self.make()
        report = self.report()
        item = report["records"][0]
        self.assertTrue(report["structural_valid"])
        self.assertTrue(report["training_rows_present"])
        self.assertTrue(report["training_eligibility_not_reassessed"])
        self.assertEqual(report["dataset_manifest_sha256"], hashlib.sha256((self.dataset / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(item["existing_export"], {"sft_rows_from_this_source": 1, "status": "included"})
        self.assertEqual(item["evidence"]["outcome"]["status"], "satisfied")
        self.assertEqual(item["evidence"]["content"]["status"], "not_required")
        self.assertEqual(item["quality"]["informational_missing"], ["system_prompt", "tool_definitions"])
        self.assertEqual(item["known_blocker_codes"], [])
        self.assertEqual(item["required_actions"], [])
        self.assertNotIn("ready", item)

    def test_unreviewed_v1_requires_truthful_task_evaluation_not_acceptance(self):
        self.make([record(reviewed=False)])
        item = self.one()
        self.assertEqual(item["evidence"]["outcome"]["status"], "missing")
        self.assertEqual(self.actions(item), {"verify_task_outcome"})
        self.assertEqual(item["existing_export"]["status"], "not_included")
        self.assertNotIn("accepted", json.dumps(item))
        self.assertFalse(item["blocked_by_negative_or_reported_conflict"])

    def test_all_runtime_gaps_and_both_missing_evidence_dimensions_visible_together(self):
        value = ordered()
        issues = ["unresolved_world_state", "unresolved_turn_context", "unresolved_thread_settings", "unresolved_history_metadata"]
        value["quality"] = {"complete": False, "issues": issues, "missing": ["sampling_context", "runtime_context_semantics"]}
        value["protocol"]["quality"] = copy.deepcopy(value["quality"])
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertTrue(set(issues) <= set(item["known_blocker_codes"]))
        self.assertEqual(item["evidence"]["outcome"]["status"], "missing")
        self.assertEqual(item["evidence"]["content"]["status"], "missing")
        self.assertTrue({"capture_request_context", "implement_runtime_adapter", "review_exact_scrubbed_content", "verify_task_outcome"} <= self.actions(item))
        self.assertEqual(item["existing_export"]["sft_rows_from_this_source"], 0)

    def test_complete_v2_has_two_existing_targets_and_exact_both_evidence(self):
        value = ordered(outcome=True, content=True)
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertEqual(item["existing_export"]["sft_rows_from_this_source"], 2)
        self.assertEqual(item["evidence"]["outcome"]["qualifying_kinds"], ["human_review"])
        self.assertEqual(item["evidence"]["content"]["qualifying_kinds"], ["content_review"])
        self.assertEqual(item["required_actions"], [])

    def test_content_is_assessed_independently_when_outcome_missing(self):
        value = ordered(content=True)
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["content"]["status"], "satisfied")
        self.assertEqual(item["evidence"]["outcome"]["status"], "missing")
        self.assertEqual(self.actions(item), {"verify_task_outcome"})

    def test_missing_content_is_visible_when_outcome_satisfied(self):
        self.make([ordered(outcome=True)], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["outcome"]["status"], "satisfied")
        self.assertEqual(item["evidence"]["content"]["status"], "missing")
        self.assertEqual(self.actions(item), {"review_exact_scrubbed_content"})

    def test_stale_evidence_is_not_a_current_review(self):
        value = ordered(outcome=True, content=True)
        for annotation in value["evidence"]:
            annotation["record_digest"] = "0" * 64
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["digest_binding"]["stale_entries"], 2)
        self.assertEqual(item["evidence"]["outcome"]["status"], "missing")
        self.assertEqual(item["evidence"]["content"]["status"], "missing")
        self.assertNotIn("private-reviewer", json.dumps(item))

    def test_content_rule_matches_exporter_actor_result_and_digest_contract(self):
        for change in ({"reviewer": " "}, {"result": "accepted"}, {"record_digest": "0" * 64}):
            value = ordered(outcome=True, content=True)
            value["evidence"][-1].update(change)
            facts = diagnosis._evidence_facts(value, export.record_digest(value))
            self.assertEqual(facts["content"]["status"], "missing")
            with self.assertRaisesRegex(export.InvalidRecord, "custom_content_review_missing"):
                export.training_evidence(value, export.record_digest(value), "sft", export.CODEMODE_PROFILE)
        value = ordered(outcome=True)
        value["evidence"].append({"kind": "content_verifier", "result": "passed", "verifier": "test", "record_digest": export.record_digest(value)})
        self.assertEqual(diagnosis._evidence_facts(value, export.record_digest(value))["content"]["qualifying_kinds"], ["content_verifier"])

    def test_negative_outcome_keeps_blocked_and_never_requests_acceptance(self):
        value = ordered(outcome=True)
        value["evidence"][0]["result"] = "rejected"
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertTrue(item["blocked_by_negative_or_reported_conflict"])
        self.assertEqual(item["evidence"]["outcome"]["status"], "blocked_negative_or_conflicting")
        self.assertIn("resolve_negative_or_conflicting_evidence", self.actions(item))
        self.assertFalse({"verify_task_outcome", "review_exact_scrubbed_content"} & self.actions(item))

    def test_negative_content_also_blocks_outcome_under_exporter_policy(self):
        value = ordered(outcome=True, content=True)
        value["evidence"][-1]["result"] = "rejected"
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["outcome"]["status"], "blocked_negative_or_conflicting")
        self.assertEqual(item["evidence"]["content"]["status"], "blocked_negative_or_conflicting")
        self.assertEqual(self.actions(item), {"resolve_negative_or_conflicting_evidence"})

    def test_unknown_kind_negative_still_uses_exporter_negative_rule(self):
        value = record()
        value["evidence"].append({"kind": "PRIVATE_KIND", "result": "failed", "record_digest": export.record_digest(value)})
        self.make([value])
        item = self.one()
        self.assertTrue(item["evidence"]["bound_negative_present"])
        self.assertNotIn("PRIVATE_KIND", json.dumps(item))

    def test_stale_negative_is_not_applied_to_changed_contents(self):
        value = record()
        value["evidence"].append({"kind": "human_review", "result": "rejected", "record_digest": "0" * 64})
        self.make([value])
        item = self.one()
        self.assertFalse(item["evidence"]["bound_negative_present"])
        self.assertEqual(item["evidence"]["outcome"]["status"], "satisfied")
        self.assertEqual(item["existing_export"]["status"], "included")

    def test_invalid_evidence_on_incomplete_candidate_is_manual_inspection(self):
        value = record()
        value["quality"]["complete"] = False
        value["evidence"] = "PRIVATE_ANNOTATION"
        self.make([value])
        item = self.one()
        self.assertEqual(item["evidence"]["outcome"]["status"], "invalid")
        self.assertIn("manual_inspection", self.actions(item))
        self.assertNotIn("PRIVATE_ANNOTATION", json.dumps(item))

    def test_malformed_annotation_container_cannot_claim_content_satisfied(self):
        value = ordered(content=True)
        value["quality"]["complete"] = False
        value["evidence"].append("PRIVATE_BAD_ANNOTATION")
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertEqual(item["evidence"]["outcome"]["status"], "invalid")
        self.assertEqual(item["evidence"]["content"]["status"], "invalid")
        self.assertIn("manual_inspection", self.actions(item))
        self.assertNotIn("PRIVATE_BAD_ANNOTATION", json.dumps(item))

    def test_v2_missing_system_context_is_a_capture_request_not_v1_informational_gap(self):
        value = ordered()
        value["quality"]["missing"] = ["system_prompt"]
        value["protocol"]["quality"]["missing"] = ["system_prompt"]
        self.make([value], profile=export.CODEMODE_PROFILE)
        item = self.one()
        self.assertIn("system_prompt", item["quality"]["blocking_missing"])
        self.assertEqual(item["quality"]["informational_missing"], [])
        self.assertIn("capture_request_context", self.actions(item))

    def test_cross_record_conflict_blocks_positive_alias_without_new_review_hint(self):
        first, second = record("a"), record("b")
        second["messages"][0]["id"] = "different-source-event"
        review(second)
        second["evidence"][0]["result"] = "rejected"
        self.make([first, second])
        positive = self.report()["records"][0]
        self.assertEqual(positive["evidence"]["outcome"]["status"], "satisfied")
        self.assertTrue(positive["blocked_by_negative_or_reported_conflict"])
        self.assertIn("cross_record_evidence_conflict", positive["reported_blocker_codes"])
        self.assertEqual(self.actions(positive), {"resolve_negative_or_conflicting_evidence"})

    def test_deduplicated_all_origins_are_marked_included(self):
        self.make([record("a"), record("b")])
        report = self.report()
        self.assertEqual(report["counts"]["sft"], 1)
        self.assertEqual(len(report["records"]), 2)
        for item in report["records"]:
            self.assertEqual(item["existing_export"], {"status": "included", "sft_rows_from_this_source": 1})
            self.assertEqual(item["known_blocker_codes"], [])
            self.assertEqual(item["required_actions"], [])
        self.assertEqual(report["records"][1]["reported_rejections"][0]["code"], "duplicate_sft")
        self.assertEqual(report["records"][1]["reported_blocker_codes"], [])

    def test_v1_missing_tool_contracts_uses_only_existing_context_api(self):
        value = tool_record()
        value.pop("tools")
        value["quality"]["missing"].append("tool_definitions")
        review(value)
        self.make([value])
        item = self.one()
        self.assertIn("tool_definitions", item["quality"]["blocking_missing"])
        action = next(action for action in item["required_actions"] if action["action"] == "supply_observed_tool_contracts")
        self.assertIn("existing_import_or_recover_context_api", action["required_materials"])

    def test_tail_requires_original_observation_not_fabricated_completion(self):
        value = record(reviewed=False)
        value["quality"].update(complete=False, issues=["partial_tail", "stop_reply_not_flushed"])
        self.make([value])
        item = self.one()
        self.assertTrue({"partial_tail", "stop_reply_not_flushed"} <= set(item["known_blocker_codes"]))
        self.assertIn("recover_original_source", self.actions(item))

    def test_unknown_identifiers_paths_tool_names_and_labels_never_leak(self):
        value = tool_record()
        value["id"] = "PRIVATE_RECORD_ID/unknown@example.com"
        value["agent"]["id"] = "PRIVATE_AGENT"
        value["provenance"].update(source="PRIVATE_SOURCE", project_id="PRIVATE_PROJECT", session_id="PRIVATE_SESSION")
        value["quality"].update(complete=False, issues=["private_customer_diagnosis"], missing=["PRIVATE_CONTEXT"])
        value["tools"][0]["function"]["name"] = "PRIVATE_TOOL"
        for block in value["messages"][1]["content"]:
            block["name"] = "PRIVATE_TOOL"
            block["input"]["path"] = "/PRIVATE_PATH"
        review(value)
        value["evidence"][0]["reviewer"] = "PRIVATE_REVIEWER"
        self.make([value])
        report = self.report()
        item = report["records"][0]
        self.assertEqual(item["trajectory_ordinal"], 1)
        self.assertEqual(item["record_digest"], export.record_digest(value))
        self.assertEqual(item["agent"], "unknown")
        self.assertIn("unknown", item["known_blocker_codes"])
        self.assertIn("manual_inspection", self.actions(item))
        rendered = json.dumps(report)
        self.assertNotIn("PRIVATE_", rendered)
        self.assertNotIn("private_customer_diagnosis", rendered)

    def test_unknown_rejection_codes_are_bucketed_and_remain_reported_facts(self):
        self.make([record(reviewed=False)])
        rows = [json.loads(line) for line in (self.dataset / "rejected.jsonl").read_text().splitlines()]
        rows[0]["reason"] = "private_customer_diagnosis"
        self.replace("rejected.jsonl", rows)
        manifest = self.manifest(); manifest["rejections"] = {"sft:private_customer_diagnosis": 1}; self.write_manifest(manifest)
        report = self.report()
        item = report["records"][0]
        self.assertEqual(item["reported_blocker_codes"], ["unknown"])
        self.assertIn("manual_inspection", self.actions(item))
        self.assertNotIn("private_customer_diagnosis", json.dumps(report))

    def test_empty_and_historical_preference_dataset_can_be_diagnosed(self):
        value = preference(); value["schema"] = "nous.preference/1"
        value["evidence"][0]["record_digest"] = export.preference_digest(value)
        self.make([], [value])
        report = self.report()
        self.assertTrue(report["diagnosis_available"])
        self.assertTrue(report["structural_valid"])
        self.assertEqual(report["records"], [])
        self.assertEqual(report["counts"]["dpo"], 1)

    def test_zero_training_rows_diagnosis_is_available(self):
        self.make([])
        report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertTrue(report["diagnosis_available"])
        self.assertFalse(report["training_rows_present"])
        self.assertEqual(report["records"], [])

    def test_v1_quality_change_is_bound_by_manifest_not_historical_record_digest(self):
        value = record(reviewed=False)
        self.make([value])
        first = self.report()
        self.dataset = self.root / "second-dataset"
        changed = copy.deepcopy(value)
        changed["quality"].update(complete=False, issues=["partial_tail"])
        self.make([changed])
        second = self.report()
        self.assertEqual(first["records"][0]["record_digest"], second["records"][0]["record_digest"])
        self.assertNotEqual(first["dataset_manifest_sha256"], second["dataset_manifest_sha256"])
        self.assertNotIn("partial_tail", first["records"][0]["known_blocker_codes"])
        self.assertIn("partial_tail", second["records"][0]["known_blocker_codes"])

    def test_shared_old_rejection_does_not_invent_incompleteness_for_included_observation(self):
        current = record()
        old = copy.deepcopy(current)
        old["quality"].update(complete=False, issues=["partial_tail"])
        self.make([old, current])
        report = self.report()
        included = next(row for row in report["records"] if row["existing_export"]["status"] == "included")
        self.assertIn("incomplete_trajectory", included["reported_blocker_codes"])
        self.assertEqual(included["known_blocker_codes"], [])
        self.assertEqual(included["required_actions"], [])

    def test_tamper_and_malformed_data_never_produce_partial_diagnoses(self):
        self.make()
        (self.dataset / "trajectories.jsonl").write_text('PRIVATE_CONTENT')
        report = self.report()
        self.assertFalse(report["structural_valid"])
        self.assertFalse(report["diagnosis_available"])
        self.assertEqual(report["records"], [])
        self.assertNotIn("PRIVATE_CONTENT", json.dumps(report))

    def test_rehashed_invalid_source_still_fails_snapshot_validator(self):
        self.make()
        rows = [json.loads(line) for line in (self.dataset / "trajectories.jsonl").read_text().splitlines()]
        rows[0]["messages"][0]["role"] = "PRIVATE_ROLE"
        self.replace("trajectories.jsonl", rows)
        report = self.report()
        self.assertFalse(report["diagnosis_available"])
        self.assertEqual(report["records"], [])
        self.assertNotIn("PRIVATE_ROLE", json.dumps(report))

    def test_record_limit_rejects_all_instead_of_truncating(self):
        self.make([record("a"), record("b")])
        with mock.patch.object(diagnosis, "MAX_DIAGNOSIS_RECORDS", 1), mock.patch.object(diagnosis, "validate_dataset") as validator:
            report = self.report()
        validator.assert_not_called()
        self.assertFalse(report["diagnosis_available"])
        self.assertFalse(report["structural_valid"])
        self.assertEqual(report["reasons"], ["diagnosis_record_limit_exceeded"])
        self.assertEqual(report["records"], [])

    def test_report_size_limit_keeps_validated_flag_but_no_partial_records(self):
        self.make()
        with mock.patch.object(diagnosis, "MAX_REPORT_BYTES", 64):
            report = self.report()
        self.assertTrue(report["structural_valid"])
        self.assertFalse(report["diagnosis_available"])
        self.assertTrue(report["training_rows_present"])
        self.assertEqual(report["reasons"], ["diagnosis_report_limit_exceeded"])
        self.assertEqual(report["records"], [])

    def test_one_bounded_snapshot_validation_only_and_no_source_mutations(self):
        self.make()
        before = {p.name: p.read_bytes() for p in self.dataset.iterdir()}
        def checked(path):
            self.assertNotEqual(Path(path), self.dataset)
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in Path(path).iterdir()))
            return validate_dataset(path)
        with mock.patch.object(diagnosis, "validate_dataset", side_effect=checked) as validator, \
             mock.patch.object(audit, "_snapshot", wraps=audit._snapshot) as snapshot:
            self.assertTrue(self.report()["diagnosis_available"])
        self.assertEqual(validator.call_count, 1)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual({p.name: p.read_bytes() for p in self.dataset.iterdir()}, before)

    def test_source_change_during_snapshot_is_static_failure(self):
        self.make()
        original = audit._snapshot
        def changed(source, snapshot, raw):
            (self.dataset / "sft.jsonl").write_text("PRIVATE_REPLACEMENT")
            return original(source, snapshot, raw)
        with mock.patch.object(audit, "_snapshot", side_effect=changed):
            report = self.report()
        self.assertFalse(report["diagnosis_available"])
        self.assertNotIn("PRIVATE_REPLACEMENT", json.dumps(report))

    def test_source_change_after_snapshot_does_not_change_diagnosed_bytes(self):
        self.make()
        manifest_sha = hashlib.sha256((self.dataset / "manifest.json").read_bytes()).hexdigest()
        original = diagnosis._diagnose
        def changed(snapshot, manifest, raw):
            (self.dataset / "manifest.json").write_text("PRIVATE_REPLACEMENT")
            return original(snapshot, manifest, raw)
        with mock.patch.object(diagnosis, "_diagnose", side_effect=changed):
            report = self.report()
        self.assertTrue(report["diagnosis_available"])
        self.assertEqual(report["dataset_manifest_sha256"], manifest_sha)

    def test_unknown_validator_error_and_exception_text_are_not_echoed(self):
        self.make()
        with mock.patch.object(diagnosis, "validate_dataset", return_value={"valid": False, "errors": [{"code": "PRIVATE_CODE", "file": "/PRIVATE_PATH"}]}):
            report = self.report()
        self.assertEqual(report["validation_errors"], [{"code": "validation_error", "count": 1}])
        self.assertNotIn("PRIVATE_", json.dumps(report))
        with mock.patch.object(diagnosis, "_diagnose", side_effect=RuntimeError("PRIVATE_EXCEPTION")):
            report = self.report()
        self.assertEqual(report["reasons"], ["diagnosis_failed"])
        self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
