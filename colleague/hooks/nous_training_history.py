"""One-shot historical import from an explicit metadata plan. Never enable hooks.

Source logs are read-only; only sanitized candidates and closed reports persist.
Existing source records are never overwritten or reauthorized by a batch replay.
"""
import collections
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import time

import nous_training as training
from nous_transcript import read_transcript_bytes, _signature

SCHEMA = 'nous.training.history-plan/1'
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_CANDIDATES = 10000
SCAN_CODES = frozenset('''invalid_scan_limits invalid_scan_root invalid_scan_roots scan_agent_mismatch scan_compressed_unsupported
scan_depth_limit scan_directory_limit scan_directory_required scan_directory_unreadable scan_entry_limit scan_file_limit
scan_header_invalid_json scan_header_limit scan_identity_conflict scan_identity_invalid scan_identity_unknown scan_path_escape
scan_regular_file_required scan_root_missing scan_secure_open_unavailable scan_source_changed scan_source_unreadable
scan_symlink_rejected scan_compressed_invalid scan_compressed_limit'''.split())
from nous_training_history_read import ERROR_CODES as HISTORY_READ_CODES
SCAN_CODES = SCAN_CODES | HISTORY_READ_CODES
SHA_PATTERN = re.compile(r'[0-9a-f]{64}\Z')
SAFE_FAILURES = frozenset('''transcript_too_large transcript_line_too_large transcript_too_many_lines transcript_changed_during_read
transcript_must_be_regular_file transcript_unreadable no_supported_messages record_too_large project_changed_during_collection
project_unavailable history_source_changed history_source_unavailable history_source_symlink history_invalid_candidate
history_project_unavailable history_plan_invalid history_plan_digest_mismatch history_source_agent_conflict
history_batch_revoked history_project_revoked history_storage_changed history_existing_record_invalid'''.split())


