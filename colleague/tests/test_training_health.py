"""Health metadata privacy, atomicity, bounds and genuine concurrent writers."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import nous_training_health as health

P1, P2, P3 = ("p_" + digit * 24 for digit in "123")
R1, R2 = ("t_" + digit * 32 for digit in "12")


class TrainingHealth(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nous-health-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.folder = self.home / "training/health"
        self.state_path = self.folder / "state.json"

    def attempt(self, project=P1, agent="claude", now=100):
        result = health.record_attempt(self.home, project, agent, authorized=True, now=now)
        self.assertEqual(result["status"], "recorded", result)
        return result["attempt_id"]

    def finish(self, token, project=P1, agent="claude", now=101, **kwargs):
        return health.record_result(self.home, project, agent, token, authorized=True, now=now,
                                    **({"status": "collected", "record_id": R1} | kwargs))

    def row(self, project=P1, agent="claude"):
        result = health.read_health(self.home)
        self.assertEqual(result["status"], "ok", result)
        return result["projects"][project][agent]

    def test_read_and_purge_absent_health_are_noncreating(self):
        self.assertEqual(health.read_health(self.home)["status"], "missing")
        self.assertEqual(health.purge_health(self.home)["status"], "missing")
        self.assertEqual(health.purge_health(self.home, P1)["status"], "missing")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_false_or_nonboolean_authorization_does_not_even_inspect_path(self):
        class PrivatePath:
            def __fspath__(self):
                raise AssertionError("must not inspect unauthorized paths")
        for authorized in (False, None, 1, "yes"):
            self.assertEqual(health.record_attempt(PrivatePath(), "PRIVATE", "PRIVATE", authorized=authorized), {"status": "disabled"})
            self.assertEqual(health.record_result(PrivatePath(), "PRIVATE", "PRIVATE", "PRIVATE", authorized=authorized,
                                                  status="failed", error_code="PRIVATE"), {"status": "disabled"})
        self.assertFalse(self.home.exists())

    def test_success_failure_and_last_timestamps_are_distinct(self):
        first = self.attempt(now=100)
        self.assertEqual(self.finish(first, now=101)["status"], "recorded")
        second = self.attempt(now=102)
        self.finish(second, now=103, status="failed", error_code="transcript_changed_during_read")
        row = self.row()
        self.assertEqual((row["attempt_count"], row["captured_count"], row["failed_count"]), (2, 1, 1))
        self.assertEqual((row["last_attempt_at"], row["last_success_at"], row["last_failure_at"]), (102, 101, 103))
        self.assertEqual(row["last_error_code"], "transcript_changed_during_read")
        self.assertEqual(row["last_record_id"], R1)
        self.assertEqual(row["pending_count"], 0)
        self.assertNotIn("pending", row)

    def test_unknown_error_strings_are_never_persisted_or_returned(self):
        token = self.attempt()
        secret = "PRIVATE_TOKEN /private/customer/session-raw failure@example.com"
        result = self.finish(token, status="failed", error_code=secret, record_id=secret)
        self.assertEqual(result["state"]["last_error_code"], "collection_failed")
        self.assertNotIn(secret, self.state_path.read_text() + json.dumps(result))

    def test_invalid_identity_and_record_ids_cannot_persist_raw_values(self):
        secret = "PRIVATE_SOURCE_SESSION_OR_PATH"
        for project, agent in ((secret, "claude"), (P1, secret)):
            result = health.record_attempt(self.home, project, agent, authorized=True)
            self.assertEqual(result["error"], "health_invalid_identity")
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(self.home.exists())
        token = self.attempt()
        before = self.state_path.read_bytes()
        self.assertEqual(self.finish(token, record_id=secret)["error"], "health_invalid_record_id")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_projects_and_agents_have_independent_counts(self):
        for project, agent in ((P1, "claude"), (P1, "codex"), (P2, "unknown")):
            token = self.attempt(project, agent)
            self.finish(token, project, agent)
        self.assertEqual(self.row(P1, "claude")["captured_count"], 1)
        self.assertEqual(self.row(P1, "codex")["captured_count"], 1)
        self.assertEqual(self.row(P2, "unknown")["captured_count"], 1)
        self.assertEqual(set(health.read_health(self.home, P2)["projects"]), {P2})

    def test_result_is_idempotent_and_cannot_recreate_purged_state(self):
        token = self.attempt()
        self.finish(token)
        self.assertEqual(self.finish(token)["status"], "ignored")
        self.assertEqual(self.row()["captured_count"], 1)
        self.assertEqual(health.purge_health(self.home)["status"], "purged")
        self.assertEqual(self.finish(token)["status"], "missing")
        self.assertFalse(self.state_path.exists())

    def test_result_without_attempt_never_creates_state(self):
        result = health.record_result(self.home, P1, "claude", "a" * 32, authorized=True, status="failed")
        self.assertEqual(result["status"], "missing")
        self.assertFalse(self.home.exists())

    def test_purge_io_failure_returns_static_error_without_raising(self):
        self.attempt()
        before = self.state_path.read_bytes()
        with mock.patch.object(health.os, "unlink", side_effect=OSError("PRIVATE_SOURCE_PATH")):
            result = health.purge_health(self.home)
        self.assertEqual(result, {"status": "unavailable", "error": "health_io_failed"})
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_state_permissions_are_checked_without_silent_repair(self):
        self.attempt()
        self.state_path.chmod(0o644)
        self.assertEqual(health.read_health(self.home)["error"], "health_permissions")
        self.assertEqual(health.record_attempt(self.home, P1, "claude", authorized=True)["error"], "health_permissions")
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o644)

    def test_pending_eviction_counts_abandonment_not_task_failure(self):
        with mock.patch.object(health, "MAX_PENDING", 2):
            tokens = [self.attempt(now=moment) for moment in (1, 2, 3)]
            row = self.row()
            self.assertEqual((row["pending_count"], row["abandoned_count"], row["failed_count"]), (2, 1, 0))
            self.assertEqual(row["last_error_code"], "collection_abandoned")
            self.assertEqual(self.finish(tokens[0], now=4)["status"], "ignored")
            self.finish(tokens[1], now=5)
            self.assertEqual(self.row()["captured_count"], 1)

    def test_project_capacity_evicts_oldest_activity(self):
        with mock.patch.object(health, "MAX_PROJECTS", 2):
            self.attempt(P1, now=100)
            self.attempt(P2, now=200)
            self.attempt(P1, now=300)
            self.attempt(P3, now=400)
            report = health.read_health(self.home)
            self.assertEqual(set(report["projects"]), {P1, P3})
            self.assertEqual(report["evicted_projects"], 1)

    def test_counters_saturate_instead_of_overflowing(self):
        with mock.patch.object(health, "COUNTER_MAX", 2):
            for n in range(4):
                self.finish(self.attempt(now=n), now=n)
            row = self.row()
            self.assertEqual(row["attempt_count"], 2)
            self.assertEqual(row["captured_count"], 2)
            self.assertTrue(row["counters_saturated"])

    def test_threaded_writers_do_not_lose_updates(self):
        def job(index):
            result = health.record_attempt(self.home, P1, "codex", authorized=True, now=100 + index)
            if result["status"] != "recorded":
                return result
            return health.record_result(self.home, P1, "codex", result["attempt_id"], authorized=True,
                                        status="collected", record_id=R1, now=200 + index)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(job, range(24)))
        self.assertTrue(all(result["status"] == "recorded" for result in results), results)
        row = self.row(agent="codex")
        self.assertEqual((row["attempt_count"], row["captured_count"], row["pending_count"]), (24, 24, 0))

    def test_separate_process_writers_share_the_same_lock(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import nous_training_health as h
for i in range(8):
    a=h.record_attempt(sys.argv[2],sys.argv[3],'claude',authorized=True)
    assert a['status']=='recorded',a
    r=h.record_result(sys.argv[2],sys.argv[3],'claude',a['attempt_id'],authorized=True,status='collected',record_id=sys.argv[4])
    assert r['status']=='recorded',r
"""
        processes = [subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(HOOKS), str(self.home), P1, R1],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.row()["attempt_count"], 32)
        self.assertEqual(self.row()["captured_count"], 32)

    def test_read_never_observes_partial_json_during_updates(self):
        self.attempt()
        def writes():
            for n in range(30):
                token = self.attempt(now=200 + n)
                self.finish(token, now=300 + n)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writes)
            while not future.done():
                self.assertEqual(health.read_health(self.home)["status"], "ok")
            future.result()

    def test_atomic_replace_failure_preserves_previous_complete_state(self):
        self.attempt()
        before = self.state_path.read_bytes()
        with mock.patch.object(health.os, "replace", side_effect=OSError("PRIVATE RAW ERROR")):
            result = health.record_attempt(self.home, P1, "claude", authorized=True)
        self.assertEqual(result, {"status": "unavailable", "error": "health_io_failed"})
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual({path.name for path in self.folder.iterdir()}, {"state.json", ".lock"})

    def test_corrupt_state_is_not_echoed_or_silently_overwritten(self):
        self.attempt()
        bad = b'{"PRIVATE_RAW_CONTENT":"secret",bad'
        self.state_path.write_bytes(bad)
        report = health.read_health(self.home)
        self.assertEqual(report["status"], "corrupt")
        self.assertNotIn("PRIVATE", json.dumps(report))
        result = health.record_attempt(self.home, P1, "claude", authorized=True)
        self.assertEqual(result["error"], "health_state_corrupt")
        self.assertEqual(self.state_path.read_bytes(), bad)
        self.assertEqual(health.purge_health(self.home)["status"], "purged")
        self.assertFalse(self.state_path.exists())

    def test_duplicate_keys_unknown_fields_and_error_values_are_corrupt(self):
        self.attempt()
        valid = self.state_path.read_text()
        cases = [valid.replace('"schema":', '"schema":"ignored","schema":', 1)]
        body = json.loads(valid)
        body["projects"][P1]["claude"]["raw_path"] = "PRIVATE"
        cases.append(json.dumps(body))
        body = json.loads(valid)
        body["projects"][P1]["claude"]["last_error_code"] = ["PRIVATE"]
        cases.append(json.dumps(body))
        for raw in cases:
            self.state_path.write_text(raw)
            self.assertEqual(health.read_health(self.home)["status"], "corrupt")

    def test_oversized_state_read_is_bounded_and_diagnosed(self):
        self.attempt()
        self.state_path.write_bytes(b"x" * 101)
        with mock.patch.object(health, "MAX_STATE_BYTES", 100):
            report = health.read_health(self.home)
        self.assertEqual(report["error"], "health_state_too_large")

    def test_private_permissions_even_with_extreme_umask(self):
        old = os.umask(0o777)
        try:
            self.attempt()
        finally:
            os.umask(old)
        for path in (self.home, self.home / "training", self.folder):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in (self.state_path, self.folder / ".lock"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_read_does_not_chmod_overly_broad_health_directory(self):
        self.attempt()
        self.folder.chmod(0o755)
        self.assertEqual(health.read_health(self.home)["error"], "health_permissions")
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o755)

    def test_state_symlink_is_not_followed_or_modified(self):
        self.attempt()
        target = self.root / "private.json"
        target.write_text("PRIVATE")
        self.state_path.unlink()
        self.state_path.symlink_to(target)
        self.assertEqual(health.read_health(self.home)["status"], "unreadable")
        self.assertEqual(health.record_attempt(self.home, P1, "claude", authorized=True)["status"], "unavailable")
        self.assertEqual(target.read_text(), "PRIVATE")

    def test_health_directory_symlink_is_not_followed(self):
        target = self.root / "elsewhere"
        target.mkdir()
        self.home.mkdir()
        (self.home / "training").mkdir()
        self.folder.symlink_to(target, target_is_directory=True)
        self.assertEqual(health.record_attempt(self.home, P1, "claude", authorized=True)["error"], "health_unsafe_path")
        self.assertEqual(list(target.iterdir()), [])

    def test_fifo_state_does_not_block(self):
        self.attempt()
        self.state_path.unlink()
        os.mkfifo(self.state_path, 0o600)
        started = time.monotonic()
        self.assertEqual(health.read_health(self.home)["error"], "health_unsafe_path")
        self.assertLess(time.monotonic() - started, 1)

    def test_contended_lock_returns_bounded_static_diagnostic(self):
        self.attempt()
        with (self.folder / ".lock").open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            started = time.monotonic()
            with mock.patch.object(health, "LOCK_TIMEOUT", 0.03):
                result = health.record_attempt(self.home, P1, "claude", authorized=True)
            self.assertEqual(result["error"], "health_busy")
            self.assertLess(time.monotonic() - started, 0.5)

    def test_purge_one_project_preserves_other_and_late_result_cannot_restore_it(self):
        token = self.attempt(P1)
        self.attempt(P2)
        self.assertEqual(health.purge_health(self.home, P1)["status"], "purged")
        self.assertEqual(set(health.read_health(self.home)["projects"]), {P2})
        self.assertEqual(self.finish(token)["status"], "ignored")
        self.assertEqual(set(health.read_health(self.home)["projects"]), {P2})

    def test_clock_regression_does_not_overwrite_newer_success(self):
        first, second = self.attempt(now=100), self.attempt(now=200)
        self.finish(second, now=250, record_id=R2)
        self.finish(first, now=150, record_id=R1)
        self.assertEqual(self.row()["last_success_at"], 250)
        self.assertEqual(self.row()["last_record_id"], R2)
        self.assertEqual(self.row()["captured_count"], 2)


if __name__ == "__main__":
    unittest.main()
