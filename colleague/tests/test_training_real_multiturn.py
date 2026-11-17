"""Controlled runner contracts: synthetic MCP/client data, never real providers."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'hooks'))
import training_real_multiturn as runner

class Streams:
 def __init__(self,raw=b''):self.buffer=io.BytesIO(raw)

class RealMultiturnRunner(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='nous-real-runner-unit-')
  self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
 def root(self,name='root'):
  root=self.base/name
  for part in ('project','audit','private'):(root/part).mkdir(parents=True,exist_ok=True)
  for name in runner.FILES:(root/'project'/name).write_text('SYNTHETIC_PUBLIC_'+name)
  return root
 def mcp(self,requests,root=None,validator=None):
  root=root or self.root()
  stdin=Streams(b''.join(runner.encoded(r)+b'\n' for r in requests));stdout=Streams()
  oracle=types.SimpleNamespace(validate_solution_safety=validator or mock.Mock(return_value=[]))
  with mock.patch.object(runner,'load_oracle',return_value=oracle),mock.patch.object(sys,'stdin',stdin),mock.patch.object(sys,'stdout',stdout):
   runner.serve_mcp(root,self.base/'fixtures','claude_merge_windows')
  return root,[json.loads(x) for x in stdout.buffer.getvalue().splitlines()]
 def request(self,name,args,ident=1):return {'jsonrpc':'2.0','id':ident,'method':'tools/call','params':{'name':name,'arguments':args}}

 def test_mcp_refuses_arbitrary_path_unknown_tool_and_extra_fields(self):
  root,answers=self.mcp([self.request('read_fixture',{'file':'../../secret'}),self.request('shell',{'command':'echo unsafe'},2),self.request('read_fixture',{'file':'TASK.md','path':'/tmp/secret'},3)])
  self.assertTrue(all('error' in r for r in answers))
  summary=json.loads((root/'audit/mcp-summary.json').read_text())
  self.assertEqual(summary['counts']['rejected'],3);self.assertEqual(summary['calls'],[])

 def test_write_requires_ast_approval_and_logs_only_digests(self):
  validator=mock.Mock(side_effect=[['solution_ast_disallowed'],[]]);root=self.root()
  before=(root/'project/solution.py').read_text();content='def merge_windows(items):\n    return []\n'
  _,answers=self.mcp([self.request('write_solution',{'content':'import os'}),self.request('write_solution',{'content':content},2)],root,validator)
  self.assertTrue(answers[0]['result']['isError']);self.assertFalse(answers[1]['result']['isError'])
  self.assertEqual((root/'project/solution.py').read_text(),content)
  validator.assert_has_calls([mock.call('import os','claude_merge_windows'),mock.call(content,'claude_merge_windows')])
  summary=(root/'audit/mcp-summary.json').read_text()
  self.assertNotIn('import os',summary);self.assertNotIn('def merge_windows',summary)
  self.assertNotIn(before,summary)

 def test_write_rejection_keeps_original_file(self):
  root=self.root();before=(root/'project/solution.py').read_bytes()
  self.mcp([self.request('write_solution',{'content':'unsafe'})],root,mock.Mock(return_value=['solution_call_disallowed']))
  self.assertEqual((root/'project/solution.py').read_bytes(),before)

 def test_symlink_solution_is_rejected_without_reading_target(self):
  root=self.root();outside=self.base/'outside';outside.write_text('SYNTHETIC_PRIVATE_SENTINEL')
  (root/'project/solution.py').unlink();(root/'project/solution.py').symlink_to(outside)
  _,answers=self.mcp([self.request('read_fixture',{'file':'solution.py'})],root)
  self.assertIn('error',answers[0]);self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL',json.dumps(answers))

 def test_mcp_public_check_is_fixed_and_rejects_supplied_commands(self):
  result={'status':'failed','codes':['output_mismatch']}
  with mock.patch.object(runner,'artifact_check',return_value=result) as check:
   root,answers=self.mcp([self.request('check_public_tests',{'command':'not permitted'}),self.request('check_public_tests',{},2)])
  check.assert_called_once_with(self.base/'fixtures',root/'project','claude_merge_windows','public',root/'private/workspace-baseline.json')
  self.assertIn('error',answers[0]);self.assertTrue(answers[1]['result']['isError'])

 def test_mcp_limits_accepted_tool_calls(self):
  root,answers=self.mcp([self.request('read_fixture',{'file':'TASK.md'},i) for i in range(17)])
  self.assertIn('error',answers[-1]);self.assertEqual(len(json.loads((root/'audit/mcp-summary.json').read_text())['calls']),16)

 def test_environment_drops_provider_secrets_and_python_injection(self):
  with mock.patch.dict(os.environ,{'OPENAI_API_KEY':'SYNTHETIC_SECRET','ANTHROPIC_AUTH_TOKEN':'SYNTHETIC_SECRET','PYTHONPATH':'/unsafe','ANTHROPIC_BASE_URL':'https://invalid.example','NOUS_HOME':'/user/state'}):
   env=runner.child_environment('claude','http://127.0.0.1:12345')
  for key in ('OPENAI_API_KEY','ANTHROPIC_AUTH_TOKEN','PYTHONPATH','NOUS_HOME'):self.assertNotIn(key,env)
  self.assertEqual(env['HOME'],os.environ['HOME']);self.assertEqual(env['ANTHROPIC_BASE_URL'],'http://127.0.0.1:12345')

 def test_commands_scope_tools_and_disable_global_configuration(self):
  root=self.root()
  a=runner.command('claude',root,self.base/'fixtures','claude_merge_windows',ROOT,'http://127.0.0.1:12345','claude-fixture')
  self.assertIn('--strict-mcp-config',a);self.assertIn('--restricted',a);self.assertIn('--no-session-persistence',a)
  self.assertEqual(a[a.index('--tools')+1],'');self.assertEqual(a[a.index('--setting-sources')+1],'')
  self.assertEqual(a[a.index('--max-turns')+1],'10');self.assertEqual(a[a.index('--max-budget-usd')+1],'0.75')
  c=runner.command('codex',root,self.base/'fixtures','codex_event_totals',ROOT,'http://127.0.0.1:12345','codex-fixture')
  self.assertIn('--ignore-user-config',c);self.assertIn('--ignore-rules',c);self.assertIn('--ephemeral',c)
  self.assertEqual(c[c.index('-s')+1],'read-only')
  settings=[c[i+1] for i,v in enumerate(c) if v=='-c']
  for setting in ('features.hooks=false','features.shell_tool=false','features.apps=false','features.memories=false','features.plugins=false'):
   self.assertIn(setting,settings)
  self.assertTrue(any(x.startswith('mcp_servers=') and 'write_solution' in x for x in settings))

 def test_client_timeout_kills_bounded_python_child(self):
  result=runner.run_client([sys.executable,'-I','-S','-c','import time;time.sleep(30)'],self.base,runner.child_environment('codex',''),timeout=.1)
  self.assertTrue(result['timed_out']);self.assertNotEqual(result['exit_code'],0)

 def test_client_output_overflow_and_errors_do_not_persist_body(self):
  marker='SYNTHETIC_PRIVATE_SENTINEL'
  code='import sys,time;sys.stdout.write('+repr(marker)+'*20000);sys.stdout.flush();time.sleep(5)'
  with mock.patch.object(runner,'MAX_OUTPUT',1024):
   result=runner.run_client([sys.executable,'-I','-S','-c',code],self.base,runner.child_environment('codex',''),timeout=2)
  self.assertTrue(result['output_limit_hit']);self.assertNotIn(marker,json.dumps(result))

 def test_model_final_is_only_a_signal_not_task_verification(self):
  message=json.dumps({'type':'result','result':runner.FINAL})
  code='print('+repr(message)+');print('+repr(json.dumps({'type':'result','result':{'untrusted':'shape'}}))+')'
  result=runner.run_client([sys.executable,'-I','-S','-c',code],self.base,runner.child_environment('codex',''),timeout=2)
  self.assertTrue(result['exact_final_observed']);self.assertNotIn('task_result_verified',result)

 def test_normal_client_exit_also_cleans_its_process_group(self):
  original=runner.os.killpg
  with mock.patch.object(runner.os,'killpg',wraps=original) as kill:
   result=runner.run_client([sys.executable,'-I','-S','-c','pass'],self.base,runner.child_environment('codex',''),timeout=2)
  self.assertEqual(result['exit_code'],0);self.assertTrue(kill.called)
  self.assertTrue(all(call.args[1]==runner.signal.SIGKILL for call in kill.call_args_list))

 def simulate_run(self,*,capture_failure=False,oracle_passes=False,bridge_passes=True,delivery=None,command_failure=False,baseline_override=None,oracle_override=None):
  sim=Path(tempfile.mkdtemp(dir=self.base));fixtures=sim/'fixtures';project=fixtures/'claude/project';project.mkdir(parents=True)
  for name in runner.FILES:(project/name).write_text('SYNTHETIC_PUBLIC')
  capture=types.ModuleType('nous_training_episode_capture')
  capture.begin_episode=mock.Mock(return_value=object())
  capture.begin_step=mock.Mock(side_effect=ValueError('SYNTHETIC_PRIVATE_ERROR') if capture_failure else None,return_value=object())
  capture.finish_step=mock.Mock(return_value={'status':'captured','response_kind':'final'})
  capture.finish_episode=mock.Mock(return_value={'status':'incomplete'})
  state={}
  class Bridge:
   def __init__(self,agent,**kwargs):state.update(kwargs);self.base_url='http://127.0.0.1:1'
   def start(self):return self
   def stop(self):state['stopped']=True
   def report(self):return {'capture_callbacks_passed':bridge_passes,'upstream_requests':1,'upstream_responses':1,'delivered_responses':1,'error_counts':{},'status_counts':{'200':1},**(delivery or {})}
  bridge=types.ModuleType('nous_training_live_bridge');bridge.LiveBridge=Bridge
  export=types.ModuleType('nous_training_export');export.export_dataset=mock.Mock()
  validate=types.ModuleType('nous_training_validate');validate.validate_dataset=mock.Mock()
  def client(*args,**kwargs):
   try:ticket=state['before'](b'SYNTHETIC_PRIVATE_REQUEST')
   except Exception:ticket=None
   state['after'](ticket,b'SYNTHETIC_PRIVATE_RESPONSE','application/json',200)
   return {'exact_final_observed':True,'exit_code':0}
  args=argparse.Namespace(out=sim/'run',fixtures=fixtures,source_root=ROOT,task='claude_merge_windows',agent='claude',run=True,executable='fixture')
  baseline={'status':'failed','workspace_boundary_passed':True,'os_sandbox_applied':True,'subprocess_limits_applied':True,'case_count':5,'cases_passed':2,'task_id':args.task,'mode':'full',**{k:'a'*64 for k in runner.ORACLE_BINDINGS},**(baseline_override or {})}
  outcome={**baseline,'status':'passed' if oracle_passes else 'failed','source_changed_from_baseline':True,'cases_passed':5 if oracle_passes else 2,**(oracle_override or {})}
  modules={'nous_training':types.ModuleType('nous_training'),'nous_training_episode_capture':capture,'nous_training_live_bridge':bridge,'nous_training_export':export,'nous_training_validate':validate}
  with mock.patch.dict(sys.modules,modules),mock.patch.dict(os.environ,{},clear=False),mock.patch.object(runner,'load_oracle',return_value=types.SimpleNamespace(freeze_workspace=lambda *args:{})),mock.patch.object(runner,'artifact_check',side_effect=[baseline,outcome]),mock.patch.object(runner,'command',side_effect=ValueError('synthetic_config_failure') if command_failure else None,return_value=['synthetic-client']),mock.patch.object(runner,'run_client',side_effect=client):
   if command_failure:
    with self.assertRaisesRegex(ValueError,'^synthetic_config_failure$'):runner.run(args)
    result=None
   else:result=runner.run(args)
  return result,capture,export,state,args.out

 def test_observer_failure_cannot_finish_episode_or_export(self):
  result,capture,export,state,out=self.simulate_run(capture_failure=True,oracle_passes=True)
  capture.finish_episode.assert_not_called();export.export_dataset.assert_not_called()
  self.assertTrue(state['stopped']);self.assertTrue(result['task_result_verified'])
  self.assertFalse(result['training_ready']);self.assertEqual(result['strict_sft_rows'],0)
  persisted=''.join(p.read_text() for p in out.rglob('*.json'))
  for marker in ('SYNTHETIC_PRIVATE_REQUEST','SYNTHETIC_PRIVATE_RESPONSE','SYNTHETIC_PRIVATE_ERROR'):self.assertNotIn(marker,persisted)

 def test_bridge_failure_cannot_export_even_if_cli_claims_completion(self):
  result,capture,export,state,out=self.simulate_run(bridge_passes=False,oracle_passes=False)
  capture.finish_episode.assert_not_called();export.export_dataset.assert_not_called()
  self.assertTrue(result['cli']['exact_final_observed']);self.assertFalse(result['task_result_verified']);self.assertFalse(result['training_ready'])

 def test_configuration_failure_stops_started_bridge(self):
  result,capture,export,state,out=self.simulate_run(command_failure=True)
  self.assertTrue(state['stopped']);capture.finish_episode.assert_not_called();export.export_dataset.assert_not_called()

 def test_incomplete_delivery_rejects_finish_and_export(self):
  for delivery in ({'delivered_responses':0},{'upstream_responses':0},{'upstream_requests':0},{'error_counts':{'client_disconnected':1}},{'status_counts':{'403':1}}):
   with self.subTest(delivery=delivery):
    result,capture,export,state,out=self.simulate_run(delivery=delivery,oracle_passes=True)
    self.assertFalse(result['delivery_complete']);capture.finish_episode.assert_not_called();export.export_dataset.assert_not_called()
    self.assertFalse(result['training_ready']);self.assertEqual(result['strict_sft_rows'],0)

 def test_complete_delivery_can_finalize_capture_but_cannot_invent_review(self):
  result,capture,export,state,out=self.simulate_run(oracle_passes=True)
  self.assertTrue(result['delivery_complete']);capture.finish_episode.assert_called_once()
  self.assertFalse(result['content_review_performed']);self.assertFalse(result['step_review_performed']);self.assertFalse(result['training_ready'])
  export.export_dataset.assert_not_called()

 def test_changed_oracle_binding_cannot_verify_task_despite_passed_status(self):
  for field in runner.ORACLE_BINDINGS:
   with self.subTest(field=field):
    result,capture,export,state,out=self.simulate_run(oracle_passes=True,oracle_override={field:'b'*64})
    self.assertFalse(result['pre_dispatch_verifier_binding_preserved']);self.assertFalse(result['task_result_verified'])

 def test_changed_task_scope_or_case_count_cannot_verify_task(self):
  for override in ({'task_id':'another_task'},{'mode':'public'},{'case_count':1},{'workspace_boundary_passed':False},{'source_changed_from_baseline':False},{'os_sandbox_applied':False}):
   with self.subTest(override=override):
    result,capture,export,state,out=self.simulate_run(oracle_passes=True,oracle_override=override)
    self.assertFalse(result['task_result_verified']);self.assertFalse(result['training_ready'])

 def test_invalid_baseline_stops_before_model_dispatch(self):
  for override in ({'os_sandbox_applied':False},{'subprocess_limits_applied':False},{'cases_passed':5},{'case_count':True},{runner.ORACLE_BINDINGS[0]:'not-a-digest'}):
   with self.subTest(override=override):
    result,capture,export,state,out=self.simulate_run(baseline_override=override)
    self.assertEqual(result['status'],'blocked_by_oracle');self.assertFalse(result['model_run_attempted'])
    capture.begin_episode.assert_not_called();self.assertEqual(state,{})

 def test_task_specific_final_accepts_json_key_order_but_rejects_other_task(self):
  expected={'task':'claude_merge_windows','status':'complete'}
  for value,wanted in [({'status':'complete','task':'claude_merge_windows'},True),({'status':'complete','task':'codex_event_totals'},False),({'status':'completed'},False)]:
   with self.subTest(value=value):
    message=json.dumps({'type':'result','result':json.dumps(value,indent=1)})
    result=runner.run_client([sys.executable,'-I','-S','-c','print('+repr(message)+')'],self.base,runner.child_environment('codex',''),timeout=2,expected_final=json.dumps(expected))
    self.assertEqual(result['exact_final_observed'],wanted)

 def test_model_prompt_uses_task_complete_contract_and_observes_initial_failures(self):
  root=self.root();args=runner.command('claude',root,self.base/'fixtures','claude_merge_windows',ROOT,'http://127.0.0.1:12345','claude-fixture')
  prompt=args[-1]
  self.assertIn('claude_merge_windows',prompt);self.assertIn('"status":"complete"',prompt)
  self.assertNotIn(runner.FINAL,prompt);self.assertIn('before',prompt)

 def test_mcp_unknown_discovery_is_classified_without_read_capability(self):
  requests=[{'jsonrpc':'2.0','id':i,'method':method,'params':{}} for i,method in enumerate(('initialize','resources/list','resources/read','SYNTHETIC_PRIVATE_METHOD'))]
  root,answers=self.mcp(requests)
  self.assertEqual(answers[0]['result']['capabilities'],{'tools':{}})
  self.assertTrue(all(x['error']['code']==-32601 for x in answers[1:]))
  audit=(root/'audit/mcp-summary.json').read_text();counts=json.loads(audit)['method_categories']
  self.assertEqual(counts['resources_list'],1);self.assertEqual(counts['unknown'],2)
  self.assertNotIn('SYNTHETIC_PRIVATE_METHOD',audit);self.assertNotIn('resources/read',audit)

 def test_mcp_argument_and_parse_errors_keep_distinct_protocol_codes(self):
  root,answers=self.mcp([self.request('read_fixture',{'file':'not-allowed'})])
  self.assertEqual(answers[0]['error']['code'],-32602)
  stdin=Streams(b'{invalid json\n');stdout=Streams()
  with mock.patch.object(runner,'load_oracle',return_value=types.SimpleNamespace()),mock.patch.object(sys,'stdin',stdin),mock.patch.object(sys,'stdout',stdout):runner.serve_mcp(root,self.base,'task')
  self.assertEqual(json.loads(stdout.buffer.getvalue())['error']['code'],-32700)

 def test_controlled_reasoning_configuration_is_explicit_not_silently_stripped(self):
  root=self.root();args=runner.command('claude',root,self.base/'fixtures','claude_merge_windows',ROOT,'http://127.0.0.1:1','claude-fixture')
  self.assertEqual(args[args.index('--model')+1],'claude-haiku-4-5-20251001');self.assertNotIn('--effort',args)
  self.assertFalse(json.loads((root/'private/claude-settings.json').read_text())['alwaysThinkingEnabled'])
  env=runner.child_environment('claude','http://127.0.0.1:1')
  self.assertEqual(env['MAX_THINKING_TOKENS'],'0');self.assertNotIn('CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS',env)
  args=runner.command('codex',root,self.base/'fixtures','codex_event_totals',ROOT,'http://127.0.0.1:1','codex-fixture')
  settings=[args[i+1] for i,v in enumerate(args) if v=='-c']
  self.assertIn('model_reasoning_effort="low"',settings);self.assertIn('model_reasoning_summary="none"',settings)
  catalog=json.loads((root/'private/model-catalog.json').read_text())['models'][0]
  self.assertEqual(catalog['default_reasoning_level'],'low');self.assertEqual([x['effort'] for x in catalog['supported_reasoning_levels']],['low'])

 def test_transport_error_diagnostics_are_exact_closed_codes(self):
  from nous_training_transport import TransportError
  class UntrustedTransportSubclass(TransportError):pass
  self.assertEqual(runner.safe_capture_error(TransportError('unsupported_hidden_reasoning')),'unsupported_hidden_reasoning')
  for error in (TransportError('SYNTHETIC_PRIVATE_ERROR'),ValueError('unsupported_hidden_reasoning'),UntrustedTransportSubclass('unsupported_hidden_reasoning'),TransportError('unsupported_hidden_reasoning','SYNTHETIC_PRIVATE_ERROR')):
   self.assertEqual(runner.safe_capture_error(error),'capture_error')

if __name__=='__main__':unittest.main()
