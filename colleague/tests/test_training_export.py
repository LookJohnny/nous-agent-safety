"""Training export invariants: authentic context, bound labels, closed tools, no leaks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import nous_training_export as export


def text(role, value):
    return {"role": role, "content": [{"type": "text", "text": value}]}


def record(identifier="session-1", reviewed=True):
    item = {"schema": "nous.trajectory/1", "id": identifier,
            "agent": {"id": "claude", "version": "test", "model": "synthetic"},
            "messages": [text("user", "计算 2+2"), text("assistant", "4")],
            "provenance": {"source": "claude", "session_id": identifier,
                           "source_sha256": "a" * 64, "project_id": "project-1"},
            "quality": {"complete": True, "missing": ["system_prompt", "tool_definitions"], "issues": []},
            "privacy": {"scrubbed": True, "scrubber_version": "test-1"}, "evidence": []}
    if reviewed:
        review(item)
    return item


def review(item):
    item["evidence"] = [{"kind": "human_review", "result": "accepted", "reviewer": "test-reviewer",
                         "record_digest": export.record_digest(item)}]
    return item


def tool_record():
    item = record()
    item["messages"] = [text("user", "读取并总结"),
                        {"role": "assistant", "content": [{"type": "tool_use", "id": "read-1", "name": "read",
                                                           "input": {"path": "a.py"}},
                                                          {"type": "tool_use", "id": "read-2", "name": "read",
                                                           "input": {"path": "b.py"}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-2",
                                                      "content": {"lines": [1, 2]}},
                                                     {"type": "tool_result", "tool_use_id": "read-1", "content": "ok"}]},
                        text("assistant", "已读取两个文件。")]
    item["tools"] = [{"type": "function", "function": {"name": "read", "description": "read a file",
                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
    item["quality"]["missing"] = ["system_prompt"]
    return review(item)


def preference():
    item = {"id": "preference-1", "prompt": [text("user", "2+2等于多少？")],
            "chosen": [text("assistant", "4")], "rejected": [text("assistant", "5")],
            "privacy": {"scrubbed": True, "scrubber_version": "test-1"}, "evidence": []}
    item["evidence"] = [{"kind": "human_preference", "result": "chosen", "reviewer": "reviewer-1",
                         "record_digest": export.preference_digest(item)}]
    return item


class TrainingExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "dataset"

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, name):
        return [json.loads(line) for line in (self.output / name).read_text().splitlines()]

    def run_export(self, records=(), preferences=(), **kwargs):
        return export.export_dataset(iter(records), self.output, preferences=iter(preferences), **kwargs)

    def test_plain_conversation_without_invented_system_and_multi_turn_preserved(self):
        item = record()
        item["messages"] += [text("user", "那 3+4 呢？"), text("assistant", "7")]
        review(item)
        manifest = self.run_export([item])
        sample = self.rows("sft.jsonl")[0]
        self.assertEqual(set(sample), {"messages"})
        self.assertEqual([m["role"] for m in sample["messages"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual(sample["messages"][-1]["content"], "7")
        self.assertEqual(self.rows("trajectories.jsonl"), [item])
        self.assertEqual(manifest["profile"], "trl")
        self.assertEqual(self.rows("dpo.jsonl"), [])

    def test_no_review_and_stale_digest_do_not_become_sft_or_dpo(self):
        unreviewed = record(reviewed=False)
        stale = record("session-2")
        stale["messages"][-1] = text("assistant", "changed after review")
        self.run_export([unreviewed, stale])
        self.assertEqual(len(self.rows("trajectories.jsonl")), 2)
        self.assertEqual(self.rows("sft.jsonl"), [])
        self.assertEqual(self.rows("dpo.jsonl"), [])
        self.assertEqual({r["reason"] for r in self.rows("rejected.jsonl")}, {"verified_outcome_missing"})

    def test_tool_exit_zero_is_not_a_full_trajectory_verifier(self):
        item = record(reviewed=False)
        item["evidence"] = [{"kind": "tool_result", "result": "passed", "exit_code": 0,
                             "record_digest": export.record_digest(item)}]
        self.run_export([item])
        self.assertEqual(self.rows("sft.jsonl"), [])

    def test_explicit_verifier_can_approve_and_conflicting_evidence_rejects(self):
        item = record()
        item["evidence"] = [{"kind": "verifier", "result": "passed", "verifier": "task-eval-v1",
                             "record_digest": export.record_digest(item)}]
        other = record("session-2")
        other["messages"][-1] = text("assistant", "a different answer")
        review(other)
        other["evidence"].append({"kind": "verifier", "result": "failed", "verifier": "task-eval-v1",
                                  "record_digest": export.record_digest(other)})
        self.run_export([item, other])
        self.assertEqual(len(self.rows("sft.jsonl")), 1)
        self.assertEqual(self.rows("rejected.jsonl")[0]["reason"], "negative_or_conflicting_evidence")

    def test_parallel_tools_preserve_ids_results_schemas_and_profile(self):
        item = tool_record()
        self.run_export([item])
        sample = self.rows("sft.jsonl")[0]
        self.assertEqual([m["role"] for m in sample["messages"]], ["user", "assistant", "tool", "tool", "assistant"])
        self.assertEqual(sample["messages"][1]["tool_calls"][0]["function"]["arguments"], {"path": "a.py"})
        self.assertEqual(sample["messages"][2]["tool_call_id"], "read-2")
        self.assertEqual(json.loads(sample["messages"][2]["content"]), {"lines": [1, 2]})
        self.assertEqual(sample["tools"], item["tools"])
        other = Path(self.temp.name) / "openai"
        manifest = export.export_dataset([item], other, profile="openai")
        sample = json.loads((other / "sft.jsonl").read_text())
        self.assertEqual(json.loads(sample["messages"][1]["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})
        self.assertEqual(manifest["formats"]["tool_arguments"], "json-string")

    def test_tool_schema_missing_and_orphan_missing_duplicate_results_reject(self):
        cases = []
        missing_schema = tool_record()
        missing_schema.pop("tools")
        cases.append((missing_schema, "tool_schema_missing"))
        missing_result = tool_record()
        missing_result["messages"][2]["content"].pop()
        cases.append((missing_result, "tool_results_missing"))
        orphan = tool_record()
        orphan["messages"][2]["content"][0]["tool_use_id"] = "never-called"
        cases.append((orphan, "orphan_or_duplicate_tool_result"))
        duplicate = tool_record()
        duplicate["messages"][1]["content"][1]["id"] = "read-1"
        cases.append((duplicate, "duplicate_tool_call_id"))
        for i, (item, expected) in enumerate(cases):
            with self.subTest(reason=expected):
                review(item)
                folder = Path(self.temp.name) / str(i)
                export.export_dataset([item], folder)
                self.assertEqual((folder / "sft.jsonl").read_text(), "")
                self.assertEqual(json.loads((folder / "rejected.jsonl").read_text())["reason"], expected)
                self.assertTrue((folder / "trajectories.jsonl").read_text())

    def test_codex_parallel_call_records_merge_before_reverse_order_results(self):
        item = tool_record()
        first, second = item["messages"][1]["content"]
        item["messages"][1:2] = [{"role": "assistant", "content": [first]},
                                   {"role": "assistant", "content": [second]}]
        item["agent"]["id"] = "codex"
        review(item)
        self.run_export([item])
        sample = self.rows("sft.jsonl")[0]
        self.assertEqual([call["id"] for call in sample["messages"][1]["tool_calls"]], ["read-1", "read-2"])
        self.assertEqual([m["tool_call_id"] for m in sample["messages"] if m["role"] == "tool"], ["read-2", "read-1"])
        self.assertEqual(len(self.rows("trajectories.jsonl")[0]["messages"]), 5)

    def test_custom_tools_and_omitted_images_survive_canonical_only(self):
        item = tool_record()
        item["messages"][1]["content"][0].update(format="custom", input="*** Begin Patch\n*** End Patch")
        review(item)
        image = record("image-session")
        image["messages"][0]["content"].append({"type": "image", "omitted": True})
        image["quality"].update(complete=False, issues=["media_omitted"])
        review(image)
        self.run_export([item, image])
        self.assertEqual(len(self.rows("trajectories.jsonl")), 2)
        self.assertEqual(self.rows("sft.jsonl"), [])
        self.assertIn("unsupported_custom_tool", [r["reason"] for r in self.rows("rejected.jsonl")])

    def test_partial_compacted_and_branched_context_cannot_train(self):
        examples = []
        for i, mutate in enumerate((lambda r: r["quality"].update(complete=False),
                                    lambda r: r["quality"].update(issues=["truncated_record"]),
                                    lambda r: r["messages"][0].update(sidechain=True),
                                    lambda r: r["messages"][0].update(compact_summary=True))):
            item = record(str(i))
            item["messages"][-1] = text("assistant", str(i))
            mutate(item)
            review(item)
            examples.append(item)
        self.run_export(examples)
        self.assertEqual(len(self.rows("trajectories.jsonl")), 4)
        self.assertEqual(self.rows("sft.jsonl"), [])

    def test_privacy_unknown_blocks_bad_json_and_empty_records_reject_without_content(self):
        secret = "private-secret-test-content"
        unmarked = record()
        unmarked.pop("privacy")
        unmarked["messages"][-1] = text("assistant", secret)
        unknown = record()
        unknown["messages"][-1]["content"] = [{"type": "thinking", "thinking": secret}]
        bad = record()
        bad["bad"] = float("nan")
        empty = record()
        empty["messages"] = []
        invalid_unicode = record()
        invalid_unicode["messages"][0] = text("user", "\ud800")
        self.run_export([unmarked, unknown, bad, empty, invalid_unicode, None, {}])
        self.assertEqual(self.rows("trajectories.jsonl"), [])
        self.assertEqual(len(self.rows("rejected.jsonl")), 7)
        for name in ("rejected.jsonl", "manifest.json"):
            self.assertNotIn(secret, (self.output / name).read_text())

    def test_explicit_preferences_only_with_digest_bound_human_choice(self):
        good = preference()
        stale = preference()
        stale["chosen"] = [text("assistant", "edited later")]
        unreviewed = preference()
        unreviewed["evidence"] = []
        self.run_export([record()], [good, stale, unreviewed])
        sample = self.rows("dpo.jsonl")[0]
        self.assertEqual(set(sample), {"prompt", "chosen", "rejected"})
        self.assertEqual(sample["prompt"], [{"role": "user", "content": "2+2等于多少？"}])
        self.assertEqual(sample["chosen"], [{"role": "assistant", "content": "4"}])
        self.assertEqual(sample["rejected"], [{"role": "assistant", "content": "5"}])
        self.assertEqual(len(self.rows("dpo.jsonl")), 1)
        self.assertEqual(len(self.rows("rejected.jsonl")), 2)

    def test_dpo_rejects_prompt_in_completion_and_identical_choices(self):
        different_context = preference()
        different_context["chosen"].insert(0, text("user", "a new prompt"))
        identical = preference()
        identical["rejected"] = copy.deepcopy(identical["chosen"])
        self.run_export(preferences=[different_context, identical])
        self.assertEqual(self.rows("dpo.jsonl"), [])
        self.assertEqual(len(self.rows("rejected.jsonl")), 2)

    def test_duplicate_content_does_not_hide_a_later_review(self):
        unreviewed, reviewed = record(reviewed=False), record("later-id")
        self.run_export([unreviewed, reviewed, reviewed])
        self.assertEqual(len(self.rows("trajectories.jsonl")), 2)
        self.assertEqual(len(self.rows("sft.jsonl")), 1)
        self.assertIn("duplicate_sft", [r["reason"] for r in self.rows("rejected.jsonl")])

    def test_manifest_hashes_lineage_permissions_and_existing_directory_guard(self):
        source = record()
        pair = preference()
        pair["prompt"] = copy.deepcopy(source["messages"][:-1])
        pair["provenance"] = {"source_record_id": "session-1"}
        pair["evidence"][0]["record_digest"] = export.preference_digest(pair)
        manifest = self.run_export([source], [pair])
        for name, info in manifest["files"].items():
            data = (self.output / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
            self.assertEqual(len(data), info["bytes"])
            self.assertEqual(len(data.splitlines()), info["records"])
            self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.output / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual({r["format"] for r in manifest["lineage"]}, {"trajectory", "preference", "sft", "dpo"})
        dpo_lineage = next(r for r in manifest["lineage"] if r["format"] == "dpo")
        self.assertEqual(dpo_lineage["provenance"]["source_record_id"], pair["provenance"]["source_record_id"])
        with self.assertRaises(FileExistsError):
            self.run_export([record()])

    def test_training_dedup_ignores_source_message_ids_and_timestamps(self):
        first, second = record("first-session"), record("second-session")
        for index, item in enumerate((first, second)):
            for n, message in enumerate(item["messages"]):
                message.update(id=f"source-{index}-{n}", ts=f"2026-11-{index + 1}")
            review(item)
        first_pair, second_pair = preference(), preference()
        second_pair["id"] = "other-preference"
        second_pair["prompt"][0].update(id="different-source-message", ts="2026-11-02")
        second_pair["evidence"][0]["record_digest"] = export.preference_digest(second_pair)
        self.run_export([first, second], [first_pair, second_pair])
        self.assertNotEqual(export.record_digest(first), export.record_digest(second))
        self.assertEqual(len(self.rows("trajectories.jsonl")), 2)
        self.assertEqual(len(self.rows("sft.jsonl")), 1)
        self.assertEqual(len(self.rows("dpo.jsonl")), 1)
        self.assertEqual({r["reason"] for r in self.rows("rejected.jsonl")}, {"duplicate_sft", "duplicate_preference"})

    def test_input_failure_leaves_no_complete_manifest(self):
        def broken():
            yield record()
            raise RuntimeError("synthetic input failure")
        with self.assertRaises(RuntimeError):
            self.run_export(broken())
        self.assertFalse((self.output / "manifest.json").exists())

    def test_cross_record_negative_evidence_blocks_prior_acceptance_in_both_orders(self):
        accepted = record()
        denied = copy.deepcopy(accepted)
        denied["evidence"][0]["result"] = "rejected"
        for i, records in enumerate(([accepted, denied], [denied, accepted])):
            folder = Path(self.temp.name) / str(i)
            manifest = export.export_dataset(iter(records), folder)
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
            self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 2)
            self.assertIn("sft:cross_record_evidence_conflict", manifest["rejections"])

    def test_same_training_sample_with_different_source_ids_and_low_quality_negative_blocks_both_orders(self):
        accepted, denied = record("source-one"), record("source-two")
        accepted["messages"][0].update(id="one", ts="2026-11-15")
        denied["messages"][0].update(id="two", ts="2026-11-16")
        review(accepted)
        review(denied)
        denied["evidence"][0]["result"] = "rejected"
        denied["quality"]["complete"] = False
        self.assertNotEqual(export.record_digest(accepted), export.record_digest(denied))
        for i, records in enumerate(([accepted, denied], [denied, accepted])):
            folder = Path(self.temp.name) / str(i)
            manifest = export.export_dataset(iter(records), folder)
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
            self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 2)
            self.assertIn("sft:cross_record_evidence_conflict", manifest["rejections"])

    def test_reverse_preferences_are_both_quarantined_and_annotations_preserved(self):
        first = preference()
        second = copy.deepcopy(first)
        second["id"] = "reverse-preference"
        second["chosen"], second["rejected"] = second["rejected"], second["chosen"]
        second["evidence"][0]["record_digest"] = export.preference_digest(second)
        manifest = self.run_export(preferences=[first, second])
        self.assertEqual(self.rows("dpo.jsonl"), [])
        self.assertEqual(self.rows("preferences.jsonl"), [{**first, "schema": "nous.preference/2"}, {**second, "schema": "nous.preference/2"}])
        self.assertEqual(manifest["rejections"]["dpo:conflicting_preference_direction"], 2)

    def test_invalid_unsupported_schema_and_wrong_arguments_stay_canonical(self):
        mutations = [lambda r: r["tools"][0]["function"]["parameters"]["properties"].update(path=17),
                     lambda r: r["tools"][0]["function"]["parameters"].update({"$ref": "#/$defs/test"}),
                     lambda r: r["messages"][1]["content"][0].update(input={"path": 123})]
        for i, change in enumerate(mutations):
            item = tool_record()
            change(item)
            review(item)
            folder = Path(self.temp.name) / str(i)
            manifest = export.export_dataset([item], folder)
            self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
            self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
            self.assertTrue(manifest["rejections"])

    def test_tool_result_error_flag_is_preserved_in_training_content(self):
        item = tool_record()
        item["messages"][2]["content"][0]["is_error"] = True
        review(item)
        self.run_export([item])
        result = json.loads(self.rows("sft.jsonl")[0]["messages"][2]["content"])
        self.assertEqual(result, {"is_error": True, "content": {"lines": [1, 2]}})

    def test_preferences_sidecar_reproduces_evidence_digest_and_sample_lineage(self):
        pair = preference()
        pair["prompt"][0].update(id="observed-message", ts="2026-11-16")
        pair["evidence"][0]["record_digest"] = export.preference_digest(pair)
        manifest = self.run_export(preferences=[pair])
        line = next(x for x in manifest["lineage"] if x["format"] == "dpo")
        self.assertEqual(line["source_file"], "preferences.jsonl")
        self.assertEqual(line["source_line"], 1)
        self.assertEqual(line["record_digest"], export.preference_digest(self.rows("preferences.jsonl")[0]))
        self.assertEqual(line["sample_digest"], export._digest(self.rows("dpo.jsonl")[0]))


class SupportedToolSchema(unittest.TestCase):
    def test_supported_constraints_and_nullable_compositions(self):
        from nous_training_schema import validate_value, SchemaError
        schema = {"type": "object", "required": ["items", "count", "tag"], "additionalProperties": False,
                  "properties": {
                      "items": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 3, "uniqueItems": True},
                      "count": {"allOf": [{"type": "number", "minimum": 0}, {"exclusiveMaximum": 3}], "multipleOf": .5},
                      "tag": {"anyOf": [{"type": "null"}, {"type": "string", "minLength": 2, "maxLength": 3, "pattern": "^[a-z]+$"}]}}}
        good = {"items": [1, 2], "count": 1.5, "tag": None}
        validate_value(good, schema)
        for value in ({**good, "items": [1, 1]}, {**good, "count": 3}, {**good, "count": True},
                      {**good, "tag": "ABC"}, {**good, "tag": "a"}, {**good, "extra": 1}):
            with self.subTest(value=value), self.assertRaises(SchemaError):
                validate_value(value, schema)
        validate_value("ok", {"oneOf": [{"const": "ok"}, {"type": "null"}]})
        with self.assertRaises(SchemaError):
            validate_value(1, {"oneOf": [{"type": "number"}, {"type": "integer"}]})

    def test_invalid_schema_shapes_and_unknown_constraints_fail_closed(self):
        from nous_training_schema import validate_schema, SchemaError
        for schema in ({"type": "madeup"}, {"required": "x"}, {"properties": {"x": 17}},
                       {"minimum": True}, {"minLength": -1}, {"pattern": "["}, {"items": []},
                       {"enum": []}, {"oneOf": []}, {"format": "email"}, {"$ref": "other.json"}):
            with self.subTest(schema=schema), self.assertRaises(SchemaError):
                validate_schema(schema)


if __name__ == "__main__":
    unittest.main()
