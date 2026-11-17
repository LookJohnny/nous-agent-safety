"""The offline quick-start is runnable without installing hooks or reading history."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "hooks" / "nous_training.py"


class TrainingCLI(unittest.TestCase):
    def test_demo_and_offline_validation_are_isolated_and_detect_tampering(self):
        with tempfile.TemporaryDirectory(prefix="nous-cli-test-") as temporary:
            root = Path(temporary)
            home, ledger = root / "unused-user-data", root / "unused-user-ledger"
            env = dict(os.environ, NOUS_HOME=str(home), NOUS_LEDGER_HOME=str(ledger), PYTHONDONTWRITEBYTECODE="1")

            def cli(*args):
                return subprocess.run([sys.executable, "-B", str(CLI), *args], env=env,
                                      capture_output=True, text=True, timeout=30)

            output = root / "demo"
            demo = cli("demo", "--out", str(output))
            self.assertEqual(demo.returncode, 0, demo.stdout + demo.stderr)
            report = json.loads(demo.stdout)
            self.assertTrue(report["synthetic"])
            self.assertTrue(report["validation"]["valid"])
            self.assertGreater(report["files"]["sft.jsonl"]["records"], 0)
            self.assertEqual(report["files"]["dpo.jsonl"]["records"], 1)
            self.assertFalse(home.exists())
            self.assertFalse(ledger.exists())
            self.assertEqual(cli("validate", "--dataset", str(output)).returncode, 0)
            audit = cli("audit", "--dataset", str(output), "--require-training-rows")
            self.assertEqual(audit.returncode, 0, audit.stderr)
            summary = json.loads(audit.stdout)
            self.assertTrue(summary["structural_valid"])
            self.assertTrue(summary["training_rows_present"])
            self.assertEqual(summary["counts"]["sft"], 1)
            self.assertEqual(summary["counts"]["dpo"], 1)
            self.assertNotIn(str(output), audit.stdout)
            diagnosed = cli("diagnose", "--dataset", str(output), "--require-training-rows")
            self.assertEqual(diagnosed.returncode, 0, diagnosed.stderr)
            summary = json.loads(diagnosed.stdout)
            self.assertTrue(summary["structural_valid"])
            self.assertTrue(summary["diagnosis_available"])
            self.assertTrue(summary["training_eligibility_not_reassessed"])
            self.assertNotIn(str(output), diagnosed.stdout)

            # A recomputed checksum must not hide structurally altered samples.
            sft = output / "sft.jsonl"
            rows = [json.loads(line) for line in sft.read_text().splitlines()]
            rows[0]["unexpected_training_field"] = True
            sft.write_text("".join(json.dumps(row) + "\n" for row in rows))
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            raw = sft.read_bytes()
            manifest["files"]["sft.jsonl"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            manifest_path.write_text(json.dumps(manifest))
            invalid = cli("validate", "--dataset", str(output))
            self.assertEqual(invalid.returncode, 1)
            self.assertFalse(json.loads(invalid.stdout)["valid"])
            audit = cli("audit", "--dataset", str(output))
            self.assertEqual(audit.returncode, 1, audit.stderr)
            self.assertFalse(json.loads(audit.stdout)["structural_valid"])
            diagnosed = cli("diagnose", "--dataset", str(output))
            self.assertEqual(diagnosed.returncode, 1, diagnosed.stderr)
            self.assertFalse(json.loads(diagnosed.stdout)["structural_valid"])
            self.assertFalse(home.exists())
            self.assertFalse(ledger.exists())

    def test_audit_candidate_only_exit_gate_does_not_create_user_state(self):
        with tempfile.TemporaryDirectory(prefix="nous-audit-cli-") as temporary:
            root = Path(temporary)
            env = dict(os.environ, NOUS_HOME=str(root / 'unused-home'), NOUS_LEDGER_HOME=str(root / 'unused-ledger'),
                       PYTHONDONTWRITEBYTECODE='1')
            script = ("import sys;sys.path.insert(0,sys.argv[1]);"
                      "from nous_training_export import export_dataset;"
                      "export_dataset([],sys.argv[2])")
            built = subprocess.run([sys.executable, '-B', '-c', script, str(CLI.parent), str(root / 'dataset')],
                                   env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(built.returncode, 0, built.stderr)
            for name in ('audit', 'diagnose'):
                for require_rows, expected in ((False, 0), (True, 1)):
                    command = [sys.executable, '-B', str(CLI), name, '--dataset', str(root / 'dataset')]
                    if require_rows:
                        command.append('--require-training-rows')
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertTrue(report['structural_valid'])
                    self.assertFalse(report['training_rows_present'])
                    if name == 'diagnose':
                        self.assertTrue(report['diagnosis_available'])
            self.assertFalse((root / 'unused-home').exists())
            self.assertFalse((root / 'unused-ledger').exists())

    def test_source_context_cli_is_explicit_read_only_and_never_claims_complete_request(self):
        with tempfile.TemporaryDirectory(prefix="nous-source-cli-") as temporary:
            root = Path(temporary)
            home, ledger = root / 'unused-home', root / 'unused-ledger'
            env = dict(os.environ, NOUS_HOME=str(home), NOUS_LEDGER_HOME=str(ledger), PYTHONDONTWRITEBYTECODE='1')
            source = root / 'synthetic-source.jsonl'
            rows = [json.loads(line) for line in (CLI.parent.parent / 'examples/training/codemode.jsonl').read_text().splitlines()]
            rows[0]['payload'].update(cli_version='0.169.0-alpha.11.1', id='synthetic-cli-context')
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            before = source.read_bytes()
            command = [sys.executable, '-B', str(CLI), 'inspect-source', '--transcript', str(source)]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['valid'])
            self.assertFalse(report['request_context_verified'])
            self.assertEqual(report['source_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(source.read_bytes(), before)
            self.assertNotIn(str(source), result.stdout)
            self.assertNotIn('Handwritten synthetic fixture', result.stdout)
            source.write_text('PRIVATE_INVALID_SOURCE_NEVER_ECHO')
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)['valid'])
            self.assertNotIn('PRIVATE_INVALID_SOURCE_NEVER_ECHO', result.stdout + result.stderr)
            self.assertFalse(home.exists())
            self.assertFalse(ledger.exists())


if __name__ == "__main__":
    unittest.main()
