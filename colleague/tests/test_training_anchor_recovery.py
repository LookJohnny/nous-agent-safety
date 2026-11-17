"""Only synthetic artifact anchors and transcripts. No real log/body fixtures."""
import copy,hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'hooks/nous_training.py').exists():sys.path.insert(0,os.environ['NOUS_TEST_SOURCE_HOOKS'])
sys.path.insert(0,str(ROOT/'hooks'))
from nous_training_anchor_recovery import recover_anchored_write,MARKER,OMITTED
from nous_transcript import parse_transcript_bytes

def sha(b):return hashlib.sha256(b).hexdigest()
class AnchoredWrite(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='nous-anchor-test-');self.addCleanup(self.temp.cleanup)
  self.base=Path(self.temp.name).resolve();self.root=self.base/'repo';self.backup=self.base/'backup';self.old=self.base/'old-project'
  self.root.mkdir();self.backup.mkdir();self.rel='docs/result.md';self.content='Synthetic anchored artifact.\n'*20
  for p in (self.root/self.rel,self.backup/self.rel):p.parent.mkdir();p.write_text(self.content)
  self.git('init','-q');self.git('add',self.rel);self.git('-c','user.name=Synthetic','-c','user.email=synthetic@example.com','commit','-qm','synthetic anchor')
  self.anchor={'relative_path':self.rel,'content_sha256':sha(self.content.encode())}
  self.manifest={'entries':{self.rel:{'type':'file','size':len(self.content.encode()),'sha256':self.anchor['content_sha256']}}}
  self.source=[{'type':'assistant','uuid':'a','sessionId':'session','cwd':str(self.old),'message':{'role':'assistant','content':[{'type':'tool_use','id':'call-1','name':'Write','input':{'file_path':str(self.old/self.rel),'content':self.content}}]}}, {'type':'user','uuid':'u','sessionId':'session','cwd':str(self.old),'message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'call-1','content':'Observed synthetic write result','is_error':False}]}}]
  parsed=parse_transcript_bytes(self.snapshot(),agent='claude')
  self.record={'schema':'nous.trajectory/1','id':'synthetic-record','agent':{'id':'claude'},'messages':parsed['messages'],'quality':{'complete':False,'issues':['historical_workspace_unavailable'],'missing':['request_context','tool_definitions']},'provenance':{'historical_tool_payloads_omitted':2},'evidence':[{'kind':'verifier','result':'passed','verifier':'synthetic-old','record_digest':'0'*64}]}
  self.record['messages'][0]['content'][0]['input']=copy.deepcopy(OMITTED);self.record['messages'][1]['content'][0]['content']=MARKER
 def git(self,*args):return subprocess.run(['git','-C',str(self.root),*args],capture_output=True,check=True).stdout
 def snapshot(self):return b''.join((json.dumps(x)+'\n').encode() for x in self.source)
 def recover(self,**kwargs):
  return recover_anchored_write(kwargs.pop('record',self.record),kwargs.pop('snapshot',self.snapshot()),kwargs.pop('anchor',self.anchor),original_root=kwargs.pop('original_root',self.old),target_root=kwargs.pop('target_root',self.root),backup_root=self.backup,backup_manifest=kwargs.pop('backup_manifest',self.manifest),**kwargs)
 def held(self,code=None,**kw):
  before=copy.deepcopy(kw.get('record',self.record));r,receipt=self.recover(**kw)
  self.assertEqual(r,before);self.assertEqual(receipt['status'],'not_recovered')
  if code:self.assertEqual(receipt['code'],code)
 def test_exact_pair_only_preserves_quality_old_evidence_and_raw_path(self):
  old=copy.deepcopy(self.record);r,x=self.recover()
  self.assertEqual(x['status'],'recovered_observed_pair');self.assertEqual(x['omitted_payloads_after'],0)
  self.assertEqual(r['quality'],old['quality']);self.assertEqual(r['evidence'],old['evidence']);self.assertEqual(self.record,old)
  self.assertEqual(r['messages'][0]['content'][0]['input'],self.source[0]['message']['content'][0]['input'])
  self.assertEqual(r['messages'][1]['content'][0]['content'],'Observed synthetic write result')
  self.assertNotIn(self.content,json.dumps(x));self.assertNotIn(str(self.old),json.dumps(x))
 def test_observed_content_hash_mismatch(self):
  self.source[0]['message']['content'][0]['input']['content']='different';self.held('anchor_observed_content_mismatch')
 def test_current_target_mismatch(self):
  (self.root/self.rel).write_text('changed');self.held('anchor_content_mismatch')
 def test_backup_file_and_manifest_mismatch(self):
  (self.backup/self.rel).write_text('changed');self.held('anchor_content_mismatch')
  m=copy.deepcopy(self.manifest);m['entries'][self.rel]['sha256']='0'*64;self.held('anchor_manifest_mismatch',backup_manifest=m)
 def test_git_head_blob_mismatch(self):
  (self.root/self.rel).write_text('different committed content');self.git('add',self.rel);self.git('-c','user.name=Synthetic','-c','user.email=synthetic@example.com','commit','-qm','different');(self.root/self.rel).write_text(self.content);self.held('anchor_git_mismatch')
 def test_current_git_exclusion_blocks(self):
  # Tracked anchors cannot normally be ignored; fail-closed policy response must
  # still block a source whose current exclusion environment cannot be checked.
  with mock.patch('nous_training_anchor_recovery.nous_privacy.excluded',return_value='privacy_exclusion_unverified'):self.held('anchor_current_exclusion')
 def test_actual_current_gitignore_blocks_untracked_target(self):
  self.git('rm','--cached',self.rel);(self.root/'.gitignore').write_text(self.rel+'\n');self.held('anchor_current_exclusion')
 def test_static_excluded_operand_and_escaping_evidence(self):
  a={'relative_path':'.env','content_sha256':self.anchor['content_sha256']};self.held('anchor_static_exclusion',anchor=a)
  for path in ('../result.md','/outside','docs/../result.md'):
   a={'relative_path':path,'content_sha256':self.anchor['content_sha256']};self.held('anchor_invalid_path',anchor=a)
 def test_target_or_original_symlink_is_rejected(self):
  target=self.root/self.rel;target.unlink();target.symlink_to(self.backup/self.rel);self.held('anchor_symlink')
  target.unlink();target.write_text(self.content);self.old.symlink_to(self.root,target_is_directory=True);self.held('anchor_symlink')
 def test_duplicate_calls_even_when_parser_deduplicates_uuid(self):
  self.source.insert(1,copy.deepcopy(self.source[0]));self.held('anchor_call_ambiguous')
 def test_unknown_input_key_and_different_path(self):
  self.source[0]['message']['content'][0]['input']['extra']='unapproved';self.held('anchor_input_contract')
  del self.source[0]['message']['content'][0]['input']['extra'];self.source[0]['message']['content'][0]['input']['file_path']=str(self.old/'wrong.md');self.held('anchor_call_ambiguous')
 def test_missing_duplicate_or_bad_link_result_is_never_invented(self):
  original=copy.deepcopy(self.source);self.source=self.source[:1];self.held('anchor_result_ambiguous')
  self.source=copy.deepcopy(original)+[copy.deepcopy(original[1])];self.held('anchor_result_ambiguous')
  self.source=copy.deepcopy(original);self.source[1]['message']['content'][0]['tool_use_id']='other';self.held('anchor_result_ambiguous')
 def test_existing_nonplaceholder_result_is_unchanged(self):
  r=copy.deepcopy(self.record);r['messages'][1]['content'][0]['content']='Already retained';self.held('anchor_cached_result_mismatch',record=r)
 def test_observed_failure_not_converted_to_success(self):
  self.source[1]['message']['content'][0].update(content='Observed failure after writing',is_error=True)
  self.record['messages'][1]['content'][0]['is_error']=True
  r,x=self.recover();self.assertEqual(x['status'],'recovered_observed_pair');self.assertTrue(x['result_is_error']);self.assertFalse(r['quality']['complete'])
 def test_midcheck_target_change_is_rejected(self):
  import nous_training_anchor_recovery as module
  original=module.parse_transcript_bytes
  def changed(*a,**kw):
   result=original(*a,**kw);(self.root/self.rel).write_text('changed during source parse');return result
  with mock.patch.object(module,'parse_transcript_bytes',side_effect=changed):self.held('anchor_changed')

if __name__=='__main__':unittest.main()