class HistoryError(ValueError):
    """Static reason codes only."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _no_links(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise HistoryError('history_source_unavailable')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise HistoryError('history_source_symlink')


def _stat(path):
    _no_links(path)
    try:
        info = os.lstat(path)
    except OSError:
        raise HistoryError('history_source_unavailable') from None
    if not stat.S_ISREG(info.st_mode):
        raise HistoryError('history_source_unavailable')
    return dict(zip(('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'), _signature(info)))


def _candidate(value):
    if not isinstance(value, dict) or value.get('agent') not in ('claude', 'codex'):
        raise HistoryError('history_invalid_candidate')
    if any(type(value.get(key)) is not str or not value[key] for key in ('path', 'cwd', 'session_id', 'source_id')):
        raise HistoryError('history_invalid_candidate')
    if not os.path.isabs(value['cwd']) or not os.path.isabs(value['path']):
        raise HistoryError('history_invalid_candidate')
    if value['source_id'] != _sha(os.fsencode(os.path.abspath(value['path']))):
        raise HistoryError('history_invalid_candidate')
    if value.get('compression', 'none') not in ('none', 'zstd'):
        raise HistoryError('history_invalid_candidate')
    if value.get('source_role', 'unknown') not in ('sessions','archived_sessions','projects','explicit','unknown') or value.get('branch_role', 'unknown') not in ('main','subagent','branch','sidechain','unknown'):
        raise HistoryError('history_invalid_candidate')
    if not value['path'].endswith('.jsonl.zst' if value.get('compression') == 'zstd' else '.jsonl'):
        raise HistoryError('history_invalid_candidate')
    info = value.get('stat')
    if type(info) is not dict or set(info) != {'dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'} or any(type(n) is not int or n < 0 for n in info.values()):
        raise HistoryError('history_invalid_candidate')
    return value


def make_plan(*, roots=None, agent='both', projects=None, max_files=MAX_CANDIDATES):
    from nous_training_history_scan import scan_history
    if agent not in ('both', 'claude', 'codex'):
        raise HistoryError('history_plan_invalid')
    scan = scan_history(roots=roots, max_files=max_files)
    wanted = {os.path.realpath(str(p)) for p in projects or []}
    candidates = [row for row in scan['candidates'] if (agent == 'both' or row['agent'] == agent)
                  and (not wanted or os.path.realpath(row['cwd']) in wanted)]
    for row in candidates: _candidate(row)
    plan = {'schema': SCHEMA, 'created_at': int(time.time()), 'scope': 'all_local_projects' if not wanted else 'explicit_projects',
            'agent': agent, 'scan_complete': scan['complete'], 'scan_counts': scan['counts'],
            'scan_skipped': scan['skipped'], 'scan_errors': scan['errors'], 'candidates': candidates,
            'scan_skipped_counts':scan.get('skipped_counts',{}),'scan_skipped_details_truncated':scan.get('skipped_details_truncated',False),
            'continuous_capture_enabled': False, 'raw_transcripts_in_plan': False}
    plan['plan_digest'] = training._hash(plan)
    return plan


def _validate_plan(plan):
    if type(plan) is not dict or plan.get('schema') != SCHEMA or type(plan.get('candidates')) is not list:
        raise HistoryError('history_plan_invalid')
    if type(plan.get('scan_complete')) is not bool or type(plan.get('scan_skipped')) is not list or type(plan.get('scan_errors')) is not list:
        raise HistoryError('history_plan_invalid')
    for row in plan['scan_skipped']:
        if (type(row) is not dict or set(row) != {'source_id','code'} or type(row['source_id']) is not str
                or not SHA_PATTERN.fullmatch(row['source_id']) or row['code'] not in SCAN_CODES):
            raise HistoryError('history_plan_invalid')
    if any(type(code) is not str or code not in SCAN_CODES for code in plan['scan_errors']):
        raise HistoryError('history_plan_invalid')
    for key in ('scan_counts','scan_skipped_counts'):
        counts = plan.get(key,{})
        names = {'roots','directories','entries','jsonl_files','compressed_files','source_files','candidates','skipped'} if key == 'scan_counts' else SCAN_CODES
        if type(counts) is not dict or any(name not in names or type(number) is not int or number < 0 for name,number in counts.items()):
            raise HistoryError('history_plan_invalid')
    raw = training._json(plan).encode()
    if len(raw) > MAX_PLAN_BYTES or len(plan['candidates']) > MAX_CANDIDATES:
        raise HistoryError('history_plan_invalid')
    if plan.get('plan_digest') != training._hash({k:v for k,v in plan.items() if k != 'plan_digest'}):
        raise HistoryError('history_plan_digest_mismatch')
    seen = set()
    for row in plan['candidates']:
        _candidate(row)
        if row['source_id'] in seen:
            raise HistoryError('history_plan_invalid')
        seen.add(row['source_id'])


def read_plan(path):
    from nous_training_export import strict_json
    info = _stat(path)
    if info['size'] > MAX_PLAN_BYTES:
        raise HistoryError('history_plan_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise HistoryError('history_plan_invalid')
        raw = stream.read(MAX_PLAN_BYTES + 1)
    try:
        value = strict_json(raw.decode())
        _validate_plan(value)
    except (ValueError, UnicodeError, RecursionError):
        raise HistoryError('history_plan_invalid') from None
    if _stat(path) != info:
        raise HistoryError('history_source_changed')
    return value


def write_plan(plan, out):
    _validate_plan(plan)
    out = Path(out)
    if out.exists() or out.is_symlink():
        raise HistoryError('history_output_exists')
    training._atomic(out, plan)
    return {'plan': str(out), 'candidate_files': len(plan['candidates']), 'scan_complete': plan['scan_complete'],
            'skipped': len(plan['scan_skipped']), 'continuous_capture_enabled': False}


def _records_stamp():
    path = training._root() / 'records'
    try: return _signature(path.stat())
    except FileNotFoundError: return None


def _existing_sources():
    result = {}
    for path in (training._root() / 'records').glob('*.json'):
        info = _stat(path)
        if info['size'] > training.MAX_RECORD_BYTES:
            raise HistoryError('history_existing_record_invalid')
        try:
            record = json.loads(path.read_bytes())
            provenance = record['provenance']
            key = provenance.get('source_transcript_id')
            if key:
                result.setdefault(key, []).append((record.get('agent', {}).get('id'), record['id'], provenance.get('source_file_sha256',provenance.get('source_snapshot_sha256'))))
        except (KeyError, TypeError, ValueError):
            raise HistoryError('history_existing_record_invalid') from None
    return result


def _failure(error):
    from nous_training_history_identity import HistoryIdentityError, ERROR_CODES
    if type(error) is HistoryIdentityError and len(error.args) == 1 and error.args[0] in ERROR_CODES:
        return error.args[0]
    if type(error) in (HistoryError, training.TrainingError) and len(error.args) == 1 and error.args[0] in SAFE_FAILURES:
        return error.args[0]
    from nous_training_history_read import HistoryReadError, ERROR_CODES as READ_CODES
    if type(error) is HistoryReadError and len(error.args) == 1 and error.args[0] in READ_CODES:
        return error.args[0]
    from nous_transcript import TranscriptReadError
    if type(error) is TranscriptReadError and len(error.args) == 1 and error.args[0] in SAFE_FAILURES:
        return error.args[0]
    return 'history_import_failed'


def import_plan(plan, out, *, progress=None):
    """Explicit batch permission only; never changes capture/export switches.

    A fresh invocation may authorize new source files. Existing/revoked records
    retain their old permission and evidence. Raw snapshots never reach reports.
    """
    from nous_training_history_identity import validate_snapshot
    _validate_plan(plan)
    out = Path(out)
    if not out.is_absolute() or out.exists() or out.is_symlink():
        raise HistoryError('history_output_exists')
    _no_links(out.parent)
    out.mkdir(mode=0o700)
    training._atomic(out / 'plan.json', plan)
    report = {'schema': 'nous.training.history-import/1', 'started_at': int(time.time()),
              'plan_digest': plan['plan_digest'], 'scan_complete': plan['scan_complete'], 'processing_complete': False,
              'all_sources_imported': False, 'full_model_request_context_recovered': False, 'continuous_capture_enabled': False, 'upload_performed': False,
              'candidate_files': len(plan['candidates']), 'scan_skipped_counts':plan.get('scan_skipped_counts',{}), 'scan_skipped': plan['scan_skipped'], 'scan_errors': plan['scan_errors'],
              'entries': [], 'counts': {}, 'datasets': {}}
    counts = collections.Counter()
    with training._Lock():
        cfg = training._config()
        global_generation = cfg.get('revocation_generation', 0)
        generations = {pid: state.get('revocation_generation', 0) for pid,state in cfg['projects'].items()}
        home = str(training.HOME)
        existing = _existing_sources()
        existing_stamp = _records_stamp()
    imported = []
    def authorized(pid):
        if str(training.HOME) != home:
            raise HistoryError('history_storage_changed')
        observed = training._revocation_generation(pid)
        if observed[0] != global_generation:
            raise HistoryError('history_batch_revoked')
        if observed[1] != generations.get(pid, 0):
            raise HistoryError('history_project_revoked')
    stopped = False
    for candidate in plan['candidates']:
        entry = {'source_id': candidate['source_id'], 'agent': candidate['agent'], 'status': 'failed'}
        try:
            source = candidate['path']
            binding = training._bind_project(candidate['cwd'])
            project, pid, identity = binding
            offline = identity is None
            try: _no_links(Path(candidate['cwd']))
            except HistoryError: offline = True
            if offline:
                project = os.path.abspath(candidate['cwd'])
                pid = training._project_id_for_path(project)
            authorized(pid)
            if not offline: training._check_project(binding)
            if _stat(source) != candidate['stat']:
                raise HistoryError('history_source_changed')
            from nous_training_history_read import read_history_bytes
            read = read_history_bytes(candidate)
            snapshot, raw_sha = read['snapshot'], read['source_sha256']
            snapshot_sha = read['snapshot_sha256']
            if _stat(source) != candidate['stat']:
                raise HistoryError('history_source_changed')
            source_key = training._hash(['transcript', source])[:24]
            with training._SessionLock(pid, source, scope='source'):
                prior = existing.get(source_key, [])
                if prior:
                    if any(agent != candidate['agent'] for agent,_,_ in prior):
                        raise HistoryError('history_source_agent_conflict')
                    entry.update(status='skipped', code='already_imported' if any(sha == raw_sha for _,_,sha in prior)
                                 else 'source_changed_existing' if any(sha is not None for _,_,sha in prior) else 'existing_source_unverified')
                else:
                    context = validate_snapshot(snapshot, candidate)
                    if offline:
                        from nous_training_history_privacy import build_unavailable_record
                        record = build_unavailable_record(snapshot, candidate, context)
                    else:
                        record = training.build_record(source, project, agent=candidate['agent'],
                            session='history:' + candidate['agent'] + ':' + candidate['source_id'],
                            _expected_session=candidate['session_id'], _project_binding=binding, _snapshot=snapshot)
                    if record['schema'] not in ('nous.trajectory/1', 'nous.trajectory/2'):
                        raise HistoryError('history_invalid_candidate')
                    record['id'] = 't_' + training._hash(['history', candidate['agent'], candidate['source_id'], raw_sha])[:32]
                    if context['issues'] or context['missing']:
                        record['quality']['complete'] = False
                        for key in ('issues', 'missing'):
                            record['quality'][key] = sorted(set(record['quality'][key]) | set(context[key]))
                    record['provenance'].update(source='historical_transcript', authorization='one_shot_import',
                        history_source_id=candidate['source_id'], source_file_sha256=raw_sha,source_snapshot_sha256=snapshot_sha,
                        history_compression=candidate.get('compression','none'),historical_workspace_available=not offline,
                        observation_scope='retained_transcript_only', history_source_stat=candidate['stat'],
                        history_source_role=candidate.get('source_role', 'unknown'),
                        history_branch_role=candidate.get('branch_role', 'unknown'))
                    if context.get('session_group_id'):
                        record['provenance']['session_group_id'] = context['session_group_id']
                    omitted_count = record.get('privacy',{}).get('historical_tool_payloads_omitted',0)
                    record['provenance']['historical_tool_payloads_omitted'] = omitted_count
                    record['evidence'] = []
                    # No re-signing or external path/body metadata after this point.
                    record = training.scrub_record(record, '' if offline else project)
                    if len(training._json(record).encode()) > training.MAX_RECORD_BYTES:
                        raise training.TrainingError('record_too_large')
                    with training._Lock():
                        authorized(pid)
                        if not offline: training._check_project(binding)
                        if _stat(source) != candidate['stat']:
                            raise HistoryError('history_source_changed')
                        # Another import/recovery could have arrived while parsing.
                        if _records_stamp() != existing_stamp:
                            existing = _existing_sources()
                            existing_stamp = _records_stamp()
                        if source_key in existing:
                            entry.update(status='skipped', code='existing_source_concurrent')
                        else:
                            path = training._root() / 'records' / (record['id'] + '.json')
                            if path.exists() or path.is_symlink():
                                raise HistoryError('history_existing_record_invalid')
                            cfg = training._config()
                            state = cfg['projects'].setdefault(pid, {'capture': False, 'export': False})
                            state['imports'] = sorted(set(state.get('imports', [])) | {record['id']})
                            state.update(purpose='local_training', version=1)
                            training._atomic(training._root() / 'config.json', cfg)
                            training._atomic(path, record)
                            existing_stamp = _records_stamp()
                            existing.setdefault(source_key, []).append((candidate['agent'],record['id'],raw_sha))
                            imported.append((pid,record['id'],record['schema']))
                            entry.update(status='imported', id=record['id'], project_id=pid, schema=record['schema'],
                                         source_snapshot_sha256=snapshot_sha, source_file_sha256=raw_sha, workspace_available=not offline, messages=len(record['messages']),
                                         quality_complete=record['quality']['complete'],tool_payloads_omitted=omitted_count,
                                         quality_issue_count=len(record['quality']['issues']), missing_count=len(record['quality']['missing']))
                del snapshot
        except Exception as error:
            code = _failure(error)
            entry.update(status='skipped' if code in {'history_project_unavailable','history_project_revoked','history_source_changed'} else 'failed', code=code)
            if code in {'history_batch_revoked','history_storage_changed'}:
                stopped = True
        report['entries'].append(entry)
        counts[entry['status']] += 1
        report['counts'] = dict(counts)
        training._atomic(out / 'report.json', report)
        if progress is not None:
            progress({'processed':len(report['entries']),'total':len(plan['candidates']),'counts':dict(counts)})
        if stopped: break
    # Export only this invocation's newly imported and still-authorized records.
    # Holding the same revocation lock prevents revoke racing publication.
    if str(training.HOME) != home or training._config().get('revocation_generation',0) != global_generation:
        stopped = True
        report['stop_code'] = 'history_batch_revoked'
    if not stopped:
        from nous_training_export import export_dataset
        from nous_training_validate import validate_dataset
        with training._Lock():
            if str(training.HOME) != home or training._config().get('revocation_generation',0) != global_generation:
                stopped = True
                report['stop_code'] = 'history_batch_revoked'
            excluded = set()
            for pid,_,_ in imported:
                try: authorized(pid)
                except HistoryError: excluded.add(pid)
            report['publication_revoked_projects'] = len(excluded)
            selected = [(pid,rid,schema) for pid,rid,schema in imported if pid not in excluded] if not stopped else []
            def records_for(profile):
                for pid,rid,schema in selected:
                    if (schema == 'nous.trajectory/1') != (profile == 'trl'): continue
                    path = training._root() / 'records' / (rid + '.json')
                    if not path.is_file() or path.is_symlink(): continue
                    record = json.loads(path.read_bytes())
                    if not training._allowed_record(record): continue
                    yield training.scrub_record(record)
            for profile in ('trl','codemode-events-v1'):
                if not any((schema == 'nous.trajectory/1') == (profile == 'trl') for _,_,schema in selected): continue
                folder = out / profile
                export_dataset(records_for(profile), folder, profile=profile)
                result = validate_dataset(folder)
                report['datasets'][profile] = {'valid':result['valid'],'counts':result['counters']}
    report.update(processing_complete=not stopped, finished_at=int(time.time()),
                  all_sources_imported=bool(plan['scan_complete'] and not plan['scan_skipped'] and not plan['scan_errors']
                    and not report.get('publication_revoked_projects',0) and all(e['status']=='imported' or e.get('code')=='already_imported' for e in report['entries']) and not stopped))
    report['workspace_unavailable_records'] = sum(e.get('workspace_available') is False for e in report['entries'])
    report['tool_payloads_omitted'] = sum(e.get('tool_payloads_omitted',0) for e in report['entries'])
    report['reason_counts'] = dict(collections.Counter(e['code'] for e in report['entries'] if 'code' in e))
    training._atomic(out / 'report.json', report)
    return report
