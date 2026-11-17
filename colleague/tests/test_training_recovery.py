"""Recovery must preserve identity and refuse stale writes or revoked consent."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hooks'))
import nous_ledger
import nous_training as training
import nous_transcript
import nous_training_health as health


class Recovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='nous-recovery-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'home'
        self.project = self.root / 'project'
        self.project.mkdir()
        self.source = self.root / 'fixture.jsonl'
        self.rows = [
            {'type': 'user', 'uuid': 'u1', 'sessionId': 'synthetic-session',
             'message': {'role': 'user', 'content': 'What is two plus two?'}},
            {'type': 'assistant', 'uuid': 'a1', 'sessionId': 'synthetic-session',
             'message': {'role': 'assistant', 'content': 'Four.'}},
        ]
        self.write()
        for patch in (mock.patch.object(training, 'HOME', self.home),
                      mock.patch.object(nous_ledger, '_key', return_value=b'synthetic-recovery-key-32-bytes!!')):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, rows=None):
        self.source.write_text(''.join(json.dumps(row) + '\n' for row in (self.rows if rows is None else rows)))

    def collect(self, **kwargs):
        return training.collect(self.source, str(self.project), manual=True, **kwargs)['id']

    def stored(self, rid):
        return json.loads(self.record_path(rid).read_text())

    def record_path(self, rid):
        return self.home / 'training' / 'records' / (rid + '.json')

    def recover(self, rid, **kwargs):
        return training.recover(rid, self.source, str(self.project), **kwargs)

    def test_partial_tail_recovers_same_record_after_actual_flush(self):
        self.write(self.rows[:1])
        with self.source.open('a') as out:
            out.write('{"type":')
        rid = self.collect(session='hook-session-alias')
        old = self.stored(rid)
        self.assertIn('partial_tail', old['quality']['issues'])
        self.write()
        result = self.recover(rid)
        record = self.stored(rid)
        self.assertEqual(result['status'], 'recovered')
        self.assertNotIn('partial_tail', record['quality']['issues'])
        self.assertEqual(record['provenance']['session_id'], old['provenance']['session_id'])
        self.assertEqual(len(list(self.record_path(rid).parent.glob('*.json'))), 1)
        self.assertEqual(record['evidence'], [])
        self.assertFalse(training._allowed(training.project_id(str(self.project)), 'capture'))

    def test_unchanged_source_keeps_review_but_modified_answer_drops_it(self):
        rid = self.collect(session='hook-session-alias')
        training.review(rid, 'synthetic-reviewer', 'accepted')
        result = self.recover(rid)
        self.assertFalse(result['content_changed'])
        self.assertEqual(result['reviews_preserved'], 1)
        self.rows[-1]['message']['content'] = '2 + 2 = 4.'
        self.write()
        self.assertTrue(self.recover(rid)['content_changed'])
        self.assertEqual(self.stored(rid)['evidence'], [])

    def test_codemode_review_survives_only_identical_bound_contents(self):
        self.source.write_bytes((ROOT / 'examples/training/codemode.jsonl').read_bytes())
        rid = self.collect(agent='codex', session='native-hook-alias')
        training.review(rid, 'synthetic-reviewer', 'accepted')
        training.review(rid, 'synthetic-reviewer', 'cleared', purpose='content')
        result = self.recover(rid, agent='codex')
        self.assertFalse(result['content_changed'])
        self.assertEqual(result['reviews_preserved'], 2)

    def test_wrong_source_and_project_rejected_before_source_read(self):
        rid = self.collect()
        with mock.patch.object(training, 'build_record', side_effect=AssertionError('do not read')):
            with self.assertRaisesRegex(training.TrainingError, '^recovery_source_mismatch$'):
                training.recover(rid, self.root / 'other.jsonl', str(self.project))
            with self.assertRaisesRegex(training.TrainingError, '^recovery_project_mismatch$'):
                training.recover(rid, self.source, str(self.root / 'other-project'))

    def test_reused_path_with_different_native_session_rejected(self):
        rid = self.collect()
        original = self.record_path(rid).read_bytes()
        for row in self.rows:
            row['sessionId'] = 'different-session'
        self.write()
        with self.assertRaisesRegex(training.TrainingError, '^recovery_session_mismatch$'):
            self.recover(rid)
        self.assertEqual(self.record_path(rid).read_bytes(), original)

    def test_truncated_source_cannot_replace_longer_record(self):
        rid = self.collect()
        original = self.record_path(rid).read_bytes()
        self.write(self.rows[:1])
        with self.assertRaisesRegex(training.TrainingError, '^recovery_source_regressed$'):
            self.recover(rid)
        self.assertEqual(self.record_path(rid).read_bytes(), original)

    def test_revoked_record_cannot_regrant_permission_via_recovery(self):
        rid = self.collect()
        training.set_project(str(self.project), False)
        with mock.patch.object(training, 'build_record', side_effect=AssertionError('do not read')):
            with self.assertRaisesRegex(training.TrainingError, '^recovery_authorization_revoked$'):
                self.recover(rid)
        self.assertFalse(training._allowed_record(self.stored(rid)))

    def test_concurrent_revoke_wins_over_recovery(self):
        rid = self.collect()
        original = self.record_path(rid).read_bytes()
        build = training.build_record
        def revoke(*args, **kwargs):
            record = build(*args, **kwargs)
            training.set_project(str(self.project), False)
            return record
        with mock.patch.object(training, 'build_record', side_effect=revoke):
            with self.assertRaisesRegex(training.TrainingError, '^recovery_authorization_revoked$'):
                self.recover(rid)
        self.assertEqual(self.record_path(rid).read_bytes(), original)

    def test_concurrent_review_is_never_overwritten(self):
        rid = self.collect()
        build = training.build_record
        def review(*args, **kwargs):
            record = build(*args, **kwargs)
            training.review(rid, 'synthetic-reviewer', 'accepted')
            return record
        with mock.patch.object(training, 'build_record', side_effect=review):
            with self.assertRaisesRegex(training.TrainingError, '^record_changed_during_recovery$'):
                self.recover(rid)
        self.assertEqual(len(self.stored(rid)['evidence']), 1)

    def test_parser_failure_retains_existing_record_and_authorization(self):
        rid = self.collect()
        original = self.record_path(rid).read_bytes()
        with mock.patch.object(training, 'build_record', side_effect=RuntimeError('synthetic-secret')):
            with self.assertRaises(RuntimeError):
                self.recover(rid)
        self.assertEqual(self.record_path(rid).read_bytes(), original)
        self.assertTrue(training._allowed_record(self.stored(rid)))

    def test_both_parsers_use_one_snapshot_even_if_writer_appends_after_read(self):
        import nous_codemode_protocol as protocol
        self.source.write_bytes((ROOT / 'examples/training/codemode.jsonl').read_bytes())
        read = nous_transcript.read_transcript_bytes
        parse_messages = nous_transcript.parse_transcript_bytes
        parse_protocol = protocol.parse_protocol_bytes
        observed = []
        def changed(data, *args, **kwargs):
            observed.append(data)
            value = parse_messages(data, *args, **kwargs)
            with self.source.open('a') as out:
                out.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'future-only-output'}]}}) + '\n')
            return value
        def native(data):
            observed.append(data)
            return parse_protocol(data)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', wraps=read) as reader, \
             mock.patch.object(nous_transcript, 'parse_transcript_bytes', side_effect=changed), \
             mock.patch.object(protocol, 'parse_protocol_bytes', side_effect=native):
            rid = self.collect(agent='codex')
        self.assertEqual(reader.call_count, 1)
        self.assertIs(observed[0], observed[1])
        self.assertNotIn('future-only-output', self.record_path(rid).read_text())

    def test_snapshot_read_failure_never_creates_record_or_permission(self):
        with mock.patch.object(nous_transcript, 'read_transcript_bytes',
                               side_effect=nous_transcript.TranscriptReadError('transcript_changed_during_read')):
            with self.assertRaisesRegex(training.TrainingError, '^transcript_changed_during_read$'):
                self.collect()
        self.assertFalse((self.home / 'training/records').exists())
        self.assertEqual(training._config()['projects'], {})

    def test_path_alias_is_resolved_once_and_not_rebound_after_snapshot(self):
        alias = self.root / 'alias.jsonl'
        alias.symlink_to(self.source)
        other = self.root / 'other.jsonl'
        other.write_text('other-sensitive-content')
        read = nous_transcript.read_transcript_bytes
        def change_alias(path, *args, **kwargs):
            value = read(path, *args, **kwargs)
            alias.unlink()
            alias.symlink_to(other)
            return value
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=change_alias):
            rid = training.collect(alias, str(self.project), manual=True)['id']
        source_id = training._hash(['transcript', os.path.realpath(self.source)])[:24]
        self.assertEqual(self.stored(rid)['provenance']['source_transcript_id'], source_id)
        self.assertNotIn('other-sensitive-content', self.record_path(rid).read_text())

    def test_non_regular_transcript_is_rejected_before_read(self):
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        with mock.patch.object(nous_transcript, 'parse_transcript_bytes', side_effect=AssertionError('do not parse')):
            with self.assertRaisesRegex(training.TrainingError, '^transcript_must_be_regular_file$'):
                training.build_record(fifo, str(self.project))

    def test_unknown_record_does_not_create_state(self):
        with self.assertRaisesRegex(training.TrainingError, '^recovery_record_unavailable$'):
            self.recover('t_' + 'a' * 32)
        self.assertFalse(self.home.exists())

    def test_synthetic_provenance_is_not_erased_by_recovery(self):
        rid = self.collect()
        record = self.stored(rid)
        record['provenance'].update(synthetic=True, source='live_synthetic_fixture')
        training._atomic(self.record_path(rid), record)
        self.recover(rid)
        self.assertTrue(self.stored(rid)['provenance']['synthetic'])
        self.assertEqual(self.stored(rid)['provenance']['source'], 'live_synthetic_fixture')

    def test_health_cli_is_read_only_without_state(self):
        env = dict(os.environ, NOUS_HOME=str(self.home), NOUS_LEDGER_HOME=str(self.home / 'ledger'),
                   PYTHONDONTWRITEBYTECODE='1')
        run = subprocess.run([sys.executable, '-B', str(ROOT / 'hooks/nous_training.py'), 'health'],
                             env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['status'], 'missing')
        self.assertFalse(self.home.exists())

    def hook(self):
        return training.capture_stop({'event': 'Stop', 'agent': 'claude'},
                                     {'cwd': str(self.project), 'transcript_path': str(self.source),
                                      'session_id': 'synthetic-session'})

    def health_row(self):
        return health.read_health(self.home)['projects'][training.project_id(str(self.project))]['claude']

    def test_capture_health_records_success_and_static_failure_only(self):
        training.set_project(str(self.project), True)
        self.assertEqual(self.hook()['status'], 'collected')
        self.assertEqual(self.health_row()['captured_count'], 1)
        with mock.patch.object(training, 'build_record', side_effect=RuntimeError('private-prompt-never-save')):
            self.assertEqual(self.hook(), {'status': 'collection_failed'})
        row = self.health_row()
        self.assertEqual(row['attempt_count'], 2)
        self.assertEqual(row['failed_count'], 1)
        self.assertEqual(row['last_error_code'], 'collection_failed')
        state = (self.home / 'training/health/state.json').read_text()
        for private in ('private-prompt-never-save', str(self.project), str(self.source), 'synthetic-session'):
            self.assertNotIn(private, state)

    def test_disabled_capture_does_not_create_health_or_read_source(self):
        with mock.patch.object(training, 'build_record', side_effect=AssertionError('no read')):
            self.assertIsNone(self.hook())
        self.assertFalse(self.home.exists())

    def test_health_storage_failure_does_not_block_capture(self):
        training.set_project(str(self.project), True)
        with mock.patch.object(health, 'record_attempt', side_effect=OSError('private-path')):
            self.assertEqual(self.hook()['status'], 'collected')

    def test_hook_retries_one_changed_snapshot_and_counts_one_attempt(self):
        training.set_project(str(self.project), True)
        read = nous_transcript.read_transcript_bytes
        calls = []
        def busy_once(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise nous_transcript.TranscriptReadError('transcript_changed_during_read')
            return read(path, *args, **kwargs)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=busy_once):
            self.assertEqual(self.hook()['status'], 'collected')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.health_row()['attempt_count'], 1)
        self.assertEqual(self.health_row()['captured_count'], 1)

    def test_hook_never_retries_unboundedly_or_retries_oversized_input(self):
        training.set_project(str(self.project), True)
        for reason, expected_calls in [('transcript_changed_during_read', 2), ('transcript_line_too_large', 1)]:
            with mock.patch.object(nous_transcript, 'read_transcript_bytes',
                                   side_effect=nous_transcript.TranscriptReadError(reason)) as reader:
                self.assertEqual(self.hook()['status'], 'collection_failed')
                self.assertEqual(reader.call_count, expected_calls)
            self.assertEqual(self.health_row()['last_error_code'], reason)

    def test_revoke_and_regrant_between_attempts_does_not_revive_old_stop(self):
        training.set_project(str(self.project), True)
        def regrant(_seconds):
            training.set_project(str(self.project), False, purge=True)
            training.set_project(str(self.project), True)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes',
                               side_effect=nous_transcript.TranscriptReadError('transcript_changed_during_read')) as reader, \
             mock.patch.object(training.time, 'sleep', side_effect=regrant):
            self.assertEqual(self.hook(), {'status': 'disabled'})
            self.assertEqual(reader.call_count, 1)
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))
        self.assertEqual(health.read_health(self.home).get('projects', {}), {})

    def test_hook_refuses_source_from_another_native_session(self):
        training.set_project(str(self.project), True)
        for row in self.rows:
            row['sessionId'] = 'another-native-session'
        self.write()
        self.assertEqual(self.hook(), {'status': 'collection_failed'})
        self.assertEqual(self.health_row()['last_error_code'], 'transcript_session_mismatch')
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))

    def test_retry_remains_bound_to_initial_alias_target(self):
        training.set_project(str(self.project), True)
        original = self.source
        alias = self.root / 'retry-alias.jsonl'
        alias.symlink_to(original)
        self.source = alias
        other = self.root / 'unrequested-target.jsonl'
        rows = json.loads(json.dumps(self.rows))
        rows[-1]['message']['content'] = 'unrequested-target-output'
        other.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        read = nous_transcript.read_transcript_bytes
        calls = []
        def busy_once(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise nous_transcript.TranscriptReadError('transcript_changed_during_read')
            return read(path, *args, **kwargs)
        def retarget(_seconds):
            alias.unlink()
            alias.symlink_to(other)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=busy_once), \
             mock.patch.object(training.time, 'sleep', side_effect=retarget):
            result = self.hook()
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(calls, [os.path.realpath(original), os.path.realpath(original)])
        self.assertNotIn('unrequested-target-output', self.record_path(result['id']).read_text())

    def check_project_alias_change(self, operation):
        original = self.project
        alias = self.root / 'project-alias'
        alias.symlink_to(original, target_is_directory=True)
        other = self.root / 'unapproved-project'
        other.mkdir()
        pid = training.set_project(str(alias), True)
        other_pid = training.project_id(str(other))
        rid = self.collect() if operation == 'recover' else None
        read = nous_transcript.read_transcript_bytes
        scrub = training.scrub_record
        scrub_projects = []
        def retarget(path, *args, **kwargs):
            data = read(path, *args, **kwargs)
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
            return data
        def record_scrub(record, project):
            scrub_projects.append(project)
            return scrub(record, project)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=retarget), \
             mock.patch.object(training, 'scrub_record', side_effect=record_scrub):
            if operation == 'build':
                record = training.build_record(self.source, str(alias), agent='claude')
            elif operation == 'recover':
                result = training.recover(rid, self.source, str(alias))
                self.assertEqual(result['status'], 'recovered')
                record = self.stored(rid)
            else:
                if operation == 'capture':
                    result = training.capture_stop({'event': 'Stop', 'agent': 'claude'},
                        {'cwd': str(alias), 'transcript_path': str(self.source), 'session_id': 'synthetic-session'})
                else:
                    result = training.collect(self.source, str(alias), agent='claude', session='synthetic-session')
                self.assertEqual(result['status'], 'collected')
                record = self.stored(result['id'])
        self.assertEqual(record['provenance']['project_id'], pid)
        self.assertEqual(scrub_projects, [os.path.realpath(original)])
        self.assertFalse(training._allowed(other_pid, 'capture'))
        self.assertEqual(set(training._config()['projects']), {pid})

    def test_build_binds_project_identity_and_scrub_path_before_alias_changes(self):
        self.check_project_alias_change('build')

    def test_collect_keeps_initial_project_when_input_alias_changes(self):
        self.check_project_alias_change('collect')

    def test_capture_keeps_initial_project_when_input_alias_changes(self):
        self.check_project_alias_change('capture')

    def test_recovery_keeps_initial_project_when_input_alias_changes(self):
        self.check_project_alias_change('recover')

    def test_stop_retry_remains_bound_to_initial_project_alias_target(self):
        alias = self.root / 'project-alias'
        alias.symlink_to(self.project, target_is_directory=True)
        other = self.root / 'unapproved-project'
        other.mkdir()
        pid = training.set_project(str(alias), True)
        read = nous_transcript.read_transcript_bytes
        calls = []
        def busy_once(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise nous_transcript.TranscriptReadError('transcript_changed_during_read')
            return read(path, *args, **kwargs)
        def retarget(_seconds):
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=busy_once), \
             mock.patch.object(training.time, 'sleep', side_effect=retarget):
            result = training.capture_stop({'event': 'Stop', 'agent': 'claude'},
                {'cwd': str(alias), 'transcript_path': str(self.source), 'session_id': 'synthetic-session'})
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.stored(result['id'])['provenance']['project_id'], pid)
        self.assertEqual(set(training._config()['projects']), {pid})
        self.assertEqual(self.health_row()['captured_count'], 1)

    def replace_canonical_project(self, replacement='symlink'):
        moved = self.project.with_name('original-project-moved')
        self.project.rename(moved)
        if replacement == 'symlink':
            other = self.project.with_name('unapproved-project')
            other.mkdir()
            self.project.symlink_to(other, target_is_directory=True)
        elif replacement == 'directory':
            self.project.mkdir()

    def test_canonical_project_replaced_before_read_is_rejected_without_source_access(self):
        bind = training._bind_project
        def replace(project):
            binding = bind(project)
            self.replace_canonical_project()
            return binding
        with mock.patch.object(training, '_bind_project', side_effect=replace), \
             mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(training.TrainingError, '^project_changed_during_collection$'):
                training.build_record(self.source, str(self.project))
        self.assertFalse(self.home.exists())

    def test_canonical_project_replaced_during_read_is_rejected_before_scrub(self):
        read = nous_transcript.read_transcript_bytes
        def replace(path, *args, **kwargs):
            data = read(path, *args, **kwargs)
            self.replace_canonical_project('directory')
            return data
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=replace), \
             mock.patch.object(training, 'scrub_record', side_effect=AssertionError('must not scrub')):
            with self.assertRaisesRegex(training.TrainingError, '^project_changed_during_collection$'):
                self.collect()
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))
        self.assertEqual(training._config()['projects'], {})

    def test_project_disappearance_during_scrub_prevents_import_permission_and_record(self):
        scrub = training.scrub_record
        def replace(record, project):
            value = scrub(record, project)
            self.replace_canonical_project('missing')
            return value
        with mock.patch.object(training, 'scrub_record', side_effect=replace):
            with self.assertRaisesRegex(training.TrainingError, '^project_changed_during_collection$'):
                self.collect()
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))
        self.assertEqual(training._config()['projects'], {})

    def test_canonical_project_replacement_after_build_prevents_collection_write(self):
        build = training.build_record
        def replace(*args, **kwargs):
            record = build(*args, **kwargs)
            self.replace_canonical_project()
            return record
        with mock.patch.object(training, 'build_record', side_effect=replace):
            with self.assertRaisesRegex(training.TrainingError, '^project_changed_during_collection$'):
                self.collect()
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))
        self.assertEqual(training._config()['projects'], {})

    def test_canonical_project_replacement_after_build_preserves_recovery_record(self):
        rid = self.collect()
        original = self.record_path(rid).read_bytes()
        build = training.build_record
        def replace(*args, **kwargs):
            record = build(*args, **kwargs)
            self.replace_canonical_project()
            return record
        with mock.patch.object(training, 'build_record', side_effect=replace):
            with self.assertRaisesRegex(training.TrainingError, '^project_changed_during_collection$'):
                self.recover(rid)
        self.assertEqual(self.record_path(rid).read_bytes(), original)

    def test_initial_missing_project_fails_with_static_code_without_source_access(self):
        with mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(training.TrainingError, '^project_unavailable$'):
                training.build_record(self.source, str(self.root / 'missing-project'))

    def test_stop_retry_rejects_canonical_directory_replacement_and_reports_static_failure(self):
        pid = training.set_project(str(self.project), True)
        with mock.patch.object(nous_transcript, 'read_transcript_bytes',
                               side_effect=nous_transcript.TranscriptReadError('transcript_changed_during_read')) as reader, \
             mock.patch.object(training.time, 'sleep', side_effect=lambda _: self.replace_canonical_project()):
            self.assertEqual(self.hook(), {'status': 'collection_failed'})
        self.assertEqual(reader.call_count, 1)
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))
        row = health.read_health(self.home)['projects'][pid]['claude']
        self.assertEqual(row['last_error_code'], 'project_changed_during_collection')
        self.assertEqual(row['failed_count'], 1)

    def test_codemode_partial_tail_is_retried_without_reply_hint(self):
        fixture = (ROOT / 'examples/training/codemode.jsonl').read_bytes()
        self.source.write_bytes(fixture[:-6])
        training.set_project(str(self.project), True)
        # The fixture has no platform native session ID; bind only this test's
        # explicit hook alias and verify actual source rereading after flush.
        with mock.patch.object(training.time, 'sleep', side_effect=lambda _: self.source.write_bytes(fixture)) as wait:
            result = training.capture_stop({'event': 'Stop', 'agent': 'codex'},
                {'cwd': str(self.project), 'transcript_path': str(self.source), 'session_id': 'fixture-codemode'})
        self.assertEqual(result['status'], 'collected')
        self.assertNotIn('partial_tail', result['quality']['issues'])
        self.assertEqual(wait.call_count, 1)

    def test_concurrent_purge_does_not_recreate_health_on_completion(self):
        training.set_project(str(self.project), True)
        build = training.build_record
        def revoke(*args, **kwargs):
            record = build(*args, **kwargs)
            training.set_project(str(self.project), False, purge=True)
            return record
        with mock.patch.object(training, 'build_record', side_effect=revoke):
            self.assertEqual(self.hook()['status'], 'disabled')
        report = health.read_health(self.home)
        self.assertEqual(report.get('projects', {}), {})
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))

    def test_global_purge_removes_health_and_records(self):
        training.set_project(str(self.project), True)
        self.hook()
        training.revoke_all(purge=True)
        self.assertEqual(health.read_health(self.home).get('projects', {}), {})
        self.assertFalse(list((self.home / 'training/records').glob('*.json')))

    def test_health_cleanup_failure_is_reported_after_record_deletion(self):
        rid = self.collect()
        with mock.patch.object(health, 'purge_health', return_value={'status': 'unavailable', 'error': 'health_io_failed'}):
            with self.assertRaisesRegex(training.TrainingError, '^health_purge_failed$'):
                training.set_project(str(self.project), False, purge=True)
        self.assertFalse(self.record_path(rid).exists())
        self.assertFalse(training._allowed(training.project_id(str(self.project)), 'capture'))

    def test_global_cleanup_failure_returns_report_without_blocking_caller(self):
        rid = self.collect()
        with mock.patch.object(health, 'purge_health', return_value={'status': 'unavailable'}):
            report = training.revoke_all(purge=True)
        self.assertFalse(report['cleanup_complete'])
        self.assertEqual(report['errors'], ['health_purge_failed'])
        self.assertFalse(self.record_path(rid).exists())

    def test_record_cleanup_errors_are_not_silently_reported_as_success(self):
        rid = self.collect()
        target = self.record_path(rid)
        unlink = Path.unlink
        def deny(path, *args, **kwargs):
            if path == target:
                raise PermissionError('synthetic-private-path')
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', deny):
            with self.assertRaisesRegex(training.TrainingError, '^records_purge_failed$'):
                training.set_project(str(self.project), False, purge=True)
            report = training.revoke_all(purge=True)
        self.assertFalse(report['cleanup_complete'])
        self.assertEqual(report['errors'], ['records_purge_failed'])
        self.assertTrue(target.is_file())
        self.assertFalse(training._allowed_record(self.stored(rid)))

    def test_collect_read_before_recovery_cannot_overwrite_recovered_snapshot(self):
        rid = self.collect(session='shared-session')
        ready, release, recovery_started = threading.Event(), threading.Event(), threading.Event()
        build = training.build_record
        def delayed(*args, **kwargs):
            value = build(*args, **kwargs)
            if threading.current_thread().name.startswith('old-collector'):
                ready.set()
                if not release.wait(5):
                    raise RuntimeError('test_timeout')
            return value
        def later():
            recovery_started.set()
            return self.recover(rid)
        with mock.patch.object(training, 'build_record', side_effect=delayed):
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='old-collector') as old_pool, \
                 ThreadPoolExecutor(max_workers=1, thread_name_prefix='recovery') as recovery_pool:
                old_future = old_pool.submit(training.collect, self.source, str(self.project),
                                             session='shared-session', manual=True)
                try:
                    self.assertTrue(ready.wait(3))
                    more = self.rows + [
                        {'type': 'user', 'uuid': 'u2', 'sessionId': 'synthetic-session',
                         'message': {'role': 'user', 'content': 'And three plus three?'}},
                        {'type': 'assistant', 'uuid': 'a2', 'sessionId': 'synthetic-session',
                         'message': {'role': 'assistant', 'content': 'Six.'}},
                    ]
                    self.write(more)
                    recovered = recovery_pool.submit(later)
                    self.assertTrue(recovery_started.wait(3))
                    # Source serialization makes recovery read after the pending
                    # older writer. It cannot commit before that writer finishes.
                    with self.assertRaises(TimeoutError):
                        recovered.result(timeout=.05)
                finally:
                    release.set()
                self.assertEqual(old_future.result(timeout=3)['status'], 'collected')
                self.assertEqual(recovered.result(timeout=3)['status'], 'recovered')
        self.assertEqual(len(self.stored(rid)['messages']), 4)


if __name__ == '__main__':
    unittest.main()
