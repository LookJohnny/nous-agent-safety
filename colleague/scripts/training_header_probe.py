#!/usr/bin/env python3
"""Explicit CLI -> loopback metadata probe; never dispatch to model upstream.

Requires the reviewed real-multiturn runner and fixtures. CLI owns its existing
auth; header values/bodies/stdout stay in RAM. Its normal auth/background behavior
is not a proof of zero background network, but no inference leaves this bridge.
"""
import argparse,importlib.util,json,os,shutil,subprocess,sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent',choices=('codex','claude'),required=True)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--executable')
    parser.add_argument('--run-probe',action='store_true')
    args=parser.parse_args()
    if not args.run_probe:
        print(json.dumps({'status':'plan_only','agent':args.agent,'max_local_requests':1,'timeout_seconds':45,'upstream_forwarding':False,'credential_files_read_by_probe':False}))
        return 0
    os.umask(0o077)
    source=args.source_root.resolve();fixtures=args.fixtures.resolve();root=args.out
    if not root.is_absolute() or root.exists():raise ValueError('fresh_absolute_output_required')
    sys.path.insert(0,str(source/'hooks'));sys.path.insert(0,str(source/'scripts'))
    # This staged probe always uses its reviewed adjacent probe-capable bridge.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hooks'))
    import nous_training_live_bridge as bridge
    spec=importlib.util.spec_from_file_location('probe_runner',source/'scripts/training_real_multiturn.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    root.mkdir(parents=True,mode=0o700)
    for name in ('private','audit'):(root/name).mkdir(mode=0o700)
    task={'codex':'codex_event_totals','claude':'claude_merge_windows'}[args.agent]
    shutil.copytree(fixtures/args.agent/'project',root/'project')
    subprocess.run(['git','-c','init.templateDir=','init','--quiet',str(root/'project')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    runner.write_json(root/'private/workspace-baseline.json',runner.load_oracle(fixtures).freeze_workspace(root/'project',task))
    def forbidden(*args):raise AssertionError('probe_callback_must_not_run')
    with bridge.LiveBridge(args.agent,before=forbidden,after=forbidden,max_requests=1,total_timeout=45,io_timeout=15,probe_only=True) as service:
        argv=runner.command(args.agent,root,fixtures,task,source,service.base_url,args.executable)
        runner.write_json(root/'private/command-review.json',{'argv':argv})
        cli=runner.run_client(argv,root/'project',runner.child_environment(args.agent,service.base_url),timeout=45)
        report=service.report()
    assert report['upstream_requests']==report['upstream_responses']==0
    result={'schema':'nous.live-header-probe/1','agent':args.agent,'cli_started':True,'upstream_inference_forwarded':False,'provider_model_calls':0,'capture_labels_written':False,'bridge':report,'cli_exit_code':cli['exit_code'],'cli_timed_out':cli['timed_out'],'cli_output_limit_hit':cli['output_limit_hit'],'credential_files_read_by_probe':False,'unknown_names_or_header_values_persisted':False,'raw_request_body_persisted':False}
    runner.write_json(root/'report.json',result);print(json.dumps(result))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status':'failed','exception_type':type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)
