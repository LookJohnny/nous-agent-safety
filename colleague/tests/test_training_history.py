"""Independent history-import regression tests; synthetic local files only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / 'hooks'
sys.path.insert(0, str(HOOKS))
import nous_ledger
import nous_training as training
import nous_training_history as history
import nous_transcript
import nous_training_history_read as history_reader
from nous_training_validate import validate_dataset


def encoded(rows):
    return b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode() for row in rows)


class HistoryImport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='nous-history-regression-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / 'private-state'
        self.project = self.root / 'project'
        self.project.mkdir()
        self.roots = []
        for agent in ('claude', 'codex'):
            folder = self.root / agent
            folder.mkdir()
            self.roots.append({'agent': agent, 'path': str(folder), 'source_role': 'explicit'})
        for patch in (mock.patch.object(training, 'HOME', self.home),
                      mock.patch.object(nous_ledger, '_key', return_value=b'synthetic-history-key-32-bytes!!')):
            patch.start(); self.addCleanup(patch.stop)
        self.sequence = 0

    def rows(self, agent='claude', session='synthetic-session', project=None, sidechain=False):
        cwd = str(project or self.project)
        if agent == 'codex':
            return [{'type': 'session_meta', 'payload': {'id': session, 'cwd': cwd, 'source': 'cli'}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                      'content': [{'type': 'input_text', 'text': 'SYNTHETIC_PRIVATE_TASK'}]}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                      'content': [{'type': 'output_text', 'text': 'SYNTHETIC_PRIVATE_ANSWER'}]}}]
        return [{'type': role, 'sessionId': session, 'cwd': cwd, 'uuid': role + '-id',
                 'parentUuid': None if role == 'user' else 'user-id', 'isSidechain': sidechain,
                 **({'agentId': 'synthetic-child'} if sidechain else {}),
                 'message': {'role': role, 'content': 'SYNTHETIC_PRIVATE_' + role.upper()}}
                for role in ('user', 'assistant')]

    def source(self, name='one.jsonl', *, agent='claude', rows=None, **options):
        path = self.root / agent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded(self.rows(agent, **options) if rows is None else rows))
        return path

    def plan(self, **options):
        return history.make_plan(roots=self.roots, **options)

    def run_import(self, plan=None, **kwargs):
        self.sequence += 1
        out = self.root / ('result-' + str(self.sequence))
        report = history.import_plan(self.plan() if plan is None else plan, out, **kwargs)
        return report, out

    def records(self):
        return {path.name: json.loads(path.read_text()) for path in (self.home / 'training/records').glob('*.json')}

    def state_bytes(self):
        return {path.name: path.read_bytes() for path in (self.home / 'training/records').glob('*.json')}

    def test_empty_explicit_roots_do_not_discover_platform_history(self):
        plan = history.make_plan(roots=[])
        self.assertEqual(plan['candidates'], [])
        report, _ = self.run_import(plan)
        self.assertEqual(report['candidate_files'], 0)
        self.assertTrue(report['processing_complete'])
        self.assertEqual(self.records(), {})

    def test_agent_and_explicit_project_scope(self):
        other = self.root / 'other-project'; other.mkdir()
        self.source('a.jsonl')
        self.source('b.jsonl', project=other)
        self.source('c.jsonl', agent='codex')
        plan = self.plan(agent='claude', projects=[str(self.project)])
        self.assertEqual(plan['scope'], 'explicit_projects')
        self.assertEqual(len(plan['candidates']), 1)
        report, _ = self.run_import(plan)
        self.assertEqual(report['counts'], {'imported': 1})
        self.assertEqual({record['agent']['id'] for record in self.records().values()}, {'claude'})

    def test_both_platforms_are_candidates_only_and_do_not_enable_capture(self):
        files = [self.source(), self.source(agent='codex')]
        before = {str(path): path.read_bytes() for path in files}
        report, out = self.run_import()
        self.assertTrue(report['processing_complete'])
        self.assertEqual(report['counts'], {'imported': 2})
        self.assertFalse(report['continuous_capture_enabled']); self.assertFalse(report['upload_performed'])
        for value in self.records().values():
            self.assertIn(value['schema'], ('nous.trajectory/1', 'nous.trajectory/2'))
            self.assertEqual(value['evidence'], [])
            self.assertEqual(value['provenance']['observation_scope'], 'retained_transcript_only')
        for state in training._config()['projects'].values():
            self.assertFalse(state.get('capture')); self.assertFalse(state.get('export'))
        for profile, dataset in report['datasets'].items():
            self.assertTrue(dataset['valid']); self.assertEqual(dataset['counts']['sft'], 0)
            self.assertTrue(validate_dataset(out / profile)['valid'])
        self.assertEqual(before, {str(path): path.read_bytes() for path in files})
        safe = json.dumps(report)
        for private in ('SYNTHETIC_PRIVATE', str(self.project), str(files[0]), 'synthetic-session'):
            self.assertNotIn(private, safe)

    def test_codemode_history_stays_v2_without_request_or_episode_promotion(self):
        rows = [json.loads(line) for line in (HOOKS.parent / 'examples/training/codemode.jsonl').read_text().splitlines()]
        rows[0]['payload'].update(id='synthetic-codemode', cwd=str(self.project), source='cli')
        self.source(agent='codex', rows=rows)
        report, out = self.run_import()
        value = next(iter(self.records().values()))
        self.assertEqual(value['schema'], 'nous.trajectory/2')
        self.assertNotIn('exchange', value); self.assertNotIn('episode', value)
        self.assertEqual(value['evidence'], [])
        self.assertEqual(report['datasets']['codemode-events-v1']['counts']['sft'], 0)
        self.assertTrue(validate_dataset(out / 'codemode-events-v1')['valid'])

    def test_parent_and_subagent_same_native_id_never_share_record_identity(self):
        self.source('main.jsonl')
        self.source('main/subagents/child.jsonl', sidechain=True)
        report, _ = self.run_import()
        values = list(self.records().values())
        self.assertEqual(report['counts'], {'imported': 2})
        self.assertEqual(len({v['id'] for v in values}), 2)
        self.assertEqual(len({v['provenance']['source_transcript_id'] for v in values}), 2)
        self.assertEqual(len({v['provenance']['session_group_id'] for v in values}), 1)
        child = next(v for v in values if v['provenance']['history_branch_role'] == 'subagent')
        self.assertFalse(child['quality']['complete'])

    def test_replay_preserves_record_bytes_review_and_permissions(self):
        self.source(); self.run_import()
        rid = next(iter(self.records().values()))['id']
        training.review(rid, 'synthetic-reviewer', 'accepted')
        records = self.state_bytes(); config = (self.home / 'training/config.json').read_bytes()
        report, _ = self.run_import()
        self.assertEqual(report['reason_counts'], {'already_imported': 1})
        self.assertEqual(self.state_bytes(), records)
        self.assertEqual((self.home / 'training/config.json').read_bytes(), config)
        self.assertEqual(report['datasets'], {})

    def test_changed_source_is_reported_without_overwriting_old_review(self):
        source = self.source(); self.run_import()
        rid = next(iter(self.records().values()))['id']
        training.review(rid, 'synthetic-reviewer', 'accepted')
        before = self.state_bytes()
        with source.open('ab') as stream:
            stream.write(encoded([{'type': 'progress', 'synthetic': 'new observation'}]))
        report, _ = self.run_import()
        self.assertEqual(report['reason_counts'], {'source_changed_existing': 1})
        self.assertEqual(self.state_bytes(), before)

    def test_legacy_collected_source_with_review_is_never_replaced(self):
        source = self.source()
        prior = training.collect(source, str(self.project), session='legacy-hook-alias', manual=True)
        training.review(prior['id'], 'synthetic-reviewer', 'accepted')
        before = self.state_bytes()
        report, _ = self.run_import()
        self.assertEqual(report['reason_counts'], {'existing_source_unverified': 1})
        self.assertEqual(self.state_bytes(), before)

    def test_retained_revoked_record_is_not_reauthorized_by_replay(self):
        self.source(); self.run_import(); training.revoke_all(purge=False)
        before = self.state_bytes(); config = (self.home / 'training/config.json').read_bytes()
        report, _ = self.run_import()
        self.assertEqual(report['reason_counts'], {'already_imported': 1})
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual((self.home / 'training/config.json').read_bytes(), config)
        self.assertTrue(all(not training._allowed_record(v) for v in self.records().values()))

    def test_generation_pinned_before_unknown_project_first_import(self):
        self.source('a.jsonl'); self.source('b.jsonl')
        def revoke_after_first(progress):
            if progress['processed'] == 1:
                training.set_project(str(self.project), False, purge=False)
        report, _ = self.run_import(progress=revoke_after_first)
        self.assertEqual(report['counts'].get('imported'), 1)
        self.assertEqual(report['reason_counts'].get('history_project_revoked'), 1)
        self.assertFalse(report['all_sources_imported']); self.assertEqual(report['datasets'], {})
        self.assertEqual(training._config()['projects'][training.project_id(str(self.project))].get('imports', []), [])

    def test_global_revoke_stops_remaining_batch_and_does_not_regrant(self):
        for label in ('a', 'b', 'c'): self.source(label + '.jsonl')
        def revoke_after_first(progress):
            if progress['processed'] == 1: training.revoke_all()
        report, _ = self.run_import(progress=revoke_after_first)
        self.assertFalse(report['processing_complete']); self.assertFalse(report['all_sources_imported'])
        self.assertLess(len(report['entries']), 3)
        self.assertEqual(report['reason_counts'].get('history_batch_revoked'), 1)
        self.assertEqual(self.records(), {}); self.assertEqual(report['datasets'], {})

    def test_revoke_after_last_entry_cannot_report_full_success(self):
        self.source()
        report, _ = self.run_import(progress=lambda _progress: training.revoke_all())
        self.assertFalse(report['processing_complete']); self.assertFalse(report['all_sources_imported'])
        self.assertEqual(self.records(), {}); self.assertEqual(report['datasets'], {})
        self.assertIn('history_batch_revoked', json.dumps(report))

    def test_revoke_during_build_blocks_current_commit(self):
        self.source(); original = training.build_record
        def build_then_revoke(*args, **kwargs):
            result = original(*args, **kwargs); training.revoke_all(); return result
        with mock.patch.object(training, 'build_record', side_effect=build_then_revoke):
            report, _ = self.run_import()
        self.assertEqual(self.records(), {})
        self.assertFalse(report['processing_complete'])
        self.assertEqual(report['reason_counts'], {'history_batch_revoked': 1})

    def test_keyboard_interrupt_leaves_reviewable_checkpoint_and_safe_resume(self):
        self.source('a.jsonl'); self.source('b.jsonl')
        plan = self.plan(); out = self.root / 'interrupted'
        def interrupt(_): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): history.import_plan(plan, out, progress=interrupt)
        checkpoint = json.loads((out / 'report.json').read_text())
        self.assertFalse(checkpoint['processing_complete']); self.assertEqual(len(checkpoint['entries']), 1)
        before = self.state_bytes()
        report, _ = self.run_import(plan)
        self.assertEqual(report['counts'], {'skipped': 1, 'imported': 1})
        self.assertTrue(all(self.state_bytes()[key] == value for key, value in before.items()))

    def test_source_change_after_plan_is_not_imported(self):
        source = self.source(); plan = self.plan()
        source.write_bytes(source.read_bytes() + b'\n')
        report, _ = self.run_import(plan)
        self.assertEqual(report['reason_counts'], {'history_source_changed': 1})
        self.assertEqual(self.records(), {})

    def test_one_snapshot_read_drives_hash_identity_and_build(self):
        source = self.source(); plan = self.plan(); raw = source.read_bytes()
        original = history_reader.read_history_bytes
        with mock.patch.object(history_reader, 'read_history_bytes', wraps=original) as read, \
                mock.patch.object(nous_transcript, 'read_transcript_bytes', side_effect=AssertionError('must reuse snapshot')):
            report, _ = self.run_import(plan)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(report['counts'], {'imported': 1})
        value = next(iter(self.records().values()))
        self.assertEqual(value['provenance']['source_snapshot_sha256'], hashlib.sha256(raw).hexdigest())

    def test_source_change_after_snapshot_read_blocks_commit(self):
        source = self.source(); plan = self.plan(); original = history_reader.read_history_bytes
        def read_then_change(candidate):
            result = original(candidate); source.write_bytes(result['snapshot'] + b'\n'); return result
        with mock.patch.object(history_reader, 'read_history_bytes', side_effect=read_then_change):
            report, _ = self.run_import(plan)
        self.assertEqual(self.records(), {})
        self.assertEqual(report['reason_counts'], {'history_source_changed': 1})

    def test_missing_workspace_preserves_visible_candidate_without_invented_directory(self):
        missing = self.root / 'moved-project'
        self.source(project=missing)
        report, _ = self.run_import()
        self.assertEqual(report['counts'], {'imported': 1})
        self.assertFalse(missing.exists())
        value=next(iter(self.records().values()))
        self.assertFalse(value['quality']['complete'])
        self.assertIn('historical_workspace',value['quality']['missing'])
        self.assertFalse(value['provenance']['historical_workspace_available'])
        self.assertEqual(report['datasets']['trl']['counts']['sft'],0)
        self.assertTrue(report['all_sources_imported'])
        self.assertFalse(report['full_model_request_context_recovered'])

    def test_strict_full_source_failure_reports_static_code_without_body(self):
        rows = self.rows() + [{'type': 'progress'}] * 130
        source = self.source(rows=rows)
        with source.open('ab') as stream: stream.write(b'{"private":"RAW_SECRET_BODY",invalid}\n')
        report, _ = self.run_import()
        self.assertEqual(self.records(), {})
        self.assertEqual(report['counts'], {'failed': 1})
        self.assertNotIn('RAW_SECRET_BODY', json.dumps(report)); self.assertNotIn(str(source), json.dumps(report))

    def test_late_session_identity_conflict_cannot_hide_after_scan_header(self):
        rows = self.rows() + [{'type': 'progress'}] * 130 + self.rows(session='other-private-session')
        self.source(rows=rows)
        report, _ = self.run_import()
        self.assertEqual(self.records(), {}); self.assertEqual(report['counts'], {'failed': 1})
        self.assertNotIn('other-private-session', json.dumps(report))

    def test_short_structured_tool_secrets_never_reach_records_or_reports(self):
        rows = self.rows()
        base = {key: rows[0][key] for key in ('sessionId', 'cwd')}
        rows[1:1] = [dict(base, type='assistant', uuid='call-message', parentUuid='user-id', message={
            'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tool-1', 'name': 'lookup', 'input': {'password': 'tiny-in'}}]}),
            dict(base, type='user', uuid='result-message', parentUuid='call-message', message={
            'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': {'password': 'tiny-out'}}]})]
        rows[-1]['parentUuid'] = 'result-message'
        source = self.source(rows=rows); before = source.read_bytes()
        report, _ = self.run_import()
        self.assertEqual(report['counts'], {'imported': 1})
        stored = json.dumps(self.records()) + json.dumps(report)
        self.assertNotIn('tiny-in', stored); self.assertNotIn('tiny-out', stored)
        self.assertEqual(source.read_bytes(), before)

    def test_plan_roundtrip_is_private_and_has_no_transcript_body(self):
        self.source(); plan = self.plan(); path = self.root / 'private-plan/plan.json'
        old = os.umask(0)
        try: history.write_plan(plan, path)
        finally: os.umask(old)
        self.assertEqual(history.read_plan(path), plan)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('SYNTHETIC_PRIVATE', path.read_text())
        with self.assertRaises(history.HistoryError): history.write_plan(plan, path)

    def test_plan_tamper_duplicate_sources_and_unknown_diagnostics_fail_closed(self):
        self.source(); original = self.plan()
        altered = copy.deepcopy(original); altered['candidates'][0]['session_id'] = 'tampered'
        with self.assertRaises(history.HistoryError): self.run_import(altered)
        for mutate in (lambda p: p['candidates'].append(copy.deepcopy(p['candidates'][0])),
                       lambda p: p['scan_errors'].append('PRIVATE_DYNAMIC_ERROR_BODY'),
                       lambda p: p['scan_skipped'].append({'code': 'PRIVATE_DYNAMIC_ERROR_BODY'})):
            plan = copy.deepcopy(original); mutate(plan)
            plan['plan_digest'] = training._hash({key: value for key, value in plan.items() if key != 'plan_digest'})
            with self.assertRaises(history.HistoryError): self.run_import(plan)

    def test_unexpected_exception_text_never_enters_report(self):
        self.source()
        with mock.patch.object(training, 'build_record', side_effect=RuntimeError('PRIVATE_DYNAMIC_ERROR_BODY')):
            report, _ = self.run_import()
        self.assertEqual(report['reason_counts'], {'history_import_failed': 1})
        self.assertNotIn('PRIVATE_DYNAMIC_ERROR_BODY', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
