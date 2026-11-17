"""Synthetic-only checks for the ordered event privacy boundary."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'hooks'))
from nous_codemode_privacy import ProtocolPrivacyError, omit_calls, scrub_protocol
from nous_codemode_protocol import parse_protocol, validate_protocol_shape


PRIVATE = 'SYNTHETIC_PRIVATE_VALUE'
EMAIL = 'person@private-company.invalid'
OMITTED = '[excluded file content omitted]'


def clean(value, secret_fields=True):
    if isinstance(value, str):
        return value.replace(EMAIL, '<EMAIL_TEST>')
    if isinstance(value, list):
        return [clean(v, secret_fields) for v in value]
    if isinstance(value, dict):
        return {clean(k, False): '<SECRET>' if secret_fields and k in {'password', 'api_key'} and v else clean(v, secret_fields)
                for k, v in value.items()}
    return value


def identifier(value):
    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value) and clean(value) == value:
        return value
    return 'redacted_' + hashlib.sha256(value.encode()).hexdigest()[:24]


def event(kind, seq=1, **payload):
    return {'seq': seq, 'type': kind, 'payload': {'type': kind, **payload}}


def protocol(events):
    return {'schema': 'nous.codex.events/1', 'adapter_version': 'codex-events-1', 'events': events,
            'quality': {'complete': True, 'missing': [], 'issues': []}}


def outer(call_id='outer', seq=1):
    return event('custom_tool_call', seq, call_id=call_id, name='exec', input='text(await tools.read_file({path:"private.txt"}));')


def output(call_id='outer', seq=3):
    return event('custom_tool_call_output', seq, call_id=call_id, output=PRIVATE)


def audit(seq=2, child_id='child', **overrides):
    payload = {'type': 'McpToolCall', 'id': child_id, 'server': 'fixture', 'tool': 'read_file',
               'arguments': {'path': 'private.txt', 'password': PRIVATE}, 'status': 'completed',
               'readOnlyHint': True,
               'result': {'content': [{'type': 'text', 'text': PRIVATE}], 'isError': True,
                          'structuredContent': {'raw': PRIVATE}}}
    payload.update(overrides)
    return {'seq': seq, 'type': 'runtime_tool_execution', 'model_visible': False, 'payload': payload}


class CodemodePrivacy(unittest.TestCase):
    def scrub(self, value, **kwargs):
        return scrub_protocol(value, clean=clean, identifier=identifier, **kwargs)

    def test_unchanged_custom_input_is_not_parsed_or_rewritten_and_needs_review(self):
        value = protocol([outer(), output(seq=2)])
        safe, issues, excluded, review = self.scrub(value)
        self.assertEqual(safe, value)
        self.assertEqual((issues, excluded, review), ([], set(), True))
        self.assertEqual(safe['events'][0]['payload']['input'], value['events'][0]['payload']['input'])

    def test_private_runtime_child_taints_outer_input_output_and_preserves_error(self):
        value = protocol([outer(), audit(), output()])
        original = copy.deepcopy(value)
        with mock.patch('nous_codemode_privacy.nous_privacy.excluded', return_value='.gitignore ignored') as check:
            safe, issues, excluded, review = self.scrub(value, project='/synthetic/project')
        self.assertEqual(value, original)
        self.assertEqual(excluded, {'outer', 'child'})
        self.assertTrue(review)
        self.assertIn('excluded_file_content', issues)
        self.assertFalse(safe['quality']['complete'])
        self.assertNotIn(PRIVATE, json.dumps(safe))
        self.assertEqual(safe['events'][0]['payload']['input'], OMITTED)
        self.assertEqual(safe['events'][2]['payload']['output'], OMITTED)
        self.assertEqual(safe['events'][1]['payload']['arguments'], {'omitted': 'excluded_file'})
        self.assertIs(safe['events'][1]['payload']['result']['isError'], True)
        check.assert_called_once_with('/synthetic/project/private.txt', '/synthetic/project', strict=True)
        validate_protocol_shape(safe)

    def test_git_unavailable_excludes_runtime_child_and_all_ambiguous_active_calls(self):
        value = protocol([outer('a', 1), outer('b', 2), audit(3), output('a', 4), output('b', 5)])
        with tempfile.TemporaryDirectory(prefix='nous-codemode-privacy-') as temp:
            Path(temp, 'private.txt').write_text(PRIVATE)
            with mock.patch('nous_privacy.subprocess.run', side_effect=FileNotFoundError(PRIVATE)):
                safe, issues, excluded, review = self.scrub(value, project=temp)
        self.assertEqual(excluded, {'a', 'b', 'child'})
        self.assertIn('privacy_exclusion_unverified', issues)
        self.assertNotIn(PRIVATE, json.dumps(safe))
        self.assertTrue(review)

    def test_sensitive_child_and_outer_ids_map_consistently_before_exclusion(self):
        value = protocol([outer(EMAIL), audit(child_id='child@private-company.invalid'), output(EMAIL)])
        with mock.patch('nous_codemode_privacy.nous_privacy.excluded', return_value='privacy_exclusion_unverified'):
            safe, issues, excluded, _ = self.scrub(value, project='/synthetic')
        self.assertEqual(excluded, {identifier(EMAIL), identifier('child@private-company.invalid')})
        self.assertEqual(safe['events'][0]['payload']['call_id'], safe['events'][2]['payload']['call_id'])
        self.assertIn('redacted_metadata', issues)
        self.assertNotIn(EMAIL, json.dumps(safe))
        self.assertNotIn(PRIVATE, json.dumps(safe))

    def test_closed_outer_is_not_tainted_by_later_unrelated_audit(self):
        value = protocol([outer('closed', 1), output('closed', 2), outer('active', 3), audit(4), output('active', 5)])
        with mock.patch('nous_codemode_privacy.nous_privacy.excluded', return_value='ignored'):
            safe, _, excluded, _ = self.scrub(value, project='/synthetic')
        self.assertEqual(excluded, {'active', 'child'})
        self.assertEqual(safe['events'][1]['payload']['output'], PRIVATE)
        self.assertEqual(safe['events'][-1]['payload']['output'], OMITTED)

    def test_path_without_project_is_unverified_not_an_implicit_allow(self):
        safe, issues, excluded, _ = self.scrub(protocol([outer(), audit(), output()]))
        self.assertIn('privacy_exclusion_unverified', issues)
        self.assertEqual(excluded, {'outer', 'child'})
        self.assertNotIn(PRIVATE, json.dumps(safe))

    def test_safe_runtime_json_values_are_recursively_scrubbed(self):
        item = audit(arguments={'key': 'beta', 'password': PRIVATE},
                     result={'content': [{'type': 'text', 'text': EMAIL}], 'isError': False,
                             'structuredContent': {'password': PRIVATE, EMAIL: 'kept'}})
        safe, issues, excluded, _ = self.scrub(protocol([outer(), item, output()]))
        result = safe['events'][1]['payload']
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn(EMAIL, json.dumps(result))
        self.assertEqual(result['arguments']['key'], 'beta')
        self.assertIs(result['result']['isError'], False)
        self.assertEqual((issues, excluded), ([], set()))

    def test_failed_runtime_status_stays_failed_and_absent_result_is_allowed(self):
        item = audit(arguments={'key': 'beta'}, status='failed')
        item['payload'].pop('result')
        safe, _, _, _ = self.scrub(protocol([item]))
        self.assertEqual(safe['events'][0]['payload']['status'], 'failed')
        self.assertNotIn('result', safe['events'][0]['payload'])
        validate_protocol_shape(safe)

    def test_schema_property_structure_survives_but_sensitive_literals_are_removed(self):
        params = {'type': 'object', 'properties': {'password': {'type': 'string', 'default': PRIVATE,
                  'examples': [PRIVATE], 'enum': [PRIVATE], 'const': PRIVATE},
                  'key': {'type': 'string', 'default': 'beta'}},
                  'required': ['password'], 'examples': [{'password': PRIVATE, 'key': 'beta'}]}
        tools = [{'type': 'function', 'name': 'lookup', 'parameters': params}]
        safe, issues, _, _ = self.scrub(protocol([event('additional_tools', role='developer', tools=tools)]))
        mapped = safe['events'][0]['payload']['tools'][0]['parameters']
        self.assertEqual(mapped['properties']['password'], {'type': 'string'})
        self.assertEqual(mapped['required'], ['password'])
        self.assertEqual(mapped['properties']['key']['default'], 'beta')
        self.assertNotIn(PRIVATE, json.dumps(safe))
        self.assertIn('contract_redacted', issues)
        self.assertFalse(safe['quality']['complete'])

    def test_name_description_and_grammar_changes_are_contract_redactions(self):
        for field in ('name', 'description', 'definition'):
            with self.subTest(field=field):
                tool = {'type': 'custom', 'name': 'exec', 'description': 'known tool',
                        'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: /.+/'}}
                if field == 'definition':
                    tool['format'][field] = EMAIL
                else:
                    tool[field] = EMAIL
                safe, issues, _, _ = self.scrub(protocol([event('additional_tools', role='developer', tools=[tool])]))
                self.assertIn('contract_redacted', issues)
                self.assertNotIn(EMAIL, json.dumps(safe))

    def test_namespace_contracts_are_recursively_scrubbed(self):
        tool = {'type': 'namespace', 'name': 'functions', 'tools': [{'type': 'custom', 'name': 'exec',
                'description': EMAIL, 'format': {'type': 'text'}}]}
        safe, issues, _, _ = self.scrub(protocol([event('additional_tools', role='developer', tools=[tool])]))
        self.assertNotIn(EMAIL, json.dumps(safe))
        self.assertIn('contract_redacted', issues)

    def test_hidden_events_and_content_are_removed_but_effort_is_preserved(self):
        items = [event('reasoning', 1, summary=PRIVATE),
                 event('message', 2, role='assistant', channel='analysis', content=[{'type': 'output_text', 'text': PRIVATE}]),
                 event('message', 3, role='assistant', content=[{'type': 'thinking', 'thinking': PRIVATE},
                       {'type': 'output_text', 'text': 'visible'}]),
                 event('configuration_update', 4, reasoning={'effort': 'low'})]
        safe, issues, _, _ = self.scrub(protocol(items))
        self.assertNotIn(PRIVATE, json.dumps(safe))
        self.assertEqual([e['seq'] for e in safe['events']], [3, 4])
        self.assertEqual(safe['events'][1]['payload']['reasoning'], {'effort': 'low'})
        self.assertEqual(issues, [])

    def test_unknown_payload_fields_fail_with_static_errors(self):
        cases = [protocol([event('message', role='user', content=[{'type': 'input_text', 'text': 'ok'}], secret=PRIVATE)]),
                 protocol([event('unknown', private=PRIVATE)]),
                 protocol([event('configuration_update', reasoning={'effort': 'low', 'summary': PRIVATE})]),
                 protocol([event('additional_tools', role='developer', tools=[{'type': 'custom', 'name': 'exec',
                          'format': {'type': 'text'}, 'unknown': PRIVATE}])])]
        for value in cases:
            with self.subTest(value_type=value['events'][0]['type']):
                with self.assertRaises(ProtocolPrivacyError) as caught:
                    self.scrub(value)
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertRegex(str(caught.exception), r'^[a-z_]+$')

    def test_unknown_schema_fields_are_not_saved_as_annotations(self):
        value = protocol([event('additional_tools', role='developer', tools=[{'type': 'function', 'name': 'f',
                           'parameters': {'type': 'object', 'unknown_private_field': PRIVATE}}])])
        with self.assertRaisesRegex(ProtocolPrivacyError, '^invalid_protocol_tool_schema$'):
            self.scrub(value)

    def test_invalid_values_and_order_fail_closed(self):
        for bad in (float('nan'), object(), {1: 'bad'}):
            value = protocol([audit(arguments={'key': bad})])
            with self.assertRaises(ProtocolPrivacyError):
                self.scrub(value)
        for seq in (0, True, '1'):
            with self.assertRaises(ProtocolPrivacyError):
                self.scrub(protocol([outer(seq=seq)]))
        with self.assertRaises(ProtocolPrivacyError):
            self.scrub(protocol([outer(), output(seq=1)]))

    def test_wrong_type_protocol_enums_fail_with_static_exception(self):
        cases = [event('message', role=[], content=[]),
                 event('configuration_update', reasoning={'effort': {PRIVATE: True}}),
                 event('message', role='assistant', channel={PRIVATE: True}, content=[]),
                 event('custom_tool_call', call_id='x', name='exec', input='x', status=[PRIVATE])]
        for item in cases:
            with self.assertRaises(ProtocolPrivacyError) as caught:
                self.scrub(protocol([item]))
            self.assertNotIn(PRIVATE, str(caught.exception))

    def test_invalid_schema_keyword_values_fail_closed(self):
        cases = [{'type': []}, {'required': PRIVATE}, {'minimum': True}, {'additionalProperties': PRIVATE},
                 {'items': 3}, {'enum': PRIVATE}, {'type': 'unknown'}, {'pattern': {PRIVATE: True}}]
        for params in cases:
            value = protocol([event('additional_tools', role='developer', tools=[{'type': 'function', 'name': 'f', 'parameters': params}])])
            with self.assertRaisesRegex(ProtocolPrivacyError, '^invalid_protocol_tool_schema$'):
                self.scrub(value)

    def test_function_argument_whitespace_preserved_unless_scrubbing_changes_values(self):
        original = '{ "key": "beta" }'
        item = event('function_call', call_id='f1', name='lookup', arguments=original)
        safe, _, _, review = self.scrub(protocol([item]))
        self.assertEqual(safe['events'][0]['payload']['arguments'], original)
        self.assertFalse(review)
        item['payload']['arguments'] = '{"password":"' + PRIVATE + '"}'
        safe, _, _, _ = self.scrub(protocol([item]))
        self.assertNotIn(PRIVATE, json.dumps(safe))
        self.assertEqual(json.loads(safe['events'][0]['payload']['arguments']), {'password': '<SECRET>'})

    def test_function_duplicate_json_keys_are_rejected(self):
        item = event('function_call', call_id='f1', name='lookup', arguments='{"key":"a","key":"b"}')
        with self.assertRaisesRegex(ProtocolPrivacyError, '^invalid_protocol_arguments$'):
            self.scrub(protocol([item]))

    def test_sensitive_explicit_custom_file_name_excludes_input_and_result(self):
        item = outer()
        item['payload']['input'] = 'text(await tools.read_file({path:".env"}));'
        safe, issues, excluded, review = self.scrub(protocol([item, output(seq=2)]))
        self.assertEqual(excluded, {'outer'})
        self.assertIn('excluded_file_content', issues)
        self.assertTrue(review)
        self.assertNotIn(PRIVATE, json.dumps(safe))

    def test_world_refs_usage_and_context_preserve_only_known_fields(self):
        items = [{'seq': 1, 'type': 'base_instructions', 'payload': {'text': EMAIL, 'source': 'session_meta.base_instructions'}},
                 {'seq': 2, 'type': 'world_state', 'payload': {'full': True, 'state': {'tools': {'exec': 'a' * 64}, 'skills': {'includeInstructions': False}}}},
                 {'seq': 3, 'type': 'turn_context', 'payload': {'model': EMAIL, 'effort': 'low'}},
                 {'seq': 4, 'type': 'token_usage_record', 'payload': {'usage': {'input_tokens': 3, 'reasoning_output_tokens': 4}}}]
        safe, _, _, review = self.scrub(protocol(items))
        self.assertNotIn(EMAIL, json.dumps(safe))
        self.assertFalse(review)
        self.assertEqual(safe['events'][-1]['payload']['usage']['reasoning_output_tokens'], 4)
        items[1]['payload']['state']['private_body'] = PRIVATE
        with self.assertRaises(ProtocolPrivacyError):
            self.scrub(protocol(items))

    def test_scrubbing_is_idempotent_and_never_mutates_input(self):
        item = event('message', role='user', content=[{'type': 'input_text', 'text': EMAIL}])
        value = protocol([item, outer(EMAIL, 2), output(EMAIL, 3)])
        original = copy.deepcopy(value)
        safe, _, _, _ = self.scrub(value)
        again, _, _, _ = self.scrub(safe)
        self.assertEqual(safe, again)
        self.assertEqual(value, original)

    def test_legacy_view_exclusion_masks_native_copy_and_all_contained_audits(self):
        child = audit(arguments={'key': 'beta'})
        value = protocol([outer(EMAIL), child, output(EMAIL)])
        safe, _, _, _ = self.scrub(value)
        original = copy.deepcopy(safe)
        omitted = omit_calls(safe, {identifier(EMAIL)})
        self.assertEqual(safe, original)
        self.assertNotIn(PRIVATE, json.dumps(omitted))
        self.assertEqual(omitted['events'][0]['payload']['input'], OMITTED)
        self.assertEqual(omitted['events'][-1]['payload']['output'], OMITTED)
        self.assertIs(omitted['events'][1]['payload']['result']['isError'], True)
        self.assertFalse(omitted['quality']['complete'])
        validate_protocol_shape(omitted)

    def test_omit_child_masks_parent_and_earlier_sibling_audit_without_changing_status(self):
        value = protocol([outer(), audit(2, 'sibling', arguments={'key': 'beta'}, status='failed'),
                          audit(3, 'private-child', arguments={'key': 'beta'}), output(seq=4)])
        safe, _, _, _ = self.scrub(value)
        omitted = omit_calls(safe, {'private-child'})
        self.assertNotIn(PRIVATE, json.dumps(omitted))
        self.assertEqual(omitted['events'][1]['payload']['status'], 'failed')
        self.assertEqual(omitted['events'][0]['payload']['input'], OMITTED)

    def test_omit_function_preserves_json_argument_type_and_removes_all_output_forms(self):
        value = protocol([event('function_call', 1, call_id='f', name='lookup', arguments='{"key":"beta"}'),
                          event('function_call_output', 2, call_id='f', output=[{'type': 'input_text', 'text': PRIVATE}])])
        safe, _, _, _ = self.scrub(value)
        omitted = omit_calls(safe, {'f'})
        self.assertEqual(json.loads(omitted['events'][0]['payload']['arguments']), {'omitted': 'excluded_file'})
        self.assertNotIn(PRIVATE, json.dumps(omitted))
        self.assertEqual(omit_calls(omitted, {'f'}), omitted)
        self.assertEqual(omit_calls(safe, set()), safe)
        validate_protocol_shape(omitted)

    def test_empty_text_metadata_is_retained_but_bodies_are_rejected(self):
        block = {'type': 'output_text', 'text': 'visible', 'annotations': [], 'logprobs': []}
        value = protocol([event('message', role='assistant', content=[block])])
        safe, _, _, _ = self.scrub(value)
        self.assertEqual(safe, value)
        value['events'][0]['payload']['content'][0]['annotations'] = [{'private': PRIVATE}]
        with self.assertRaisesRegex(ProtocolPrivacyError, '^unsupported_protocol_content_metadata$'):
            self.scrub(value)

    def test_protocol_parser_synthetic_fixture_is_compatible(self):
        source = Path(__file__).parent.parent / 'examples' / 'training' / 'codemode.jsonl'
        value = parse_protocol(source)
        safe, issues, excluded, review = self.scrub(value)
        validate_protocol_shape(safe)
        self.assertTrue(review)
        self.assertEqual((issues, excluded), ([], set()))


if __name__ == '__main__':
    unittest.main()
