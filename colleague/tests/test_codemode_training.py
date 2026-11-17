"""Synthetic next-assistant event targets and fail-closed serialization."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
import nous_codemode_training as training
from nous_codemode_protocol import parse_protocol


def record():
    protocol = parse_protocol(ROOT / "examples/training/codemode.jsonl")
    return {"schema": "nous.trajectory/2", "id": "synthetic",
            "messages": [], "protocol": protocol, "quality": copy.deepcopy(protocol["quality"]),
            "privacy": {"scrubbed": True, "scrubber_version": "synthetic"}}


def payload(item, kind):
    return next(event["payload"] for event in item["protocol"]["events"] if event["type"] == kind)


def renumber(item):
    for index, event in enumerate(item["protocol"]["events"]):
        event["seq"] = index + 1


class CodeModeTraining(unittest.TestCase):
    def rejected(self, item, reason=None):
        with self.assertRaises(training.CodeModeError) as caught:
            training.samples(item)
        if reason:
            self.assertEqual(str(caught.exception), reason)

    def test_two_assistant_runs_preserve_custom_code_and_boundaries(self):
        item = record()
        result = training.samples(item)
        self.assertEqual([t for _, t in result], [{"start": 4, "end": 6}, {"start": 9, "end": 10}])
        for sample, target in result:
            self.assertEqual(training.sample_for(item, target), sample)
            self.assertTrue(training.validate_sample(sample))
        first, final = result[0][0], result[1][0]
        self.assertEqual(json.loads(first["completion"].splitlines()[1])["input"],
                         payload(item, "custom_tool_call")["input"])
        self.assertNotIn("custom_tool_call_output", first["prompt"] + first["completion"])
        self.assertIn("custom_tool_call_output", final["prompt"])
        self.assertNotIn("custom_tool_call_output", final["completion"])
        self.assertIn('"phase":"final_answer"', final["completion"])

    def test_runtime_and_usage_never_appear_as_model_input(self):
        for sample, _ in training.samples(record()):
            text = sample["prompt"] + sample["completion"]
            for marker in ("execution-fixture", "McpToolCall", "readOnlyHint", "input_tokens", '"seq":'):
                self.assertNotIn(marker, text)

    def test_code_unicode_line_separators_are_not_event_boundaries(self):
        item = record()
        code = 'text("line\u2028two\u2029three");\n'
        payload(item, "custom_tool_call")["input"] = code
        sample = training.samples(item)[0][0]
        self.assertEqual(json.loads(sample["completion"].split("\n")[1])["input"], code)
        self.assertTrue(training.validate_sample(sample))

    def test_output_error_semantics_and_structured_blocks_preserved(self):
        item = record()
        output = payload(item, "custom_tool_call_output")
        output["output"] = '{"is_error":true,"content":{"code":2}}'
        sample = training.samples(item)[1][0]
        rows = [json.loads(line) for line in sample["prompt"].split("\n")[:-1]]
        self.assertEqual(next(row["output"] for row in rows if row.get("type") == "custom_tool_call_output"), output["output"])

    def test_parallel_calls_accept_reversed_results(self):
        item = record()
        events = item["protocol"]["events"]
        second_call = copy.deepcopy(events[5])
        second_call["payload"].update(id="ctc-two", call_id="call-two")
        second_result = copy.deepcopy(events[7])
        second_result["payload"].update(id="ctco-two", call_id="call-two")
        events.insert(6, second_call)
        events.insert(8, second_result)
        renumber(item)
        rows = training.samples(item)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], {"start": 4, "end": 7})

    def test_empty_native_annotations_preserved(self):
        item = record()
        item["protocol"]["events"][-1]["payload"]["content"][0].update(annotations=[], logprobs=[])
        self.assertIn('"annotations":[]', training.samples(item)[-1][0]["completion"])

    def test_malformed_sample_always_raises_static_error(self):
        valid = training.samples(record())[0][0]
        for bad in ({"type": []}, {"type": "message", "role": "assistant", "content": [{"type": [], "text": "PRIVATE"}]}):
            sample = {**valid, "completion": training._json(bad) + "\n"}
            with self.assertRaises(training.CodeModeError) as caught:
                training.validate_sample(sample)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_quality_gaps_block_even_with_valid_structure(self):
        item = record()
        item["quality"]["missing"] = ["tool_definitions"]
        self.rejected(item, "codemode_quality_incomplete")
        self.assertEqual(len(training.samples(item, require_quality=False)), 2)

    def test_incomplete_negative_final_still_blocks_earlier_assistant_target(self):
        positive = record()
        negative = copy.deepcopy(positive)
        negative["protocol"]["events"][-1]["payload"]["phase"] = "commentary"
        negative["quality"].update(complete=False, issues=["incomplete_final"])
        negative["protocol"]["quality"].update(complete=False, issues=["incomplete_final"])
        self.rejected(negative)
        self.assertEqual(training.samples(positive)[0], training.negative_samples(negative)[0])

    def test_unknown_negative_suffix_keeps_earlier_targets_but_no_splicing(self):
        positive = record()
        negative = copy.deepcopy(positive)
        negative["protocol"]["events"].insert(6, {"seq": 7, "type": "unknown_visible", "payload": {"type": "unknown_visible"}})
        renumber(negative)
        negative["quality"].update(complete=False, issues=["unknown_response_item"])
        negative["protocol"]["quality"].update(complete=False, issues=["unknown_response_item"])
        keys = training.negative_samples(negative)
        self.assertEqual(keys, [training.samples(positive)[0]])
        self.rejected(negative)

    def test_negative_missing_result_keeps_observed_call_target_only(self):
        positive = record()
        negative = copy.deepcopy(positive)
        negative["protocol"]["events"] = [e for e in negative["protocol"]["events"] if e["type"] != "custom_tool_call_output"]
        negative["quality"].update(complete=False, issues=["pending_tool_calls"])
        negative["protocol"]["quality"].update(complete=False, issues=["pending_tool_calls"])
        self.assertEqual(training.samples(negative, require_quality=False), [training.samples(positive)[0]])
        self.rejected(negative)

    def test_protocol_quality_checked_independently(self):
        item = record()
        item["protocol"]["quality"].update(complete=False, issues=["media_omitted"])
        self.rejected(item, "codemode_quality_incomplete")

    def test_privacy_marker_required(self):
        item = record()
        item["privacy"] = {}
        self.rejected(item, "codemode_privacy_not_scrubbed")

    def test_base_cannot_be_invented_or_missing(self):
        item = record()
        item["protocol"]["events"].pop(0)
        self.rejected(item, "codemode_context_missing")

    def test_world_state_always_blocks(self):
        item = record()
        item["protocol"]["events"].insert(1, {"seq": 1, "type": "world_state", "payload": {"full": True, "state": {}}})
        renumber(item)
        self.rejected(item, "codemode_unresolved_runtime_context")

    def test_catalog_must_precede_call(self):
        item = record()
        catalog = item["protocol"]["events"].pop(1)
        item["protocol"]["events"].insert(6, catalog)
        renumber(item)
        self.rejected(item, "codemode_tool_contract_missing")

    def test_unsupported_grammar_is_not_treated_as_plain_text(self):
        item = record()
        payload(item, "additional_tools")["tools"][0]["tools"][0]["format"]["definition"] = "start: /.+/"
        self.rejected(item, "codemode_unsupported_grammar")

    def test_ambiguous_short_name_across_namespaces(self):
        item = record()
        catalog = payload(item, "additional_tools")["tools"]
        other = copy.deepcopy(catalog[0])
        other["name"] = "another"
        catalog.append(other)
        self.rejected(item, "codemode_tool_contract_ambiguous")

    def test_fully_qualified_name_resolves_unique_namespace(self):
        item = record()
        catalog = payload(item, "additional_tools")["tools"]
        other = copy.deepcopy(catalog[0])
        other["name"] = "another"
        catalog.append(other)
        payload(item, "custom_tool_call")["name"] = "functions.exec"
        self.assertEqual(len(training.samples(item)), 2)

    def test_duplicate_contract_refused(self):
        item = record()
        catalog = payload(item, "additional_tools")["tools"]
        catalog.append(copy.deepcopy(catalog[0]))
        self.rejected(item, "codemode_duplicate_contract")

    def test_missing_result_refused(self):
        item = record()
        item["protocol"]["events"] = [e for e in item["protocol"]["events"] if e["type"] != "custom_tool_call_output"]
        self.rejected(item, "codemode_pending_tool_call")

    def test_final_commentary_not_final_answer(self):
        item = record()
        item["protocol"]["events"][-1]["payload"]["phase"] = "commentary"
        self.rejected(item, "codemode_final_response_missing")

    def test_function_call_uses_true_schema_and_stays_native(self):
        item = record()
        leaf = payload(item, "additional_tools")["tools"][0]["tools"][0]
        leaf.clear()
        leaf.update(type="function", name="lookup", parameters={"type": "object", "properties": {"key": {"const": "beta"}}, "required": ["key"], "additionalProperties": False})
        for event in item["protocol"]["events"]:
            if event["type"] == "custom_tool_call":
                event["type"] = event["payload"]["type"] = "function_call"
                event["payload"].pop("input")
                event["payload"].update(name="lookup", arguments='{"key":"beta"}')
            elif event["type"] == "custom_tool_call_output":
                event["type"] = event["payload"]["type"] = "function_call_output"
        self.assertIn('"arguments":"{\\"key\\":\\"beta\\"}"', training.samples(item)[0][0]["completion"])
        payload(item, "function_call")["arguments"] = '{"key":"wrong"}'
        self.rejected(item)

    def test_target_cannot_select_tool_result_or_half_assistant_run(self):
        item = record()
        for target in ({"start": 7, "end": 8}, {"start": 4, "end": 5}, {"start": True, "end": 6}):
            with self.assertRaises(training.CodeModeError):
                training.sample_for(item, target)

    def test_standalone_sample_rejects_tool_output_as_target(self):
        sample = training.samples(record())[0][0]
        sample["completion"] = training._json(payload(record(), "custom_tool_call_output")) + "\n"
        with self.assertRaisesRegex(training.CodeModeError, "codemode_nonassistant_target"):
            training.validate_sample(sample)

    def test_unknown_fields_and_noncanonical_serialization_rejected(self):
        sample = training.samples(record())[0][0]
        sample["metadata"] = {}
        with self.assertRaises(training.CodeModeError):
            training.validate_sample(sample)
        sample.pop("metadata")
        sample["prompt"] = " " + sample["prompt"]
        with self.assertRaisesRegex(training.CodeModeError, "codemode_noncanonical_serialization"):
            training.validate_sample(sample)

    def test_size_and_target_caps_reject_without_truncation(self):
        for constant in ("MAX_PROTOCOL_BYTES", "MAX_EVENTS", "MAX_SAMPLE_BYTES", "MAX_TOTAL_BYTES", "MAX_TARGETS"):
            with self.subTest(constant=constant), mock.patch.object(training, constant, 1):
                self.rejected(record())

    def test_unknown_protocol_version_rejected(self):
        item = record()
        item["protocol"]["adapter_version"] = "invented"
        self.rejected(item, "codemode_invalid_protocol_shape")

    def test_smoke_help_needs_no_training_dependencies(self):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/training_trainer_smoke.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--dataset", result.stdout)

    def test_smoke_nonisolated_startup_rejected(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/training_trainer_smoke.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("isolated_startup_required", result.stderr)


if __name__ == "__main__":
    unittest.main()
