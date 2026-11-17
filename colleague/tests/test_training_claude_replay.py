"""Synthetic native-replay hash, binding and fail-closed regressions."""
import copy,json,os,sys,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('NOUS_TEST_SOURCE_HOOKS',str(ROOT/'hooks')))
import nous_training_claude_replay as r

def fixture():
    message={'role':'user','content':[{'type':'text','text':'Synthetic task'}]}
    shape={'system':[{'type':'text','text':'Synthetic system'}],'tools':[{'name':'fixture','input_schema':{'type':'object','properties':{}}}]}
    params={'model':'fixture-model','max_tokens':40}
    body={**copy.deepcopy(params),**copy.deepcopy(shape),'messages':[copy.deepcopy(message)]}
    body['system'][0]['cache_control']={'type':'ephemeral'}
    body['messages'][0]['content'][0]['cache_control']={'type':'ephemeral'}
    sh=r.native_hash(shape);mh=r.native_hash(message)
    rows=[{'type':'api-request-shape','sessionId':'s','shapeHash':sh,'shape':shape,'version':'2.1.318'},
          {'type':'api-request-blob','sessionId':'s','hash':mh,'message':message},
          {'type':'api-request','sessionId':'s','id':'r1','version':'2.1.318','shapeHash':sh,'params':params,'keep':0,'tail':[mh],
           'cacheControl':[{'path':['system',0],'value':{'type':'ephemeral'}},{'path':['messages',0,'content',0],'value':{'type':'ephemeral'}}], 'requestDigest':r.native_hash(body)},
          {'type':'assistant','sessionId':'s','requestRef':'r1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'Synthetic answer'}],'stop_reason':'end_turn'}}]
    return rows,body

def decode(rows):
    return list(r.iter_replay(('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows)+'\n').encode(),session_id='s'))

class Replay(unittest.TestCase):
    def blocked(self,rows,code):
        result=decode(rows);self.assertTrue(any(x.get('code')==code for x in result),result)
        self.assertFalse(any(x['status']=='recovered' for x in result),result)

    def test_exact_body_cache_restore_and_response_binding(self):
        rows,body=fixture();got=decode(rows)
        self.assertEqual(len(got),1);self.assertEqual(got[0]['normalized_request'],body)
        self.assertTrue(got[0]['response_binding']['request_ref_bound'])
        self.assertTrue(got[0]['response_binding']['terminal_marker_observed'])
        self.assertFalse(got[0]['original_wire_complete']);self.assertFalse(got[0]['task_success_verified'])
        self.assertNotIn('Synthetic answer',json.dumps(got))

    def test_js_utf16_key_order_and_string_escaping(self):
        value={'\ue000':'控制\n"\\','\U0001f600':'astral'}
        expected='{"😀":"astral","\ue000":"控制\\n\\"\\\\"}'.encode()
        self.assertEqual(r.canonical_bytes(value),expected)
        # Native writer emits sorted keys directly, so numeric keys are lexical.
        self.assertEqual(r.canonical_bytes({'2':2,'10':10,'01':1,'4294967294':4}),
                         b'{"01":1,"10":10,"2":2,"4294967294":4}')
        for number in (0.5,1.0,2**53):
            with self.assertRaisesRegex(r.ReplayError,'replay_unsupported_number'):r.canonical_bytes(number)
        with self.assertRaisesRegex(r.ReplayError,'replay_unsupported_unicode'):r.canonical_bytes('\ud800')

    def test_request_and_shape_hash_tampering(self):
        for position in (0,1,2):
            rows,_=fixture()
            key={0:'shapeHash',1:'hash',2:'requestDigest'}[position];rows[position][key]='0'*64
            self.blocked(rows,'replay_hash_mismatch')

    def test_implicit_transcript_blobs_and_string_normalization(self):
        rows,_=fixture();rows[1]={'type':'user','sessionId':'s','message':{'role':'user','content':'Synthetic task'}}
        self.assertEqual(decode(rows)[0]['status'],'recovered')

    def test_native_extended_roles_and_extra_message_fields_preserved(self):
        rows,_=fixture();m=rows[1]['message'];m.update(role='system',output_config={'effort':'low'})
        rows[1]['hash']=r.native_hash(m);rows[2]['tail']=[rows[1]['hash']]
        body={**rows[2]['params'],**copy.deepcopy(rows[0]['shape']),'messages':[copy.deepcopy(m)]}
        r._restore_cache(body,rows[2]['cacheControl']);rows[2]['requestDigest']=r.native_hash(body)
        got=decode(rows)[0];self.assertEqual(got['normalized_request']['messages'][0]['output_config'],{'effort':'low'})

    def test_base_prefix_reuse_and_agent_scope(self):
        rows,body=fixture();second=copy.deepcopy(rows[2]);second.update(id='r2',base='r1',keep=1,tail=[]);rows.append(second)
        got=decode(rows);self.assertEqual(sum(x['status']=='recovered' for x in got),2)
        rows[-1]['keep']=2;self.assertEqual(decode(rows)[-1]['code'],'replay_invalid_prefix')
        rows[-1]['keep']=1;rows[-1]['agentId']='different';self.assertEqual(decode(rows)[-1]['code'],'replay_missing_shape')

    def test_ambiguous_or_absent_response_never_bound(self):
        rows,_=fixture();rows.append(copy.deepcopy(rows[-1]));rows[-1]['message']['id']='different'
        self.assertFalse(decode(rows)[0]['response_binding']['request_ref_bound'])
        rows=rows[:3];self.assertFalse(decode(rows)[0]['response_binding']['request_ref_bound'])

    def test_wrong_order_binding_rejected(self):
        rows,_=fixture();rows.insert(0,rows.pop())
        self.assertFalse(decode(rows)[0]['response_binding']['request_ref_bound'])

    def test_duplicate_id_is_not_double_counted_or_first_accepted(self):
        rows,_=fixture();rows.append(copy.deepcopy(rows[2]));self.blocked(rows,'replay_duplicate_request_id')

    def test_cut_withheld_context_edits_are_not_reconstructed(self):
        for key in ('cut','cutRequestDigest','withheld'):
            rows,_=fixture();rows[2][key]=[];self.blocked(rows,'replay_cut_or_withheld')
        for kind in ('fork-context-ref','content-replacement'):
            rows,_=fixture();rows.insert(0,{'type':kind});self.blocked(rows,'replay_unsupported_context_edit')

    def test_cache_paths_closed_unique_and_type_checked(self):
        for path in (['messages',True],['messages',-1],['missing'],['messages',100],['messages',0,'content',0,'text']):
            rows,_=fixture();rows[2]['cacheControl'][0]['path']=path;self.blocked(rows,'replay_invalid_cache_path')
        rows,_=fixture();rows[2]['cacheControl'].append(copy.deepcopy(rows[2]['cacheControl'][0]));self.blocked(rows,'replay_duplicate_cache_path')

    def test_missing_forward_base_unknown_fields_and_versions(self):
        rows,_=fixture();rows[2].update(base='future',keep=1,tail=[]);self.blocked(rows,'replay_missing_base')
        rows,_=fixture();rows[2]['extra']='private sentinel';self.blocked(rows,'replay_invalid_request')
        rows,_=fixture();rows[2]['version']=['malformed'];self.blocked(rows,'replay_unsupported_version')

    def test_snapshot_session_duplicate_json_and_resource_limits(self):
        rows,_=fixture();rows[2]['sessionId']='other'
        with self.assertRaisesRegex(r.ReplayError,'replay_invalid_identity'):decode(rows)
        with self.assertRaisesRegex(r.ReplayError,'replay_invalid_json'):list(r.iter_replay(b'{"x":1,"x":2}\n',session_id='s'))
        rows,_=fixture()
        with mock.patch.object(r,'MAX_STORED_BYTES',1):
            with self.assertRaisesRegex(r.ReplayError,'replay_resource_limit'):decode(rows)

    def test_repeated_blob_rejected_before_any_request_deepcopy(self):
        rows,_=fixture();rows=rows[:3]
        rows[2]['tail']*=20;rows[2]['cacheControl']=[]
        for limit,value in (('MAX_BODY_BYTES',512),('MAX_BODY_NODES',60),('MAX_MESSAGES',5)):
            with self.subTest(limit=limit), mock.patch.object(r,limit,value), \
                 mock.patch.object(r.copy,'deepcopy',side_effect=AssertionError('expanded before budget')):
                self.blocked(rows,'replay_resource_limit')

    def test_cache_value_rejected_before_any_request_deepcopy(self):
        rows,_=fixture();rows=rows[:3]
        rows[2]['cacheControl'][0]['value']='x'*2000
        with mock.patch.object(r,'MAX_BODY_BYTES',512), \
             mock.patch.object(r.copy,'deepcopy',side_effect=AssertionError('expanded before budget')):
            self.blocked(rows,'replay_resource_limit')

    def test_large_shapes_share_retained_state_budget(self):
        rows=[]
        for text in ('x'*250,'y'*250):
            shape={'system':text}
            rows.append({'type':'api-request-shape','sessionId':'s','shapeHash':r.native_hash(shape),
                         'shape':shape,'version':'2.1.318'})
        with mock.patch.object(r,'MAX_STORED_BYTES',800):
            got=decode(rows)
        self.assertEqual(got,[{'kind':'metadata','status':'blocked','line_number':2,'code':'replay_resource_limit'}])

    def test_base_prefix_refs_have_a_cumulative_budget(self):
        rows,_=fixture();rows=rows[:3]
        for index in range(2,6):
            next_row=copy.deepcopy(rows[2]);next_row.update(id=f'r{index}',base='r1',keep=1,tail=[])
            rows.append(next_row)
        with mock.patch.object(r,'MAX_STORED_REFS',3):got=decode(rows)
        self.assertEqual(sum(x['status']=='recovered' for x in got),3)
        self.assertEqual(got[-1]['code'],'replay_resource_limit')

    def test_cumulative_run_checked_before_stripping_clone(self):
        rows=[{'type':'user','sessionId':'s','message':{'role':'user','content':text}}
              for text in ('x'*250,'y'*250)]
        original=r._without_cache;cloned_prefix_lengths=[]
        def observed(value):
            if type(value) is dict and value.get('role')=='user':
                cloned_prefix_lengths.append(len(value['content']))
            return original(value)
        with mock.patch.object(r,'MAX_BODY_BYTES',512), mock.patch.object(r,'_without_cache',side_effect=observed):
            self.blocked(rows,'replay_resource_limit')
        self.assertEqual(cloned_prefix_lengths,[1])

    def test_deep_shape_and_blob_fail_static_before_recursive_helpers(self):
        nested='leaf'
        for _ in range(600):nested=[nested]
        for kind in ('api-request-shape','api-request-blob'):
            row={'type':kind,'sessionId':'s'}
            if kind=='api-request-shape':row.update(shapeHash='0'*64,shape={'system':nested},version='2.1.318')
            else:row.update(hash='0'*64,message={'role':'user','content':nested})
            with self.subTest(kind=kind), mock.patch.object(r,'_without_cache',side_effect=AssertionError('clone before depth check')), \
                 mock.patch.object(r.copy,'deepcopy',side_effect=AssertionError('copy before depth check')):
                self.blocked([row],'replay_resource_limit')

    def test_poison_records_and_metadata_indexes_are_bounded(self):
        rows=[{'type':'api-request-shape','sessionId':'s','shapeHash':f'{i:064x}',
               'shape':{},'version':'2.1.318'} for i in range(3)]
        with mock.patch.object(r,'MAX_STORED_ITEMS',2):
            with self.assertRaisesRegex(r.ReplayError,'replay_resource_limit'):decode(rows)
        rows,_=fixture()
        with mock.patch.object(r,'MAX_STORED_NODES',1):
            with self.assertRaisesRegex(r.ReplayError,'replay_resource_limit'):decode(rows)

    def test_canonical_rendering_is_chunked_and_bounded(self):
        original=r.json.dumps;lengths=[]
        def observed(value,*args,**kwargs):
            lengths.append(len(value));return original(value,*args,**kwargs)
        with mock.patch.object(r,'MAX_BODY_BYTES',512), mock.patch.object(r.json,'dumps',side_effect=observed):
            with self.assertRaisesRegex(r.ReplayError,'replay_resource_limit'):r.canonical_bytes('x'*100000)
        self.assertEqual(lengths,[4096])

if __name__=='__main__':unittest.main()
