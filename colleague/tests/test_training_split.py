"""Synthetic exported datasets; never touches personal sessions or config."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
import nous_training_split as splitting
import nous_training_export as export
from nous_training_validate import validate_dataset
from tests.test_training_export import record, review, preference, text
from tests.test_codemode_training import record as codemode_record
from tests.test_training_episode import record as episode_record, refresh as refresh_episode, review as review_episode


def trajectory(label, project='project-a', session=None, shared=None):
    value = record(label)
    value['provenance'].update(project_id=project, session_id=session or label)
    value['messages'] = [text('user', 'synthetic question ' + (shared or label)),
                         text('assistant', 'synthetic answer ' + (shared or label))]
    return review(value)


def pair(label, source_id=None, prompt=None, tools=None):
    value = preference()
    value['id'] = label
    value['schema'] = 'nous.preference/2'
    if source_id is not None:
        value['provenance'] = {'source_record_id': source_id}
        value['prompt'] = prompt or [text('user', 'synthetic question ' + source_id)]
    if tools is not None:
        value['tools'] = tools
    value['evidence'][0]['record_digest'] = export.preference_digest(value)
    return value


def episode_trajectory(label, project='episode-project', session=None, shared=None):
    value = episode_record()
    value['id'] = label
    identity = {'project_id': project, 'session_id': session or label, 'session_group_id': session or label}
    value['provenance'].update(identity)
    for step in value['episode']['steps']:
        step['provenance'].update(identity)
        for side in ('request', 'response'):
            step['exchange'][side]['model'] = 'synthetic-' + (shared or label)
    refresh_episode(value)
    digest = export.record_digest(value)
    value['evidence'] = [{'kind': 'human_review', 'result': 'accepted', 'reviewer': 'synthetic', 'record_digest': digest},
                         {'kind': 'content_review', 'result': 'cleared', 'reviewer': 'synthetic', 'record_digest': digest}]
    for index in (0, 1):
        review_episode(value, index)
    return value


class TrainingSplit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nous-split-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dataset = self.root / 'dataset'
        self.output = self.root / 'split'

    def make(self, records=None, pairs=(), profile='trl'):
        return export.export_dataset(records if records is not None else [trajectory('a')], self.dataset,
                                     preferences=pairs, profile=profile)

    def split(self, **kwargs):
        return splitting.split_dataset(self.dataset, self.output, **kwargs)

    def origin_splits(self, report):
        result = {}
        for row in report['line_provenance']:
            for origin in row['origins']:
                result.setdefault(origin['id'], set()).add(row['file'].split('/')[0])
        return result

    def input_hashes(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.dataset.iterdir()}

    def test_actual_export_and_validate_then_byte_exact_partition(self):
        records = [trajectory(str(i), project='project-' + str(i // 2)) for i in range(20)]
        self.make(records)
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        original = self.input_hashes()
        report = self.split(eval_ratio=0.5)
        self.assertEqual(report['schema'], 'nous.training.split-manifest/1')
        self.assertTrue(report['complete'])
        self.assertEqual(report['profile'], 'trl')
        self.assertEqual(report['input_manifest_sha256'], original['manifest.json'])
        self.assertEqual(self.input_hashes(), original)
        self.assertFalse((self.output / 'manifest.json').exists())
        source_rows = (self.dataset / 'sft.jsonl').read_bytes().splitlines(keepends=True)
        for row in report['line_provenance']:
            output = (self.output / row['file']).read_bytes().splitlines(keepends=True)
            self.assertEqual(output[row['line'] - 1], source_rows[row['input_line'] - 1])
        self.assertEqual(sum(report['counts'][part]['sft'] for part in ('train', 'eval')), 20)
        for i in range(0, 20, 2):
            grouped = self.origin_splits(report)
            self.assertEqual(grouped[str(i)], grouped[str(i + 1)])
        for path, info in report['files'].items():
            raw = (self.output / path).read_bytes()
            self.assertEqual(info, {'records': len(raw.splitlines()), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})

    def test_same_input_and_seed_produce_identical_files_and_report(self):
        self.make([trajectory(str(i), project='p' + str(i)) for i in range(16)])
        first = self.split(seed='fixed', eval_ratio=0.4)
        other = self.root / 'other'
        second = splitting.split_dataset(self.dataset, other, seed='fixed', eval_ratio=0.4)
        self.assertEqual(first, second)
        for path in list(first['files']) + ['split-manifest.json']:
            self.assertEqual((self.output / path).read_bytes(), (other / path).read_bytes())

    def test_different_seed_changes_component_assignments(self):
        self.make([trajectory(str(i), project='p' + str(i)) for i in range(32)])
        first = self.split(seed='a', eval_ratio=0.5)
        second = splitting.split_dataset(self.dataset, self.root / 'second', seed='b', eval_ratio=0.5)
        self.assertNotEqual(first['components'], second['components'])
        self.assertEqual([c['group_ids'] for c in first['components']], [c['group_ids'] for c in second['components']])

    def test_default_project_grouping_keeps_all_sessions_together(self):
        self.make([trajectory('a', session='s1'), trajectory('b', session='s2')])
        report = self.split()
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len({row['file'].split('/')[0] for row in report['line_provenance']}), 1)
        self.assertFalse(report['partition_nonempty'])
        self.assertFalse(report['evaluation_validated'])
        self.assertTrue({'empty_train_split', 'empty_eval_split'} & set(report['warnings']))

    def test_session_grouping_prefers_session_group_id_and_uses_project(self):
        a = trajectory('a', session='snapshot-one')
        b = trajectory('b', session='snapshot-two')
        for item in (a, b):
            item['provenance']['session_group_id'] = 'same-original-session'
        c = trajectory('c', project='another-project', session='same-original-session')
        d = trajectory('d', session='distinct-session')
        self.make([a, b, c, d])
        report = self.split(group_by='session', eval_ratio=0.5)
        self.assertEqual(len(report['components']), 3)
        origins = self.origin_splits(report)
        self.assertEqual(origins['a'], origins['b'])
        groups = {o['id']: row['group_ids'] for row in report['line_provenance'] for o in row['origins']}
        self.assertEqual(groups['a'], groups['b'])
        self.assertNotEqual(groups['a'], groups['c'])
        self.assertNotEqual(groups['a'], groups['d'])

    def test_session_id_fallback_matches_equivalent_session_group_id(self):
        a, b = trajectory('a', session='s'), trajectory('b', session='other')
        b['provenance']['session_group_id'] = 's'
        self.make([a, b])
        report = self.split(group_by='session')
        self.assertEqual(len(report['components']), 1)

    def test_dedup_multiple_origins_connect_projects_and_their_other_samples(self):
        records = [trajectory('shared-a', 'pa', shared='identical'), trajectory('shared-b', 'pb', shared='identical'),
                   trajectory('unique-a', 'pa'), trajectory('unique-b', 'pb')]
        manifest = self.make(records)
        self.assertEqual(manifest['files']['sft.jsonl']['records'], 3)
        report = self.split(eval_ratio=0.5)
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len(report['components'][0]['group_ids']), 2)
        self.assertEqual(len({r['file'].split('/')[0] for r in report['line_provenance']}), 1)
        shared = next(r for r in report['line_provenance'] if len(r['origins']) == 2)
        self.assertEqual({o['id'] for o in shared['origins']}, {'shared-a', 'shared-b'})
        self.assertEqual(len(shared['group_ids']), 2)

    def test_dedup_connectivity_is_transitive(self):
        records = [trajectory('a1', 'a', shared='ab'), trajectory('b1', 'b', shared='ab'),
                   trajectory('b2', 'b', shared='bc'), trajectory('c1', 'c', shared='bc'), trajectory('c2', 'c')]
        self.make(records)
        report = self.split()
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len(report['components'][0]['group_ids']), 3)

    def test_v2_multiple_assistant_targets_remain_in_same_session(self):
        item = codemode_record()
        item['messages'] = [text('user', 'synthetic fixture task'), text('assistant', 'fixture result')]
        item['provenance'] = {'project_id': 'synthetic-project', 'session_group_id': 'synthetic-group', 'session_id': 'snapshot'}
        digest = export.record_digest(item)
        item['evidence'] = [{'kind': 'human_review', 'result': 'accepted', 'reviewer': 'synthetic', 'record_digest': digest},
                            {'kind': 'content_review', 'result': 'cleared', 'reviewer': 'synthetic', 'record_digest': digest}]
        manifest = self.make([item], profile=export.CODEMODE_PROFILE)
        self.assertEqual(manifest['files']['sft.jsonl']['records'], 2)
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        report = self.split(group_by='session')
        self.assertEqual(report['profile'], export.CODEMODE_PROFILE)
        self.assertEqual(len(report['line_provenance']), 2)
        self.assertEqual(len({row['component_id'] for row in report['line_provenance']}), 1)
        self.assertTrue(all('target' in row['origins'][0] for row in report['line_provenance']))

    def test_v4_twelve_step_rows_keep_each_episode_in_one_partition(self):
        manifest = self.make([episode_trajectory(str(i)) for i in range(4)], profile=export.EPISODE_PROFILE)
        self.assertEqual(manifest['files']['sft.jsonl']['records'], 12)
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        original = self.input_hashes()
        report = self.split(group_by='session', eval_ratio=0.3, seed='phase7')
        self.assertEqual(report['profile'], export.EPISODE_PROFILE)
        self.assertEqual(len(report['components']), 4)
        self.assertEqual(sum(part['sft'] for part in report['counts'].values()), 12)
        self.assertEqual(self.input_hashes(), original)
        targets = {}
        for row in report['line_provenance']:
            for origin in row['origins']:
                targets.setdefault(origin['id'], set()).add(origin['target']['step'])
        self.assertEqual(targets, {str(i): {0, 1, 2} for i in range(4)})
        self.assertTrue(all(len(parts) == 1 for parts in self.origin_splits(report).values()))
        self.assertTrue(set(c['component_id'] for c in report['components'] if c['split'] == 'train').isdisjoint(
                        c['component_id'] for c in report['components'] if c['split'] == 'eval'))
        self.assertEqual(json.loads((self.output / 'split-manifest.json').read_text()), report)
        for name, info in report['files'].items():
            raw = (self.output / name).read_bytes()
            self.assertEqual(info, {'records': len(raw.splitlines()), 'bytes': len(raw),
                                    'sha256': hashlib.sha256(raw).hexdigest()})

    def test_v4_dedup_origins_and_other_steps_connect_source_sessions(self):
        records = [episode_trajectory('a', session='sa', shared='same'),
                   episode_trajectory('b', session='sb', shared='same'),
                   episode_trajectory('c', session='sa')]
        manifest = self.make(records, profile=export.EPISODE_PROFILE)
        self.assertEqual(manifest['files']['sft.jsonl']['records'], 6)
        report = self.split(group_by='session', eval_ratio=0.5)
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len(report['components'][0]['group_ids']), 2)
        self.assertEqual(len({row['file'].split('/')[0] for row in report['line_provenance']}), 1)
        shared = [row for row in report['line_provenance'] if len(row['origins']) == 2]
        self.assertEqual(len(shared), 3)
        for row in shared:
            self.assertEqual({origin['id'] for origin in row['origins']}, {'a', 'b'})
            self.assertEqual(len({origin['target']['step'] for origin in row['origins']}), 1)

    def test_target_grammar_is_bound_to_profile_schema_and_canonical_length(self):
        episode = {'schema': 'nous.trajectory/4', 'target_limit': 3}
        codemode = {'schema': 'nous.trajectory/2', 'target_limit': 10}
        for target in ({'step': -1}, {'step': 3}, {'step': True}, {'step': 1.0}, {'step': 0, 'end': 1},
                       {'start': 0, 'end': 1}, None):
            entry = {'format': 'sft', **({'target': target} if target is not None else {})}
            with self.subTest(target=target), self.assertRaisesRegex(splitting.SplitError, '^invalid_target_boundary$'):
                splitting._target(entry, episode, export.EPISODE_PROFILE)
        for entry, metadata, profile in [
                ({'format': 'sft', 'target': {'step': 0}}, codemode, export.CODEMODE_PROFILE),
                ({'format': 'sft', 'target': {'step': 0}}, episode, 'trl'),
                ({'format': 'sft', 'target': {'step': 0}}, episode, export.REQUEST_PROFILE),
                ({'format': 'dpo', 'target': {'step': 0}}, episode, export.EPISODE_PROFILE),
                ({'format': 'sft', 'target': {'step': 0}}, codemode, export.EPISODE_PROFILE),
                ({'format': 'sft', 'target': {'start': 0, 'end': 11}}, codemode, export.CODEMODE_PROFILE),
                ({'format': 'sft'}, codemode, export.CODEMODE_PROFILE)]:
            with self.subTest(entry=entry, profile=profile), self.assertRaisesRegex(splitting.SplitError, '^invalid_target_boundary$'):
                splitting._target(entry, metadata, profile)
        self.assertEqual(splitting._target({'format': 'sft', 'target': {'step': 2}}, episode,
                                          export.EPISODE_PROFILE), {'step': 2})
        self.assertEqual(splitting._target({'format': 'sft', 'target': {'start': 0, 'end': 10}}, codemode,
                                          export.CODEMODE_PROFILE), {'start': 0, 'end': 10})

    def test_actual_episode_demo_twelve_rows_split_with_verified_manifest(self):
        demo = self.root / 'actual-demo'
        hooks = Path(__file__).resolve().parents[1] / 'hooks'
        environment = dict(os.environ, NOUS_HOME=str(self.root / 'isolated-state'),
                           NOUS_LEDGER_HOME=str(self.root / 'isolated-ledger'))
        result = subprocess.run([sys.executable, str(hooks / 'nous_training_episode_demo.py'), '--out', str(demo)],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'synthetic_episode_demo_failed')
        self.dataset = demo / 'dataset'
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        original = self.input_hashes()
        report = self.split(group_by='session', eval_ratio=0.3, seed='phase7')
        self.assertEqual(sum(part['sft'] for part in report['counts'].values()), 12)
        self.assertEqual(sum(part['dpo'] for part in report['counts'].values()), 0)
        self.assertEqual(report['input_manifest_sha256'], original['manifest.json'])
        self.assertEqual(self.input_hashes(), original)
        self.assertTrue(all(len(parts) == 1 for parts in self.origin_splits(report).values()))
        source_rows = (self.dataset / 'sft.jsonl').read_bytes().splitlines(keepends=True)
        for row in report['line_provenance']:
            raw_rows = (self.output / row['file']).read_bytes().splitlines(keepends=True)
            self.assertEqual(raw_rows[row['line'] - 1], source_rows[row['input_line'] - 1])
            self.assertTrue(all(set(origin['target']) == {'step'} for origin in row['origins']))
        for name, info in report['files'].items():
            raw = (self.output / name).read_bytes()
            self.assertEqual(info['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(info['records'], len(raw.splitlines()))
        self.assertEqual(json.loads((self.output / 'split-manifest.json').read_text()), report)

    def test_dpo_binds_to_source_trajectory_even_without_preference_project_id(self):
        self.make([trajectory('source', project='pa'), trajectory('other', project='pb')], pairs=[pair('pref', 'source')])
        report = self.split(group_by='session', eval_ratio=0.5)
        grouped = self.origin_splits(report)
        self.assertEqual(grouped['source'], grouped['pref'])
        dpo = next(row for row in report['line_provenance'] if row['input_file'] == 'dpo.jsonl')
        self.assertEqual(dpo['origins'][0]['source_trajectories'][0]['id'], 'source')

    def test_deduplicated_dpo_binds_all_source_groups_and_sft_rows(self):
        a, b = trajectory('a', 'pa'), trajectory('b', 'pb')
        for item in (a, b):
            item['messages'][0] = text('user', 'same exact task')
            review(item)
        self.make([a, b], pairs=[pair('pref-a', 'a', prompt=[a['messages'][0]]),
                                 pair('pref-b', 'b', prompt=[b['messages'][0]])])
        report = self.split(eval_ratio=0.5)
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len(report['components'][0]['group_ids']), 2)
        self.assertEqual(sum(report['counts'][p]['dpo'] for p in ('train', 'eval')), 1)
        self.assertEqual(len({row['file'].split('/')[0] for row in report['line_provenance']}), 1)

    def test_same_task_dpo_links_unreferenced_sft_source_with_different_answer(self):
        a, b = trajectory('a', 'pa'), trajectory('b', 'pb')
        for item in (a, b):
            item['messages'][0] = text('user', 'shared complete task')
            review(item)
        pref = pair('pref', 'a', prompt=[a['messages'][0]])
        pref['chosen'] = [copy.deepcopy(b['messages'][-1])]
        pref['evidence'][0]['record_digest'] = export.preference_digest(pref)
        self.make([a, b], pairs=[pref])
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        report = self.split(seed='0', eval_ratio=0.5)
        grouped = self.origin_splits(report)
        self.assertEqual(grouped['a'], grouped['b'])
        self.assertEqual(grouped['a'], grouped['pref'])
        self.assertEqual(len(report['components']), 1)
        dpo = next(row for row in report['line_provenance'] if row['input_file'] == 'dpo.jsonl')
        self.assertEqual({source['id'] for source in dpo['origins'][0]['prompt_matches']}, {'a', 'b'})
        self.assertIn('cross_source_prompt_groups_connected', report['warnings'])

    def test_prompt_matching_ignores_message_ids_and_timestamps(self):
        a, b = trajectory('a', 'pa'), trajectory('b', 'pb')
        for index, item in enumerate((a, b)):
            item['messages'][0] = {**text('user', 'shared task'), 'id': str(index), 'ts': index + 1}
            review(item)
        pref = pair('pref', 'a', prompt=[{**text('user', 'shared task'), 'id': 'preference-id', 'ts': 10}])
        self.make([a, b], pairs=[pref])
        report = self.split()
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len(report['components'][0]['group_ids']), 2)

    def test_matching_only_last_user_message_does_not_join_different_full_prefixes(self):
        a, b = trajectory('a', 'pa'), trajectory('b', 'pb')
        for item in (a, b):
            item['messages'][0] = text('user', 'shared opening')
            item['messages'] += [text('user', 'same followup'), text('assistant', 'final ' + item['id'])]
            review(item)
        pref = pair('pref', 'a', prompt=copy.deepcopy(a['messages'][:-1]))
        self.make([a, b], pairs=[pref])
        report = self.split()
        self.assertEqual(len(report['components']), 2)
        dpo = next(row for row in report['line_provenance'] if row['input_file'] == 'dpo.jsonl')
        self.assertEqual([source['id'] for source in dpo['origins'][0]['prompt_matches']], ['a'])

    def test_equal_prompt_with_different_tools_does_not_join_groups(self):
        a, b = trajectory('a', 'pa'), trajectory('b', 'pb')
        for item in (a, b):
            item['messages'][0] = text('user', 'shared task')
            item['tools'] = [{'type': 'function', 'function': {'name': 'lookup', 'description': item['id'],
                              'parameters': {'type': 'object'}}}]
            review(item)
        pref = pair('pref', 'a', prompt=[a['messages'][0]], tools=copy.deepcopy(a['tools']))
        self.make([a, b], pairs=[pref])
        report = self.split()
        self.assertEqual(len(report['components']), 2)
        dpo = next(row for row in report['line_provenance'] if row['input_file'] == 'dpo.jsonl')
        self.assertEqual([source['id'] for source in dpo['origins'][0]['prompt_matches']], ['a'])

    def test_wrong_source_declaration_is_rejected_upstream_without_adding_a_group_bridge(self):
        a, b = trajectory('a', 'pa'), trajectory('b', 'pb')
        pref = pair('pref', 'a', prompt=[b['messages'][0]])
        manifest = self.make([a, b], pairs=[pref])
        self.assertEqual(manifest['files']['dpo.jsonl']['records'], 0)
        report = self.split()
        self.assertEqual(len(report['components']), 2)

    def test_late_native_session_metadata_and_alternate_paths_share_alias_component(self):
        a, b, c = [trajectory(label, session='import-' + label) for label in ('a', 'b', 'c')]
        a['provenance'].update(session_group_id='path-derived', source_transcript_id='path-one')
        b['provenance'].update(session_group_id='native-session', source_transcript_id='path-one')
        c['provenance'].update(session_group_id='native-session', source_transcript_id='path-two')
        other = trajectory('d', project='another-project')
        other['provenance'].update(session_group_id='native-session', source_transcript_id='path-one')
        self.make([a, b, c, other])
        report = self.split(group_by='session')
        self.assertEqual(len(report['components']), 2)
        source_components = {origin['id']: row['component_id'] for row in report['line_provenance'] for origin in row['origins']}
        self.assertEqual(source_components['a'], source_components['b'])
        self.assertEqual(source_components['a'], source_components['c'])
        self.assertNotEqual(source_components['a'], source_components['d'])
        self.assertTrue(all('source_transcript_id' in origin['provenance'] for row in report['line_provenance'] for origin in row['origins']))

    def test_source_path_alias_connects_legacy_session_id_overrides(self):
        a, b = trajectory('a', session='caller-one'), trajectory('b', session='caller-two')
        for item in (a, b):
            item['provenance']['source_transcript_id'] = 'stable-transcript'
        self.make([a, b])
        report = self.split(group_by='session')
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(len(report['components'][0]['group_ids']), 2)

    def test_unreviewed_snapshot_can_bridge_native_session_and_source_path_aliases(self):
        a, bridge, c = [trajectory(label, session=label) for label in ('a', 'bridge', 'c')]
        a['provenance'].update(session_group_id='path-derived', source_transcript_id='path-one')
        bridge['provenance'].update(session_group_id='native-session', source_transcript_id='path-one')
        bridge['evidence'] = []
        c['provenance'].update(session_group_id='native-session', source_transcript_id='path-two')
        manifest = self.make([a, bridge, c])
        self.assertEqual(manifest['files']['sft.jsonl']['records'], 2)
        report = self.split(group_by='session')
        self.assertEqual(len(report['components']), 1)
        self.assertNotIn('bridge', self.origin_splits(report))

    def test_unrelated_unreviewed_alias_groups_are_not_reported_as_sample_components(self):
        a, candidate = trajectory('a'), trajectory('candidate')
        candidate['provenance'].update(session_group_id='unrelated', source_transcript_id='unrelated-path')
        candidate['evidence'] = []
        self.make([a, candidate])
        report = self.split(group_by='session')
        self.assertEqual(len(report['components']), 1)
        self.assertEqual(sum(c['groups'] for c in report['counts'].values()), 1)

    def test_alias_keys_are_separate_identity_namespaces(self):
        a, b = trajectory('a'), trajectory('b')
        a['provenance'].update(session_group_id='x', source_transcript_id='y')
        b['provenance'].update(session_group_id='z', source_transcript_id='x')
        self.make([a, b])
        report = self.split(group_by='session')
        self.assertEqual(len(report['components']), 2)

    def test_declared_empty_source_alias_is_rejected_in_session_mode(self):
        a = trajectory('a')
        a['provenance']['source_transcript_id'] = ''
        self.make([a])
        with self.assertRaisesRegex(splitting.SplitError, '^source_alias_identity_missing$'):
            self.split(group_by='session')

    def test_ambiguous_preference_source_id_conservatively_connects_all_matches(self):
        a, b = trajectory('same-id', 'pa', shared='first'), trajectory('same-id', 'pb', shared='second')
        self.make([a, b], pairs=[pair('pref', 'same-id', prompt=[a['messages'][0]])])
        report = self.split()
        self.assertEqual(len(report['components']), 1)
        self.assertIn('preference_source_id_spans_groups', report['warnings'])

    def test_standalone_preference_cannot_fallback_to_row_split(self):
        self.make([], pairs=[pair('standalone')])
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        with self.assertRaisesRegex(splitting.SplitError, '^preference_source_identity_missing$'):
            self.split()
        self.assertFalse(self.output.exists())

    def test_missing_project_identity_rejected_before_output_creation(self):
        item = trajectory('missing')
        item['provenance'].pop('project_id')
        self.make([item])
        self.assertTrue(validate_dataset(self.dataset)['valid'])
        with self.assertRaisesRegex(splitting.SplitError, '^project_identity_missing$'):
            self.split()
        self.assertFalse(self.output.exists())

    def test_missing_session_rejected_but_project_mode_does_not_require_it(self):
        item = trajectory('missing')
        item['provenance'].pop('session_id')
        self.make([item])
        with self.assertRaisesRegex(splitting.SplitError, '^session_identity_missing$'):
            self.split(group_by='session')
        self.assertEqual(len(self.split()['components']), 1)

    def test_invalid_session_group_id_cannot_fallback_to_session_id(self):
        item = trajectory('known-session')
        item['provenance']['session_group_id'] = ''
        self.make([item])
        with self.assertRaisesRegex(splitting.SplitError, '^session_identity_missing$'):
            self.split(group_by='session')

    def test_existing_output_is_never_overwritten(self):
        self.make()
        self.output.mkdir()
        sentinel = self.output / 'keep.txt'
        sentinel.write_text('synthetic existing')
        with self.assertRaisesRegex(splitting.SplitError, '^output_already_exists$'):
            self.split()
        self.assertEqual(sentinel.read_text(), 'synthetic existing')
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_symlink_output_is_never_followed(self):
        self.make()
        target = self.root / 'untouched'
        target.mkdir()
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(splitting.SplitError, '^output_already_exists$'):
            self.split()
        self.assertEqual(list(target.iterdir()), [])

    def test_output_cannot_be_created_inside_input_dataset(self):
        self.make()
        original = self.input_hashes()
        with self.assertRaisesRegex(splitting.SplitError, '^output_inside_input_dataset$'):
            splitting.split_dataset(self.dataset, self.dataset / 'split')
        self.assertEqual(self.input_hashes(), original)

    def test_permissions_are_private_even_with_permissive_umask(self):
        self.make()
        old = os.umask(0)
        try:
            self.split()
        finally:
            os.umask(old)
        for path in [self.output, *self.output.rglob('*')]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_empty_dataset_and_extreme_ratios_have_explicit_warnings(self):
        self.make([])
        report = self.split()
        self.assertEqual(set(report['warnings']), {'empty_train_split', 'empty_eval_split', 'no_training_samples'})
        self.assertFalse(report['partition_nonempty'])
        self.assertFalse(report['evaluation_validated'])
        for split in ('train', 'eval'):
            self.assertEqual(report['counts'][split]['sft'], 0)

    def test_zero_and_one_ratios_are_deterministic_and_not_evaluation_claims(self):
        self.make()
        train = self.split(eval_ratio=0)
        ev = splitting.split_dataset(self.dataset, self.root / 'all-eval', eval_ratio=1)
        self.assertEqual(train['counts']['train']['sft'], 1)
        self.assertEqual(ev['counts']['eval']['sft'], 1)
        self.assertIn('empty_eval_split', train['warnings'])
        self.assertIn('empty_train_split', ev['warnings'])
        self.assertFalse(train['evaluation_validated'])

    def test_invalid_parameters_have_static_errors(self):
        for kwargs, code in [({'group_by': 'row'}, 'unsupported_group_by'), ({'seed': ''}, 'invalid_split_seed'),
                             *[({'eval_ratio': ratio}, 'invalid_eval_ratio') for ratio in (-0.1, 1.1, float('nan'), float('inf'), True, '0.1', 10 ** 1000)]]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(splitting.SplitError, '^' + code + '$'):
                    self.split(**kwargs)
        self.assertFalse(self.output.exists())

    def test_invalid_input_does_not_create_output(self):
        self.make()
        (self.dataset / 'sft.jsonl').write_text('SYNTHETIC_PRIVATE_BAD_DATA')
        with self.assertRaisesRegex(splitting.SplitError, '^invalid_dataset$'):
            self.split()
        self.assertFalse(self.output.exists())

    def test_post_validation_content_mutation_is_rejected_by_snapshot_hash(self):
        self.make()
        original_validate = splitting.validate_dataset
        def mutate(path):
            result = original_validate(path)
            if Path(path) == self.dataset:
                (self.dataset / 'sft.jsonl').write_text('{}\n')
            return result
        with mock.patch.object(splitting, 'validate_dataset', side_effect=mutate):
            with self.assertRaisesRegex(splitting.SplitError, '^input_changed_or_hash_mismatch$'):
                self.split()
        self.assertFalse(self.output.exists())

    def test_rehashed_post_validation_mutation_still_fails_snapshot_validation(self):
        self.make()
        original_validate = splitting.validate_dataset
        def mutate(path):
            result = original_validate(path)
            if Path(path) == self.dataset:
                target = self.dataset / 'sft.jsonl'
                value = json.loads(target.read_text())
                value['messages'][-1]['content'] = 'SYNTHETIC_TAMPERED'
                raw = (json.dumps(value) + '\n').encode()
                target.write_bytes(raw)
                file = self.dataset / 'manifest.json'
                manifest = json.loads(file.read_text())
                manifest['files']['sft.jsonl'] = {'records': 1, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                file.write_text(json.dumps(manifest))
            return result
        with mock.patch.object(splitting, 'validate_dataset', side_effect=mutate):
            with self.assertRaisesRegex(splitting.SplitError, '^invalid_snapshot_dataset$'):
                self.split()
        self.assertFalse(self.output.exists())

    def test_input_artifact_symlink_is_rejected(self):
        self.make()
        target = self.root / 'external.jsonl'
        target.write_bytes((self.dataset / 'sft.jsonl').read_bytes())
        (self.dataset / 'sft.jsonl').unlink()
        (self.dataset / 'sft.jsonl').symlink_to(target)
        with self.assertRaisesRegex(splitting.SplitError, '^invalid_dataset$'):
            self.split()
        self.assertFalse(self.output.exists())

    def test_manifest_contains_no_sample_bodies_or_private_raw_identity(self):
        item = trajectory('id@private.invalid', project='project@private.invalid')
        item['messages'][-1]['content'][0]['text'] = 'SYNTHETIC_SECRET_BODY'
        review(item)
        self.make([item])
        report = self.split()
        serialized = json.dumps(report)
        self.assertNotIn('SYNTHETIC_SECRET_BODY', serialized)
        self.assertNotIn('id@private.invalid', serialized)
        self.assertNotIn('project@private.invalid', serialized)

    def test_openai_profile_is_preserved_without_conversion(self):
        self.make(profile='openai')
        report = self.split()
        self.assertEqual(report['profile'], 'openai')
        file = next(row['file'] for row in report['line_provenance'])
        self.assertEqual((self.output / file).read_bytes(), (self.dataset / 'sft.jsonl').read_bytes())


if __name__ == '__main__':
    unittest.main()
