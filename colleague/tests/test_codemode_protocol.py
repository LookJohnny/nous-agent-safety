"""Protocol tests use only hand-written synthetic content and throwaway files."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import nous_codemode_protocol as protocol
import nous_transcript as transcript


def item(kind, **values):
    return {"type": "response_item", "payload": {"type": kind, **values}}


def message(role, text="synthetic", **extra):
    return item("message", role=role, content=[{"type": "output_text" if role == "assistant" else "input_text", "text": text}], **extra)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fixture.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, rows, tail=""):
        self.path.write_text("".join(json.dumps(x) + "\n" for x in rows) + tail)
        return protocol.parse_protocol(self.path)

    def fixture(self):
        return protocol.parse_protocol(Path(__file__).parent.parent / "examples/training/codemode.jsonl")

    def test_bytes_parser_matches_path_and_shares_snapshot_with_message_adapter(self):
        source = Path(__file__).parent.parent / "examples/training/codemode.jsonl"
        data = transcript.read_transcript_bytes(source)
        expected = protocol.parse_protocol(source)
        with mock.patch.object(transcript.os, "open", side_effect=AssertionError("bytes parser must not open")):
            parsed = protocol.parse_protocol_bytes(data)
            messages = transcript.parse_transcript_bytes(data)
        self.assertEqual(parsed, expected)
        self.assertEqual(messages["messages"][-1]["content"][0]["text"],
                         parsed["events"][-1]["payload"]["content"][0]["text"])

    def test_oversized_suffix_discards_all_prior_events(self):
        good = json.dumps(message("assistant", phase="final_answer")).encode() + b"\n"
        data = good + b"x" * (len(good) + 1)
        self.path.write_bytes(data)
        with mock.patch.object(protocol, "MAX_LINE_BYTES", len(good)):
            for result in (protocol.parse_protocol_bytes(data), protocol.parse_protocol(self.path)):
                self.assertEqual(result["events"], [])
                self.assertFalse(result["quality"]["complete"])
                self.assertIn("protocol_size_limit", result["quality"]["issues"])
                self.assertIn("transcript_line_too_large", result["quality"]["issues"])
        with mock.patch.object(protocol, "MAX_BYTES", len(good)):
            result = protocol.parse_protocol_bytes(good + good)
        self.assertEqual(result["events"], [])
        self.assertIn("transcript_too_large", result["quality"]["issues"])

    def test_invalid_bytes_and_missing_file_return_static_empty_observation(self):
        for value in ("SYNTHETIC_PRIVATE", bytearray(b"{}"), None):
            result = protocol.parse_protocol_bytes(value)
            self.assertEqual(result["events"], [])
            self.assertIn("invalid_transcript_bytes", result["quality"]["issues"])
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        result = protocol.parse_protocol(self.path)
        self.assertEqual(result["events"], [])
        self.assertIn("transcript_unreadable", result["quality"]["issues"])
        self.assertNotIn(str(self.path), json.dumps(result))

    def test_physical_line_limit_discards_valid_protocol_prefix(self):
        data = json.dumps(message("assistant", phase="final_answer")).encode() + b"\n\n\n"
        self.path.write_bytes(data)
        with mock.patch.object(transcript, "MAX_LINES", 2):
            for result in (protocol.parse_protocol_bytes(data), protocol.parse_protocol(self.path)):
                self.assertEqual(result["events"], [])
                self.assertFalse(result["quality"]["complete"])
                self.assertIn("transcript_too_many_lines", result["quality"]["issues"])
                self.assertIn("protocol_size_limit", result["quality"]["issues"])

    def test_protocol_path_rejects_fifo_and_symlink_without_reading(self):
        target = self.path.with_name("target")
        target.write_bytes(b"{}\n")
        self.path.symlink_to(target)
        fifo = self.path.with_name("fifo")
        os.mkfifo(fifo)
        with mock.patch.object(transcript.os, "open", side_effect=AssertionError("must not open nonregular")):
            for source in (self.path, fifo):
                result = protocol.parse_protocol(source)
                self.assertEqual(result["events"], [])
                self.assertIn("transcript_must_be_regular_file", result["quality"]["issues"])

    def test_protocol_path_change_failure_keeps_no_prefix(self):
        with mock.patch.object(protocol, "read_transcript_bytes",
                               side_effect=transcript.TranscriptReadError("transcript_changed_during_read")):
            result = protocol.parse_protocol(self.path)
        self.assertEqual(result["events"], [])
        self.assertFalse(result["quality"]["complete"])
        self.assertIn("transcript_changed_during_read", result["quality"]["issues"])

    def test_handwritten_fixture_order_contract_and_output_blocks(self):
        parsed = self.fixture()
        protocol.validate_protocol_shape(parsed)
        self.assertTrue(parsed["quality"]["complete"])
        self.assertEqual(parsed["quality"]["missing"], [])
        self.assertEqual([e["seq"] for e in parsed["events"]], list(range(1, 11)))
        catalog = parsed["events"][1]["payload"]["tools"][0]
        self.assertEqual(catalog["name"], "functions")
        self.assertEqual(catalog["tools"][0]["format"]["definition"], protocol.KNOWN_EXEC_GRAMMAR)
        call, output = parsed["events"][5]["payload"], parsed["events"][7]["payload"]
        self.assertEqual(call["name"], "exec")
        self.assertIn("tools.mcp__fixture__lookup", call["input"])
        self.assertEqual(len(output["output"]), 2)
        self.assertEqual(output["call_id"], call["call_id"])
        self.assertFalse(parsed["events"][6]["model_visible"])

    def test_observed_exec_grammar_digest(self):
        self.assertEqual(protocol.KNOWN_EXEC_GRAMMAR_SHA256, "8eead048e20069c17fcd0b6fe5ee9ed29364868723f8e08889de146a2b4b7a8e")
        self.assertEqual(hashlib.sha256(protocol.KNOWN_EXEC_GRAMMAR.encode()).hexdigest(), protocol.KNOWN_EXEC_GRAMMAR_SHA256)

    def test_hidden_analysis_reasoning_and_encrypted_fields_removed(self):
        rows = [item("reasoning", summary=[{"text": "HIDDEN_ONE"}], encrypted_content="HIDDEN_TWO"),
                message("assistant", "HIDDEN_THREE", channel="analysis"),
                item("message", role="assistant", content=[{"type": "thinking", "text": "HIDDEN_FOUR"},
                                                           {"type": "output_text", "text": "visible"}], encrypted_content="HIDDEN_FIVE")]
        parsed = self.parse(rows)
        self.assertNotIn("HIDDEN", json.dumps(parsed))
        self.assertEqual([e["seq"] for e in parsed["events"]], [3])
        protocol.validate_protocol_shape(parsed)

    def test_duplicate_json_keys_rejected_at_all_depths(self):
        tail = '{"type":"response_item","payload":{"type":"message","role":"user","role":"assistant","content":[]}}\n'
        parsed = self.parse([message("assistant")], tail)
        self.assertFalse(parsed["quality"]["complete"])
        self.assertIn("duplicate_json_key", parsed["quality"]["issues"])
        self.assertEqual(len(parsed["events"]), 1)

    def test_unknown_visible_raw_not_retained(self):
        parsed = self.parse([item("future_context", secret="DO_NOT_RETAIN"), message("assistant")])
        self.assertIn("unknown_response_item", parsed["quality"]["issues"])
        self.assertNotIn("DO_NOT_RETAIN", json.dumps(parsed))
        protocol.validate_protocol_shape(parsed)

    def test_unknown_fields_and_media_omitted_with_gap(self):
        parsed = self.parse([item("message", role="user", sensitive_new_field="OMIT_ME", content=[
            {"type": "input_image", "image_url": "OMIT_ME"}, {"type": "input_text", "text": "visible"}]), message("assistant")])
        self.assertNotIn("OMIT_ME", json.dumps(parsed))
        self.assertIn("unsupported_content", parsed["quality"]["issues"])
        self.assertIn("unsupported_event_fields", parsed["quality"]["issues"])

    def test_base_instructions_explicit_source_without_identity(self):
        parsed = self.parse([{"type": "session_meta", "payload": {"cwd": "PRIVATE_PATH", "creator_user_id": "PRIVATE_ID",
            "base_instructions": {"text": "Synthetic base.", "provenance": {"private": "PRIVATE_SOURCE"}}}}, message("assistant")])
        self.assertEqual(parsed["events"][0]["payload"], {"text": "Synthetic base.", "source": "session_meta.base_instructions.text"})
        self.assertNotIn("PRIVATE", json.dumps(parsed))
        self.assertNotIn("system_prompt", parsed["quality"]["missing"])

    def test_world_snapshot_preserves_refs_but_is_not_trainable(self):
        parsed = self.parse([{"type": "world_state", "payload": {"full": True, "state": {
            "model": {"model": "fixture-model", "instructions": "a" * 40},
            "tools": {"mcp__fixture": ""}, "top_level_tools": {"functions.exec": "b" * 40},
            "environments": {"cwd": "PRIVATE_PATH"}, "unknown": {"secret": "PRIVATE_UNKNOWN"}}}}, message("assistant")])
        self.assertFalse(parsed["quality"]["complete"])
        self.assertIn("unresolved_world_state", parsed["quality"]["issues"])
        self.assertNotIn("PRIVATE", json.dumps(parsed))
        self.assertEqual(parsed["events"][0]["payload"]["state"]["model"]["instructions"], "a" * 40)
        protocol.validate_protocol_shape(parsed)

    def test_configuration_effort_is_not_hidden_reasoning(self):
        parsed = self.parse([message("user"), item("configuration_update", reasoning={"effort": "low"}), message("assistant")])
        self.assertEqual(parsed["events"][1]["payload"], {"type": "configuration_update", "reasoning": {"effort": "low"}})
        self.assertEqual([e["seq"] for e in parsed["events"]], [1, 2, 3])
        bad = self.parse([item("configuration_update", reasoning={"effort": "low", "text": "HIDDEN"}), message("assistant")])
        self.assertNotIn("HIDDEN", json.dumps(bad))
        self.assertIn("unsupported_configuration_update", bad["quality"]["issues"])

    def test_function_native_contract_call_and_output_preserved(self):
        contract = {"type": "function", "name": "lookup", "description": "Toy.", "strict": False,
                    "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}
        parsed = self.parse([item("additional_tools", role="developer", tools=[contract]),
                             item("function_call", name="lookup", call_id="call-x", arguments='{"key":"beta"}'),
                             item("function_call_output", call_id="call-x", output="37"), message("assistant")])
        self.assertEqual(parsed["events"][0]["payload"]["tools"], [contract])
        self.assertTrue(parsed["quality"]["complete"])
        protocol.validate_protocol_shape(parsed)

    def test_duplicate_function_argument_keys_flagged(self):
        parsed = self.parse([item("function_call", name="toy", call_id="x", arguments='{"x":1,"x":2}'),
                             item("function_call_output", call_id="x", output="0"), message("assistant")])
        self.assertIn("invalid_function_arguments", parsed["quality"]["issues"])

    def test_tool_identity_and_pending_calls(self):
        parsed = self.parse([item("custom_tool_call", name="exec", call_id="one", input="text(1)"),
                             item("custom_tool_call_output", call_id="other", output="1"), message("assistant")])
        self.assertIn("pending_tool_calls", parsed["quality"]["issues"])
        self.assertIn("unmatched_tool_output", parsed["quality"]["issues"])

    def test_runtime_execution_separate_from_model_target(self):
        parsed = self.fixture()
        event = parsed["events"][6]
        self.assertEqual(event["type"], "runtime_tool_execution")
        self.assertIs(event["model_visible"], False)
        self.assertEqual(event["payload"]["arguments"], {"key": "beta"})
        self.assertFalse(event["payload"]["result"]["isError"])
        modified = copy.deepcopy(parsed)
        modified["events"][6]["model_visible"] = True
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_protocol_shape(modified)

    def test_usage_only_numeric_and_not_cumulative_sum(self):
        parsed = self.parse([{"type": "token_usage_record", "payload": {"thread_id": "PRIVATE_ID", "usage": {"input_tokens": 2},
                "turn_token_usage": {"input_tokens": 5}, "thread_token_usage": {"input_tokens": 9}}}, message("assistant")])
        self.assertNotIn("PRIVATE_ID", json.dumps(parsed))
        self.assertEqual(parsed["events"][0]["payload"]["usage"]["input_tokens"], 2)
        self.assertEqual(parsed["events"][0]["payload"]["turn_token_usage"]["input_tokens"], 5)
        protocol.validate_protocol_shape(parsed)

    def test_unsupported_and_malformed_never_copy_raw(self):
        parsed = self.parse([{"type": "future", "payload": {"private": "PRIVATE_RAW"}}, message("assistant")], '{"PRIVATE_TAIL":')
        self.assertNotIn("PRIVATE", json.dumps(parsed))
        self.assertIn("malformed_record", parsed["quality"]["issues"])
        self.assertIn("unknown_record_type", parsed["quality"]["issues"])

    def test_valid_final_line_without_newline_and_physical_seq_gaps(self):
        self.path.write_text('\n' + json.dumps(message("assistant")))
        parsed = protocol.parse_protocol(self.path)
        self.assertTrue(parsed["quality"]["complete"])
        self.assertEqual(parsed["events"][0]["seq"], 2)

    def test_size_limit_and_nan_fail_closed(self):
        with mock.patch.object(protocol, "MAX_LINE_BYTES", 8):
            parsed = self.parse([message("assistant")])
        self.assertIn("protocol_size_limit", parsed["quality"]["issues"])
        parsed = self.parse([], '{"type":"event_msg","payload":{"type":"token_count","number":NaN}}')
        self.assertIn("malformed_record", parsed["quality"]["issues"])

    def test_mirror_without_native_message_flags_gap(self):
        parsed = self.parse([{"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "missing", "content": [{"text": "MIRROR_PRIVATE"}]}}}, message("assistant")])
        self.assertIn("unmatched_completion_mirror", parsed["quality"]["issues"])
        self.assertNotIn("MIRROR_PRIVATE", json.dumps(parsed))

    def test_ui_user_mirror_id_can_differ_but_text_must_match(self):
        mirror = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "different-ui-id", "content": [{"type": "text", "text": "request", "text_elements": []}]}}}
        parsed = self.parse([message("user", "request", id="native-id"), mirror, message("assistant")])
        self.assertTrue(parsed["quality"]["complete"])
        mirror["payload"]["item"]["content"][0]["text"] = "other request"
        parsed = self.parse([message("user", "request"), mirror, message("assistant")])
        self.assertIn("unmatched_completion_mirror", parsed["quality"]["issues"])

    def test_empty_output_annotations_retained_nonempty_remain_unsupported(self):
        row = message("assistant")
        row["payload"]["content"][0].update(annotations=[], logprobs=[])
        parsed = self.parse([row])
        self.assertEqual(parsed["events"][0]["payload"]["content"], row["payload"]["content"])
        protocol.validate_protocol_shape(parsed)
        row["payload"]["content"][0]["annotations"] = [{"type": "new", "secret": "PRIVATE_ANNOTATION"}]
        parsed = self.parse([row])
        self.assertIn("unsupported_content_fields", parsed["quality"]["issues"])
        self.assertNotIn("PRIVATE_ANNOTATION", json.dumps(parsed))

    def test_closed_shape_rejects_injected_fields_and_seq_changes(self):
        original = self.fixture()
        changes = [lambda x: x.update(secret="PRIVATE"),
                   lambda x: x["events"][0].update(extra="PRIVATE"),
                   lambda x: x["events"][0]["payload"].update(extra="PRIVATE"),
                   lambda x: x["events"][1].update(seq=1),
                   lambda x: x["events"][1].update(seq=True),
                   lambda x: x["quality"].update(complete="yes"),
                   lambda x: x["events"][5]["payload"].update(encrypted_content="PRIVATE")]
        for mutate in changes:
            value = copy.deepcopy(original)
            mutate(value)
            with self.assertRaisesRegex(protocol.ProtocolError, "^invalid_protocol_shape$"):
                protocol.validate_protocol_shape(value)

    def test_final_commentary_incomplete(self):
        parsed = self.parse([message("assistant", phase="commentary")])
        self.assertIn("incomplete_final", parsed["quality"]["issues"])

    def runtime_rows(self, version=protocol.OBSERVED_RUNTIME_VERSION):
        return [{"type": "session_meta", "ordinal": 0,
                 "payload": {"cli_version": version, "base_instructions": {"text": "Synthetic base."}}},
                {**message("user", "synthetic request"), "ordinal": 1},
                {**message("assistant", phase="final_answer"), "ordinal": 2}]

    def test_exact_observed_runtime_accepts_storage_ordinal_without_model_event(self):
        rows = self.runtime_rows()
        parsed = self.parse(rows)
        self.assertEqual(parsed["adapter_version"], "codex-events-1.1")
        self.assertTrue(parsed["quality"]["complete"])
        self.assertEqual([e["seq"] for e in parsed["events"]], [1, 2, 3])
        self.assertNotIn('"ordinal"', json.dumps(parsed["events"]))
        protocol.validate_protocol_shape(parsed)
        rows[-1]["ordinal"] = 50  # Storage ordinals need not equal physical lines.
        self.assertTrue(self.parse(rows)["quality"]["complete"])

    def test_unknown_or_missing_runtime_does_not_whitelist_ordinal(self):
        for version in (None, "0.169.0-alpha.11.2", "0.170.0", "synthetic"):
            with self.subTest(version=version):
                parsed = self.parse(self.runtime_rows(version))
                self.assertFalse(parsed["quality"]["complete"])
                self.assertIn("unsupported_record_fields", parsed["quality"]["issues"])
        parsed = self.parse([{**message("assistant"), "ordinal": 1}])
        self.assertIn("unsupported_record_fields", parsed["quality"]["issues"])

    def test_bad_ordinal_type_range_and_order_fail_closed(self):
        for ordinal in (True, False, -1, 1 << 64, "2", None, 1.5):
            with self.subTest(ordinal=ordinal):
                rows = self.runtime_rows()
                rows[-1]["ordinal"] = ordinal
                parsed = self.parse(rows)
                self.assertFalse(parsed["quality"]["complete"])
                self.assertIn("invalid_record_ordinal", parsed["quality"]["issues"])
        for ordinal in (0, 1):
            rows = self.runtime_rows()
            rows[-1]["ordinal"] = ordinal
            self.assertIn("record_ordinal_order_violation", self.parse(rows)["quality"]["issues"])

    def test_nonempty_history_metadata_always_remains_a_quality_gap(self):
        rows = self.runtime_rows()
        rows[-1]["metadata"] = {"history_sequence": 4, "sampled_through_sequence": 3,
                                 "retained_source": {"revision": "PRIVATE_METADATA_BODY"}}
        parsed = self.parse(rows)
        self.assertFalse(parsed["quality"]["complete"])
        self.assertIn("unresolved_history_metadata", parsed["quality"]["issues"])
        self.assertIn("sampling_context", parsed["quality"]["missing"])
        self.assertNotIn("PRIVATE_METADATA_BODY", json.dumps(parsed))
        self.assertNotIn("unsupported_record_fields", parsed["quality"]["issues"])
        rows[-1]["metadata"]["future_visible_context"] = "PRIVATE_FUTURE_FIELD"
        parsed = self.parse(rows)
        self.assertIn("unsupported_history_metadata_fields", parsed["quality"]["issues"])
        self.assertNotIn("PRIVATE_FUTURE_FIELD", json.dumps(parsed))
        protocol.validate_protocol_shape(parsed)

    def test_empty_metadata_known_shape_only_and_invalid_metadata_are_not_copied(self):
        rows = self.runtime_rows()
        rows[-1]["metadata"] = {}
        self.assertTrue(self.parse(rows)["quality"]["complete"])
        for value in ([], None, "PRIVATE_METADATA"):
            rows[-1]["metadata"] = value
            parsed = self.parse(rows)
            self.assertIn("invalid_history_metadata", parsed["quality"]["issues"])
            self.assertNotIn("PRIVATE_METADATA", json.dumps(parsed))
        rows = self.runtime_rows()
        rows[0]["metadata"] = {}
        self.assertIn("invalid_history_metadata", self.parse(rows)["quality"]["issues"])

    def test_runtime_version_change_is_not_silently_adopted(self):
        rows = self.runtime_rows()
        rows.insert(2, {"type": "session_meta", "payload": {"cli_version": "changed"}})
        parsed = self.parse(rows)
        self.assertIn("runtime_version_changed", parsed["quality"]["issues"])

    def test_shape_validator_reads_old_adapter_and_rejects_unknown_versions(self):
        parsed = self.fixture()
        parsed["adapter_version"] = "codex-events-1"
        protocol.validate_protocol_shape(parsed)
        for version in ("codex-events-2", None, []):
            parsed["adapter_version"] = version
            with self.assertRaises(protocol.ProtocolError):
                protocol.validate_protocol_shape(parsed)


if __name__ == "__main__":
    unittest.main()
