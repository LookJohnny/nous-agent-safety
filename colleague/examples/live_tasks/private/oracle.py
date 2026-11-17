"""Frozen evaluator for two synthetic pure-function tasks; no training approval writes.

Public mode uses only published cases. Full mode also uses independently fixed
cases and the immutable fixture manifest. Candidate code runs only in a child
with a closed AST/builtin capability set, limits, timeout and OS sandbox.
"""
import argparse,ast,copy,hashlib,json,os,resource,shutil,signal,subprocess,sys,tempfile,threading,time
from pathlib import Path
BASE=Path(__file__).resolve().parent
TASKS={'claude_merge_windows':'merge_windows','codex_event_totals':'event_totals'}
MAX_CODE=16384
BUILTINS=('len','type','isinstance','list','dict','set','tuple','int','str','bool','range','sorted','enumerate','zip','all','any','min','max','abs','sum','ValueError','TypeError')
METHODS={'append','extend','copy','sort','items','keys','values','get','strip','isascii','isalpha','islower'}
NODE_NAMES=set('Module FunctionDef arguments arg Return Assign AnnAssign AugAssign Expr If For While Break Continue Pass Raise Try ExceptHandler Assert Name Constant List Tuple Dict Set Subscript Slice Attribute Call keyword BinOp UnaryOp BoolOp Compare IfExp ListComp SetComp DictComp GeneratorExp comprehension JoinedStr FormattedValue Load Store Del'.split())
def encoded(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def validate_solution_safety(source,task_id):
    """Return closed static codes. Empty means syntax capabilities allowed only."""
    if task_id not in TASKS:return ['unknown_task']
    if type(source) is not str or len(source.encode('utf-8'))>MAX_CODE:return ['solution_size_limit']
    try:tree=ast.parse(source)
    except (SyntaxError,ValueError,RecursionError):return ['solution_syntax_invalid']
    nodes=list(ast.walk(tree))
    if len(nodes)>4000:return ['solution_ast_limit']
    functions={n.name for n in tree.body if isinstance(n,ast.FunctionDef)}
    if TASKS[task_id] not in functions:return ['solution_function_missing']
    if len(functions)>16 or len(functions)!=sum(isinstance(n,ast.FunctionDef) for n in tree.body):return ['solution_function_invalid']
    for node in tree.body:
        if not isinstance(node,ast.FunctionDef) and not (isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and type(node.value.value) is str):return ['solution_toplevel_disallowed']
    for node in nodes:
        if type(node).__name__ not in NODE_NAMES and not isinstance(node,(ast.operator,ast.unaryop,ast.boolop,ast.cmpop)):
            return ['solution_ast_disallowed']
        if isinstance(node,ast.Name) and '__' in node.id:return ['solution_dunder_disallowed']
        if isinstance(node,ast.arg) and '__' in node.arg:return ['solution_dunder_disallowed']
        if isinstance(node,ast.FunctionDef):
            if '__' in node.name or node.decorator_list or node.args.defaults or node.args.kw_defaults or node.args.vararg or node.args.kwarg:return ['solution_function_invalid']
        if isinstance(node,ast.Attribute):
            if node.attr not in METHODS or not isinstance(node.ctx,ast.Load):return ['solution_attribute_disallowed']
        if isinstance(node,ast.Call):
            if isinstance(node.func,ast.Name):
                if node.func.id not in set(BUILTINS)|functions:return ['solution_call_disallowed']
            elif not isinstance(node.func,ast.Attribute):return ['solution_call_disallowed']
    return []
def _limits():
    resource.setrlimit(resource.RLIMIT_CPU,(2,2))
    resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
    resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
def _worker():
    import builtins
    _limits()
    raw=sys.stdin.buffer.read(256*1024+1)
    if len(raw)>256*1024:raise ValueError('worker_input_limit')
    request=json.loads(raw);source=request['source'];task_id=request['task_id']
    codes=validate_solution_safety(source,task_id)
    if codes:print(json.dumps({'status':'failed','codes':codes,'cases':[]}));return
    capabilities={name:getattr(builtins,name) for name in BUILTINS}
    namespace={'__builtins__':capabilities}
    exec(compile(source,'<candidate>','exec'),namespace)
    function=namespace[TASKS[task_id]];out=[]
    for case in request['cases']:
        item=copy.deepcopy(case['input']);before=encoded(item);failure=[]
        try:
            value=function(item)
            if 'raises' in case:failure.append('expected_exception_missing')
            elif encoded(value)!=encoded(case['expected']):failure.append('output_mismatch')
            if 'raises' not in case:
                again=function(copy.deepcopy(case['input']))
                if encoded(value)!=encoded(again):failure.append('nondeterministic_result')
        except Exception as error:
            expected={'ValueError':ValueError,'TypeError':TypeError}.get(case.get('raises'))
            if type(error) is not expected:failure.append('exception_mismatch')
        if encoded(item)!=before:failure.append('input_mutated')
        out.append({'id':case['id'],'passed':not failure,'codes':failure})
    print(json.dumps({'status':'passed' if all(x['passed'] for x in out) else 'failed','codes':[],'cases':out,'subprocess_limits_applied':True},separators=(',',':')))
def _no_links(path):
    return all(not p.is_symlink() for p in [path,*path.parents])
def _snapshot(project):
    if not project.is_absolute() or not project.is_dir() or not _no_links(project):raise ValueError('workspace_invalid')
    result={}
    for p in project.rglob('*'):
        if p.is_symlink():raise ValueError('workspace_symlink')
        if p.is_file():
            if p.stat().st_size>256*1024:raise ValueError('workspace_file_limit')
            result[str(p.relative_to(project))]=sha(p.read_bytes())
        elif not p.is_dir():raise ValueError('workspace_nonregular')
    return result
def _policy(scratch):
    # OS boundary protects network/writes/forks. Read capabilities are denied by
    # the closed candidate AST/builtin set, not by this OS profile: restrictive
    # read profiles abort this Homebrew Python before its worker can initialize.
    return '(version 1)(allow default)(deny network*)(deny process-fork)(deny file-write*)(allow file-write* (subpath '+json.dumps(str(scratch))+'))'
def _execute(source,task_id,cases,*,require_os_sandbox=True):
    if not require_os_sandbox:return {'status':'blocked','codes':['os_sandbox_required'],'cases':[]}
    if not Path('/usr/bin/sandbox-exec').is_file():return {'status':'blocked','codes':['os_sandbox_unavailable'],'cases':[]}
    with tempfile.TemporaryDirectory(prefix='nous-a9-oracle-worker-') as folder:
        scratch=Path(folder).resolve();worker=scratch/'worker.py';worker.write_bytes(Path(__file__).read_bytes())
        policy=scratch/'policy.sb';policy.write_text(_policy(scratch))
        command=['/usr/bin/sandbox-exec','-f',str(policy),sys.executable,'-I','-S','-B',str(worker),'--worker']
        proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=scratch,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
        received=[];done=threading.Event()
        def communicate():
            try:received.append(proc.communicate(encoded({'source':source,'task_id':task_id,'cases':cases})))
            finally:done.set()
        thread=threading.Thread(target=communicate,daemon=True);thread.start();deadline=time.monotonic()+6;limit_code=None
        while not done.wait(0.05):
            if time.monotonic()>deadline:limit_code='solution_execution_timeout';break
            try:
                measure=subprocess.run(['/bin/ps','-o','rss=','-p',str(proc.pid)],capture_output=True,timeout=1)
                if measure.returncode==0 and int(measure.stdout.strip())>256*1024:limit_code='solution_memory_watchdog';break
            except (OSError,ValueError,subprocess.TimeoutExpired):
                limit_code='worker_monitor_failed';break
        if limit_code:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            thread.join(2);return {'status':'failed','codes':[limit_code],'cases':[]}
        thread.join();stdout,stderr=received[0]
        if proc.returncode!=0:
            code='os_sandbox_unavailable' if b'sandbox_apply' in stderr else 'worker_failed'
            return {'status':'blocked' if code=='os_sandbox_unavailable' else 'failed','codes':[code],'cases':[]}
        if len(stdout)>65536 or stderr:return {'status':'failed','codes':['worker_output_invalid'],'cases':[]}
        try:result=json.loads(stdout)
        except (ValueError,UnicodeError):return {'status':'failed','codes':['worker_output_invalid'],'cases':[]}
        if set(p.name for p in scratch.iterdir())!={'worker.py','policy.sb'}:return {'status':'failed','codes':['worker_side_effect_detected'],'cases':[]}
        result['os_sandbox_applied']=True;result['worker_side_effects_observed']=False
        result['memory_limit_kind']='sampled_rss_watchdog_256MiB_not_kernel_limit'
        return result
def freeze_workspace(project,task_id):
    """Caller freezes after git init and before model dispatch, outside project."""
    if task_id not in TASKS:raise ValueError('unknown_task')
    baseline=json.loads((BASE/'baseline-manifests.json').read_bytes())[task_id]
    files=_snapshot(Path(project))
    if any(files.get(name)!=digest for name,digest in baseline['files'].items()):raise ValueError('baseline_fixture_mismatch')
    if any(name not in baseline['files'] and not name.startswith('.git/') for name in files):raise ValueError('baseline_extra_files')
    return {'schema':'nous.code-task-workspace/1','task_id':task_id,'files':files,'fixture_manifest_sha256':sha(encoded(baseline))}
def evaluate_artifact(project,task_id,*,mode='full',workspace_baseline=None):
    report={'schema':'nous.code-task-verification/1','task_id':task_id,'mode':mode,'status':'blocked','codes':[],'case_count':0,'cases_passed':0,'workspace_boundary_passed':False,'subprocess_limits_applied':False,'os_sandbox_applied':False,'human_review':False,'training_approval_written':False}
    if task_id not in TASKS or mode not in ('public','full'):report['codes']=['invalid_evaluation_option'];return report
    try:
        project=Path(project);manifests=json.loads((BASE/'baseline-manifests.json').read_bytes());tasks=json.loads((BASE/'tasks.json').read_bytes())
        baseline=manifests[task_id];before=_snapshot(project)
        source=(project/'solution.py').read_text()
        if sha(source.encode())!=before.get('solution.py'):
            report['codes']=['workspace_changed_during_verification'];return report
        report.update(solution_sha256=sha(source.encode()),oracle_sha256=sha(Path(__file__).read_bytes()),baseline_manifest_sha256=sha(encoded(baseline)),test_spec_sha256=sha(encoded(tasks[task_id])))
        expected=baseline['files']
        if workspace_baseline is not None:
            frozen=workspace_baseline
            if (set(frozen)!={'schema','task_id','files','fixture_manifest_sha256'} or frozen['schema']!='nous.code-task-workspace/1' or frozen['task_id']!=task_id or frozen['fixture_manifest_sha256']!=sha(encoded(baseline)) or any(frozen['files'].get(n)!=v for n,v in expected.items()) or any(n not in expected and not n.startswith('.git/') for n in frozen['files'])):
                report['codes']=['workspace_baseline_invalid'];return report
            expected=frozen['files'];report['workspace_baseline_sha256']=sha(encoded(frozen))
        boundary=set(before)==set(expected) and all(before[n]==v for n,v in expected.items() if n!='solution.py')
        if not boundary:report['codes']=['workspace_boundary_changed'];return report
        report['workspace_boundary_passed']=True
        safety=validate_solution_safety(source,task_id)
        if safety:report.update(status='failed',codes=safety);return report
        cases=tasks[task_id]['public']+(tasks[task_id]['hidden'] if mode=='full' else [])
        # Public fixtures cannot choose expected values: frozen registration wins.
        check=_execute(source,task_id,cases)
        report.update(status=check['status'],codes=check['codes'],case_count=len(cases),cases_passed=sum(c['passed'] for c in check['cases']),subprocess_limits_applied=check.get('subprocess_limits_applied',False),os_sandbox_applied=check.get('os_sandbox_applied',False),memory_limit_kind=check.get('memory_limit_kind'),os_sandbox_scope='network_write_fork_boundary_not_read_confidentiality')
        report['case_result_digest']=sha(encoded(check['cases']))
        # Hidden case names and per-case details are not returned to the model.
        if mode=='public':report['cases']=check['cases']
        if _snapshot(project)!=before:report.update(status='failed',codes=['workspace_changed_during_verification'],workspace_boundary_passed=False)
        report['source_changed_from_baseline']=before['solution.py']!=expected['solution.py']
    except Exception:
        report.update(status='blocked',codes=['artifact_verifier_failed'])
    return report
def check_public(project,task_id,*,workspace_baseline=None):return evaluate_artifact(project,task_id,mode='public',workspace_baseline=workspace_baseline)
def main():
    if sys.argv[1:]==['--worker']:
        try:_worker()
        except Exception:print(json.dumps({'status':'failed','codes':['worker_failed'],'cases':[]}))
        return
    parser=argparse.ArgumentParser();parser.add_argument('--task',required=True,choices=sorted(TASKS));parser.add_argument('--project',required=True);parser.add_argument('--mode',choices=('public','full'),default='full');parser.add_argument('--out');parser.add_argument('--baseline')
    args=parser.parse_args();baseline=json.loads(Path(args.baseline).read_bytes()) if args.baseline else None
    report=evaluate_artifact(args.project,args.task,mode=args.mode,workspace_baseline=baseline)
    if args.out:
        fd=os.open(args.out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump(report,f,sort_keys=True,indent=2);f.write('\n')
    print(json.dumps(report,sort_keys=True));raise SystemExit(0 if report['status']=='passed' else 1)
if __name__=='__main__':main()
