"""Reproducible allowlisted package and isolated smoke checks; synthetic data only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import training_build as builder
import training_release_check as checker
import training_trainer_smoke as trainer_smoke


class TrainingRelease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nous-release-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        for name in checker.RELEASE_FILES:
            source, target = ROOT / name, self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def test_reproducible_archives_and_explicit_allowlist_exclude_private_material(self):
        (self.source / ".env").write_text("SYNTHETIC_SECRET=never-package")
        (self.source / "private-trajectory.jsonl").write_text('{"synthetic":true}')
        (self.source / "research-weights.bin").write_bytes(b"synthetic-not-model-weights")
        first = builder.build(self.source, self.base / "first", smoke=False)
        second = builder.build(self.source, self.base / "second", smoke=False)
        self.assertEqual(first["archives"], second["archives"])
        self.assertEqual(first["smoke"]["status"], "not_run")
        self.assertFalse(first["published"])
        prefix = "nous-training-0.1.0a9/"
        expected = {prefix + name for name in checker.RELEASE_FILES} | {prefix + "release-manifest.json"}
        with tarfile.open(self.base / "first" / (prefix[:-1] + ".tar.gz")) as archive:
            self.assertEqual(set(archive.getnames()), expected)
            self.assertTrue(all(m.isfile() and m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in archive))
        with zipfile.ZipFile(self.base / "first" / (prefix[:-1] + ".zip")) as archive:
            self.assertEqual(set(archive.namelist()), expected)
        manifest = json.loads((self.base / "first" / "release-manifest.json").read_text())
        for item in manifest["files"]:
            self.assertEqual(item["sha256"], hashlib.sha256((self.source / item["path"]).read_bytes()).hexdigest())
        self.assertEqual(manifest["external_python_dependencies"], [])
        self.assertEqual(manifest["optional_runtime_capabilities"][0]["name"], "git")
        self.assertFalse(manifest["windows_supported"])

    def test_clean_unpacked_package_smoke_runs_without_full_repository_or_site_packages(self):
        report = builder.build(self.source, self.base / "built", smoke=True)
        smoke = report["smoke"]
        self.assertEqual(smoke["status"], "passed", report)
        self.assertTrue(smoke["report"]["passed"])
        self.assertEqual(smoke["report"]["real_platform_acceptance"], "not_run")
        checks = {entry["name"]: entry for entry in smoke["report"]["checks"]}
        self.assertEqual(checks["source_tests"]["status"], "not_run")
        for name in ("all_runtime_imports", "cli_help", "isolated_doctor", "synthetic_demo", "offline_dataset_validation", "release_manifest",
                     "synthetic_request_demo", "offline_request_dataset_validation", "controlled_live_cli_help"):
            self.assertEqual(checks[name]["status"], "passed", name)

    def test_missing_runtime_and_symlink_are_rejected(self):
        path = self.source / "hooks/nous_training_validate.py"
        path.unlink()
        inventory = checker.runtime_inventory(self.source)
        self.assertIn("hooks/nous_training_validate.py", inventory["missing"])
        with self.assertRaises(builder.BuildError):
            builder.build(self.source, self.base / "missing", smoke=False)
        path.symlink_to(ROOT / "hooks/nous_training_validate.py")
        with self.assertRaises(builder.BuildError):
            builder.build(self.source, self.base / "symlink", smoke=False)

    def test_existing_output_directory_is_not_overwritten(self):
        out = self.base / "existing"
        out.mkdir()
        (out / "keep").write_text("preserve")
        with self.assertRaises(FileExistsError):
            builder.build(self.source, out, smoke=False)
        self.assertEqual((out / "keep").read_text(), "preserve")

    def test_package_file_tampering_fails_before_import_or_execution(self):
        builder.build(self.source, self.base / "built", smoke=False)
        extracted = self.base / "unpacked"
        extracted.mkdir()
        prefix = "nous-training-0.1.0a9"
        builder._unpack_tar(self.base / "built" / (prefix + ".tar.gz"), extracted, prefix)
        package = extracted / prefix
        with (package / "hooks/nous_training.py").open("a") as handle:
            handle.write("\n# tampered fixture\n")
        report = checker.check_release(package, smoke_only=True)
        self.assertFalse(report["passed"])
        self.assertIn("release_file_hash_mismatch", [x.get("code") for x in report["checks"]])
        self.assertNotIn("all_runtime_imports", [x["name"] for x in report["checks"]])

    def test_isolated_environment_preserves_operating_system_home_and_codex_home(self):
        env = checker.isolated_env(self.base)
        for key in ("HOME", "CODEX_HOME"):
            self.assertEqual(env.get(key), os.environ.get(key))
        self.assertEqual(env["NOUS_HOME"], str(self.base / "nous"))
        self.assertEqual(env["NOUS_LEDGER_HOME"], str(self.base / "ledger"))

    def unpack(self, destination):
        out = self.base / (destination + "-build")
        builder.build(self.source, out, smoke=False)
        unpacked = self.base / destination
        unpacked.mkdir()
        prefix = "nous-training-0.1.0a9"
        builder._unpack_tar(out / (prefix + ".tar.gz"), unpacked, prefix)
        return unpacked / prefix

    def test_unlisted_shadow_modules_and_directories_are_rejected_before_execution(self):
        for i, relative in enumerate(("hooks/hashlib.py", "scripts/hashlib.py", "hashlib.py", "hooks/injected/__init__.py")):
            with self.subTest(relative=relative):
                package = self.unpack("shadow-" + str(i))
                marker = self.base / ("executed-" + str(i))
                injected = package / relative
                injected.parent.mkdir(parents=True, exist_ok=True)
                injected.write_text("open(" + repr(str(marker)) + ", 'w').write('EXECUTED')\n")
                process = subprocess.run([sys.executable, "-I", "-S", "-B",
                                          str(package / "scripts/training_release_check.py"), "--smoke-only"],
                                         cwd=package, env=checker.isolated_env(self.base / "state"), capture_output=True, text=True)
                self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
                report = json.loads(process.stdout)
                self.assertFalse(report["passed"])
                self.assertIn(report["checks"][0]["code"], {"unlisted_package_file", "unlisted_package_directory"})
                self.assertFalse(marker.exists())

    def test_unsafe_checker_startup_refuses_before_scripts_shadow_import(self):
        package = self.unpack("unsafe-startup")
        marker = self.base / "unsafe-startup-executed"
        (package / "scripts/hashlib.py").write_text("open(" + repr(str(marker)) + ", 'w').write('EXECUTED')\n")
        process = subprocess.run([sys.executable, "-S", "-B", str(package / "scripts/training_release_check.py"), "--smoke-only"],
                                 cwd=package, env=checker.isolated_env(self.base / "state"), capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn("isolated_startup_required", process.stderr)
        self.assertFalse(marker.exists())

    def test_package_smoke_requires_manifest_and_cannot_downgrade_to_source_mode(self):
        package = self.unpack("missing-manifest")
        (package / "release-manifest.json").unlink()
        marker = self.base / "missing-manifest-executed"
        (package / "hooks/hashlib.py").write_text("open(" + repr(str(marker)) + ", 'w').write('EXECUTED')\n")
        for args, expected in ((["--smoke-only"], "package_manifest_required"), ([], "source_test_suite_missing")):
            process = subprocess.run([sys.executable, "-I", "-S", "-B", str(package / "scripts/training_release_check.py"), *args],
                                     cwd=package, env=checker.isolated_env(self.base / "state"), capture_output=True, text=True)
            self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
            self.assertIn(expected, [item.get("code") for item in json.loads(process.stdout)["checks"]])
            self.assertFalse(marker.exists())

    def test_git_capability_declaration_cannot_silently_disappear(self):
        package = self.unpack("capability-manifest")
        path = package / "release-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["optional_runtime_capabilities"] = []
        path.write_text(json.dumps(manifest))
        report = checker.check_release(package, smoke_only=True)
        self.assertFalse(report["passed"])
        self.assertEqual(report["checks"][0]["code"], "invalid_release_capability_declaration")

    def test_request_runtime_and_docs_are_part_of_exact_release_inventory(self):
        self.assertEqual(len(checker.RUNTIME_MODULES), 36)
        self.assertEqual(len(set(checker.RELEASE_FILES)), len(checker.RELEASE_FILES))
        for suffix in ("request", "verify", "transport", "observer", "request_demo"):
            self.assertIn("hooks/nous_training_" + suffix + ".py", checker.RELEASE_FILES)
        for suffix in ("REQUEST", "VERIFY", "TRANSPORT", "OBSERVER"):
            self.assertIn("docs/TRAINING_" + suffix + ".md", checker.RELEASE_FILES)
        for suffix in ("request", "verify", "transport", "observer", "request_pipeline"):
            self.assertIn("test_training_" + suffix, checker.REQUIRED_TESTS)

    def test_history_repair_runtime_is_packaged_and_cannot_be_omitted(self):
        for suffix in ("claude_replay", "anchor_recovery", "live_bridge"):
            name = "hooks/nous_training_" + suffix + ".py"
            self.assertIn(name, checker.RELEASE_FILES)
            with self.subTest(module=suffix):
                raw = (self.source / name).read_bytes()
                (self.source / name).unlink()
                try:
                    with self.assertRaisesRegex(builder.BuildError, "^required_source_missing_or_symlink$"):
                        builder.build(self.source, self.base / ("missing-" + suffix), smoke=False)
                finally:
                    (self.source / name).write_bytes(raw)
        for suffix in ("claude_replay", "anchor_recovery", "secret_sweep", "write_operands", "real_multiturn", "live_bridge"):
            self.assertIn("test_training_" + suffix, checker.REQUIRED_TESTS)
        self.assertIn("docs/TRAINING_CLAUDE_REPLAY.md", checker.RELEASE_FILES)
        self.assertIn("docs/TRAINING_HISTORY_REPAIR.md", checker.RELEASE_FILES)
        for name in ("real_multiturn", "live_smoke", "multiturn_smoke", "request_smoke", "header_probe"):
            self.assertIn("scripts/training_" + name + ".py", checker.RELEASE_FILES)
        self.assertEqual(len(checker.LIVE_FIXTURE_FILES), 11)
        self.assertFalse(any("reference" in name for name in checker.LIVE_FIXTURE_FILES))

    def test_optional_trainer_selects_native_request_serialization_without_converting_it(self):
        with mock.patch.object(sys, "path", [str(ROOT / "hooks"), *sys.path]):
            from nous_training_request import RequestError, sample_for
            from tests.test_training_request_pipeline import source
            sample = sample_for(source())
            validate = trainer_smoke._validator_for_profile("request-sft-v1")
            self.assertTrue(validate(sample))
            changed = dict(sample)
            prompt = json.loads(changed["prompt"])
            prompt["schema"] = "nous.observed-visible-events/1"
            changed["prompt"] = json.dumps(prompt) + "\n"
            with self.assertRaises(RequestError):
                validate(changed)
            with self.assertRaisesRegex(trainer_smoke.SmokeError, "^dataset_profile_mismatch$"):
                trainer_smoke._validator_for_profile("trl")


if __name__ == "__main__":
    unittest.main()
