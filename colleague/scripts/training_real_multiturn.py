#!/usr/bin/env python3
"""Opt-in real-provider multi-turn artifact acceptance with scoped local MCP.

No auth files are read. Only the CLI's existing login is used. Raw wire payloads
and CLI output stay in memory; successful observation uses the privacy pipeline.
No review or task-success labels are invented by this runner.
"""
import argparse, collections, hashlib, importlib.util, json, os
from pathlib import Path
import re, shutil, signal, subprocess, sys, tempfile, threading, time, uuid

BASE=Path(__file__).resolve().parents[1]
MAX_LINE=128*1024
MAX_OUTPUT=2*1024*1024
FINAL='{"status":"completed"}'
CLAUDE_MODEL='claude-haiku-4-5-20251001'
SERVER='nous_acceptance'
FILES=('solution.py','TASK.md','test_public.py','public_cases.json')
ORACLE_BINDINGS=('oracle_sha256','test_spec_sha256','baseline_manifest_sha256','workspace_baseline_sha256')
TRANSPORT_CODES=frozenset('''duplicate_json_key duplicate_or_invalid_start duplicate_or_missing_block_stop duplicate_response_item event_after_message_delta invalid_json invalid_json_number invalid_message_delta invalid_native_response invalid_response_identity invalid_sse_encoding invalid_stream_delta invalid_stream_index invalid_stream_sequence json_depth_limit non_successful_response partial_http_body partial_response partial_sse_frame response_id_mismatch response_item_mismatch response_size_limit sse_event_limit sse_event_type_mismatch stream_content_mismatch stream_part_missing stream_start_missing terminal_missing terminal_output_mismatch transport_timeout unsupported_content_encoding unsupported_content_type unsupported_hidden_reasoning unsupported_provider unsupported_response_item unsupported_sse_event unsupported_sse_field unsupported_stream_start'''.split())
TOOLS=[
 {'name':'read_fixture','description':'Read one explicitly named public file from this isolated code task. No other files are accessible.',
  'inputSchema':{'type':'object','properties':{'file':{'type':'string','enum':list(FILES)}},'required':['file'],'additionalProperties':False}},
 {'name':'write_solution','description':'Replace only solution.py with a pure Python solution. No imports, dynamic evaluation, file, network or process operations are allowed. A closed AST policy validates it first.',
  'inputSchema':{'type':'object','properties':{'content':{'type':'string','maxLength':16384}},'required':['content'],'additionalProperties':False}},
 {'name':'check_public_tests','description':'Run the immutable public task cases in a bounded isolated subprocess against the current solution.py. No command or source arguments accepted.',
  'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]

def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def safe_capture_error(error):
 from nous_training_transport import TransportError
 from training_multiturn_smoke import safe_capture_error as legacy_code
 if type(error) is TransportError and len(error.args)==1 and type(error.args[0]) is str and error.args[0] in TRANSPORT_CODES:return error.args[0]
 return legacy_code(error)
def write_json(path,value):
 path=Path(path);fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.acceptance-')
 try:
  with os.fdopen(fd,'wb') as f:f.write(encoded(value)+b'\n');f.flush();os.fsync(f.fileno())
  os.chmod(tmp,0o600);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def load_oracle(fixtures):
 path=fixtures/'private/oracle.py'
 spec=importlib.util.spec_from_file_location('nous_artifact_oracle',path);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
 return m
def oracle_binding_matches(before,after):
 return (before.get('status')=='failed' and before.get('workspace_boundary_passed') is True
         and before.get('os_sandbox_applied') is True and before.get('subprocess_limits_applied') is True
         and type(before.get('case_count')) is int and type(before.get('cases_passed')) is int
         and 0<=before['cases_passed']<before['case_count']
         and before.get('task_id')==after.get('task_id') and before.get('mode')==after.get('mode')=='full'
         and before.get('case_count')==after.get('case_count')
         and all(type(before.get(k)) is str and re.fullmatch('[0-9a-f]{64}',before[k]) and before[k]==after.get(k) for k in ORACLE_BINDINGS))
def artifact_check(fixtures,project,task,mode,baseline=None):
 argv=[sys.executable,'-B',str(fixtures/'private/oracle.py'),'--task',task,'--project',str(project),'--mode',mode]
 if baseline is not None:argv+=['--baseline',str(baseline)]
 p=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40)
 if len(p.stdout)>128*1024:raise ValueError('oracle_output_limit')
 result=json.loads(p.stdout)
 if result.get('status') not in ('passed','failed','blocked'):raise ValueError('oracle_status_invalid')
 return result

def serve_mcp(root,fixtures,task):
 """Fixed file operations and oracle only; never execute arbitrary commands."""
 project=root/'project';audit=root/'audit';oracle=load_oracle(fixtures)
 frozen={name:sha((project/name).read_bytes()) for name in FILES if name!='solution.py'}
 counts={'initialize':0,'tools_list':0,'read_fixture':0,'write_solution':0,'check_public_tests':0,'rejected':0}
 calls=[]
 method_names={'initialize':'initialize','ping':'ping','tools/list':'tools_list','tools/call':'tools_call','resources/list':'resources_list','resources/templates/list':'resource_templates_list','prompts/list':'prompts_list','logging/setLevel':'logging_set_level'}
 methods={key:0 for key in (*method_names.values(),'notification','unknown')}
 def persist():write_json(audit/'mcp-summary.json',{'counts':counts,'calls':calls,'method_categories':methods,'fixed_contracts_sha256':sha(encoded(TOOLS))})
 def bound():
  return all(not (project/name).is_symlink() and sha((project/name).read_bytes())==h for name,h in frozen.items()) and not (project/'solution.py').is_symlink()
 while True:
  line=sys.stdin.buffer.readline(MAX_LINE+1)
  if not line:return 0
  ident=None;rpc_error=-32600
  try:
   if len(line)>MAX_LINE:raise ValueError('mcp_line_limit')
   try:message=json.loads(line)
   except ValueError:rpc_error=-32700;raise
   if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':raise ValueError('mcp_invalid')
   if 'id' not in message:methods['notification']+=1;persist();continue
   ident=message['id']
   if type(ident) not in (str,int) or len(str(ident))>128:raise ValueError('mcp_invalid_id')
   method=message.get('method')
   methods[method_names.get(method,'unknown') if type(method) is str else 'unknown']+=1
   rpc_error=-32602
   if method=='initialize':
    counts['initialize']+=1;result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':SERVER,'version':'0.1.0a9'}}
   elif method=='ping':result={}
   elif method=='tools/list':
    counts['tools_list']+=1;result={'tools':TOOLS};write_json(audit/'tools-list.json',result)
   elif method=='tools/call':
    if len(calls)>=16 or not bound():raise ValueError('mcp_boundary')
    params=message.get('params',{});name=params.get('name');args=params.get('arguments',{})
    if name not in counts or name not in {t['name'] for t in TOOLS} or type(args) is not dict:raise ValueError('mcp_tool_invalid')
    success=True;before=sha((project/'solution.py').read_bytes())
    if name=='read_fixture':
     if set(args)!={'file'} or args['file'] not in FILES:raise ValueError('mcp_file_invalid')
     data=(project/args['file']).read_bytes()
     if len(data)>65536:raise ValueError('mcp_file_limit')
     content={'file':args['file'],'content':data.decode()}
    elif name=='write_solution':
     if set(args)!={'content'} or type(args['content']) is not str or not 0<len(args['content'].encode())<=16384:raise ValueError('mcp_write_invalid')
     codes=oracle.validate_solution_safety(args['content'],task)
     if codes:content={'status':'rejected','codes':codes};success=False
     else:
      target=project/'solution.py';fd,tmp=tempfile.mkstemp(dir=project,prefix='.solution-')
      try:
       with os.fdopen(fd,'w') as f:f.write(args['content'])
       os.chmod(tmp,0o600);os.replace(tmp,target)
      finally:
       if os.path.exists(tmp):os.unlink(tmp)
      content={'status':'written','solution_sha256':sha(target.read_bytes())}
    else:
     if args:raise ValueError('mcp_check_invalid')
     content=artifact_check(fixtures,project,task,'public',root/'private/workspace-baseline.json');success=content['status']=='passed'
    counts[name]+=1
    result={'content':[{'type':'text','text':encoded(content).decode()}],'isError':not success}
    calls.append({'name':name,'arguments_sha256':sha(encoded(args)),'result_sha256':sha(encoded(result)),'success':success,'solution_before_sha256':before,'solution_after_sha256':sha((project/'solution.py').read_bytes())})
   else:rpc_error=-32601;raise ValueError('mcp_method_invalid')
   persist();response={'jsonrpc':'2.0','id':ident,'result':result}
  except Exception:
   counts['rejected']+=1;persist();response={'jsonrpc':'2.0','id':ident,'error':{'code':rpc_error,'message':'Acceptance tool rejected this request.'}}
  sys.stdout.buffer.write(encoded(response)+b'\n');sys.stdout.buffer.flush()

def child_environment(agent,base_url):
 keep={'HOME','CODEX_HOME','PATH','USER','LOGNAME','SHELL','TMPDIR','LANG','LC_ALL','LC_CTYPE','SYSTEMROOT','SSL_CERT_FILE','SSL_CERT_DIR'}
 env={k:v for k,v in os.environ.items() if k in keep}
 env.update(PYTHONDONTWRITEBYTECODE='1',RUST_LOG='off')
 if agent=='claude':env.update(ANTHROPIC_BASE_URL=base_url,CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',DISABLE_TELEMETRY='1',DISABLE_ERROR_REPORTING='1',DISABLE_AUTOUPDATER='1',MAX_THINKING_TOKENS='0',ENABLE_TOOL_SEARCH='false')
 return env
def run_client(argv,cwd,env,timeout=180,expected_final=FINAL):
 process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
 chunks=[bytearray(),bytearray()];overflow=threading.Event()
 def pump(stream,dest):
  try:
   while True:
    part=stream.read(65536)
    if not part:break
    if len(dest)+len(part)>MAX_OUTPUT:overflow.set();break
    dest.extend(part)
  finally:stream.close()
 readers=[threading.Thread(target=pump,args=(stream,dest),daemon=True) for stream,dest in zip((process.stdout,process.stderr),chunks)]
 for t in readers:t.start()
 deadline=time.monotonic()+timeout
 while process.poll() is None and time.monotonic()<deadline and not overflow.is_set():time.sleep(.05)
 timed_out=process.poll() is None
 if timed_out:
  try:os.killpg(process.pid,signal.SIGKILL)
  except ProcessLookupError:pass
 process.wait(timeout=5)
 # Own process group includes this invocation's MCP children. Close it even
 # after a normal CLI exit; no background fixture may outlive the acceptance.
 try:os.killpg(process.pid,signal.SIGKILL)
 except ProcessLookupError:pass
 for t in readers:t.join(timeout=2)
 from training_live_smoke import diagnostic_codes
 codes=diagnostic_codes((bytes(chunks[0])+bytes(chunks[1])).decode(errors='replace'))
 final=False;turn_complete=False
 def matches(value):
  if type(value) is not str:return False
  try:return json.loads(value)==json.loads(expected_final)
  except (ValueError,TypeError):return False
 for line in bytes(chunks[0]).splitlines():
  try:r=json.loads(line)
  except Exception:continue
  if type(r) is not dict:continue
  item=r.get('item')
  if type(item) is dict and item.get('type')=='agent_message' and matches(item.get('text')):final=True
  if r.get('type')=='result' and matches(r.get('result')):final=True
  turn_complete |= r.get('type') in ('turn.completed','result')
 return {'exit_code':process.returncode,'timed_out':timed_out,'output_limit_hit':overflow.is_set(),'diagnostic_codes':codes,'exact_final_observed':final,'turn_complete_event':turn_complete,'stdout_bytes':len(chunks[0]),'stderr_bytes':len(chunks[1])}

def command(agent,root,fixtures,task,source,base_url,executable=None):
 from training_live_smoke import codex_assignment
 python=str(Path(sys.executable).resolve())
 mcp={'command':python,'args':['-B',str(Path(__file__).resolve()),'--serve','--out',str(root),'--fixtures',str(fixtures),'--task',task,'--source-root',str(source)]}
 expected_final=encoded({'task':task,'status':'complete'}).decode()
 prompt=('Complete the isolated Python code task using only the nous_acceptance MCP tools. First read TASK.md and solution.py, inspect the public tests and cases. '
         'Run check_public_tests before editing to observe the existing failures. Use write_solution to implement the requirements, then call check_public_tests again and fix any failures. Do not use other tools. '
         'No imports, dynamic code, dunder names, file/network/process access are permitted inside solution.py. '
         'You must perform multiple real tool rounds; do not claim a test passed unless the tool reports it. '
         'Once the public tests actually pass, answer only this JSON object: '+expected_final+'. The external evaluator independently checks the file and task requirements.')
 if agent=='claude':
  executable=executable or shutil.which('claude')
  write_json(root/'private/claude-settings.json',{'autoMemoryEnabled':False,'disableClaudeAiConnectors':True,'alwaysThinkingEnabled':False})
  write_json(root/'private/mcp.json',{'mcpServers':{SERVER:mcp}})
  args=[executable,'-p','--restricted','--setting-sources','','--settings',str(root/'private/claude-settings.json'),'--strict-mcp-config','--mcp-config',str(root/'private/mcp.json'),'--tools','','--allowedTools',*[f'mcp__{SERVER}__{t["name"]}' for t in TOOLS],'--permission-mode','dontAsk','--permission-prompts','none','--disable-slash-commands','--no-chrome','--no-session-persistence','--system-prompt','You complete only this isolated pure Python task through the provided MCP tools. No other actions are authorized.','--model',CLAUDE_MODEL,'--max-turns','10','--max-budget-usd','0.75','--output-format','stream-json','--verbose',prompt]
 else:
  executable=executable or shutil.which('codex')
  model='gpt-6.1-sol'
  catalog={'models':[{'slug':model,'display_name':'Controlled task with real model','base_instructions':'Complete the isolated user task through the provided MCP tools only.','supported_reasoning_levels':[{'effort':'low','description':'Low effort for this controlled task.'}],'default_reasoning_level':'low','shell_type':'disabled','visibility':'list','supported_in_api':True,'priority':0,'support_verbosity':False,'truncation_policy':{'mode':'tokens','limit':12000},'experimental_supported_tools':[],'use_responses_lite':False,'tool_mode':'direct','node_repl_disabled':True}]}
  write_json(root/'private/model-catalog.json',catalog)
  provider={'name':'Local observation to official Codex service','base_url':base_url+'/v1','wire_api':'responses','requires_openai_auth':True,'supports_websockets':False,'request_max_retries':0,'stream_max_retries':0,'stream_idle_timeout_ms':45000}
  config={'model_provider':'nous_observed','model_providers':{'nous_observed':provider},'model':model,'model_catalog_json':str(root/'private/model-catalog.json'),'model_reasoning_effort':'low','model_reasoning_summary':'none',
    'analytics.enabled':False,'features.hooks':False,'features.plugins':False,'features.apps':False,'features.shell_tool':False,'features.memories':False,'features.multi_agent':False,'features.enable_request_compression':False,'skills.include_instructions':False,'memories.use_memories':False,'memories.generate_memories':False,'project_doc_max_bytes':0,'project_root_markers':[],'web_search':'disabled','history.persistence':'none','log_dir':str(root/'private/codex-logs'),'sqlite_home':str(root/'private/codex-state'),'features.rollout_budget.enabled':True,'features.rollout_budget.limit_tokens':12000,'features.rollout_budget.reminder_at_remaining_tokens':[3000],
    'projects':{str(root/'project'):{'trust_level':'trusted'}},'mcp_servers':{SERVER:{**mcp,'enabled_tools':[t['name'] for t in TOOLS],'required':True,'default_tools_approval_mode':'approve','startup_timeout_sec':10,'tool_timeout_sec':45}}}
  args=[executable,'exec','--ignore-user-config','--ignore-rules','--skip-git-repo-check','--ephemeral','-C',str(root/'project'),'-s','read-only','--json']
  for k,v in config.items():args+=['-c',codex_assignment(k,v)]
  args.append(prompt)
 return args

def run(args):
 root=args.out;fixtures=args.fixtures.resolve();source=args.source_root.resolve()
 if not root.is_absolute() or root.exists():raise ValueError('fresh_absolute_output_required')
 os.umask(0o077);root.mkdir(parents=True,mode=0o700)
 for name in ('private','audit'): (root/name).mkdir(mode=0o700)
 task=args.task or {'claude':'claude_merge_windows','codex':'codex_event_totals'}[args.agent]
 shutil.copytree(fixtures/args.agent/'project',root/'project')
 subprocess.run(['git','-c','init.templateDir=','init','--quiet',str(root/'project')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for path in (root/'project').glob('*'):
  if path.is_file():path.chmod(0o600)
 plan={'schema':'nous.real-multiturn-plan/1','agent':args.agent,'real_model':True,'task_id':task,'max_requests':8,'timeout_seconds':180,'cli_token_limit':12000 if args.agent=='codex' else None,'claude_estimated_usd_limit':.75 if args.agent=='claude' else None,'does_not_guarantee_billing_ceiling':True,'global_configuration_modified':False,'auth_files_read_by_harness':False,'raw_wire_persisted':False,'persistent_capture_enabled':False,'tools':[t['name'] for t in TOOLS],'cli_run_authorized':args.run}
 plan['requested_model']=CLAUDE_MODEL if args.agent=='claude' else 'gpt-6.1-sol'
 write_json(root/'plan.json',plan)
 write_json(root/'private/workspace-baseline.json',load_oracle(fixtures).freeze_workspace(root/'project',task))
 baseline=artifact_check(fixtures,root/'project',task,'full',root/'private/workspace-baseline.json');write_json(root/'baseline-oracle.json',baseline)
 if baseline['status']=='passed':raise ValueError('buggy_baseline_unexpectedly_passed')
 baseline_valid=oracle_binding_matches(baseline,baseline)
 if not args.run or not baseline_valid:
  report={**plan,'status':'blocked_by_oracle' if not baseline_valid else 'plan_only','model_run_attempted':False};write_json(root/'report.json',report);return report
 os.environ['NOUS_HOME']=str(root/'private/nous-data');os.environ['NOUS_LEDGER_HOME']=str(root/'private/nous-data/ledger')
 import nous_training as training
 from nous_training_episode_capture import begin_episode,begin_step,finish_step,finish_episode
 from nous_training_live_bridge import LiveBridge
 from nous_training_export import export_dataset
 from nous_training_validate import validate_dataset
 provider={'claude':'anthropic-messages','codex':'openai-responses'}[args.agent]
 handle=begin_episode(provider,project=str(root/'project'),session=str(uuid.uuid4()),manual=True,synthetic=False,oracle=None,oracle_registration='before_dispatch')
 observations=[];failed=False
 def before(body):
  nonlocal failed
  item={'request_sha256':sha(body),'request_bytes':len(body),'status':'observed'};observations.append(item)
  if failed:raise ValueError('capture_already_failed')
  try:ticket=begin_step(handle,body)
  except Exception as error:failed=True;item.update(status='capture_failed',code=safe_capture_error(error));raise
  return (ticket,item)
 def after(state,body,content_type,status):
  nonlocal failed
  if state is None:failed=True;return
  ticket,item=state;item.update(response_sha256=sha(body),response_bytes=len(body),http_status=status)
  if failed or status!=200:failed=True;item.update(status='upstream_rejected');return
  try:r=finish_step(handle,ticket,body,content_type=content_type);item.update(status=r['status'],response_kind=r.get('response_kind'))
  except Exception as error:failed=True;item.update(status='capture_failed',code=safe_capture_error(error));raise
 bridge=LiveBridge(args.agent,before=before,after=after,max_requests=8,total_timeout=180,io_timeout=45).start()
 try:
  argv=command(args.agent,root,fixtures,task,source,bridge.base_url,args.executable)
  write_json(root/'private/command-review.json',{'argv':argv})
  cli=run_client(argv,root/'project',child_environment(args.agent,bridge.base_url),expected_final=encoded({'task':task,'status':'complete'}).decode())
 finally:bridge.stop()
 observed=bridge.report();capture={'status':'blocked'}
 delivery_complete=(observed.get('upstream_requests',0)>0 and observed.get('upstream_requests')==observed.get('upstream_responses')==observed.get('delivered_responses') and not observed.get('error_counts') and set(observed.get('status_counts',{}))=={'200'})
 if not failed and observed.get('capture_callbacks_passed') and delivery_complete:
  try:capture=finish_episode(handle)
  except Exception as error:capture={'status':'failed','code':safe_capture_error(error)}
 oracle=artifact_check(fixtures,root/'project',task,'full',root/'private/workspace-baseline.json');write_json(root/'artifact-oracle.json',oracle)
 verifier_bound=oracle_binding_matches(baseline,oracle)
 task_verified=(verifier_bound and oracle['status']=='passed' and oracle.get('workspace_boundary_passed') is True and oracle.get('source_changed_from_baseline') is True and oracle.get('os_sandbox_applied') is True and oracle.get('subprocess_limits_applied') is True)
 write_json(root/'wire-observations.json',{'schema':'nous.wire-metadata/1','raw_bodies_persisted':False,'observations':observations})
 validation=None
 if capture.get('status')=='captured':
  record=json.loads((training._root()/'records'/(capture['id']+'.json')).read_bytes())
  export_dataset([record],root/'candidate-dataset',profile='request-episode-sft-v1');validation=validate_dataset(root/'candidate-dataset');write_json(root/'candidate-validation.json',validation)
 audit=json.loads((root/'audit/mcp-summary.json').read_bytes()) if (root/'audit/mcp-summary.json').exists() else {'counts':{},'calls':[]}
 calls=audit['calls']
 report={'schema':'nous.real-multiturn-report/1','agent':args.agent,'task_id':task,'real_cli':True,'real_model_requested':True,'model_run_attempted':True,'synthetic_provider':False,'cli':cli,'bridge':observed,'delivery_complete':delivery_complete,'capture':capture,'artifact_status':oracle['status'],'artifact_verifier_sha256':oracle.get('oracle_sha256'),'pre_dispatch_verifier_binding_preserved':verifier_bound,'solution_sha256':oracle.get('solution_sha256'),'tool_calls':len(calls),'tool_rounds_observed':sum(x.get('response_kind')=='tool_calls' for x in observations),'mcp_counts':audit['counts'],'candidate_valid':validation['valid'] if validation else False,'strict_sft_rows':validation['counters']['sft'] if validation else 0,'task_result_verified':task_verified,'content_review_performed':False,'step_review_performed':False,'training_ready':False,'global_configuration_modified':False,'auth_files_read_by_harness':False,'raw_wire_persisted':False,'persistent_capture_enabled':False}
 report['requested_model']=plan['requested_model']
 write_json(root/'report.json',report)
 return report

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--agent',choices=('claude','codex'))
 parser.add_argument('--out',type=Path,required=True)
 parser.add_argument('--fixtures',type=Path,default=BASE/'examples/live_tasks')
 parser.add_argument('--source-root',type=Path,default=BASE)
 parser.add_argument('--task');parser.add_argument('--executable');parser.add_argument('--run',action='store_true');parser.add_argument('--serve',action='store_true')
 args=parser.parse_args();sys.path.insert(0,str(args.source_root/'hooks'));sys.path.insert(0,str(args.source_root/'scripts'))
 if args.serve:return serve_mcp(args.out,args.fixtures,args.task)
 if not args.agent:parser.error('--agent is required')
 try:print(json.dumps(run(args),ensure_ascii=False));return 0
 except Exception as error:
  print(json.dumps({'status':'failed','exception_type':type(error).__name__}),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
