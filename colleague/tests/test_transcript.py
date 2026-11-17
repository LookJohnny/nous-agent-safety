"""Synthetic fixtures only: transcript normalization and loss/accounting guarantees."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import nous_transcript as transcript
from nous_transcript import TranscriptReadError, parse_transcript, parse_transcript_bytes, read_transcript_bytes


def claude(role, content, ident=None, **extra):
    return {"type": role, "message": {"role": role, "id": ident, "content": content}, **extra}


def codex(kind, **fields):
    return {"type": "response_item", "payload": {"type": kind, **fields}}


class Transcript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "synthetic.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, tail=b""):
        with open(self.path, "wb") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")
            handle.write(tail)
        return self.path

    def test_claude_text_tools_and_hidden_content(self):
        rows = [claude("user", "检查文件", version="2.fixture"),
                claude("assistant", [{"type": "thinking", "thinking": "PRIVATE_REASONING"},
                                      {"type": "tool_use", "id": "c1", "name": "Read",
                                       "input": {"file_path": "app.py"}}], "m1"),
                claude("user", [{"type": "tool_result", "tool_use_id": "c1",
                                 "content": [{"type": "text", "text": "hello"}]}]),
                claude("assistant", "文件可读", "m2")]
        result = parse_transcript(self.write(rows))
        self.assertEqual(result["agent"], "claude")
        self.assertEqual(result["version"], "2.fixture")
        self.assertEqual(result["offset"], 4)
        self.assertTrue(result["quality"]["complete"])
        self.assertEqual(result["quality"]["missing"], ["system_prompt", "tool_definitions"])
        self.assertNotIn("PRIVATE_REASONING", json.dumps(result))
        self.assertEqual(result["messages"][2]["content"][0]["content"], "hello")

    def test_claude_same_message_id_keeps_distinct_blocks_and_deduplicates_snapshot(self):
        text = {"type": "text", "text": "I will inspect."}
        call = {"type": "tool_use", "id": "c1", "name": "Read", "input": {"file_path": "app.py"}}
        rows = [claude("assistant", [text], "m1", uuid="r1"),
                claude("assistant", [call], "m1", uuid="r2"),
                claude("assistant", [text, call], "m1", uuid="r3"),
                claude("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}])]
        result = parse_transcript(self.write(rows))
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual([b["type"] for b in result["messages"][0]["content"]], ["text", "tool_use"])
        self.assertTrue(result["quality"]["complete"])

    def test_same_text_is_not_a_duplicate_user_turn(self):
        result = parse_transcript(self.write([claude("user", "retry"), claude("user", "retry")]))
        self.assertEqual(len(result["messages"]), 2)

    def test_live_codex_exec_shape_preserves_code_and_quarantines_new_context(self):
        # Handwritten regression for the shape observed in the 0.169 alpha live
        # smoke; no private transcript or model reasoning is included here.
        program = 'text(await tools.mcp__fixture__lookup({key:"beta"}));'
        rows = [
            {"type": "session_meta", "payload": {"cli_version": "0.169.fixture"}},
            codex("additional_tools", role="developer", tools=[{"type": "namespace", "name": "functions", "tools": []}]),
            {"type": "world_state", "payload": {"full": True, "state": {"synthetic": True}}},
            codex("configuration_update", reasoning={"effort": "low"}),
            codex("message", role="user", content=[{"type": "input_text", "text": "Run the fixed lookup."}]),
            codex("custom_tool_call", call_id="exec-one", name="exec", input=program),
            {"type": "token_usage_record", "payload": {"usage": {"input_tokens": 1}}},
            codex("custom_tool_call_output", call_id="exec-one", output=[
                {"type": "input_text", "text": "Script completed"},
                {"type": "input_text", "text": '{"content":[{"type":"text","text":"37"}],"isError":false}'}]),
            codex("message", role="assistant", phase="final_answer", content=[{"type": "output_text", "text": "37"}]),
        ]
        parsed = parse_transcript(self.write(rows), agent="codex")
        self.assertFalse(parsed["quality"]["complete"])
        self.assertIn("unknown_response_item", parsed["quality"]["issues"])
        self.assertIn("unknown_record_type", parsed["quality"]["issues"])
        call = parsed["messages"][1]["content"][0]
        self.assertEqual((call["name"], call["format"], call["input"]), ("exec", "custom", program))
        self.assertIn('"isError":false', parsed["messages"][2]["content"][0]["content"])

        import nous_training_export as export
        from nous_training_validate import validate_dataset
        with self.assertRaisesRegex(export.InvalidRecord, "unsupported_custom_tool"):
            export.convert_messages(parsed["messages"])
        record = {"schema": "nous.trajectory/1", "id": "synthetic-live-shape", "messages": parsed["messages"],
                  "quality": parsed["quality"], "tools": [], "evidence": [],
                  "privacy": {"scrubbed": True, "scrubber_version": "synthetic-test"},
                  "provenance": {"source": "handwritten_regression"}}
        out = os.path.join(self.tmp.name, "candidate-dataset")
        manifest = export.export_dataset([record], out)
        self.assertEqual(manifest["files"]["trajectories.jsonl"]["records"], 1)
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)
        self.assertIn("sft:incomplete_trajectory", manifest["rejections"])
        self.assertTrue(validate_dataset(out)["valid"])

    def test_growing_text_snapshot_replaces_the_prior_prefix(self):
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                if source == "claude":
                    rows = [claude("assistant", [{"type": "text", "text": text}], "stable-id", uuid=f"row-{i}")
                            for i, text in enumerate(("Hello", "Hello world", "Hello world!"))]
                else:
                    rows = [codex("message", role="assistant", id="stable-id", content=[{"type": "output_text", "text": text}])
                            for text in ("Hello", "Hello world", "Hello world!")]
                result = parse_transcript(self.write(rows))
                self.assertTrue(result["quality"]["complete"])
                self.assertEqual(len(result["messages"]), 1)
                self.assertEqual(result["messages"][0]["content"], [{"type": "text", "text": "Hello world!"}])

    def test_growing_snapshot_retains_its_tool_blocks_and_result(self):
        call = {"type": "tool_use", "id": "c1", "name": "Read", "input": {"file_path": "app.py"}}
        rows = [claude("assistant", [{"type": "text", "text": "Inspect"}, call], "m1"),
                claude("assistant", [{"type": "text", "text": "Inspect the file."}, call], "m1"),
                claude("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}])]
        result = parse_transcript(self.write(rows))
        self.assertTrue(result["quality"]["complete"])
        blocks = result["messages"][0]["content"]
        self.assertEqual(blocks[0]["text"], "Inspect the file.")
        self.assertEqual([b["id"] for b in blocks if b["type"] == "tool_use"], ["c1"])

    def test_unanchored_text_delta_or_replacement_is_explicitly_ambiguous(self):
        for old, new in (("Hello", " world"), ("Hello world", "Hello"), ("Before", "After")):
            with self.subTest(old=old, new=new):
                rows = [claude("assistant", old, "stable"), claude("assistant", new, "stable")]
                result = parse_transcript(self.write(rows))
                self.assertFalse(result["quality"]["complete"])
                self.assertIn("ambiguous_message_update", result["quality"]["issues"])
                self.assertIn("streamed_content", result["quality"]["missing"])

    def test_text_after_an_exact_tool_overlap_has_known_order(self):
        call = {"type": "tool_use", "id": "c1", "name": "Read", "input": {}}
        rows = [claude("assistant", [{"type": "text", "text": "Before"}, call], "m1"),
                claude("assistant", [call, {"type": "text", "text": "After"}], "m1"),
                claude("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}])]
        result = parse_transcript(self.write(rows))
        self.assertTrue(result["quality"]["complete"])
        self.assertEqual([b["type"] for b in result["messages"][0]["content"]], ["text", "tool_use", "text"])

    def test_reordered_snapshot_does_not_silently_change_content_order(self):
        a, b = {"type": "text", "text": "first"}, {"type": "text", "text": "second"}
        rows = [claude("assistant", [a, b], "m1"), claude("assistant", [b, a], "m1")]
        result = parse_transcript(self.write(rows))
        self.assertFalse(result["quality"]["complete"])
        self.assertIn("ambiguous_message_update", result["quality"]["issues"])
        self.assertEqual(result["messages"][0]["content"], [a, b])

    def test_exact_duplicate_uuid_is_removed(self):
        row = claude("user", "retry", uuid="same-row")
        self.assertEqual(len(parse_transcript(self.write([row, row]))["messages"]), 1)

    def test_same_message_id_does_not_reorder_tool_result(self):
        rows = [claude("assistant", [{"type": "tool_use", "id": "c1", "name": "Read", "input": {}}], "m1"),
                claude("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]),
                claude("assistant", "done", "m1")]
        result = parse_transcript(self.write(rows))
        self.assertEqual([m["role"] for m in result["messages"]], ["assistant", "user", "assistant"])

    def test_codex_function_custom_calls_and_event_mirrors(self):
        rows = [{"type": "session_meta", "payload": {"cli_version": "fixture.1"}},
                {"type": "turn_context", "payload": {"model": "model-fixture"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "MIRROR"}},
                codex("message", role="user", content=[{"type": "input_text", "text": "fix"}]),
                codex("reasoning", summary=[{"type": "summary_text", "text": "PRIVATE_REASONING"}]),
                codex("message", role="assistant", channel="analysis", content=[{"type": "output_text", "text": "PRIVATE_ANALYSIS"}]),
                codex("function_call", name="exec_command", call_id="f1", arguments='{"cmd":"printf hello"}'),
                codex("function_call_output", call_id="f1", output={"exit_code": 0, "stdout": "hello"}),
                codex("custom_tool_call", name="apply_patch", call_id="p1", input="*** Begin Patch\n*** End Patch"),
                codex("custom_tool_call_output", call_id="p1", output="ok"),
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "MIRROR"}},
                codex("message", role="assistant", phase="final_answer", content=[{"type": "output_text", "text": "done"}])]
        result = parse_transcript(self.write(rows))
        self.assertEqual(result["agent"], "codex")
        self.assertEqual(result["version"], "fixture.1")
        self.assertEqual(result["model"], "model-fixture")
        self.assertEqual(len(result["messages"]), 6)
        self.assertEqual(result["messages"][1]["content"][0]["input"], {"cmd": "printf hello"})
        self.assertEqual(result["messages"][2]["content"][0]["content"], {"exit_code": 0, "stdout": "hello"})
        self.assertEqual(result["messages"][3]["content"][0]["format"], "custom")
        self.assertEqual(result["messages"][3]["content"][0]["input"], "*** Begin Patch\n*** End Patch")
        self.assertEqual(result["messages"][-1]["phase"], "final_answer")
        self.assertTrue(result["quality"]["complete"])
        self.assertNotIn("MIRROR", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_codex_duplicate_call_and_result_items_do_not_duplicate_tools(self):
        call = codex("function_call", call_id="c1", name="run", arguments="[]")
        result = codex("function_call_output", call_id="c1", output=False)
        parsed = parse_transcript(self.write([call, call, result, result]))
        self.assertEqual(len(parsed["messages"]), 2)
        self.assertEqual(parsed["messages"][0]["content"][0]["input"], [])
        self.assertIs(parsed["messages"][1]["content"][0]["content"], False)
        self.assertTrue(parsed["quality"]["complete"])

    def test_invalid_tool_arguments_are_retained_and_flagged(self):
        result = parse_transcript(self.write([codex("function_call", name="run", call_id="c1", arguments="{broken")]))
        self.assertEqual(result["messages"][0]["content"][0]["input"], "{broken")
        self.assertIn("invalid_tool_arguments", result["quality"]["issues"])
        self.assertIn("unresolved_tool_call", result["quality"]["issues"])

    def test_arbitrary_json_inputs_and_outputs_preserve_types(self):
        rows = []
        values = [None, False, 1, ["x"], {"x": "y"}, "a quoted string"]
        for index, value in enumerate(values):
            rows.extend([codex("function_call", name="fixture", call_id=str(index), arguments=json.dumps(value)),
                         codex("function_call_output", call_id=str(index), output=value)])
        result = parse_transcript(self.write(rows))
        for index, value in enumerate(values):
            self.assertEqual(result["messages"][index * 2]["content"][0]["input"], value)
            self.assertEqual(result["messages"][index * 2 + 1]["content"][0]["content"], value)

    def test_partial_tail_offset_allows_next_read_to_recover(self):
        self.write([claude("user", "one")], b'{"type":"assistant","message":')
        first = parse_transcript(self.path)
        self.assertEqual(first["offset"], 1)
        self.assertIn("partial_tail", first["quality"]["issues"])
        with open(self.path, "ab") as handle:
            handle.write(b'{"role":"assistant","content":"two"}}\n')
        second = parse_transcript(self.path, start=first["offset"])
        self.assertEqual(second["offset"], 2)
        self.assertEqual(second["messages"][0]["content"][0]["text"], "two")
        self.assertTrue(second["quality"]["complete"])

    def test_complete_final_json_without_newline_is_consumed(self):
        self.write([], json.dumps(claude("user", "complete")).encode())
        result = parse_transcript(self.path)
        self.assertEqual(result["offset"], 1)
        self.assertTrue(result["quality"]["complete"])

    def test_malformed_complete_line_is_accounted_for(self):
        self.write([claude("user", "valid")], b"{invalid}\n")
        result = parse_transcript(self.path)
        self.assertEqual(result["offset"], 2)
        self.assertIn("malformed_json", result["quality"]["issues"])
        self.assertFalse(result["quality"]["complete"])

    def test_invalid_utf8_is_not_silently_replaced(self):
        self.write([], b'{"type":"user","message":{"content":"\xff"}}\n')
        self.assertIn("invalid_utf8", parse_transcript(self.path)["quality"]["issues"])

    def test_slice_recovers_metadata_without_replaying_messages(self):
        rows = [{"type": "session_meta", "payload": {"cli_version": "test"}},
                {"type": "turn_context", "payload": {"model": "fixture"}},
                codex("message", role="user", content="old"),
                codex("message", role="assistant", content="new"),
                codex("message", role="user", content="later")]
        result = parse_transcript(self.write(rows), start=3, end=4)
        self.assertEqual(result["version"], "test")
        self.assertEqual(result["model"], "fixture")
        self.assertEqual(result["offset"], 4)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["content"][0]["text"], "new")

    def test_file_shrink_resets_offset_and_reports_context_loss(self):
        result = parse_transcript(self.write([claude("user", "new file")]), start=10)
        self.assertEqual(result["offset"], 1)
        self.assertEqual(len(result["messages"]), 1)
        self.assertIn("offset_reset", result["quality"]["issues"])

    def test_sidechain_compaction_media_and_unknown_content_are_explicit(self):
        rows = [claude("user", "summary", isCompactSummary=True),
                claude("assistant", [{"type": "image", "source": {"data": "PRIVATE_BASE64"}},
                                      {"type": "new_visible_format", "body": "unsupported"}],
                       "m1", isSidechain=True, agentId="fixture-subagent")]
        result = parse_transcript(self.write(rows))
        self.assertFalse(result["quality"]["complete"])
        self.assertTrue({"sidechain", "compacted", "media_omitted", "unknown_content_block"}
                        .issubset(result["quality"]["issues"]))
        self.assertNotIn("PRIVATE_BASE64", json.dumps(result))

    def test_compaction_records_are_not_fabricated_conversation(self):
        for marker in [{"type": "compacted", "payload": {"message": "summary"}},
                       {"type": "system", "subtype": "compact_boundary"}]:
            with self.subTest(marker=marker):
                result = parse_transcript(self.write([marker, claude("user", "next")]))
                self.assertEqual(len(result["messages"]), 1)
                self.assertIn("compacted", result["quality"]["issues"])

    def test_orphan_missing_and_duplicate_call_ids_are_reported(self):
        rows = [codex("function_call_output", call_id="orphan", output="x"),
                codex("function_call", name="run", arguments="{}"),
                codex("function_call", name="run", call_id="same", arguments="{}"),
                codex("function_call", name="run", call_id="same", arguments='{"different":true}'),
                codex("function_call_output", call_id="same", output="ok")]
        result = parse_transcript(self.write(rows))
        self.assertTrue({"orphan_tool_result", "missing_tool_call_id", "duplicate_tool_call"}
                        .issubset(result["quality"]["issues"]))

    def test_unknown_response_item_is_not_silently_approved(self):
        result = parse_transcript(self.write([codex("future_visible_action", content="x")]))
        self.assertIn("unknown_response_item", result["quality"]["issues"])

    def test_explicit_partial_answer_is_not_a_completed_trajectory(self):
        for fields in ({"phase": "commentary"}, {"phase": "partial_answer"}, {"channel": "commentary"}):
            with self.subTest(fields=fields):
                rows = [codex("message", role="user", content="inspect"),
                        codex("message", role="assistant", content="Checking now.", **fields)]
                partial = parse_transcript(self.write(rows))
                self.assertFalse(partial["quality"]["complete"])
                self.assertIn("incomplete_final", partial["quality"]["issues"])
                self.assertIn("final_answer", partial["quality"]["missing"])
                for key, value in fields.items():
                    self.assertEqual(partial["messages"][-1][key], value)
                rows.append(codex("message", role="assistant", phase="final_answer", content="Finished."))
                complete = parse_transcript(self.write(rows))
                self.assertTrue(complete["quality"]["complete"])
                self.assertNotIn("incomplete_final", complete["quality"]["issues"])

    def test_malformed_discriminators_and_ids_do_not_crash(self):
        rows = [{"type": []}, codex({}, content="x"),
                claude("user", [{"type": []}]),
                claude("user", "content", uuid=["not-an-id"]),
                codex("message", id=["not-an-id"], role=[], content="content")]
        result = parse_transcript(self.write(rows))
        self.assertFalse(result["quality"]["complete"])
        self.assertEqual(result["offset"], len(rows))

    def test_non_finite_numbers_are_rejected_as_invalid_json(self):
        self.write([], b'{"type":"user","message":{"content":NaN}}\n')
        self.assertIn("malformed_json", parse_transcript(self.path)["quality"]["issues"])
        result = parse_transcript(self.write([codex("function_call", name="run", call_id="c1", arguments="NaN")]))
        self.assertIn("invalid_tool_arguments", result["quality"]["issues"])

    def test_event_only_and_unavailable_transcripts_are_not_complete(self):
        rows = [{"type": "event_msg", "payload": {"type": "agent_message", "message": "mirror"}}]
        self.assertFalse(parse_transcript(self.write(rows))["quality"]["complete"])
        self.assertIn("transcript_unavailable", parse_transcript(None)["quality"]["issues"])
        self.assertEqual(parse_transcript(None, start=4)["offset"], 4)

    def test_synthetic_examples_cover_two_turns_and_a_recovered_tool_error(self):
        fixtures = os.path.join(os.path.dirname(__file__), "..", "examples", "training")
        with open(os.path.join(fixtures, "tools.json"), encoding="utf-8") as handle:
            context = json.load(handle)
        self.assertEqual(context["tools"][0]["function"]["name"], "fixture_lookup")
        for source in ("claude", "codex"):
            with self.subTest(source=source):
                result = parse_transcript(os.path.join(fixtures, source + ".jsonl"))
                self.assertEqual(result["agent"], source)
                self.assertTrue(result["quality"]["complete"])
                user_turns = [m for m in result["messages"] if m["role"] == "user"
                              and any(b["type"] == "text" for b in m["content"])]
                self.assertEqual(len(user_turns), 2)
                tool_results = [b for m in result["messages"] for b in m["content"] if b["type"] == "tool_result"]
                self.assertEqual(len(tool_results), 3)
                self.assertEqual([b["is_error"] for b in tool_results], [False, True, False])
                self.assertEqual(result["messages"][-1]["role"], "assistant")

    def test_bytes_api_matches_path_offsets_and_metadata_without_opening_source(self):
        rows = [{"type": "session_meta", "payload": {"id": "synthetic-session", "cli_version": "fixture"}},
                codex("message", role="user", content="earlier"),
                codex("message", role="assistant", content="later", phase="final_answer")]
        self.write(rows)
        expected = parse_transcript(self.path, start=2, end=3, agent="codex")
        data = read_transcript_bytes(self.path)
        with mock.patch.object(transcript.os, "open", side_effect=AssertionError("bytes must not reopen")):
            actual = parse_transcript_bytes(data, start=2, end=3, agent="codex")
        self.assertEqual(actual, expected)
        self.assertEqual(actual["offset"], 3)
        self.assertEqual(actual["version"], "fixture")

    def test_bytes_partial_tail_is_not_consumed_and_completed_tail_can_recover(self):
        first = json.dumps(claude("user", "question")).encode() + b"\n"
        partial = first + b'{"type":"assistant","message":'
        result = parse_transcript_bytes(partial)
        self.assertEqual(result["offset"], 1)
        self.assertIn("partial_tail", result["quality"]["issues"])
        completed = first + json.dumps(claude("assistant", "answer")).encode()
        next_result = parse_transcript_bytes(completed, start=result["offset"])
        self.assertEqual(next_result["offset"], 2)
        self.assertEqual(next_result["messages"][0]["content"][0]["text"], "answer")

    def test_bytes_limits_discard_valid_prefix_even_when_end_excludes_bad_suffix(self):
        good = json.dumps(claude("assistant", "SYNTHETIC_PREFIX")).encode() + b"\n"
        data = good + b"x" * (len(good) + 1)
        with mock.patch.object(transcript, "MAX_LINE_BYTES", len(good)):
            for parse in (lambda: parse_transcript_bytes(data, start=1, end=1),
                          lambda: parse_transcript(self.write([], data), start=1, end=1)):
                result = parse()
                self.assertEqual(result["messages"], [])
                self.assertEqual(result["offset"], 1)
                self.assertIn("transcript_line_too_large", result["quality"]["issues"])
                self.assertFalse(result["quality"]["complete"])
                self.assertNotIn("SYNTHETIC_PREFIX", json.dumps(result))
        with mock.patch.object(transcript, "MAX_BYTES", len(good)):
            result = parse_transcript_bytes(good + good, start=5)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["offset"], 5)
        self.assertIn("transcript_too_large", result["quality"]["issues"])

    def test_bytes_api_rejects_mutable_or_wrong_input_types(self):
        for value in (bytearray(b"{}\n"), memoryview(b"{}\n"), "SYNTHETIC_PRIVATE", None):
            result = parse_transcript_bytes(value, start=4)
            self.assertEqual(result["offset"], 4)
            self.assertEqual(result["messages"], [])
            self.assertIn("invalid_transcript_bytes", result["quality"]["issues"])
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_physical_line_limit_rejects_entire_snapshot_before_parsing(self):
        good = json.dumps(claude("assistant", "SYNTHETIC_PREFIX")).encode() + b"\n"
        Path(self.path).write_bytes(good + b"\n\n")
        with mock.patch.object(transcript, "MAX_LINES", 2):
            for data in (good + b"\n\n", good + b"\n{}"):
                result = parse_transcript_bytes(data, start=1, end=1)
                self.assertEqual(result["messages"], [])
                self.assertEqual(result["offset"], 1)
                self.assertFalse(result["quality"]["complete"])
                self.assertIn("transcript_too_many_lines", result["quality"]["issues"])
                self.assertNotIn("SYNTHETIC_PREFIX", json.dumps(result))
            with self.assertRaisesRegex(TranscriptReadError, "^transcript_too_many_lines$"):
                read_transcript_bytes(self.path)
            result = parse_transcript(self.path)
            self.assertEqual(result["messages"], [])
            self.assertIn("transcript_too_many_lines", result["quality"]["issues"])
            self.assertEqual(transcript._check_bytes(b"\n\n"), 2)


class StableTranscriptRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nous-reader-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "SYNTHETIC_PRIVATE_PATH.jsonl"
        self.data = json.dumps(claude("assistant", "synthetic answer")).encode() + b"\n"
        self.path.write_bytes(self.data)

    def read_with_mutation(self, mutation):
        fdopen = transcript.os.fdopen
        budgets = []
        class Reader:
            def __init__(self, *args, **kwargs):
                self.handle = fdopen(*args, **kwargs)
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def read(self, budget):
                budgets.append(budget)
                result = self.handle.read(budget)
                mutation()
                return result
            def fileno(self):
                return self.handle.fileno()
        with mock.patch.object(transcript.os, "fdopen", side_effect=Reader):
            with self.assertRaisesRegex(TranscriptReadError, "^transcript_changed_during_read$") as caught:
                read_transcript_bytes(self.path)
        self.assertEqual(budgets, [len(self.data) + 1])
        self.assertNotIn("SYNTHETIC_PRIVATE", str(caught.exception))

    def test_append_during_bounded_read_is_rejected_without_reading_until_eof(self):
        def append():
            with self.path.open("ab") as handle:
                handle.write(self.data * 10)
        self.read_with_mutation(append)

    def test_truncation_and_same_size_rewrite_are_rejected(self):
        self.read_with_mutation(lambda: self.path.write_bytes(b""))
        self.path.write_bytes(self.data)
        stamp = self.path.stat().st_mtime_ns
        def rewrite():
            self.path.write_bytes(b"x" * len(self.data))
            os.utime(self.path, ns=(stamp + 1000000, stamp + 1000000))
        self.read_with_mutation(rewrite)

    def test_atomic_file_replacement_is_rejected(self):
        replacement = self.path.with_name("replacement.jsonl")
        replacement.write_bytes(self.data)
        self.read_with_mutation(lambda: os.replace(replacement, self.path))

    def test_symlink_replacement_during_read_is_rejected(self):
        target = self.path.with_name("target.jsonl")
        target.write_bytes(self.data)
        def replace():
            self.path.unlink()
            self.path.symlink_to(target)
        self.read_with_mutation(replace)

    def test_direct_symlinks_directories_and_fifo_are_rejected_before_open(self):
        link = self.path.with_name("link.jsonl")
        link.symlink_to(self.path)
        fifo = self.path.with_name("pipe")
        os.mkfifo(fifo)
        with mock.patch.object(transcript.os, "open", side_effect=AssertionError("nonregular must not open")):
            for path in (link, fifo, self.path.parent):
                with self.assertRaisesRegex(TranscriptReadError, "^transcript_must_be_regular_file$"):
                    read_transcript_bytes(path)

    def test_path_swapped_to_fifo_before_open_uses_nonblocking_descriptor(self):
        original_open = transcript.os.open
        def swapped(path, flags):
            self.assertTrue(flags & os.O_NONBLOCK)
            self.path.unlink()
            os.mkfifo(self.path)
            return original_open(path, flags)
        with mock.patch.object(transcript.os, "open", side_effect=swapped), \
             mock.patch.object(transcript.os, "fdopen", side_effect=AssertionError("FIFO must not read")):
            with self.assertRaisesRegex(TranscriptReadError, "^transcript_must_be_regular_file$"):
                read_transcript_bytes(self.path)

    def test_path_swapped_to_symlink_before_open_does_not_follow_it(self):
        original_open = transcript.os.open
        target = self.path.with_name("target.jsonl")
        target.write_bytes(self.data)
        def swapped(path, flags):
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.path.unlink()
            self.path.symlink_to(target)
            return original_open(path, flags)
        with mock.patch.object(transcript.os, "open", side_effect=swapped):
            with self.assertRaisesRegex(TranscriptReadError, "^transcript_unreadable$"):
                read_transcript_bytes(self.path)

    def test_oversized_sparse_file_is_rejected_before_open(self):
        with self.path.open("wb") as handle:
            handle.truncate(transcript.MAX_BYTES + 1)
        with mock.patch.object(transcript.os, "open", side_effect=AssertionError("oversized file must not open")):
            with self.assertRaisesRegex(TranscriptReadError, "^transcript_too_large$"):
                read_transcript_bytes(self.path)

    def test_custom_limits_and_line_boundaries_include_newline_bytes(self):
        self.path.write_bytes(b"abc\r\n")
        self.assertEqual(read_transcript_bytes(self.path, max_bytes=5, max_line_bytes=5), b"abc\r\n")
        with self.assertRaisesRegex(TranscriptReadError, "^transcript_line_too_large$"):
            read_transcript_bytes(self.path, max_bytes=5, max_line_bytes=4)
        self.path.write_bytes(b"a\n12345\nb")
        with self.assertRaisesRegex(TranscriptReadError, "^transcript_line_too_large$"):
            read_transcript_bytes(self.path, max_line_bytes=4)
        self.path.write_bytes(b"a\n" + b"b" * (64 * 1024 + 2))
        with self.assertRaisesRegex(TranscriptReadError, "^transcript_line_too_large$"):
            read_transcript_bytes(self.path, max_line_bytes=64 * 1024 + 1)

    def test_reader_returns_immutable_snapshot_and_never_writes_a_cache(self):
        before = set(self.path.parent.iterdir())
        snapshot = read_transcript_bytes(self.path)
        self.assertIs(type(snapshot), bytes)
        self.path.write_bytes(json.dumps(claude("assistant", "changed later")).encode())
        self.assertEqual(parse_transcript_bytes(snapshot)["messages"][0]["content"][0]["text"], "synthetic answer")
        self.assertEqual(parse_transcript(self.path)["messages"][0]["content"][0]["text"], "changed later")
        self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_unreadable_error_and_legacy_path_failure_are_static(self):
        with mock.patch.object(transcript.os, "open", side_effect=OSError("SYNTHETIC_PRIVATE_BODY")):
            with self.assertRaisesRegex(TranscriptReadError, "^transcript_unreadable$"):
                read_transcript_bytes(self.path)
            result = parse_transcript(self.path, start=7)
        self.assertEqual(result["offset"], 7)
        self.assertEqual(result["messages"], [])
        self.assertIn("transcript_unavailable", result["quality"]["issues"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_invalid_limits_are_rejected_without_opening_source(self):
        for limits in ({"max_bytes": -1}, {"max_bytes": True}, {"max_bytes": transcript.MAX_BYTES + 1},
                       {"max_line_bytes": 0}, {"max_line_bytes": "100"}):
            with self.assertRaisesRegex(TranscriptReadError, "^invalid_transcript_limits$"):
                read_transcript_bytes(self.path, **limits)



if __name__ == "__main__":
    unittest.main()
