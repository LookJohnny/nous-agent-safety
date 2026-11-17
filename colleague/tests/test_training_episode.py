"""Synthetic complete native snapshots, chain integrity and selective labels."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_training_episode as episode
from nous_training_request import normalize_exchange, request_messages


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def record(provider="openai-responses", *, parallel=False, error=False):
    schema = {"type": "object", "properties": {"key": {"type": "string"}},
              "required": ["key"], "additionalProperties": False}
    if provider == "openai-responses":
        request = {"model": "synthetic", "tools": [{"type": "function", "name": "lookup", "parameters": schema}],
                   "input": [{"type": "message", "role": "user", "content": "Return a JSON answer using the synthetic lookup."}]}
    else:
        request = {"model": "synthetic", "max_tokens": 256, "tools": [{"name": "lookup", "input_schema": schema}],
                   "messages": [{"role": "user", "content": "Return a JSON answer using the synthetic lookup."}]}
    steps = []
    for index in range(3):
        final = index == 2
        call_ids = ["call-" + str(index)] + (["parallel-call"] if parallel and index == 0 else [])
        if provider == "openai-responses":
            output = ([{"type": "message", "id": "message-final", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"answer":4}'}]}] if final else [
                       {"type": "function_call", "id": "item-" + call, "status": "completed", "call_id": call,
                        "name": "lookup", "arguments": '{"key":"alpha"}'} for call in call_ids])
            response = {"id": "response-" + str(index), "object": "response", "model": "synthetic",
                        "status": "completed", "output": output}
        else:
            content = ([{"type": "text", "text": '{"answer":4}'}] if final else [
                       {"type": "tool_use", "id": call, "name": "lookup", "input": {"key": "alpha"}} for call in call_ids])
            response = {"id": "response-" + str(index), "type": "message", "role": "assistant", "model": "synthetic",
                        "stop_reason": "end_turn" if final else "tool_use", "content": content}
        exchange = normalize_exchange(provider, request, response, schema="nous.request-exchange/2")
        steps.append({"index": index, "exchange": exchange,
            "provenance": {"project_id": "synthetic-project", "session_id": "synthetic-session", "session_group_id": "synthetic-group",
                           "request_sha256": digest(request), "response_sha256": digest(response),
                           "request_content_sha256": digest(request), "response_content_sha256": digest(response)},
            "quality": {"complete": True, "issues": [], "missing": []},
            "privacy": {"scrubbed": True, "tool_history_checked": True, "scrubber_version": "synthetic/1", "counts": {}}})
        request = copy.deepcopy(request)
        if provider == "openai-responses":
            request["input"].extend(copy.deepcopy(output))
            request["input"].extend({"type": "function_call_output", "call_id": call, "output": '{"value":4}'}
                                    for call in reversed(call_ids))
        else:
            request["messages"].append({"role": "assistant", "content": copy.deepcopy(content)})
            request["messages"].append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call, "content": "synthetic failure" if error and index == 0 else '{"value":4}',
                 **({"is_error": True} if error and index == 0 else {})} for call in reversed(call_ids)]})
    return {"schema": episode.SCHEMA, "id": "synthetic-episode", "supervision_policy": episode.POLICY,
            "episode": {"schema": episode.EPISODE_SCHEMA, "provider": provider, "steps": steps},
            "messages": request_messages(steps[-1]["exchange"]), "quality": {"complete": True, "issues": [], "missing": []},
            "privacy": {"scrubbed": True, "tool_history_checked": True, "scrubber_version": "synthetic/1", "counts": {}},
            "provenance": {"source": "request_episode_observer", "project_id": "synthetic-project", "session_id": "synthetic-session",
                           "session_group_id": "synthetic-group", "request_sha256": steps[0]["provenance"]["request_sha256"]},
            "evidence": [], "step_evidence": []}


def refresh(value, *, projection=True):
    """Recompute synthetic capture hashes to test chain semantics independently."""
    for step in value["episode"]["steps"]:
        for side in ("request", "response"):
            sha = digest(step["exchange"][side])
            step["provenance"][side + "_sha256"] = sha
            step["provenance"][side + "_content_sha256"] = sha
    value["provenance"]["request_sha256"] = value["episode"]["steps"][0]["provenance"]["request_sha256"]
    if projection:
        value["messages"] = request_messages(value["episode"]["steps"][-1]["exchange"])
    return value


def review(value, index, result="accepted"):
    value["step_evidence"].append({"kind": "step_review", "result": result, "reviewer": "synthetic-reviewer",
        "episode_digest": episode.episode_digest(value), "step_digest": episode.step_digest(value, index), "step": index})
    return value


class Episode(unittest.TestCase):
    def invalid(self, value, code=None, **kwargs):
        with self.assertRaises(episode.EpisodeError) as found:
            episode.validate_episode(value, **kwargs)
        if code:
            self.assertEqual(str(found.exception), code)
        self.assertNotIn("synthetic-project", str(found.exception))

    def test_complete_three_step_chains_for_both_providers(self):
        for provider in ("openai-responses", "anthropic-messages"):
            with self.subTest(provider=provider):
                value = record(provider)
                episode.validate_episode(value)
                self.assertEqual(episode.final_text(value), '{"answer":4}')
                rows = episode.samples(value)
                self.assertEqual([target for _, target in rows], [{"step": 2}])
                episode.validate_sample(rows[0][0])

    def test_whole_task_pass_never_labels_intermediate_tool_decisions(self):
        value = record()
        value["evidence"] = [{"kind": "verifier", "result": "passed", "verifier": "synthetic-oracle",
                              "record_digest": episode.episode_digest(value)}]
        self.assertEqual([target for _, target in episode.samples(value)], [{"step": 2}])
        with self.assertRaisesRegex(episode.EpisodeError, "^episode_step_review_missing_or_negative$"):
            episode.sample_for(value, {"step": 0})

    def test_explicit_step_review_only_adds_its_target(self):
        value = review(record(), 0)
        self.assertEqual([target for _, target in episode.samples(value)], [{"step": 0}, {"step": 2}])
        review(value, 1)
        self.assertEqual(len(episode.samples(value)), 3)

    def test_step_rejection_does_not_mark_other_steps_correct_or_block_final(self):
        value = review(review(record(), 0), 0, "rejected")
        self.assertEqual([target for _, target in episode.samples(value)], [{"step": 2}])
        self.assertEqual(len(value["step_evidence"]), 2)

    def test_final_step_negative_is_respected(self):
        value = review(record(), 2, "rejected")
        self.assertEqual(episode.samples(value), [])
        with self.assertRaisesRegex(episode.EpisodeError, "^episode_step_review_missing_or_negative$"):
            episode.sample_for(value, {"step": 2})

    def test_step_review_cannot_be_reused_for_another_index_or_episode(self):
        value = review(record(), 0)
        value["step_evidence"][0]["step"] = 1
        self.assertEqual([target for _, target in episode.samples(value)], [{"step": 2}])
        value = review(record(), 0)
        value["provenance"]["session_id"] = "different-session"
        for step in value["episode"]["steps"]:
            step["provenance"]["session_id"] = "different-session"
        self.assertEqual([target for _, target in episode.samples(value)], [{"step": 2}])

    def test_sampling_has_no_mutation_or_tool_result_labels(self):
        value = review(review(record(), 0), 1)
        before = copy.deepcopy(value)
        for sample, target in episode.samples(value):
            episode.validate_sample(sample)
            self.assertNotIn('function_call_output', sample["completion"])
            self.assertNotIn('{\\"value\\":4}', sample["completion"])
            self.assertEqual(json.loads(sample["prompt"])["request"], value["episode"]["steps"][target["step"]]["exchange"]["request"])
        self.assertEqual(before, value)

    def test_parallel_results_may_arrive_in_reverse_order(self):
        for provider in ("openai-responses", "anthropic-messages"):
            episode.validate_episode(record(provider, parallel=True))

    def test_error_result_is_preserved_without_success_inference(self):
        value = record("anthropic-messages", error=True)
        episode.validate_episode(value)
        sample = episode.samples(value)[0][0]
        self.assertIn('"is_error":true', sample["prompt"])
        self.assertEqual(len(value["step_evidence"]), 0)

    def test_dropped_middle_step_is_not_reconstructed_from_later_context(self):
        value = record()
        del value["episode"]["steps"][1]
        value["episode"]["steps"][1]["index"] = 1
        self.invalid(value)

    def test_mid_episode_start_is_rejected(self):
        value = record()
        value["episode"]["steps"] = value["episode"]["steps"][1:]
        for index, step in enumerate(value["episode"]["steps"]):
            step["index"] = index
        refresh(value)
        self.invalid(value, "episode_initial_tool_history")

    def test_reordering_steps_is_rejected(self):
        value = record()
        value["episode"]["steps"][0], value["episode"]["steps"][1] = value["episode"]["steps"][1], value["episode"]["steps"][0]
        self.invalid(value, "episode_step_order_invalid")

    def test_duplicate_snapshots_are_rejected(self):
        value = record()
        value["episode"]["steps"][1]["provenance"]["request_sha256"] = value["episode"]["steps"][0]["provenance"]["request_sha256"]
        self.invalid(value, "episode_duplicate_snapshot")

    def test_duplicate_response_identity_is_rejected(self):
        value = record()
        value["episode"]["steps"][1]["exchange"]["response"]["id"] = "response-0"
        refresh(value)
        self.invalid(value, "episode_duplicate_response_identity")

    def test_cross_project_session_or_session_group_step_is_rejected(self):
        for field in ("project_id", "session_id", "session_group_id"):
            value = record()
            value["episode"]["steps"][1]["provenance"][field] = "different"
            self.invalid(value, "episode_cross_source_step")

    def test_cross_provider_and_legacy_exchange_cannot_enter_episode(self):
        for field, text in (("provider", "anthropic-messages"), ("schema", "nous.request-exchange/1")):
            value = record()
            value["episode"]["steps"][1]["exchange"][field] = text
            self.invalid(value, "episode_exchange_schema_or_provider_mismatch")

    def test_inconsistent_native_content_digest_is_rejected(self):
        for side in ("request", "response"):
            value = record()
            value["episode"]["steps"][1]["provenance"][side + "_content_sha256"] = "0" * 64
            self.invalid(value, "episode_content_digest_mismatch")

    def test_initial_raw_request_binding_is_required(self):
        value = record()
        value["provenance"]["request_sha256"] = "0" * 64
        self.invalid(value, "episode_initial_request_digest_mismatch")

    def test_changed_instructions_or_tool_contract_are_context_switches(self):
        value = record()
        value["episode"]["steps"][1]["exchange"]["request"]["instructions"] = "different task"
        refresh(value)
        self.invalid(value, "episode_request_context_changed")
        value = record()
        value["episode"]["steps"][1]["exchange"]["request"]["tools"][0]["description"] = "different contract"
        refresh(value)
        self.invalid(value, "episode_request_context_changed")

    def test_actual_request_prefix_cannot_change_even_audit_ids(self):
        value = record()
        value["episode"]["steps"][2]["exchange"]["request"]["input"][1]["id"] = "changed-history-id"
        refresh(value)
        self.invalid(value, "episode_history_prefix_changed")

    def test_anthropic_text_alias_and_cache_markers_only_change_comparison_view(self):
        value = record("anthropic-messages")
        original_digest = episode.episode_digest(value)
        steps = value["episode"]["steps"]
        text = steps[0]["exchange"]["request"]["messages"][0]["content"]
        steps[0]["exchange"]["request"]["messages"][0]["content"] = [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        steps[2]["exchange"]["request"]["messages"][0]["content"] = [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        # The cached result breakpoint moves between actual requests. Its text
        # and explicit error semantics do not change.
        steps[1]["exchange"]["request"]["messages"][2]["content"][0]["cache_control"] = {"type": "ephemeral"}
        refresh(value)
        before = copy.deepcopy(value)
        episode.validate_episode(value)
        self.assertEqual(value, before)
        self.assertNotEqual(episode.episode_digest(value), original_digest)
        sample = episode.samples(value)[0][0]
        self.assertEqual(json.loads(sample["prompt"])["request"], steps[-1]["exchange"]["request"])
        self.assertIn("cache_control", json.loads(sample["prompt"])["request"]["messages"][0]["content"][0])

    def test_anthropic_copied_assistant_text_can_add_native_cache_marker(self):
        value = record("anthropic-messages")
        steps = value["episode"]["steps"]
        steps[0]["exchange"]["response"]["content"].insert(0, {"type": "text", "text": "Synthetic lookup follows."})
        for index, step in enumerate(steps[1:]):
            step["exchange"]["request"]["messages"][1]["content"].insert(0,
                {"type": "text", "text": "Synthetic lookup follows.",
                 "cache_control": {"type": "ephemeral", "ttl": "5m" if index == 0 else "1h"}})
        refresh(value)
        episode.validate_episode(value)
        changed = copy.deepcopy(value)
        changed["episode"]["steps"][1]["exchange"]["request"]["messages"][1]["content"][0]["text"] += " changed"
        refresh(changed)
        self.invalid(changed, "episode_observed_response_missing_or_changed")

    def test_anthropic_text_alias_does_not_merge_trim_or_ignore_extra_fields(self):
        for content in ([{"type": "text", "text": "changed"}],
                        [{"type": "text", "text": "Return a JSON answer "},
                         {"type": "text", "text": "using the synthetic lookup."}],
                        [{"type": "text", "text": "Return a JSON answer using the synthetic lookup. "}],
                        [{"type": "text", "text": "Return a JSON answer using the synthetic lookup.", "citations": []}]):
            with self.subTest(content=content):
                value = record("anthropic-messages")
                value["episode"]["steps"][1]["exchange"]["request"]["messages"][0]["content"] = content
                refresh(value)
                self.invalid(value, "episode_history_prefix_changed")

    def test_anthropic_tool_input_user_cache_control_key_is_never_stripped(self):
        value = record("anthropic-messages")
        for step in value["episode"]["steps"]:
            ex = step["exchange"]
            ex["request"]["tools"][0]["input_schema"]["properties"]["cache_control"] = {"type": "string"}
            contents = [m["content"] for m in ex["request"]["messages"]] + [ex["response"]["content"]]
            for content in contents:
                if isinstance(content, list):
                    for block in content:
                        if block["type"] == "tool_use":
                            block["input"]["cache_control"] = "original-user-value"
        refresh(value)
        episode.validate_episode(value)
        value["episode"]["steps"][2]["exchange"]["request"]["messages"][1]["content"][0]["input"]["cache_control"] = "changed-user-value"
        refresh(value)
        self.invalid(value, "episode_history_prefix_changed")

    def test_anthropic_result_json_and_error_flag_remain_exact(self):
        for change in ("json", "error"):
            with self.subTest(change=change):
                value = record("anthropic-messages")
                for step in value["episode"]["steps"][1:]:
                    step["exchange"]["request"]["messages"][2]["content"][0]["content"] = '{"cache_control":"original-user-value"}'
                refresh(value)
                episode.validate_episode(value)
                result = value["episode"]["steps"][2]["exchange"]["request"]["messages"][2]["content"][0]
                if change == "json":
                    result["content"] = '{"cache_control":"changed-user-value"}'
                else:
                    result["is_error"] = True
                refresh(value)
                self.invalid(value, "episode_history_prefix_changed")

    def test_anthropic_nested_result_blocks_are_not_recursively_normalized(self):
        value = record("anthropic-messages")
        for step in value["episode"]["steps"][1:]:
            step["exchange"]["request"]["messages"][2]["content"][0]["content"] = [
                {"type": "text", "text": '{"value":4}'}]
        refresh(value)
        episode.validate_episode(value)
        value["episode"]["steps"][2]["exchange"]["request"]["messages"][2]["content"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
        refresh(value)
        self.invalid(value, "episode_history_prefix_changed")

    def test_anthropic_top_level_cache_context_still_must_match(self):
        value = record("anthropic-messages")
        value["episode"]["steps"][1]["exchange"]["request"]["cache_control"] = {"type": "ephemeral"}
        refresh(value)
        self.invalid(value, "episode_request_context_changed")

    def test_anthropic_unknown_cache_shapes_cannot_bypass_native_validation(self):
        value = record("anthropic-messages")
        value["episode"]["steps"][1]["exchange"]["request"]["messages"][0]["content"] = [
            {"type": "text", "text": "Return a JSON answer using the synthetic lookup.",
             "cache_control": {"type": "unrecognized"}}]
        refresh(value)
        self.invalid(value, "episode_native_exchange_invalid")

    def test_known_response_reinsertion_audit_metadata_can_be_omitted(self):
        value = record()
        for step in value["episode"]["steps"][1:]:
            for item in step["exchange"]["request"]["input"]:
                if item.get("type") == "function_call":
                    item.pop("id", None); item.pop("status", None)
        refresh(value)
        episode.validate_episode(value)
        self.assertIn("id", value["episode"]["steps"][0]["exchange"]["response"]["output"][0])
        self.assertNotIn("id", value["episode"]["steps"][1]["exchange"]["request"]["input"][1])

    def test_reinserted_call_arguments_must_remain_exact(self):
        value = record()
        value["episode"]["steps"][1]["exchange"]["request"]["input"][1]["arguments"] = '{ "key": "alpha" }'
        refresh(value)
        self.invalid(value, "episode_observed_response_missing_or_changed")

    def test_missing_or_duplicate_results_are_rejected(self):
        value = record(parallel=True)
        value["episode"]["steps"][1]["exchange"]["request"]["input"].pop()
        refresh(value, projection=False)
        self.invalid(value, "episode_native_exchange_invalid")
        value = record()
        value["episode"]["steps"][1]["exchange"]["request"]["input"].append(copy.deepcopy(value["episode"]["steps"][1]["exchange"]["request"]["input"][-1]))
        refresh(value, projection=False)
        self.invalid(value, "episode_native_exchange_invalid")

    def test_new_user_instruction_inside_tool_loop_is_not_inferred_same_task(self):
        value = record()
        value["episode"]["steps"][1]["exchange"]["request"]["input"].append({"type": "message", "role": "user", "content": "Now a different task"})
        refresh(value)
        self.invalid(value, "episode_unobserved_context_append")

    def test_unfinished_pending_tail_is_candidate_only(self):
        value = record()
        value["episode"]["steps"].pop()
        value["messages"] = request_messages(value["episode"]["steps"][-1]["exchange"])
        value["quality"].update(complete=False, issues=["episode_incomplete"])
        episode.validate_episode(value, require_quality=False)
        self.assertEqual(len(episode.samples(value, require_quality=False)), 2)
        self.invalid(value, "episode_quality_incomplete")

    def test_complete_flag_does_not_invent_a_final_answer(self):
        value = record()
        value["episode"]["steps"].pop()
        value["messages"] = request_messages(value["episode"]["steps"][-1]["exchange"])
        self.invalid(value, "episode_final_response_missing")

    def test_every_step_quality_and_privacy_gate_is_checked(self):
        value = record()
        value["episode"]["steps"][0]["quality"]["missing"] = ["private-synthetic-gap"]
        self.invalid(value, "episode_quality_incomplete")
        value = record()
        value["episode"]["steps"][0]["privacy"]["tool_history_checked"] = False
        self.invalid(value, "episode_privacy_not_checked")

    def test_ui_projection_must_match_last_actual_exchange(self):
        value = record()
        value["messages"][-1]["content"][0]["text"] = '{"answer":999}'
        self.invalid(value, "episode_message_projection_mismatch")

    def test_digest_excludes_volatile_privacy_counters_versions_and_top_timestamp(self):
        value = review(record(), 0)
        before, step = episode.episode_digest(value), episode.step_digest(value, 0)
        value["privacy"].update(counts={"changed_values": 99}, scrubber_version="new")
        value["provenance"]["captured_at"] = 999
        for item in value["episode"]["steps"]:
            item["privacy"].update(counts={"changed_values": 7}, scrubber_version="new")
        self.assertEqual(episode.episode_digest(value), before)
        self.assertEqual(episode.step_digest(value, 0), step)
        self.assertEqual(len(episode.samples(value)), 2)

    def test_digest_binds_native_content_source_quality_and_privacy_gates(self):
        value = record()
        before = episode.episode_digest(value)
        for field, changed in (("quality", {"complete": False, "missing": [], "issues": []}),
                               ("supervision_policy", "invent-labels")):
            other = copy.deepcopy(value); other[field] = changed
            self.assertNotEqual(episode.episode_digest(other), before)
        other = copy.deepcopy(value)
        other["episode"]["steps"][0]["privacy"]["tool_history_checked"] = False
        self.assertNotEqual(episode.episode_digest(other), before)

    def test_policy_and_unknown_context_extensions_are_not_implicit(self):
        value = record(); value.pop("supervision_policy")
        self.invalid(value, "episode_supervision_policy_required")
        value = record(); value["protocol"] = {}
        self.invalid(value, "episode_unbound_context_extension")

    def test_negative_quarantine_cannot_be_used_as_step_review(self):
        value = review(record(), 0, "rejected")
        self.assertEqual(len(episode.samples(value, require_quality=False)), 3)
        self.assertEqual(len(episode.samples(value)), 1)

    def test_target_boundaries_are_closed(self):
        for target in ({"step": True}, {"step": -1}, {"step": 3}, {"step": 2, "extra": True}, None):
            with self.assertRaisesRegex(episode.EpisodeError, "^episode_target_invalid$"):
                episode.sample_for(record(), target)

    def test_resource_limits_fail_without_truncation(self):
        value = record()
        with mock.patch.object(episode, "MAX_STEPS", 2):
            self.invalid(value, "episode_step_limit")
        with mock.patch.object(episode, "MAX_BYTES", 32):
            self.invalid(value, "episode_size_limit")


def accept(value, *, outcome=True, content=True):
    value["evidence"] = []
    bound = episode.episode_digest(value)
    if outcome:
        value["evidence"].append({"kind": "verifier", "result": "passed", "verifier": "synthetic-whole-task",
                                  "record_digest": bound})
    if content:
        value["evidence"].append({"kind": "content_review", "result": "cleared", "reviewer": "synthetic-content",
                                  "record_digest": bound})
    return value


class EpisodeExport(unittest.TestCase):
    def setUp(self):
        import nous_training_export as exporter
        from nous_training_validate import validate_dataset
        self.exporter, self.validate = exporter, validate_dataset
        temporary = tempfile.TemporaryDirectory(prefix="nous-episode-export-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / "dataset"

    def export(self, values, *, profile=episode.PROFILE, preferences=()):
        manifest = self.exporter.export_dataset(values, self.dataset, profile=profile, preferences=preferences)
        result = self.validate(self.dataset)
        self.assertTrue(result["valid"], result)
        return manifest

    def test_final_only_export_has_bound_target_and_no_implicit_step_labels(self):
        manifest = self.export([accept(record())])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 1)
        self.assertFalse(manifest["quality_policy"]["missing_system_prompt_allowed"])
        self.assertEqual(manifest["formats"]["supervision_policy"], episode.POLICY)
        lineage = [item for item in manifest["lineage"] if item["format"] == "sft"]
        self.assertEqual([item["target"] for item in lineage], [{"step": 2}])
        self.assertEqual(lineage[0]["evidence_kinds"], ["content_review", "verifier"])

    def test_reviewed_intermediates_are_individual_training_targets(self):
        manifest = self.export([review(review(accept(record()), 0), 1)])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 3)
        self.assertEqual([item["target"] for item in manifest["lineage"] if item["format"] == "sft"],
                         [{"step": 0}, {"step": 1}, {"step": 2}])

    def test_outcome_and_content_remain_independent_required_gates(self):
        for index, options in enumerate(({"outcome": False}, {"content": False})):
            self.dataset = self.root / str(index)
            manifest = self.export([review(accept(record(), **options), 0)])
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_old_profiles_do_not_flatten_episode_or_enable_dpo(self):
        from tests.test_training_export import preference
        for index, profile in enumerate(("trl", "openai", "request-sft-v1", "codemode-events-v1")):
            self.dataset = self.root / str(index)
            manifest = self.export([accept(record())], profile=profile)
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.dataset = self.root / "no-dpo"
        manifest = self.export([accept(record())], preferences=[preference()])
        self.assertEqual(manifest["files"]["dpo.jsonl"]["records"], 0)
        self.assertIn("dpo:episode_preferences_not_supported", manifest["rejections"])

    def test_incomplete_pending_candidate_has_no_training_rows(self):
        value = record()
        value["episode"]["steps"].pop()
        value["messages"] = request_messages(value["episode"]["steps"][-1]["exchange"])
        value["quality"].update(complete=False, issues=["episode_incomplete"])
        manifest = self.export([accept(value)])
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_global_negative_pending_prefix_blocks_observed_earlier_targets(self):
        good = review(review(accept(record()), 0), 1)
        bad = record(); bad["id"] = "pending-negative"
        bad["episode"]["steps"].pop()
        bad["messages"] = request_messages(bad["episode"]["steps"][-1]["exchange"])
        bad["quality"].update(complete=False, issues=["episode_incomplete"])
        bad["evidence"] = [{"kind": "human_review", "result": "rejected", "reviewer": "synthetic",
                            "record_digest": episode.episode_digest(bad)}]
        manifest = self.export([good, bad])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 1)
        self.assertEqual([item["target"] for item in manifest["lineage"] if item["format"] == "sft"], [{"step": 2}])

    def test_step_negative_alias_blocks_only_matching_step_across_sources(self):
        for reverse in (False, True):
            self.dataset = self.root / str(reverse)
            good = review(review(accept(record()), 0), 1)
            bad = record(); bad["id"] = "other-source"
            bad["provenance"]["project_id"] = "another-project"
            for step in bad["episode"]["steps"]:
                step["provenance"]["project_id"] = "another-project"
            review(accept(bad), 0, "rejected")
            manifest = self.export([bad, good] if reverse else [good, bad])
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 2)
            self.assertTrue(all(item["target"] != {"step": 0} for item in manifest["lineage"] if item["format"] == "sft"))

    def test_final_negative_alias_cannot_be_overridden_by_another_origin(self):
        good, bad = accept(record()), review(accept(record()), 2, "rejected")
        bad["id"] = "negative-final"
        manifest = self.export([good, bad])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_validator_rejects_lineage_pointing_to_unreviewed_step(self):
        manifest = self.export([accept(record())])
        item = next(item for item in manifest["lineage"] if item["format"] == "sft")
        item["target"] = {"step": 0}
        (self.dataset / "manifest.json").write_text(json.dumps(manifest))
        result = self.validate(self.dataset)
        self.assertFalse(result["valid"])
        self.assertIn("episode_step_review_missing_or_negative", {item["code"] for item in result["errors"]})

    def test_validator_requires_every_deduplicated_origin_and_target(self):
        first, second = review(accept(record()), 0), review(accept(record()), 0)
        second["id"] = "second-origin"
        manifest = self.export([first, second])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 2)
        manifest["lineage"] = [item for item in manifest["lineage"] if not
            (item["format"] == "sft" and item["id"] == "second-origin" and item["target"] == {"step": 0})]
        (self.dataset / "manifest.json").write_text(json.dumps(manifest))
        result = self.validate(self.dataset)
        self.assertFalse(result["valid"])
        self.assertIn("training_origin_lineage_missing", {item["code"] for item in result["errors"]})

    def test_digest_delegation_stays_stable_after_privacy_counter_refresh(self):
        value = review(accept(record()), 0)
        original = self.exporter.record_digest(value)
        value["privacy"].update(counts={"changed_values": 123}, scrubber_version="new-policy-observation")
        for step in value["episode"]["steps"]:
            step["privacy"].update(counts={}, scrubber_version="new-policy-observation")
        self.assertEqual(self.exporter.record_digest(value), original)
        manifest = self.export([value])
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 2)


if __name__ == "__main__":
    unittest.main()
