"""Isolated config fixtures and synthetic stdin; never touch installed agent settings."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import nous_training_hook as hook
import nous_training_setup as setup


def copy_json(value):
    return json.loads(json.dumps(value))


class TrainingSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nous-setup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "data space $literal"
        self.configs = {"claude": self.root / "claude" / "settings.json",
                        "codex": self.root / "codex" / "hooks.json"}

    def write(self, agent, settings):
        path = self.configs[agent]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        return path

    def settings(self, agent):
        return json.loads(self.configs[agent].read_text())

    def install(self, agent="both"):
        return setup.setup(agent, self.project, self.home, config_path=self.configs)

    def remove(self, agent="both", **kwargs):
        return setup.uninstall(agent, self.project, self.home, config_path=self.configs, **kwargs)

    def runtime(self, name):
        folder = self.root / name
        folder.mkdir()
        for filename in setup.RUNTIME_FILES:
            (folder / filename).touch()
        return folder / 'nous_training_hook.py'

    def handler(self, agent='claude', **kwargs):
        return {'type': 'command', 'command': setup.command(agent, self.home, **kwargs), 'timeout': 20}

    def test_doctor_reports_disabled_platform_without_overriding_it(self):
        self.write("claude", {"disableAllHooks": True})
        report = self.install("claude")
        self.assertTrue(self.settings("claude")["disableAllHooks"])
        self.assertIn("platform_hooks_disabled", report["agents"][0]["issues"])
        self.assertTrue(report["agents"][0]["platform_disabled"])

    def test_explicit_ledger_path_matches_installed_command_and_doctor(self):
        ledger = self.root / "custom ledger"
        report = setup.setup("codex", self.project, self.home, config_path=self.configs, ledger_home=ledger)
        self.assertTrue(report["installed"])
        handler = self.settings("codex")["hooks"]["Stop"][0]["hooks"][0]
        self.assertIn("NOUS_LEDGER_HOME=" + str(ledger.resolve()), shlex.split(handler["command"]))
        diagnostic = setup.doctor("codex", self.project, self.home, config_path=self.configs, ledger_home=ledger)
        self.assertTrue(diagnostic["installed"])

    def test_doctor_is_read_only_and_does_not_claim_trust(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        report = setup.doctor("both", self.project, self.home, config_path=self.configs)
        self.assertFalse(report["installed"])
        self.assertFalse(report["capture_enabled"])
        self.assertFalse(report["execution_verified"])
        self.assertTrue(all(p["trust"] == "unknown" for p in report["agents"]))
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_doctor_detects_missing_export_validator_dependencies(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        for name in setup.RUNTIME_FILES:
            (runtime / name).touch()
        for name in ("nous_training_schema.py", "nous_training_validate.py", "nous_training_demo.py"):
            (runtime / name).unlink()
            with mock.patch.object(setup, "HOOK", runtime / "nous_training_hook.py"):
                report = setup.doctor("both", self.project, self.home, config_path=self.configs)
            self.assertFalse(report["runtime_present"])
            self.assertEqual(report["missing_runtime_files"], [name])
            self.assertIn("missing_runtime_files", report["warnings"])
            (runtime / name).touch()

    def test_doctor_reports_missing_git_without_claiming_privacy_verification(self):
        with mock.patch.object(setup.shutil, "which", return_value=None):
            report = setup.doctor("both", self.project, self.home, config_path=self.configs)
        self.assertFalse(report["capabilities"]["git_available"])
        self.assertFalse(report["capabilities"]["file_exclusion_verified"])
        self.assertIn("git_unavailable_file_tool_content_will_be_excluded", report["warnings"])
        self.assertFalse(self.home.exists())

    def test_setup_only_installs_stop_without_enabling_collection(self):
        report = self.install()
        self.assertTrue(report["installed"])
        self.assertTrue(report["changed"])
        self.assertFalse(report["capture_enabled"])
        self.assertFalse(self.home.exists())
        for agent in setup.AGENTS:
            settings = self.settings(agent)
            self.assertEqual(set(settings["hooks"]), {"Stop"})
            handler = settings["hooks"]["Stop"][0]["hooks"][0]
            self.assertNotIn("async", handler)
            args = shlex.split(handler["command"])
            self.assertEqual(args[:3], ["/usr/bin/env", "NOUS_HOME=" + str(self.home.resolve()),
                                       "NOUS_LEDGER_HOME=" + str(self.home.resolve() / "ledger")])
            self.assertEqual(args[3], str(Path(sys.executable).resolve()))
            self.assertEqual(args[4], str(HOOKS / "nous_training_hook.py"))
            self.assertEqual(args[5:], ["--agent", agent, "--managed-marker", setup.MARKER])
            self.assertNotIn("nous_hook.py", handler["command"])

    def test_mixed_groups_keep_user_handlers_and_unrelated_settings(self):
        own = {"type": "command", "command": setup.command("claude", self.home), "timeout": 20}
        user = {"type": "command", "command": "echo user-hook"}
        mimic = {"type": "command", "command": "echo nous_training_hook.py --managed-marker nous-training-v1"}
        settings = {"model": "fixture-model", "env": {"EXISTING": "keep"},
                    "permissions": {"deny": ["Read(.env)"]},
                    "hooks": {"Stop": [{"matcher": "", "custom": "keep-group", "hooks": [own, user]}],
                              "UserPromptSubmit": [{"hooks": [mimic]}]}}
        self.write("claude", settings)
        self.install("claude")
        actual = self.settings("claude")
        self.assertEqual(actual["env"], settings["env"])
        self.assertEqual(actual["permissions"], settings["permissions"])
        self.assertEqual(actual["hooks"]["Stop"][0], {"matcher": "", "custom": "keep-group", "hooks": [own, user]})
        self.assertEqual(actual["hooks"]["UserPromptSubmit"], settings["hooks"]["UserPromptSubmit"])
        self.assertEqual(sum(setup._owned(h) for g in actual["hooks"]["Stop"] for h in g["hooks"]), 1)

    def test_reinstall_is_idempotent_and_keeps_original_backup(self):
        path = self.write("codex", {"unrelated": True})
        before = path.read_bytes()
        first = self.install("codex")
        backup = Path(first["agents"][0]["backup_path"])
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        installed = path.read_bytes()
        backups = sorted(path.parent.glob("*.bak-nous-training-*"))
        second = self.install("codex")
        self.assertFalse(second["changed"])
        self.assertIsNone(second["agents"][0]["backup_path"])
        self.assertEqual(installed, path.read_bytes())
        self.assertEqual(backups, sorted(path.parent.glob("*.bak-nous-training-*")))

    def test_corrupt_json_preflight_never_overwrites_either_platform(self):
        claude = self.write("claude", {"model": "keep"})
        codex = self.write("codex", {})
        codex.write_text('{"hooks": BROKEN synthetic-secret', encoding="utf-8")
        originals = {p: p.read_bytes() for p in (claude, codex)}
        with self.assertRaisesRegex(setup.SetupError, "invalid_config_json"):
            self.install()
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob("*.bak-nous-training-*")), [])
        report = setup.doctor("codex", self.project, self.home, config_path=self.configs)
        self.assertIn("invalid_config_json", report["agents"][0]["issues"])
        self.assertNotIn("synthetic-secret", json.dumps(report))

    def test_invalid_shapes_and_symlinks_are_not_replaced(self):
        for value in ([], {"hooks": []}, {"hooks": {"Stop": [{"hooks": "broken"}]}}):
            path = self.write("claude", value)
            before = path.read_bytes()
            with self.assertRaises(setup.SetupError):
                self.install("claude")
            self.assertEqual(path.read_bytes(), before)
        link = self.configs["codex"]
        link.parent.mkdir()
        target = self.root / "target.json"
        target.write_text("{}")
        link.symlink_to(target)
        with self.assertRaisesRegex(setup.SetupError, "config_symlink_not_supported"):
            self.install("codex")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "{}")

    def test_partial_override_is_rejected_instead_of_falling_back_to_user_home(self):
        with self.assertRaisesRegex(setup.SetupError, "config_path_mapping_incomplete"):
            setup.setup("both", self.project, self.home, config_path={"claude": self.configs["claude"]})
        self.assertFalse(self.configs["claude"].exists())

    def test_doctor_detects_legacy_without_disabling_it(self):
        legacy = {"type": "command", "command": "python3 /synthetic/nous_hook.py stop-check"}
        self.write("claude", {"hooks": {"Stop": [{"hooks": [legacy]}]}})
        report = self.install("claude")
        self.assertTrue(report["agents"][0]["legacy_hook_present"])
        self.assertIn("legacy_safety_hook_may_also_collect", report["agents"][0]["issues"])
        self.assertEqual(self.settings("claude")["hooks"]["Stop"][0]["hooks"], [legacy])

    def test_doctor_reports_capture_disabled_or_enabled_without_reading_records(self):
        self.install("claude")
        import hashlib
        pid = "p_" + hashlib.sha256(os.path.realpath(self.project).encode()).hexdigest()[:24]
        config = self.home / "training" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"projects": {pid: {"capture": True}}}))
        report = setup.doctor("claude", self.project, self.home, config_path=self.configs)
        self.assertTrue(report["capture_enabled"])
        self.assertFalse(report["execution_verified"])
        self.assertFalse((self.home / "training" / "records").exists())

    def test_upgrade_repoints_old_runtime_in_place_and_keeps_exact_backup(self):
        old_hook, new_hook = self.runtime('old release'), self.runtime('new release')
        with mock.patch.object(setup, 'HOOK', old_hook):
            old_handler = self.handler()
        user = {'type': 'command', 'command': 'echo user hook'}
        settings = {'model': 'keep-model', 'hooks': {'Stop': [{'matcher': '', 'hooks': [user, old_handler]}]}}
        path = self.write('claude', settings)
        original = path.read_bytes()
        with mock.patch.object(setup, 'HOOK', new_hook):
            before = setup.doctor('claude', self.project, self.home, config_path=self.configs)
            self.assertIn('outdated_hook_script_path', before['agents'][0]['issues'])
            self.assertFalse(before['installed'])
            report = self.install('claude')
            expected = self.handler()
        self.assertTrue(report['installed'])
        self.assertTrue(report['configuration_ready'])
        self.assertFalse(report['execution_verified'])
        self.assertEqual(self.settings('claude')['hooks']['Stop'], [{'matcher': '', 'hooks': [user, expected]}])
        self.assertEqual(self.settings('claude')['model'], 'keep-model')
        self.assertEqual(Path(report['agents'][0]['backup_path']).read_bytes(), original)

    def test_uninstall_preserves_mixed_handlers_group_metadata_and_legacy_safety(self):
        managed = self.handler()
        user = {'type': 'command', 'command': 'echo user'}
        mimic = {'type': 'command', 'command': 'echo nous_training_hook.py --managed-marker nous-training-v1'}
        legacy = {'type': 'command', 'command': 'python3 /synthetic/nous_hook.py stop-check'}
        settings = {'disableAllHooks': True, 'permissions': {'deny': ['Read(.env)']}, 'env': {'USER': 'keep'},
                    'hooks': {'Stop': [{'matcher': '', 'custom': 'keep', 'hooks': [managed, user]},
                                      {'hooks': [legacy]}], 'Other': [{'hooks': [mimic]}]}}
        path = self.write('claude', settings)
        before = path.read_bytes()
        report = self.remove('claude')
        expected = copy_json(settings)
        expected['hooks']['Stop'][0]['hooks'] = [user]
        self.assertEqual(self.settings('claude'), expected)
        self.assertEqual(report['removed_handlers'], 1)
        self.assertTrue(report['uninstalled'])
        self.assertTrue(report['agents'][0]['legacy_hook_present'])
        self.assertEqual(Path(report['agents'][0]['backup_path']).read_bytes(), before)
        self.assertFalse(report['trust_changed'])

    def test_uninstall_is_idempotent_and_does_not_create_more_backups(self):
        self.install()
        first = self.remove()
        self.assertTrue(first['uninstalled'])
        self.assertEqual(first['removed_handlers'], 2)
        self.assertFalse(first['installed'])
        configs = {agent: path.read_bytes() for agent, path in self.configs.items()}
        backups = sorted(str(p) for p in self.root.rglob('*.bak-nous-training-*'))
        second = self.remove()
        self.assertFalse(second['changed'])
        self.assertEqual(second['removed_handlers'], 0)
        self.assertTrue(second['uninstalled'])
        self.assertEqual(backups, sorted(str(p) for p in self.root.rglob('*.bak-nous-training-*')))
        for agent, path in self.configs.items():
            self.assertEqual(path.read_bytes(), configs[agent])

    def test_uninstall_missing_config_does_not_create_platform_or_training_directories(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        report = self.remove()
        self.assertTrue(report['uninstalled'])
        self.assertFalse(report['changed'])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_uninstall_keeps_authorization_records_keys_and_other_projects(self):
        self.install('claude')
        pid = setup._project_state(self.project, self.home)[0]
        config = self.home / 'training' / 'config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'projects': {pid: {'capture': True}, 'p_other': {'capture': True}}}))
        records = self.home / 'training' / 'records'
        records.mkdir()
        (records / 'synthetic.json').write_text('SYNTHETIC_DATA_UNCHANGED')
        ledger = self.home / 'ledger'
        ledger.mkdir()
        (ledger / 'ledger.key').write_bytes(b'SYNTHETIC_KEY_UNCHANGED')
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        report = self.remove('claude')
        self.assertTrue(report['capture_enabled'])
        self.assertFalse(report['authorization_changed'])
        self.assertFalse(report['data_deleted'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_user_modified_handlers_are_preserved_by_setup_and_uninstall(self):
        for change in ({'timeout': 5}, {'timeout': 20.0}, {'async': True}, {'custom': 'keep'},
                       {'command': self.handler()['command'] + ' --user-flag'},
                       {'command': '  ' + self.handler()['command']}):
            with self.subTest(fields=list(change)):
                modified = {**self.handler(), **change}
                path = self.write('claude', {'hooks': {'Stop': [{'hooks': [modified]}]}})
                original = path.read_bytes()
                installed = self.install('claude')
                self.assertFalse(installed['changed'])
                self.assertFalse(installed['installed'])
                self.assertIn('modified_managed_handler_preserved', installed['agents'][0]['issues'])
                removed = self.remove('claude')
                self.assertFalse(removed['changed'])
                self.assertFalse(removed['uninstalled'])
                self.assertEqual(removed['removed_handlers'], 0)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob('*.bak-nous-training-*')), [])

    def test_other_data_home_and_other_agent_handlers_are_never_removed(self):
        other_home = {**self.handler(), 'command': setup.command('claude', self.root / 'other home')}
        other_agent = self.handler('codex')
        settings = {'hooks': {'Stop': [{'hooks': [self.handler(), other_home, other_agent]}]}}
        self.write('claude', settings)
        report = self.remove('claude')
        self.assertEqual(self.settings('claude')['hooks']['Stop'][0]['hooks'], [other_home, other_agent])
        self.assertEqual(report['removed_handlers'], 1)
        self.assertEqual(report['agents'][0]['other_scope_hook_count'], 2)
        self.assertTrue(report['uninstalled'])

    def test_changed_ledger_scope_is_preserved_and_does_not_install_duplicate(self):
        ledger = self.root / 'other ledger'
        handler = self.handler(ledger_home=ledger)
        path = self.write('claude', {'hooks': {'Stop': [{'hooks': [handler]}]}})
        original = path.read_bytes()
        report = self.install('claude')
        self.assertFalse(report['changed'])
        self.assertFalse(report['installed'])
        self.assertEqual(report['agents'][0]['storage_conflict_count'], 1)
        removed = self.remove('claude')
        self.assertFalse(removed['uninstalled'])
        self.assertEqual(path.read_bytes(), original)
        exact = self.remove('claude', ledger_home=ledger)
        self.assertTrue(exact['uninstalled'])
        self.assertEqual(exact['removed_handlers'], 1)

    def test_uninstall_old_missing_runtime_and_interpreter_remains_possible(self):
        handler = self.handler()
        args = shlex.split(handler['command'])
        args[3] = str(self.root / 'missing-python')
        args[4] = str(self.root / 'missing-release' / 'nous_training_hook.py')
        handler['command'] = shlex.join(args)
        self.write('claude', {'hooks': {'Stop': [{'hooks': [handler]}]}})
        before = setup.doctor('claude', self.project, self.home, config_path=self.configs)
        self.assertFalse(before['installed'])
        self.assertFalse(before['configuration_ready'])
        self.assertTrue({'outdated_hook_script_path', 'configured_hook_script_missing',
                         'configured_runtime_incomplete', 'configured_python_unavailable'} <= set(before['agents'][0]['issues']))
        report = self.remove('claude')
        self.assertTrue(report['uninstalled'])
        self.assertEqual(report['removed_handlers'], 1)

    def test_uninstall_keeps_empty_user_groups_and_custom_metadata(self):
        settings = {'hooks': {'Stop': [{'hooks': [self.handler()]},
                                      {'matcher': 'custom-filter', 'custom': 'keep', 'hooks': [self.handler()]},
                                      {'hooks': []}], 'Other': []}}
        self.write('claude', settings)
        report = self.remove('claude')
        self.assertEqual(report['removed_handlers'], 2)
        self.assertEqual(self.settings('claude'), {'hooks': {'Stop': [
            {'matcher': 'custom-filter', 'custom': 'keep', 'hooks': []}, {'hooks': []}], 'Other': []}})

    def test_uninstall_invalid_second_config_does_not_modify_first(self):
        self.install()
        first = self.configs['claude'].read_bytes()
        self.configs['codex'].write_text('{BROKEN SYNTHETIC_PRIVATE}')
        with self.assertRaisesRegex(setup.SetupError, '^invalid_config_json$'):
            self.remove()
        self.assertEqual(self.configs['claude'].read_bytes(), first)
        self.assertEqual(list(self.configs['claude'].parent.glob('*.bak-nous-training-*')), [])

    def test_duplicate_json_keys_are_never_rewritten_by_setup_or_uninstall(self):
        path = self.write('claude', {})
        path.write_text('{"model":"one","model":"SYNTHETIC_PRIVATE"}')
        original = path.read_bytes()
        for operation in (self.install, self.remove):
            with self.assertRaisesRegex(setup.SetupError, '^invalid_config_json$'):
                operation('claude')
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('*.bak-nous-training-*')), [])

    def test_config_edit_after_read_is_detected_without_backup_or_overwrite(self):
        path = self.write('claude', {'model': 'original'})
        original = path.read_bytes()
        path.write_text('{"model":"edited by user"}')
        with self.assertRaisesRegex(setup.SetupError, '^config_changed_during_update$'):
            setup._write(path, {'model': 'replacement'}, original)
        self.assertEqual(path.read_text(), '{"model":"edited by user"}')
        self.assertEqual(list(path.parent.glob('*.bak-nous-training-*')), [])

    def test_incomplete_runtime_blocks_setup_before_touching_config(self):
        hook_path = self.runtime('incomplete-runtime')
        hook_path.with_name('nous_training_health.py').unlink()
        path = self.write('claude', {'model': 'keep'})
        original = path.read_bytes()
        with mock.patch.object(setup, 'HOOK', hook_path):
            with self.assertRaisesRegex(setup.SetupError, '^runtime_files_missing$'):
                self.install('claude')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('*.bak-nous-training-*')), [])

    def test_doctor_does_not_report_installed_when_configured_runtime_is_incomplete(self):
        hook_path = self.runtime('configured-runtime')
        with mock.patch.object(setup, 'HOOK', hook_path):
            self.write('claude', {'hooks': {'Stop': [{'hooks': [self.handler()]}]}})
            hook_path.with_name('nous_training_export.py').unlink()
            report = setup.doctor('claude', self.project, self.home, config_path=self.configs)
        self.assertFalse(report['installed'])
        self.assertFalse(report['configuration_ready'])
        self.assertFalse(report['agents'][0]['configured'])
        self.assertIn('configured_runtime_incomplete', report['agents'][0]['issues'])
        self.assertEqual(report['agents'][0]['missing_configured_runtime_files'], ['nous_training_export.py'])

    def test_duplicate_owned_handlers_are_diagnosed_and_uninstall_removes_only_them(self):
        self.write('claude', {'hooks': {'Stop': [{'hooks': [self.handler(), self.handler()]}]}})
        report = setup.doctor('claude', self.project, self.home, config_path=self.configs)
        self.assertTrue(report['installed'])
        self.assertFalse(report['configuration_ready'])
        self.assertIn('duplicate_managed_hooks', report['agents'][0]['issues'])
        removed = self.remove('claude')
        self.assertEqual(removed['removed_handlers'], 2)
        self.assertTrue(removed['uninstalled'])

    def test_upgrade_preserves_user_event_and_filter_instead_of_adding_unconditional_stop(self):
        old_hook = self.runtime('filtered-old-runtime')
        with mock.patch.object(setup, 'HOOK', old_hook):
            old = self.handler()
        self.write('claude', {'hooks': {'UserPromptSubmit': [{'matcher': 'user-filter', 'hooks': [old]}]}})
        report = self.install('claude')
        self.assertFalse(report['installed'])
        self.assertFalse(report['configuration_ready'])
        self.assertNotIn('Stop', self.settings('claude')['hooks'])
        self.assertEqual(self.settings('claude')['hooks']['UserPromptSubmit'][0]['matcher'], 'user-filter')
        self.assertTrue({'managed_hook_event_mismatch', 'managed_hook_filter_preserved'} <= set(report['agents'][0]['issues']))

    def test_doctor_health_is_project_scoped_read_only_and_not_execution_proof(self):
        self.install('claude')
        import nous_training_health
        pid = setup._project_state(self.project, self.home)[0]
        health = {'status': 'ok', 'projects': {pid: {'codex': {'attempted': 2}}}}
        with mock.patch.object(nous_training_health, 'read_health', return_value=health) as read:
            report = setup.doctor('claude', self.project, self.home, config_path=self.configs)
        read.assert_called_once_with(self.home.resolve(), pid)
        self.assertEqual(report['collection_health'], health)
        self.assertFalse(report['execution_verified'])
        self.assertFalse(self.home.exists())

    def test_doctor_health_failure_has_no_private_exception_text(self):
        import nous_training_health
        with mock.patch.object(nous_training_health, 'read_health', side_effect=RuntimeError('SYNTHETIC_PRIVATE')):
            report = setup.doctor('both', self.project, self.home, config_path=self.configs)
        self.assertEqual(report['collection_health']['status'], 'unavailable')
        self.assertIn('collection_health_unavailable', report['warnings'])
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(report))

    def test_independent_hook_captures_without_trace_or_services(self):
        import nous_training
        import nous_ledger
        with mock.patch.object(nous_training, "HOME", self.home), \
             mock.patch.object(nous_ledger, "_key", return_value=b"synthetic-training-test-key-32-bytes"):
            nous_training.set_project(str(self.project), True)
        # The old trace is explicitly off; no services, token or credentials exist.
        (self.home / "policy.toml").write_text('version = 1\n[trace]\nlevel = "off"\n')
        transcript = self.root / "fixture.jsonl"
        transcript.write_text(json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "2+2?"}}) + "\n" +
                              json.dumps({"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": "4"}}) + "\n")
        payload = {"hook_event_name": "Stop", "cwd": str(self.project), "session_id": "fixture-session",
                   "transcript_path": str(transcript), "last_assistant_message": "4"}
        args = shlex.split(setup.command("claude", self.home))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", NOUS_LOCAL_API="http://127.0.0.1:9")
        for _ in range(2):
            result = subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
        records = list((self.home / "training" / "records").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["messages"][-1]["content"][0]["text"], "4")
        self.assertTrue((self.home / "ledger" / "ledger.key").exists())
        self.assertFalse((self.home / "hook.log").exists())
        self.assertFalse((self.home / "chores.json").exists())


class TrainingHook(unittest.TestCase):
    args = ["--agent", "codex", "--managed-marker", setup.MARKER]

    def run_hook(self, raw, args=None):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            result = hook.main(self.args if args is None else args, stream=io.BytesIO(raw), error_stream=stderr)
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")
        return stderr.getvalue()

    def test_empty_invalid_oversized_stdin_and_bad_arguments_soft_exit(self):
        for raw in (b"", b"not JSON synthetic-secret", b"[]", b'{"x":NaN}', b"x" * (hook.MAX_INPUT_BYTES + 1)):
            self.assertEqual(self.run_hook(raw), "nous-training: invalid_hook_input\n")
        self.assertEqual(self.run_hook(b"{}", args=["--secret", "synthetic-secret"]),
                         "nous-training: invalid_hook_input\n")

    def test_unsupported_events_are_silent_and_missing_fields_are_safe(self):
        self.assertEqual(self.run_hook(b'{"hook_event_name":"SessionEnd"}'), "")
        self.assertEqual(self.run_hook(b'{"hook_event_name":"Stop"}'), "nous-training: incomplete_hook_input\n")

    def test_collector_exception_never_exposes_source_text(self):
        import nous_training
        raw = json.dumps({"hook_event_name": "Stop", "cwd": "/synthetic/project", "session_id": "s1",
                          "transcript_path": "/synthetic/session.jsonl"}).encode()
        with mock.patch.object(nous_training, "capture_stop", side_effect=RuntimeError("synthetic-secret")):
            self.assertEqual(self.run_hook(raw), "nous-training: collection_failed\n")

    def test_disabled_project_does_not_parse_or_write(self):
        import nous_training
        with tempfile.TemporaryDirectory(prefix="nous-hook-disabled-") as root:
            home = Path(root) / "home"
            raw = json.dumps({"hook_event_name": "Stop", "cwd": root, "session_id": "s1",
                              "transcript_path": "/does-not-exist"}).encode()
            with mock.patch.object(nous_training, "HOME", home), \
                 mock.patch.object(nous_training, "build_record", side_effect=AssertionError("must not parse")):
                self.assertEqual(self.run_hook(raw), "")
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
