"""Synthetic full-source identity checks; no user transcript or filesystem reads."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_training_history_identity as identity


PROJECT = "/synthetic/history/project"
SOURCE = "/synthetic/history/transcript.jsonl"


def encoded(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def candidate(data, agent="codex", session="fixture-thread", **kwargs):
    result = {"path": SOURCE, "agent": agent, "source_id": hashlib.sha256(os.fsencode(SOURCE)).hexdigest(),
              "source_role": "explicit", "session_id": session, "cwd": PROJECT,
              "stat": {"dev": 1, "ino": 2, "size": len(data), "mtime_ns": 3, "ctime_ns": 4},
              "branch_role": "main", "context_hints": []}
    result.update(kwargs)
    return result


def codex(**metadata):
    meta = {"id": "fixture-thread", "cwd": PROJECT, "cli_version": "synthetic"}
    meta.update(metadata)
    return [{"type": "session_meta", "payload": meta},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "synthetic"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "answer"}}]


def claude():
    return [{"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "fixture-thread",
             "cwd": PROJECT, "message": {"role": "user", "content": "synthetic"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "fixture-thread",
             "cwd": PROJECT, "message": {"role": "assistant", "content": "answer"}}]


class HistoryIdentity(unittest.TestCase):
    def check(self, rows, agent="codex", **kwargs):
        data = encoded(rows)
        return identity.validate_snapshot(data, candidate(data, agent, **kwargs))

    def reject(self, rows, code, agent="codex", **kwargs):
        with self.assertRaises(identity.HistoryIdentityError) as caught:
            self.check(rows, agent, **kwargs)
        self.assertEqual(str(caught.exception), code)

    def test_main_sources_have_no_added_gap_and_no_body_in_result(self):
        for agent, rows in (("codex", codex()), ("claude", claude())):
            with self.subTest(agent=agent):
                result = self.check(rows, agent)
                self.assertEqual(result["issues"], [])
                self.assertEqual(result["missing"], [])
                self.assertEqual(result["project_contexts"], 1)
                self.assertEqual(len(result["session_group_id"]), 24)
                rendered = json.dumps(result)
                for secret in (PROJECT, "fixture-thread", "synthetic", "answer"):
                    self.assertNotIn(secret, rendered)

    def test_is_pure_and_does_not_mutate_inputs(self):
        data = encoded(codex())
        item = candidate(data)
        before = copy.deepcopy(item)
        with mock.patch("builtins.open", side_effect=AssertionError("no io")), mock.patch("os.stat", side_effect=AssertionError("no stat")):
            identity.validate_snapshot(data, item)
        self.assertEqual(item, before)

    def test_fake_candidate_identity_project_path_hash_or_size_rejected(self):
        self.reject(codex(), "history_session_mismatch", session="different")
        self.reject(codex(), "history_project_mismatch", cwd="/synthetic/other")
        self.reject(codex(), "history_candidate_invalid", source_id="0" * 64)
        self.reject(codex(), "history_snapshot_size_mismatch", stat={"dev": 1, "ino": 2, "size": 1, "mtime_ns": 3, "ctime_ns": 4})

    def test_conflict_beyond_header_is_checked(self):
        rows = codex() + [{"type": "event_msg", "payload": {"type": "token_count"}}] * 150
        rows.append({"type": "session_meta", "payload": {"id": "other", "cwd": PROJECT}})
        self.reject(rows, "history_session_mismatch")
        rows = claude() + [{"type": "progress"}] * 150 + [{"type": "progress", "sessionId": "other"}]
        self.reject(rows, "history_session_mismatch", "claude")

    def test_every_known_native_cwd_is_rechecked(self):
        for entry in ({"type": "session_meta", "payload": {"id": "fixture-thread", "cwd": "/other"}},
                      {"type": "turn_context", "payload": {"cwd": "/other"}},
                      {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_settings": {"cwd": "/other"}}}):
            self.reject(codex() + [entry], "history_project_mismatch")
        self.reject(claude() + [{"type": "progress", "cwd": "/other"}], "history_project_mismatch", "claude")

    def test_tool_payload_cwd_and_session_names_are_not_metadata(self):
        rows = codex() + [{"type": "response_item", "payload": {"type": "function_call", "arguments": {"cwd": "/other", "sessionId": "other"}}}]
        self.assertEqual(self.check(rows)["issues"], [])

    def test_metadata_identity_types_and_controls_rejected(self):
        for value in (None, True, 42, [], "", "bad\nidentity", "\ud800"):
            with self.subTest(value=repr(value)):
                self.reject(codex(id=value), "history_identity_invalid")
        self.reject(codex(cwd="relative"), "history_identity_invalid")

    def test_duplicate_json_keys_and_non_json_fail_without_source_text(self):
        for data in (b'{"type":"session_meta","type":"PRIVATE"}\n', b'PRIVATE\n', b'{"type":', b'[]\n'):
            with self.assertRaises(identity.HistoryIdentityError) as caught:
                identity.validate_snapshot(data, candidate(data))
            self.assertIn(str(caught.exception), {"history_json_invalid", "history_metadata_invalid"})
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_no_identity_no_project_and_wrong_agent_rejected(self):
        self.reject(codex()[1:], "history_identity_missing")
        rows = claude()
        for row in rows:
            del row["cwd"]
        self.reject(rows, "history_identity_missing", "claude")
        self.reject(claude(), "history_agent_mismatch")
        self.reject(codex(), "history_agent_mismatch", "claude")

    def test_root_family_groups_sibling_threads_without_overwriting_ids(self):
        first = self.check(codex(session_id="family"))
        second = self.check(codex(id="child", session_id="family", parent_thread_id="fixture-thread"), session="child")
        self.assertEqual(first["session_group_id"], second["session_group_id"])
        self.assertNotEqual(first["session_group_id"], self.check(claude(), "claude")["session_group_id"])
        rows = codex(session_id="family") + [{"type": "session_meta", "payload": {"id": "fixture-thread", "cwd": PROJECT, "session_id": "other"}}]
        self.reject(rows, "history_family_mismatch")

    def test_inherited_and_nonlegacy_flags_are_rebuilt_from_source(self):
        rows = codex(history_mode="paginated", history_base={"thread_id": "parent", "end_ordinal_exclusive": 3, "end_byte_offset": 200},
                     subagent_history_start_ordinal=4, forked_from_id="parent")
        result = self.check(rows)
        self.assertEqual(result["issues"], ["history_inherited_context", "history_nonlegacy_mode"])
        self.assertEqual(result["missing"], ["history_ancestry", "history_replay_semantics"])
        for values in ({"source": {"subagent": "review"}}, {"attached_thread": True}, {"parent_thread_id": "parent"}):
            self.assertIn("history_inherited_context", self.check(codex(**values))["issues"])

    def test_ancestry_metadata_invalid_ordinal_types_and_bounds(self):
        for value in (True, -1, 1 << 64, 0.5, "1"):
            self.reject(codex(subagent_history_start_ordinal=value), "history_metadata_invalid")
        self.reject(codex(history_base={"thread_id": "parent"}), "history_metadata_invalid")
        self.reject(codex(history_mode=True), "history_metadata_invalid")
        self.reject(codex(attached_thread=1), "history_metadata_invalid")

    def test_candidate_hints_only_tighten_and_unknown_hints_reject(self):
        self.assertIn("history_inherited_context", self.check(codex(), context_hints=["subagent_path"])["issues"])
        self.assertIn("history_inherited_context", self.check(codex(), branch_role="branch")["issues"])
        self.reject(codex(), "history_candidate_invalid", context_hints=["PRIVATE"])

    def test_claude_sidechain_agent_and_logical_parent_are_gaps(self):
        for extra in ({"isSidechain": True}, {"agentId": "agent-a"}, {"logicalParentUuid": "ancestor"}):
            rows = claude()
            rows[-1].update(extra)
            self.assertIn("history_inherited_context", self.check(rows, "claude")["issues"])
        rows = claude()
        rows[0]["agentId"], rows[1]["agentId"] = "one", "two"
        self.reject(rows, "history_agent_identity_mismatch", "claude")

    def test_branch_cycle_missing_parent_and_order_are_explicit(self):
        rows = claude()
        rows.append({**rows[-1], "uuid": "a2"})
        self.assertIn("history_branch", self.check(rows, "claude")["issues"])
        rows = claude()
        rows[0]["parentUuid"] = "a1"
        result = self.check(rows, "claude")
        self.assertTrue({"history_parent_cycle", "history_parent_order"} <= set(result["issues"]))
        rows = claude()
        rows[-1]["parentUuid"] = "missing"
        self.assertIn("history_missing_parent", self.check(rows, "claude")["issues"])

    def test_multiple_roots_unknown_chain_and_parent_conflict(self):
        rows = claude()
        rows[-1]["parentUuid"] = None
        self.assertIn("history_branch", self.check(rows, "claude")["issues"])
        rows = claude()
        del rows[0]["parentUuid"]
        self.assertIn("history_parent_chain_unavailable", self.check(rows, "claude")["issues"])
        rows = claude()
        rows.append({**rows[-1], "parentUuid": None})
        self.reject(rows, "history_parent_identity_mismatch", "claude")

    def test_deep_linear_chain_is_iterative(self):
        rows = claude()[:1]
        parent = "u1"
        for number in range(2200):
            node = "node" + str(number)
            rows.append({"type": "assistant", "uuid": node, "parentUuid": parent})
            parent = node
        self.assertEqual(self.check(rows, "claude")["issues"], [])

    def test_snapshot_limits_are_static(self):
        with mock.patch.object(identity, "_check_bytes", side_effect=identity.TranscriptReadError("do-not-copy")):
            self.reject(codex(), "history_snapshot_limits")

    def test_compressed_snapshot_has_separate_physical_size(self):
        result = self.check(codex(), compression="zstd", stat={"dev": 1, "ino": 2, "size": 1, "mtime_ns": 3, "ctime_ns": 4})
        self.assertEqual(result["issues"], [])
        self.reject(codex(), "history_candidate_invalid", compression="gzip")

    def test_exported_errors_are_static(self):
        self.assertIsInstance(identity.ERROR_CODES, frozenset)
        self.assertIn("history_session_mismatch", identity.ERROR_CODES)
        self.assertTrue(all(code.startswith("history_") and code.isascii() for code in identity.ERROR_CODES))


if __name__ == "__main__":
    unittest.main()
