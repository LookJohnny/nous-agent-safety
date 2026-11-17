"""Synthetic secret-sweep regressions. No real credentials or network access."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / 'hooks'
# The staged module is isolated; other imports are from the local source checkout.
if not (HOOKS / 'nous_training.py').exists():
    sys.path.insert(0, os.environ['NOUS_TEST_SOURCE_HOOKS'])
sys.path.insert(0, str(HOOKS))
import nous_mask
import nous_ledger
import nous_training as training
from nous_training_export import record_digest


def fake_apify():
    return 'apify_api_' + 'SYNTHETIC0' * 3 + 'ABC123'


def fake_hook(host='open.feishu.cn'):
    return 'https://' + host + '/open-apis/bot/v2/hook/' + '00000000-1111-2222-3333-444444444444'


class SecretSweep(unittest.TestCase):
    def test_apify_token_family_anywhere(self):
        value = fake_apify()
        for source in [value, 'Use ' + value + ' now.', json.dumps({'value': value}), '`' + value + '`']:
            with self.subTest(shape=source[:5]):
                result = nous_mask.mask(source, env_file=False)
                self.assertNotIn(value, result)
                self.assertNotIn('SYNTHETIC0', result)
                self.assertIn('●●●', result)
                self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_feishu_and_lark_webhook_bearer_segment_only(self):
        for host in ['open.feishu.cn', 'open.larksuite.com']:
            original = fake_hook(host)
            value = original.rsplit('/', 1)[1]
            prefix = original.rsplit('/', 1)[0] + '/'
            for source in [original, 'URL: ' + original + '.', json.dumps({'url': original})]:
                with self.subTest(host=host):
                    result = nous_mask.mask(source, env_file=False)
                    self.assertNotIn(value, result)
                    self.assertIn(prefix, result)
                    self.assertEqual(nous_mask.mask(result, env_file=False), result)
            parsed = json.loads(nous_mask.mask(json.dumps({'url':original}), env_file=False))
            self.assertTrue(parsed['url'].startswith(prefix))

    def test_chinese_supplied_values_and_delimiters(self):
        for label in ['密码是', '密码：', '密码为 ', '密钥: ', '口令=', '令牌就是']:
            value = 'SyntheticOnly9'
            source = '共享' + label + value + '，请保管。'
            result = nous_mask.mask(source, env_file=False)
            self.assertEqual(result, '共享' + label + nous_mask.hide(value) + '，请保管。')
            self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_english_supplied_values_keep_prose(self):
        value = 'SyntheticOnly9!'
        for label in ['password is ', 'password: ', 'token = ', 'api key is ', 'API_KEY: ']:
            source = 'My ' + label + value + '. Please keep it private.'
            result = nous_mask.mask(source, env_file=False)
            self.assertNotIn(value, result)
            self.assertEqual(result, 'My ' + label + nous_mask.hide(value) + '. Please keep it private.')
            self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_explicit_assignments_accept_short_and_symbol_leading_values(self):
        for value in ['a', '!SyntheticOnly9', '_SyntheticOnly9', '@SyntheticOnly9']:
            for label in ['密码：', 'password=', 'token: ']:
                source = 'Use ' + label + value + ', then continue.'
                self.assertEqual(nous_mask.mask(source, env_file=False), 'Use ' + label + nous_mask.hide(value) + ', then continue.')

    def test_quoted_values_mask_spaces_unicode_and_escaped_quotes(self):
        examples = [
            ('共享密码是"Synthetic Only 9"。', 'Synthetic Only 9'),
            ("The password is 'Synthetic Only 9'.", 'Synthetic Only 9'),
            ('密钥为`合成测试口令`。', '合成测试口令'),
            ('The password is "Synthetic\\"Only9".', 'Synthetic\\"Only9'),
        ]
        for source, secret in examples:
            result = nous_mask.mask(source, env_file=False)
            self.assertNotIn(secret, result)
            self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_inline_env_assignments_preserve_other_arguments(self):
        source = 'Run: APIFY_TOKEN="Synthetic Only 9" PASSWORD=OtherSynthetic9 PUBLIC=visible --count=3'
        result = nous_mask.mask(source, env_file=False)
        self.assertNotIn('Synthetic Only 9', result)
        self.assertNotIn('OtherSynthetic9', result)
        self.assertIn('PUBLIC=visible --count=3', result)
        self.assertIn('APIFY_TOKEN="', result)
        self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_json_escape_values_remain_parseable(self):
        obj = {'password':'Synthetic"Quote\\Value', 'name':'public', 'nested':{'api_key':'Synthetic\nLine'}}
        result = nous_mask.mask(json.dumps(obj), env_file=False)
        parsed = json.loads(result)
        self.assertNotIn('Synthetic', result)
        self.assertEqual(parsed['name'], 'public')
        self.assertIn('●●●', parsed['password'])
        self.assertIn('●●●', parsed['nested']['api_key'])
        self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_escaped_json_keys_and_chinese_secret_labels(self):
        source = r'{"\u0070assword":"SyntheticOnly9","\u5bc6\u94a5":"SyntheticOnly8","label":"public"}'
        result = nous_mask.mask(source, env_file=False)
        parsed = json.loads(result)
        self.assertEqual(set(parsed), {'password', '密钥', 'label'})
        self.assertNotIn('SyntheticOnly', result)
        self.assertEqual(parsed['label'], 'public')
        self.assertEqual(nous_mask.mask(result, env_file=False), result)

    def test_existing_placeholders_stable_across_modes(self):
        for value in ['<REDACTED_SECRET>', '<SECRET>', '<PII_ab12>', nous_mask.hide('synthetic')]:
            for source in ['TOKEN='+value, json.dumps({'password':value}), '密码是"'+value+'"']:
                for mode in [False, True]:
                    first = nous_mask.mask(source, env_file=mode)
                    self.assertEqual(nous_mask.mask(first, env_file=mode), first)

    def test_harmless_prose_identifiers_and_example_urls(self):
        texts = [
            'A password is required. The token is optional. The password is not stored.',
            'The token is defined by the protocol. The password is stored securely.',
            '密码是保护账户安全的凭证。密钥：用于验证签名。',
            'The tokenizer uses max_tokens. session_id and tool_call_id are identifiers.',
            'Run: max_tokens=128 session_id=thread_1 tokenizer=wordpiece',
            'https://example.com/webhook/00000000-1111-2222-3333-444444444444',
            'https://open.feishu.cn/open-apis/bot/v2/hook/<YOUR_WEBHOOK_ID>',
            'https://open.larksuite.com/open-apis/bot/v2/hook/example',
            'apify_api_ is the token prefix.',
        ]
        for source in texts:
            self.assertEqual(nous_mask.mask(source, env_file=False), source)

    def test_tool_schema_ids_and_exact_content_scrubbing(self):
        schema = {'type':'function','function':{'name':'lookup','parameters':{'type':'object','properties':{
            'password':{'type':'string'},'token':{'type':'string'},'max_tokens':{'type':'integer'}},'required':['password']}}}
        record = {'messages':[{'role':'user','id':'u1','content':'共享密码是SyntheticOnly9'},
                  {'role':'assistant','id':'a1','content':[{'type':'tool_use','id':'call-secret-1','name':'lookup','input':{'password':'SyntheticOnly8'}}]}],
                  'quality':{'complete':True,'issues':[],'missing':[]}, 'tools':[copy.deepcopy(schema)], 'evidence':[]}
        with mock.patch.object(nous_ledger, '_key', return_value=b'synthetic-sweep-key-32-bytes-only!'):
            safe = training.scrub_record(record)
            second = training.scrub_record(safe)
        self.assertNotIn('SyntheticOnly', json.dumps(safe))
        self.assertEqual(safe['tools'][0], schema)
        self.assertEqual(safe['messages'][1]['content'][0]['id'], 'call-secret-1')
        self.assertEqual(safe['messages'][1]['content'][0]['name'], 'lookup')
        self.assertEqual(record_digest(safe), record_digest(second))
        self.assertEqual(record['messages'][0]['content'], '共享密码是SyntheticOnly9')

    def test_structured_chinese_secret_fields(self):
        props = {name:{'type':'string'} for name in ('密码','密钥','口令','令牌')}
        schema = {'type':'function','function':{'name':'lookup','parameters':{'type':'object','properties':props}}}
        record = {'messages':[{'role':'assistant','content':[{'type':'tool_use','id':'call-1','name':'lookup','input':{name:'SyntheticOnly9' for name in props}}]}],
                  'tools':[copy.deepcopy(schema)],'quality':{'complete':True,'issues':[],'missing':[]},'evidence':[]}
        with mock.patch.object(nous_ledger,'_key',return_value=b'synthetic-sweep-key-32-bytes-only!'):
            safe=training.scrub_record(record)
            again=training.scrub_record(safe)
        self.assertNotIn('SyntheticOnly9',json.dumps(safe))
        self.assertEqual(safe['tools'][0],schema)
        self.assertEqual(safe['messages'][0]['content'][0]['input'],{name:'<SECRET>' for name in props})
        self.assertEqual(record_digest(safe),record_digest(again))

    def test_new_redaction_does_not_resign_old_review(self):
        record={'messages':[{'role':'user','content':[{'type':'text','text':'密码是SyntheticOnly9'}]},{'role':'assistant','content':[{'type':'text','text':'received'}]}],
                'quality':{'complete':True,'issues':[],'missing':[]},'evidence':[]}
        old=record_digest(record)
        record['evidence']=[{'kind':'verifier','result':'passed','verifier':'synthetic-only','record_digest':old}]
        with mock.patch.object(nous_ledger,'_key',return_value=b'synthetic-sweep-key-32-bytes-only!'):
            safe=training.scrub_record(record)
        self.assertNotEqual(record_digest(safe),old)
        self.assertEqual(safe['evidence'][0]['record_digest'],old)


if __name__ == '__main__':
    unittest.main()
