"""Recover visible history when its original workspace cannot be verified.

Drop every tool payload before the common scrubber sees it. Never access old
file operands, run Git, or reconstruct missing native protocol / workspace.
"""
import copy
import os

import nous_training as training
from nous_transcript import parse_transcript_bytes

MARKER = '[historical tool payload omitted: workspace unavailable]'


def build_unavailable_record(snapshot, candidate, context):
    parsed = parse_transcript_bytes(snapshot, agent=candidate['agent'])
    messages = copy.deepcopy(parsed['messages'])
    if not messages:
        raise training.TrainingError('no_supported_messages')
    omitted = {'tool_inputs':0, 'tool_results':0, 'role_tool_messages':0}
    for message in messages:
        if message.get('role') == 'tool':
            message['content'] = [{'type':'text', 'text':MARKER}]
            omitted['role_tool_messages'] += 1
            continue
        for block in message.get('content', []):
            if block.get('type') == 'tool_use':
                # Field whitelist keeps only parser-established linkage, never
                # custom code / opaque arguments hidden in a second field.
                safe = {k:v for k,v in block.items() if k in {'type','id','name','is_error','format'}}
                safe['input'] = {'omitted':'historical_workspace_unavailable'}
                block.clear(); block.update(safe)
                omitted['tool_inputs'] += 1
            elif block.get('type') == 'tool_result':
                safe = {k:v for k,v in block.items() if k in {'type','tool_use_id','is_error'}}
                safe['content'] = MARKER
                block.clear(); block.update(safe)
                omitted['tool_results'] += 1
    quality = copy.deepcopy(parsed['quality'])
    quality['complete'] = False
    quality['missing'] = sorted(set(quality['missing']) | {'historical_workspace','request_context'} | set(context['missing']))
    issues = {'historical_workspace_unavailable'} | set(context['issues'])
    if sum(omitted.values()): issues.add('historical_tool_payloads_omitted')
    if candidate['agent'] == 'codex': issues.add('historical_native_protocol_unavailable')
    quality['issues'] = sorted(set(quality['issues']) | issues)
    source, project = candidate['path'], os.path.abspath(candidate['cwd'])
    record = {'schema':'nous.trajectory/1', 'id':'t_'+training._hash(['history',candidate['source_id']])[:32],
              'agent':{'id':parsed['agent'],'version':parsed.get('version'),'model':parsed.get('model')},
              'messages':messages,'quality':quality,'evidence':[],
              'provenance':{'source':'historical_transcript','observation_scope':'retained_transcript_only',
                'project_id':training._project_id_for_path(project),
                'session_id':training._hash(['history',candidate['agent'],candidate['source_id']])[:24],
                'session_group_id':context['session_group_id'],
                'source_transcript_id':training._hash(['transcript',source])[:24],
                'line_offset':parsed.get('offset')}}
    # Empty project avoids operand probing; all tool bodies are already gone.
    record = training.scrub_record(record, project='')
    record['privacy']['historical_omitted_counts'] = omitted
    record['privacy']['historical_tool_payloads_omitted'] = sum(omitted.values())
    record['provenance']['source_sha256'] = training._hash(record['messages'])
    return record
