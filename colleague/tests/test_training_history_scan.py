"""All history fixtures are synthetic, isolated, and never read user sessions."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import nous_training_history_scan as scan
import nous_training_history_read as reader


def codex(ident="synthetic-thread", cwd="/synthetic/project", **extra):
    return {"type": "session_meta", "payload": {"id": ident, "cwd": cwd, "source": "cli", **extra}}


def claude(ident="synthetic-session", cwd="/synthetic/project", **extra):
    return {"type": "user", "sessionId": ident, "cwd": cwd,
            "message": {"content": "NEVER_OUTPUT_BODY"}, **extra}


class HistoryScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "history"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative, rows):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        return path

    def run_scan(self, agent="codex", **kwargs):
        return scan.scan_history(roots=[{"path": str(self.root), "agent": agent}], **kwargs)

    def test_nested_sorted_identity_stat_no_body_or_persistence(self):
        self.write("z/y.jsonl", [codex("b", base_instructions={"text": "NEVER_OUTPUT_BODY"})])
        first = self.write("a/x.jsonl", [codex("a", session_id="root-family")])
        before = sorted(str(p) for p in self.base.rglob("*"))
        result = self.run_scan()
        self.assertTrue(result["complete"])
        self.assertEqual([c["session_id"] for c in result["candidates"]], ["a", "b"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["source_id"], hashlib.sha256(os.fsencode(str(first))).hexdigest())
        self.assertEqual(candidate["stat"], scan._signature(first.stat()))
        self.assertEqual(candidate["branch_role"], "main")
        self.assertNotIn("NEVER_OUTPUT_BODY", json.dumps(result))
        self.assertNotIn("root-family", json.dumps(result))
        self.assertEqual(before, sorted(str(p) for p in self.base.rglob("*")))

    def test_claude_after_file_history_and_subagent_not_dedup_session(self):
        self.write("project/s.jsonl", [{"type": "file-history-snapshot", "snapshot": {}}, claude(isSidechain=False)])
        self.write("project/s/subagents/nested/agent-a.jsonl", [claude(isSidechain=True, agentId="a")])
        result = self.run_scan("claude")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual({c["branch_role"] for c in result["candidates"]}, {"main", "subagent"})
        self.assertEqual(len({c["source_id"] for c in result["candidates"]}), 2)
        self.assertNotIn("NEVER_OUTPUT_BODY", json.dumps(result))

    def test_nonlegacy_fork_and_subagent_context_hints(self):
        self.write("fork.jsonl", [codex(forked_from_id="private-parent", history_mode="paginated", history_base={"id": "private"})])
        self.write("child.jsonl", [codex(source={"subagent": {"thread_spawn": {"parent_thread_id": "private"}}},
                                                   parent_thread_id="private", subagent_history_start_ordinal=0)])
        result = self.run_scan()
        child, fork = result["candidates"]
        self.assertEqual(child["branch_role"], "subagent")
        self.assertEqual(fork["branch_role"], "branch")
        self.assertIn("nonlegacy_history", fork["context_hints"])
        self.assertIn("inherited_history", child["context_hints"])
        self.assertNotIn("private-parent", json.dumps(result))

    def test_unknown_identity_never_uses_filename_or_project_slug(self):
        self.write("project-cwd/session-name.jsonl", [{"type": "response_item", "payload": {"type": "message"}}])
        result = self.run_scan()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped_counts"], {"scan_identity_unknown": 1})

    def test_conflicting_ids_or_cwd_skip_static(self):
        self.write("ids.jsonl", [codex(), codex("other")])
        self.write("cwd.jsonl", [codex(), codex(cwd="/other")])
        self.assertEqual(self.run_scan()["skipped_counts"], {"scan_identity_conflict": 2})

    def test_codex_root_session_id_does_not_replace_actual_id(self):
        self.write("a.jsonl", [codex("child-a", session_id="shared")])
        self.write("b.jsonl", [codex("child-b", session_id="shared")])
        self.assertEqual([c["session_id"] for c in self.run_scan()["candidates"]], ["child-a", "child-b"])

    def test_relative_cwd_control_id_and_missing_pair_rejected(self):
        self.write("relative.jsonl", [codex(cwd="relative")])
        self.write("control.jsonl", [codex("bad\nvalue")])
        self.write("split.jsonl", [{"type": "user", "sessionId": "s"}, {"type": "user", "cwd": "/p"}])
        self.assertEqual(self.run_scan("claude")["skipped_counts"], {"scan_agent_mismatch": 2, "scan_identity_unknown": 1})
        self.assertEqual(self.run_scan()["skipped_counts"]["scan_identity_invalid"], 2)

    def test_leaf_symlink_and_symlink_directory_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.jsonl").write_text(json.dumps(codex()) + "\n")
        (self.root / "link.jsonl").symlink_to(outside / "a.jsonl")
        (self.root / "linkdir").symlink_to(outside, target_is_directory=True)
        result = self.run_scan()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped_counts"], {"scan_symlink_rejected": 2})

    def test_root_and_ancestor_symlink_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        child = self.root / "sub"
        child.mkdir()
        for root in (alias, alias / "sub"):
            result = scan.scan_history(roots=[{"path": str(root), "agent": "codex"}])
            self.assertFalse(result["complete"])
            self.assertEqual(result["errors"], ["scan_symlink_rejected"])

    def test_parent_traversal_cannot_hide_a_symlink_component(self):
        result = scan.scan_history(roots=[{"path": str(self.base / "alias" / ".." / "history"), "agent": "codex"}])
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"], ["invalid_scan_root"])

    def test_fifo_never_opened(self):
        os.mkfifo(self.root / "fifo.jsonl")
        with mock.patch.object(scan, "_header", side_effect=AssertionError("must not open")):
            result = self.run_scan()
        self.assertEqual(result["skipped_counts"], {"scan_regular_file_required": 1})

    def test_compressed_history_explicitly_unsupported(self):
        (self.root / "a.jsonl.zst").write_bytes(b"not read")
        with mock.patch.object(reader, "_zstd", None):
            result = self.run_scan()
        self.assertEqual(result["counts"]["compressed_files"], 1)
        self.assertEqual(result["skipped_counts"], {"scan_compressed_unsupported": 1})

    @unittest.skipIf(reader._zstd is None, "requires optional stdlib compression.zstd")
    def test_compressed_header_candidate_preserves_source_file_identity(self):
        body = json.dumps(codex()).encode() + b"\n"
        path = self.root / "a.jsonl.zst"
        path.write_bytes(reader._zstd.compress(body))
        result = self.run_scan()
        self.assertTrue(result["complete"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["compression"], "zstd")
        self.assertEqual(candidate["stat"]["size"], path.stat().st_size)
        self.assertEqual(candidate["source_id"], hashlib.sha256(os.fsencode(str(path))).hexdigest())
        self.assertEqual(candidate["header"]["bytes_read"], len(body))
        self.assertEqual(result["counts"]["source_files"], 1)
        self.assertEqual(result["counts"]["compressed_files"], 1)

    def test_exact_file_limit_complete_only_when_no_extra_file(self):
        self.write("a.jsonl", [codex("a")])
        self.assertTrue(self.run_scan(max_files=1)["complete"])
        self.write("b.jsonl", [codex("b")])
        result = self.run_scan(max_files=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"], ["scan_file_limit"])
        self.assertEqual(result["counts"]["jsonl_files"], 1)
        self.assertEqual(len(result["candidates"]), 1)

    def test_entry_directory_depth_limits_are_explicit(self):
        self.write("d/e/a.jsonl", [codex()])
        for setting, limit, code in (("MAX_ENTRIES", 1, "scan_entry_limit"),
                                     ("MAX_DIRECTORIES", 1, "scan_directory_limit"),
                                     ("MAX_DEPTH", 0, "scan_depth_limit")):
            with self.subTest(setting=setting), mock.patch.object(scan, setting, limit):
                result = self.run_scan()
                self.assertFalse(result["complete"])
                self.assertIn(code, result["errors"])

    def test_header_line_limit_no_read_past_limit(self):
        self.write("late.jsonl", [{"type": "event_msg"}] * 128 + [codex()])
        self.write("early.jsonl", [codex()] + [{"type": "event_msg"}] * 128 + [codex("other")])
        result = self.run_scan()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["header"]["lines_read"], 128)
        self.assertEqual(result["candidates"][0]["header"]["limit"], "lines")
        self.assertFalse(result["candidates"][0]["header"]["complete"])
        self.assertEqual(result["skipped_counts"], {"scan_header_limit": 1})

    def test_header_byte_limit_discard_partial_line(self):
        self.write("a.jsonl", [codex(), {"body": "X" * 1000}])
        with mock.patch.object(scan, "MAX_HEADER_BYTES", 160):
            result = self.run_scan()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["header"]["bytes_read"], 160)
        self.assertEqual(result["candidates"][0]["header"]["limit"], "bytes")

    def test_malformed_duplicate_nonfinite_surrogate_headers_static(self):
        for index, data in enumerate((b'{bad}', b'{"type":"x","type":"x"}', b'{"n":NaN}', b'{"n":1e999}', b'{"s":"\\ud800"}')):
            (self.root / (str(index) + ".jsonl")).write_bytes(data)
        result = self.run_scan()
        self.assertEqual(result["skipped_counts"], {"scan_header_invalid_json": 5})
        self.assertNotIn("NaN", json.dumps(result))

    def test_changed_file_during_header_rejected(self):
        path = self.write("a.jsonl", [codex()])
        original = scan._loads
        def mutate(line):
            value = original(line)
            with path.open("ab") as handle:
                handle.write(b"\n")
            return value
        with mock.patch.object(scan, "_loads", side_effect=mutate):
            result = self.run_scan()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped_counts"], {"scan_source_changed": 1})

    def test_leaf_replaced_after_discovery_rejected(self):
        path = self.write("a.jsonl", [codex()])
        original = scan._header
        def replace(*args):
            path.unlink()
            path.symlink_to(self.base / "outside")
            return original(*args)
        with mock.patch.object(scan, "_header", side_effect=replace):
            result = self.run_scan()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["counts"]["skipped"], 1)

    def test_env_roots_and_missing_defaults_no_state(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.base / "codex"), "CLAUDE_CONFIG_DIR": str(self.base / "claude")}):
            result = scan.scan_history()
            roots = scan.default_roots()
        self.assertTrue(result["complete"])
        self.assertEqual(result["skipped_counts"], {"scan_root_missing": 3})
        self.assertEqual([r["source_role"] for r in roots], ["sessions", "archived_sessions", "projects"])
        self.assertFalse((self.base / "codex").exists())

    def test_invalid_arguments_static_and_bounded_details(self):
        for kwargs in ({"max_files": True}, {"max_files": 10001}, {"roots": [{"agent": "private-agent", "path": "/x"}]}):
            result = scan.scan_history(**kwargs)
            self.assertFalse(result["complete"])
            self.assertNotIn("private-agent", json.dumps(result))
        for i in range(3):
            self.write(str(i) + ".jsonl", [{"type": "event_msg"}])
        with mock.patch.object(scan, "MAX_SKIPPED", 1):
            result = self.run_scan()
        self.assertTrue(result["skipped_details_truncated"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped_counts"]["scan_identity_unknown"], 3)

    def test_overlapping_roots_do_not_duplicate_file(self):
        self.write("sub/a.jsonl", [codex()])
        roots = [{"agent": "codex", "path": str(self.root)}, {"agent": "codex", "path": str(self.root / "sub")}]
        self.assertEqual(len(scan.scan_history(roots=roots)["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
