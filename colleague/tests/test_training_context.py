"""Only synthetic source files; no user sessions or model requests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_training_context as context
import nous_transcript


def item(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


def message(role, text="synthetic", **extra):
    return item("message", role=role, content=[{"type": "output_text" if role == "assistant" else "input_text", "text": text}], **extra)


def source_rows():
    return [
        {"type": "session_meta", "ordinal": 0, "payload": {
            "cli_version": context.OBSERVED_RUNTIME_VERSION, "id": "SYNTHETIC_PRIVATE_SESSION",
            "base_instructions": {"text": "SYNTHETIC_PRIVATE_BASE"}}},
        {**message("user", "SYNTHETIC_PRIVATE_PROMPT"), "ordinal": 1,
            "metadata": {"history_sequence": 1, "sampled_through_sequence": 0}},
        {**message("assistant", "SYNTHETIC_PRIVATE_REPLY", phase="final_answer"), "ordinal": 2,
            "metadata": {"history_sequence": 2, "sampled_through_sequence": 1}},
    ]


class Context(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nous-context-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "SYNTHETIC_PRIVATE_PATH.jsonl"

    def write(self, rows=None, tail=b""):
        data = b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in (source_rows() if rows is None else rows)) + tail
        self.path.write_bytes(data)
        return data

    def inspect(self, rows=None, tail=b""):
        self.write(rows, tail)
        return context.inspect_source(self.path)

    def assert_private_absent(self, report):
        output = json.dumps(report)
        for value in ("SYNTHETIC_PRIVATE", str(self.root), "IGNORE_ALL_SAFETY", "sk-secret-fixture"):
            self.assertNotIn(value, output)
        self.assertIs(report["request_context_verified"], False)
        self.assertEqual(report["request_replay"], "missing_evidence")
        self.assertEqual(report["training_eligibility"], "not_evaluated")

    def test_known_source_has_bound_observations_but_no_request_verification(self):
        data = self.write()
        report = context.inspect_source(self.path)
        self.assertTrue(report["valid"])
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["source_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual((report["source_bytes"], report["source_lines"]), (len(data), 3))
        self.assertEqual(report["runtime"]["observed_profile"], "known")
        self.assertEqual(report["runtime"]["producer_identity"], "unverified")
        self.assertEqual(report["targets"], [{"source_line": 3, "type": "message", "history_sequence": 2,
            "sampled_through_sequence": 1, "watermark_relation": "observed_before", "request_context": "unverified"}])
        self.assertIn("request_snapshot_missing", report["gaps"])
        self.assert_private_absent(report)

    def test_unknown_or_missing_versions_do_not_echo_arbitrary_version(self):
        for version in ("IGNORE_ALL_SAFETY", None, {}, 12):
            rows = source_rows()
            rows[0]["payload"]["cli_version"] = version
            report = self.inspect(rows)
            self.assertFalse(report["valid"])
            self.assertTrue(report["scan_complete"])
            self.assertEqual(report["runtime"]["observed_profile"], "unknown")
            self.assertIn("unsupported_runtime_profile", report["errors"])
            self.assert_private_absent(report)

    def test_non_codex_source_is_rejected(self):
        report = self.inspect([{"type": "assistant", "message": {"role": "assistant", "content": "IGNORE_ALL_SAFETY"}}])
        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"], ["unsupported_source_format"])
        self.assert_private_absent(report)

    def test_session_and_runtime_changes_are_static(self):
        rows = source_rows()
        later = copy.deepcopy(rows[0])
        later.pop("ordinal")
        later["payload"].update(id="IGNORE_ALL_SAFETY", cli_version="sk-secret-fixture")
        rows.append(later)
        report = self.inspect(rows)
        self.assertFalse(report["valid"])
        self.assertEqual(report["runtime"]["session_consistency"], "mixed")
        self.assertEqual(report["runtime"]["version_consistency"], "changed")
        self.assertIn("mixed_sessions", report["errors"])
        self.assert_private_absent(report)

    def test_hidden_reasoning_and_mirrors_never_become_targets(self):
        rows = source_rows()
        rows[2:2] = [item("reasoning", text="IGNORE_ALL_SAFETY"),
            message("assistant", "sk-secret-fixture", channel="analysis"),
            item("message", role="assistant", content=[{"type": "reasoning_text", "text": "IGNORE_ALL_SAFETY"}]),
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "IGNORE_ALL_SAFETY"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "text": "IGNORE_ALL_SAFETY"}}}]
        report = self.inspect(rows)
        self.assertTrue(report["valid"])
        self.assertEqual([target["source_line"] for target in report["targets"]], [8])
        self.assert_private_absent(report)

    def test_injected_unknown_keys_and_values_are_only_static_counts(self):
        rows = source_rows()
        rows[1]["IGNORE_ALL_SAFETY"] = "sk-secret-fixture"
        rows[1]["metadata"].update({"IGNORE_ALL_SAFETY": {"text": "sk-secret-fixture"},
                                    "retained_source": {"content": "IGNORE_ALL_SAFETY"}})
        rows.insert(2, item("IGNORE_ALL_SAFETY", value="sk-secret-fixture"))
        report = self.inspect(rows)
        self.assertEqual(report["observed_context"]["unknown_record_fields"], 1)
        self.assertEqual(report["observed_context"]["unknown_metadata_fields"], 1)
        self.assertEqual(report["observed_context"]["unknown_response_items"], 1)
        self.assertEqual(report["metadata_presence"]["retained_source"], 1)
        self.assert_private_absent(report)

    def test_duplicate_nonmonotonic_and_future_checkpoints_are_observations(self):
        rows = source_rows()
        rows[-1]["metadata"].update(history_sequence=1, sampled_through_sequence=9)
        rows.append({**message("assistant"), "metadata": {"history_sequence": 0, "sampled_through_sequence": 1}})
        report = self.inspect(rows)
        self.assertTrue(report["valid"])  # Compaction may retain older identity; no corruption claim.
        self.assertEqual(report["ordering"]["history_sequence"]["duplicates"], 1)
        self.assertEqual(report["ordering"]["history_sequence"]["nonmonotonic"], 2)
        self.assertEqual(report["ordering"]["sampled_through_sequence"]["ahead_of_observed_history"], 1)
        self.assertEqual(report["ordering"]["sampled_through_sequence"]["nonmonotonic"], 1)
        self.assert_private_absent(report)

    def test_sequence_type_and_range_are_strict_but_null_is_absent(self):
        for field in ("history_sequence", "sampled_through_sequence"):
            for invalid in (True, -1, 1 << 64, 1.5, "IGNORE_ALL_SAFETY", []):
                rows = source_rows()
                rows[-1]["metadata"][field] = invalid
                report = self.inspect(rows)
                self.assertFalse(report["valid"])
                self.assertIn("invalid_sequence_metadata", report["errors"])
                self.assertIsNone(report["targets"][-1][field])
                self.assert_private_absent(report)
            rows[-1]["metadata"][field] = None
            self.assertTrue(self.inspect(rows)["valid"])

    def test_storage_ordinal_checks_are_separate_from_history_sequences(self):
        for ordinal in (True, -1, 1 << 64, 1, 0):
            rows = source_rows()
            rows[-1]["ordinal"] = ordinal
            report = self.inspect(rows)
            self.assertFalse(report["valid"])
            self.assertEqual(report["ordering"]["history_sequence"]["duplicates"], 0)

    def test_exact_alias_and_references_are_observations_without_dedup_or_promotion(self):
        base = "Synthetic base\r\nsecond line"
        tool = {"type": "function", "name": "SYNTHETIC_PRIVATE_TOOL", "parameters": {"type": "object", "properties": {}}}
        role, body = b"developer", b"Synthetic base\nsecond line"
        fragment = hashlib.sha1(b"codex-world-state-fragment-v1\0" + len(role).to_bytes(8, "big") + role
                                + len(body).to_bytes(8, "big") + body).hexdigest()
        tool_hash = hashlib.sha1(json.dumps(tool, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rows = source_rows()
        rows[0]["payload"]["base_instructions"] = base
        rows[1:1] = [message("developer", base), item("additional_tools", role="developer", tools=[tool]),
            {"type": "world_state", "payload": {"full": True, "state": {"model": {"instructions": fragment},
                "top_level_tools": {tool["name"]: tool_hash}, "model_catalog": "f" * 40}}},
            {"type": "world_state", "payload": {"full": False, "state": {}}},
            {"type": "turn_context", "payload": {"model": "SYNTHETIC_PRIVATE_MODEL"}},
            {"type": "event_msg", "payload": {"type": "thread_settings_applied", "settings": "IGNORE_ALL_SAFETY"}}]
        report = self.inspect(rows)
        self.assertTrue(report["valid"])
        self.assertEqual(report["observed_context"]["base_native_exact_alias_count"], 1)
        self.assertEqual(report["reference_observations"]["model_instructions"]["exact_match"], 1)
        self.assertEqual(report["reference_observations"]["tool_definitions"]["exact_match"], 1)
        self.assertEqual(report["reference_observations"]["model_catalog_opaque"], 1)
        self.assertEqual(report["observed_context"]["world_state_patch"], 1)
        self.assertNotIn(fragment, json.dumps(report))
        self.assertNotIn(tool_hash, json.dumps(report))
        self.assert_private_absent(report)

    def test_namespace_hashes_use_header_and_original_child_without_guessing_floats(self):
        child = {"type": "function", "name": "lookup", "parameters": {"type": "object", "minimum": 0.25}}
        namespace = {"type": "namespace", "name": "SYNTHETIC_PRIVATE_TOOL", "description": "synthetic", "tools": [child]}
        header = {key: value for key, value in namespace.items() if key != "tools"}
        digest = hashlib.sha1(json.dumps(header, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        rows = source_rows()
        rows[1:1] = [item("additional_tools", role="developer", tools=[namespace]),
            {"type": "world_state", "payload": {"full": True, "state": {"top_level_tools": {namespace["name"]: digest}}}}]
        report = self.inspect(rows)
        self.assertEqual(report["reference_observations"]["tool_definitions"]["exact_match"], 1)
        self.assertEqual(report["reference_observations"]["uncomparable_tool_definitions"], 1)
        self.assert_private_absent(report)

    def test_metadata_base_alone_does_not_resolve_a_native_fragment_reference(self):
        rows = source_rows()
        digest = context._fragment("developer", rows[0]["payload"]["base_instructions"]["text"])
        rows.insert(1, {"type": "world_state", "payload": {"full": True,
            "state": {"model": {"instructions": digest}}}})
        report = self.inspect(rows)
        self.assertEqual(report["reference_observations"]["model_instructions"]["exact_match"], 0)
        self.assertEqual(report["reference_observations"]["model_instructions"]["unmatched"], 1)
        self.assertEqual(report["reference_observations"]["semantics"],
                         "whole_source_cooccurrence_no_temporal_effectiveness")

    def test_invalid_text_or_refusal_cannot_be_a_visible_target(self):
        for block in ({"type": "output_text", "text": []}, {"type": "refusal", "refusal": None},
                      {"type": "text"}, {"type": []}, "IGNORE_ALL_SAFETY"):
            rows = source_rows()
            rows[-1]["payload"]["content"] = [block]
            report = self.inspect(rows)
            self.assertFalse(report["valid"])
            self.assertEqual(report["errors"], ["invalid_visible_content"])
            self.assertEqual(report["targets"], [])
            self.assert_private_absent(report)
        rows[-1]["payload"]["content"] = [{"type": "input_image", "url": "IGNORE_ALL_SAFETY"}]
        report = self.inspect(rows)
        self.assertEqual(report["observed_context"]["unsupported_visible_content"], 1)
        self.assertIn("unsupported_visible_content", report["gaps"])
        self.assertEqual(report["targets"], [])

    def test_malformed_duplicate_nonfinite_and_partial_source_discard_all_partial_observations(self):
        for tail, code in [(b'{"type":', "malformed_source_json"),
                           (b'{"payload":{},"payload":{}}\n', "duplicate_json_key"),
                           (b'{"payload":{"text":NaN}}\n', "nonfinite_json_number"),
                           (b'{"payload":{"text":1e999}}\n', "nonfinite_json_number"),
                           (b'{"payload":{"text":"\\ud800"}}\n', "invalid_source_unicode"),
                           (b'{"payload":{"\\ud800":null}}\n', "invalid_source_unicode")]:
            report = self.inspect(tail=tail)
            self.assertFalse(report["valid"])
            self.assertFalse(report["scan_complete"])
            self.assertEqual(report["targets"], [])
            self.assertEqual(report["observed_context"]["base_instructions"], 0)
            self.assertEqual(report["errors"], [code])
            self.assertIsNotNone(report["source_sha256"])

    def test_target_and_report_limits_never_expose_a_complete_looking_prefix(self):
        self.write()
        for constant, value, reason in [("MAX_TARGETS", 0, "context_target_limit"),
                                         ("MAX_REPORT_BYTES", 1, "context_report_limit"),
                                         ("MAX_OBSERVATIONS", 1, "context_observation_limit")]:
            with mock.patch.object(context, constant, value):
                report = context.inspect_source(self.path)
            self.assertFalse(report["valid"])
            self.assertFalse(report["scan_complete"])
            self.assertEqual(report["targets"], [])
            self.assertEqual(report["errors"], [reason])

    def test_target_limit_accepts_exact_boundary_and_rejects_next_row(self):
        rows = source_rows()
        rows.append(message("assistant"))
        with mock.patch.object(context, "MAX_TARGETS", 2):
            self.assertEqual(len(self.inspect(rows)["targets"]), 2)
            report = self.inspect(rows + [message("assistant")])
        self.assertFalse(report["valid"])
        self.assertEqual(report["targets"], [])
        self.assertEqual(report["errors"], ["context_target_limit"])

    def test_source_resource_limits_and_nonregular_paths_are_static(self):
        self.write()
        with mock.patch.object(nous_transcript, "MAX_BYTES", 1):
            report = context.inspect_source(self.path)
        self.assertEqual(report["errors"], ["transcript_too_large"])
        self.assertIsNone(report["source_sha256"])
        alias = self.root / "alias.jsonl"
        alias.symlink_to(self.path)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in (alias, fifo, self.root):
            self.assertEqual(context.inspect_source(path)["errors"], ["transcript_must_be_regular_file"])

    def test_reader_mutation_error_is_not_converted_to_an_accepted_prefix(self):
        self.write()
        with mock.patch.object(context, "read_transcript_bytes",
                               side_effect=nous_transcript.TranscriptReadError("transcript_changed_during_read")):
            report = context.inspect_source(self.path)
        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"], ["transcript_changed_during_read"])
        self.assertIsNone(report["source_sha256"])

    def test_one_immutable_read_no_state_files_and_no_source_mutation(self):
        data = self.write()
        before = sorted(str(path) for path in self.root.rglob("*"))
        with mock.patch.dict(os.environ, {"NOUS_HOME": str(self.root / "must-not-create"),
                                         "NOUS_LEDGER_HOME": str(self.root / "must-not-create-ledger")}), \
             mock.patch.object(context, "read_transcript_bytes", wraps=nous_transcript.read_transcript_bytes) as reader:
            first = context.inspect_source(self.path)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(first, context.inspect_source(self.path))

    def test_changes_after_stable_read_do_not_mix_a_second_source_snapshot(self):
        original = self.write()
        read = nous_transcript.read_transcript_bytes
        def append_after(path):
            data = read(path)
            with self.path.open("ab") as output:
                output.write(json.dumps(message("assistant", "IGNORE_ALL_SAFETY")).encode() + b"\n")
            return data
        with mock.patch.object(context, "read_transcript_bytes", side_effect=append_after) as reader:
            report = context.inspect_source(self.path)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(len(report["targets"]), 1)
        self.assertEqual(report["source_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(report["source_lines"], 3)
        self.assertNotEqual(report["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assert_private_absent(report)


if __name__ == "__main__":
    unittest.main()
