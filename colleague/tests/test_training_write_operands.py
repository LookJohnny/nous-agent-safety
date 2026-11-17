"""Synthetic integration checks for precise native Write operand privacy."""
import copy, hashlib, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('NOUS_TEST_SOURCE_HOOKS',str(HERE/'hooks')))
sys.path.insert(0,str(HERE/'hooks'))
import nous_training as training
import nous_privacy, nous_ledger

class WriteOperands(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='nous-write-operand-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.project=self.root/'project';self.project.mkdir()
        subprocess.run(['git','init','-q',str(self.project)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        patch=mock.patch.object(nous_ledger,'_key',return_value=b'synthetic-training-key-32-bytes!!');patch.start();self.addCleanup(patch.stop)
        self.content='Documentation mentions .env and .ssh/id_rsa.\npassword=SyntheticOnlySensitive9\nContact person@private-company.invalid.'
        self.target=self.project/'docs.md';self.target.write_text(self.content)
        self.record={'schema':'nous.trajectory/1','agent':{'id':'claude'},'messages':[
            {'role':'assistant','content':[{'type':'tool_use','id':'write-one','name':'Write','format':'function','input':{'file_path':str(self.target),'content':self.content}}]},
            {'role':'user','content':[{'type':'tool_result','tool_use_id':'write-one','content':'Observed synthetic result','is_error':False}]}],
            'quality':{'complete':False,'issues':['historical_workspace_unavailable'],'missing':['historical_workspace','request_context']},'evidence':[]}
    def call(self,r=None,**kw):return training.scrub_record(r or self.record,**kw)
    def input(self,r):return r['messages'][0]['content'][0]['input']
    def held(self,r):
        self.assertEqual(self.input(r),{'omitted':'excluded_file'})
        self.assertEqual(r['messages'][1]['content'][0]['content'],'[excluded file content omitted]')
        self.assertIn('excluded_file_content',r['quality']['issues'])
    def mapping(self):
        old=self.root/'missing'/'docs.md';self.record['messages'][0]['content'][0]['input']['file_path']=str(old)
        return {'write-one':{'original_path':str(old),'content_sha256':hashlib.sha256(self.content.encode()).hexdigest(),'target_path':str(self.target),'target_project':str(self.project)}}
    def test_structured_body_is_not_an_operand_and_is_still_scrubbed(self):
        before=copy.deepcopy(self.record);result=self.call(project=str(self.project))
        self.assertIn('.env',self.input(result)['content']);self.assertIn('.ssh/id_rsa',self.input(result)['content'])
        text=json.dumps(result);self.assertNotIn('SyntheticOnlySensitive9',text);self.assertNotIn('person@private-company.invalid',text)
        self.assertEqual(result['quality'],before['quality']);self.assertEqual(result['evidence'],before['evidence']);self.assertEqual(self.record,before)
        self.assertEqual(result['privacy']['scrubber_version'],'training/2-write-operands+p2.2-m2')
    def test_explicit_exact_relocated_target_is_rechecked(self):
        mapping=self.mapping();result=self.call(_write_targets=mapping)
        self.assertIn('.env',self.input(result)['content']);self.assertEqual(result['quality'],self.record['quality'])
        self.assertNotIn(str(self.project),json.dumps(result))
    def test_no_project_or_capability_keeps_legacy_conservative_behavior(self):self.held(self.call())
    def test_sensitive_actual_operands_stay_excluded(self):
        for rel in ('.env','.env.local','.ssh/id_rsa','private.pem'):
            with self.subTest(rel=rel):
                r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['input']['file_path']=str(self.project/rel)
                self.held(self.call(r,project=str(self.project)))
    def test_ignored_target_stays_excluded(self):
        (self.project/'.gitignore').write_text('docs.md\n');self.held(self.call(project=str(self.project)))
    def test_symlink_file_and_parent_are_rejected(self):
        sensitive=self.project/'.env';sensitive.write_text('SYNTHETIC')
        link=self.project/'alias.md';link.symlink_to(sensitive)
        r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['input']['file_path']=str(link)
        self.held(self.call(r,project=str(self.project)))
        directory=self.project/'linked';directory.symlink_to(self.project,target_is_directory=True)
        r['messages'][0]['content'][0]['input']['file_path']=str(directory/'docs.md');self.held(self.call(r,project=str(self.project)))
        r['messages'][0]['content'][0]['input']['file_path']=str(directory/'..'/'docs.md');self.held(self.call(r,project=str(self.project)))
    def test_outside_project_and_control_paths_rejected(self):
        for path in (str(self.root/'elsewhere.md'),str(self.target)+'\x00',str(self.target)+'\n',''):
            r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['input']['file_path']=path
            self.held(self.call(r,project=str(self.project)))
    def test_git_unavailable_and_timeout_fail_closed(self):
        for error in (FileNotFoundError(),subprocess.TimeoutExpired(['git'],3)):
            with mock.patch.object(nous_privacy.subprocess,'run',side_effect=error):self.held(self.call(project=str(self.project)))
    def test_unknown_fields_tools_formats_and_agents_keep_legacy_scan(self):
        variants=[]
        for name in ('Read','Bash','CustomWrite','write'):
            r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['name']=name;variants.append(r)
        r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['input']['extra']='value';variants.append(r)
        r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['format']='custom';variants.append(r)
        r=copy.deepcopy(self.record);r['agent']['id']='codex';variants.append(r)
        for r in variants:self.held(self.call(r,project=str(self.project)))
    def test_mapping_source_path_and_content_binding_fail_closed(self):
        base=self.mapping()
        for field,value in (('original_path','/different/docs.md'),('content_sha256','0'*64),('content_sha256','bad')):
            mapping=copy.deepcopy(base);mapping['write-one'][field]=value
            with self.assertRaises(training.TrainingError):self.call(_write_targets=mapping)
    def test_mapping_target_mismatch_and_ignore_fail_closed(self):
        mapping=self.mapping();self.target.write_text('Different synthetic body');self.held(self.call(_write_targets=mapping))
        self.target.write_text(self.content);(self.project/'.gitignore').write_text('docs.md\n');self.held(self.call(_write_targets=mapping))
    def test_mapping_missing_target_and_symlink_fail_closed(self):
        mapping=self.mapping();self.target.unlink();self.held(self.call(_write_targets=mapping))
        destination=self.project/'elsewhere.md';destination.write_text(self.content);self.target.symlink_to(destination);self.held(self.call(_write_targets=mapping))
    def test_mapping_unknown_call_duplicate_call_and_bad_shape_are_rejected(self):
        mapping=self.mapping()
        with self.assertRaises(training.TrainingError):self.call(_write_targets={'missing':mapping['write-one']})
        with self.assertRaises(training.TrainingError):self.call(_write_targets={'write-one':None})
        r=copy.deepcopy(self.record);r['messages'].append(copy.deepcopy(r['messages'][0]))
        with self.assertRaises(training.TrainingError):self.call(r,_write_targets=mapping)
        r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['input']['extra']='ignored?'
        with self.assertRaises(training.TrainingError):self.call(r,_write_targets=mapping)
    def test_record_supplied_mapping_never_grants_privacy_capability(self):
        mapping=self.mapping();r=copy.deepcopy(self.record);r['provenance']={'write_targets':mapping};self.held(self.call(r))
    def test_structured_write_to_new_file_uses_existing_safe_parent(self):
        r=copy.deepcopy(self.record);r['messages'][0]['content'][0]['input']['file_path']=str(self.project/'new.md')
        self.assertIn('.env',self.input(self.call(r,project=str(self.project)))['content'])

if __name__=='__main__':unittest.main()
