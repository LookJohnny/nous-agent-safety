"""Bounded in-memory Claude 2.1.318/319 normalized request replay.

Verified against the locally installed writer's sorted-JSON hash, message-content
normalization, prefix/blob references and cache-control path recording. This is
NOT raw-wire recovery: the native writer omits billing system blocks and the
metadata/stream/thread/diagnostics/safeguards body keys. RequestRefs bind response
observations, not task success or complete native response bytes. No I/O or review
labels. Callers must scrub returned request bodies before storing or displaying.
"""
import copy
import hashlib
import json
from collections import Counter
from typing import NamedTuple

from nous_transcript import _check_bytes, _iter_lines, _loads, TranscriptReadError

VERSIONS = {'2.1.318', '2.1.319'}
MAX_REQUESTS = 10000
MAX_MESSAGES = 100000
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64
MAX_STORED_BYTES = 128 * 1024 * 1024
MAX_BLOBS = 10000
MAX_BODY_NODES = 500000
MAX_STORED_NODES = 2000000
MAX_STORED_REFS = 200000
MAX_STORED_ITEMS = 50000
MAX_KEY_CHARS = 65536
OMITTED_BODY_KEYS = {'metadata','stream','thread','diagnostics','safeguards'}
ERROR_CODES = frozenset('''replay_invalid_json replay_invalid_snapshot replay_invalid_identity replay_unsupported_version
replay_invalid_shape replay_invalid_blob replay_hash_mismatch replay_missing_shape replay_missing_message
replay_invalid_request replay_duplicate_request_id replay_missing_base replay_invalid_prefix replay_cut_or_withheld
replay_unsupported_context_edit replay_invalid_cache_path replay_duplicate_cache_path replay_unsupported_number
replay_unsupported_unicode replay_resource_limit replay_invalid_response_binding'''.split())


class ReplayError(ValueError):
    """Only static reason codes; never source content."""


def _fail(code):
    raise ReplayError(code)


def _string(value):
    if type(value) is not str or not value or len(value) > 1024:
        _fail('replay_invalid_identity')
    if any(0xd800<=ord(ch)<=0xdfff for ch in value):_fail('replay_unsupported_unicode')
    return value


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        _fail('replay_invalid_request')
    return value


class _Stats(NamedTuple):
    size: int
    nodes: int
    depth: int


def _canonical_walk(value, sink=None, *, omit_cache_control=False):
    """Measure/emit bounded chunks, never build an oversized rendered body.

    Native h() manually emits Object.keys(...).sort(); even integer-looking keys
    are therefore UTF-16 lexicographic, without JS object reinsertion ordering.
    """
    size=nodes=deepest=0
    def emit(chunk):
        nonlocal size
        size+=len(chunk)
        if size>MAX_BODY_BYTES:_fail('replay_resource_limit')
        if sink is not None:sink(chunk)
    def visit(v,depth):
        nonlocal nodes,deepest
        nodes+=1;deepest=max(deepest,depth)
        if depth>MAX_DEPTH or nodes>MAX_BODY_NODES:_fail('replay_resource_limit')
        if v is None:emit(b'null')
        elif type(v) is bool:emit(b'true' if v else b'false')
        elif type(v) is int:
            if abs(v)>2**53-1:_fail('replay_unsupported_number')
            emit(str(v).encode('ascii'))
        elif type(v) is float:_fail('replay_unsupported_number')
        elif type(v) is str:
            emit(b'"')
            for start in range(0,len(v),4096):
                part=v[start:start+4096]
                if any(0xd800<=ord(ch)<=0xdfff for ch in part):_fail('replay_unsupported_unicode')
                emit(json.dumps(part,ensure_ascii=False)[1:-1].encode('utf-8'))
            emit(b'"')
        elif type(v) is list:
            if len(v)>MAX_BODY_NODES-nodes:_fail('replay_resource_limit')
            emit(b'[')
            for index,item in enumerate(v):
                if index:emit(b',')
                visit(item,depth+1)
            emit(b']')
        elif type(v) is dict:
            if len(v)>MAX_BODY_NODES-nodes:_fail('replay_resource_limit')
            key_units=0
            for key in v:
                if type(key) is not str:_fail('replay_invalid_json')
                if len(key)>MAX_KEY_CHARS:_fail('replay_resource_limit')
                if any(0xd800<=ord(ch)<=0xdfff for ch in key):_fail('replay_unsupported_unicode')
                key_units+=len(key)
                if key_units>MAX_BODY_BYTES:_fail('replay_resource_limit')
            emit(b'{');first=True
            for key in sorted(v,key=lambda k:k.encode('utf-16-be')):
                if omit_cache_control and key=='cache_control':continue
                if not first:emit(b',')
                first=False;visit(key,depth+1);emit(b':');visit(v[key],depth+1)
            emit(b'}')
        else:_fail('replay_invalid_json')
    try:visit(value,0)
    except RecursionError:_fail('replay_resource_limit')
    return _Stats(size,nodes,deepest)


def canonical_bytes(value, *, omit_cache_control=False):
    """Bounded JS sorted-JSON subset; unsupported numbers/Unicode fail closed."""
    output=bytearray()
    _canonical_walk(value,output.extend,omit_cache_control=omit_cache_control)
    return bytes(output)


def native_hash(value, *, omit_cache_control=False):
    digest=hashlib.sha256()
    _canonical_walk(value,digest.update,omit_cache_control=omit_cache_control)
    return digest.hexdigest()


def normalize_message(message):
    if type(message) is not dict or message.get('role') not in ('user','assistant','system','developer') or type(message.get('content')) not in (str,list):
        _fail('replay_invalid_blob')
    # Inputs remain immutable. A shallow view avoids copying before body budgets
    # have been checked; request expansion later takes independent deep copies.
    result=dict(message)
    if isinstance(result['content'],str): result['content']=[{'type':'text','text':result['content']}]
    _canonical_walk(result)
    return result


class _Retention:
    """Charge all retained replay/index state, including poison and base refs.

    Canonical bytes bound payloads, nodes bound Python container overhead, and
    reference/item caps bound index/list growth. These are conservative accounting
    units, not a claim of exact Python heap measurement.
    """
    def __init__(self):self.costs={};self.size=self.nodes=self.refs=0
    def cost(self,slot,stats,refs=0):
        _,scope,ident=slot
        identity_bytes=sum(len(x.encode('utf-8')) for x in (*scope,ident) if x is not None)
        return (stats.size+identity_bytes+128,stats.nodes+8,refs)
    def check(self,slot,cost):
        old=self.costs.get(slot,(0,0,0))
        totals=tuple(a-b+c for a,b,c in zip((self.size,self.nodes,self.refs),old,cost))
        if (totals[0]>MAX_STORED_BYTES or totals[1]>MAX_STORED_NODES or totals[2]>MAX_STORED_REFS
            or (slot not in self.costs and len(self.costs)>=MAX_STORED_ITEMS)):_fail('replay_resource_limit')
    def put(self,slot,cost):
        self.check(slot,cost);old=self.costs.get(slot,(0,0,0))
        self.size,self.nodes,self.refs=tuple(a-b+c for a,b,c in zip((self.size,self.nodes,self.refs),old,cost))
        self.costs[slot]=cost
    def drop_kinds(self,kinds):
        for slot in list(self.costs):
            if slot[0] in kinds:
                a,b,c=self.costs.pop(slot);self.size-=a;self.nodes-=b;self.refs-=c


def _scope(row, session_id):
    if row.get('sessionId') != session_id: _fail('replay_invalid_identity')
    agent=row.get('agentId')
    if agent is not None: _string(agent)
    return (session_id,agent)


def _without_cache(v):
    if isinstance(v,list):return [_without_cache(x) for x in v]
    if isinstance(v,dict):return {k:_without_cache(x) for k,x in v.items() if k!='cache_control'}
    return v


def _has_cache(v):
    if type(v) is list:return any(_has_cache(x) for x in v)
    if type(v) is dict:return 'cache_control' in v or any(_has_cache(x) for x in v.values())
    return False


def _cache_preflight(body, entries):
    stats=_canonical_walk(body)
    size,nodes,depth=stats
    if type(entries) is not list: _fail('replay_invalid_cache_path')
    if len(entries)>MAX_MESSAGES:_fail('replay_resource_limit')
    seen=set()
    for entry in entries:
        if type(entry) is not dict or set(entry)!={'path','value'}: _fail('replay_invalid_cache_path')
        path=entry['path']
        if type(path) is not list or not path or len(path)>MAX_DEPTH or path[0] not in ('system','tools','messages'):
            _fail('replay_invalid_cache_path')
        if any(type(x) not in (str,int) for x in path): _fail('replay_invalid_cache_path')
        key=tuple(path)
        if key in seen: _fail('replay_duplicate_cache_path')
        seen.add(key);at=body
        for step in path:
            if type(at) is dict and type(step) is str and step in at: at=at[step]
            elif type(at) is list and type(step) is int and 0<=step<len(at):at=at[step]
            else:_fail('replay_invalid_cache_path')
        if type(at) is not dict or 'cache_control' in at: _fail('replay_invalid_cache_path')
        value_stats=_canonical_walk(entry['value'])
        size+=len(b'"cache_control":')+value_stats.size+bool(at)
        nodes+=1+value_stats.nodes
        depth=max(depth,len(path)+1+value_stats.depth)
        if size>MAX_BODY_BYTES or nodes>MAX_BODY_NODES or depth>MAX_DEPTH:_fail('replay_resource_limit')
    return _Stats(size,nodes,depth)


def _apply_cache(body,entries):
    for entry in entries:
        at=body
        for step in entry['path']:at=at[step]
        at['cache_control']=copy.deepcopy(entry['value'])


def _restore_cache(body, entries):
    _cache_preflight(body,entries)
    _apply_cache(body,entries)


def _rows(snapshot):
    for line_number,line in enumerate(_iter_lines(snapshot),1):
        if not line.strip():continue
        try:r=_loads(line.decode('utf-8'))
        except (ValueError,UnicodeError,RecursionError):_fail('replay_invalid_json')
        if type(r) is not dict:_fail('replay_invalid_json')
        yield line_number,r


def iter_replay(snapshot, *, session_id):
    """Yield request observations or static metadata errors, raw bodies in RAM only.

    Forward/missing references, unsupported values and cuts are never repaired by
    guessing. `normalized_request_verified` means the retained native replay hash
    matched; it does not mean original-wire or task-success verification.
    """
    _string(session_id)
    try:_check_bytes(snapshot)
    except TranscriptReadError:_fail('replay_invalid_snapshot')
    retained=_Retention()
    request_counts=Counter();responses={};response_stats={};request_total=0;response_total=0
    for number,row in _rows(snapshot):
        if row.get('type')=='api-request':
            key=(_scope(row,session_id),_string(row.get('id')))
            slot=('request_index',*key)
            retained.put(slot,retained.cost(slot,_Stats(16,1,0)))
            request_counts[key]+=1;request_total+=1
            if request_total>MAX_REQUESTS:_fail('replay_resource_limit')
        elif row.get('type')=='assistant' and 'requestRef' in row:
            response_total+=1
            if response_total>MAX_MESSAGES:_fail('replay_resource_limit')
            key=(_scope(row,session_id),_string(row['requestRef']))
            if len(responses)>=MAX_REQUESTS and key not in responses:_fail('replay_resource_limit')
            message=row.get('message');valid=type(message) is dict
            ident=message.get('id') if valid else None
            stop=message.get('stop_reason') if valid else None
            valid=valid and type(ident) is str and 0<len(ident)<=1024 and not any(0xd800<=ord(ch)<=0xdfff for ch in ident) and message.get('role')=='assistant'
            known_stops={None,'end_turn','tool_use','max_tokens','stop_sequence','pause_turn','refusal'}
            if type(stop) not in (str,type(None)) or stop not in known_stops:valid=False;stop='unknown'
            # Store only metadata; never response text or reasoning content.
            fragment={'line':number,'message_id':ident if valid else None,'stop_reason':stop,'valid':valid}
            before=response_stats.get(key,_Stats(2,1,0));addition=_canonical_walk(fragment)
            after=_Stats(before.size+addition.size+bool(key in responses),before.nodes+addition.nodes,max(before.depth,addition.depth+1))
            slot=('response_index',*key)
            retained.put(slot,retained.cost(slot,after,len(responses.get(key,()))+1))
            response_stats[key]=after;responses.setdefault(key,[]).append(fragment)
    shapes={};blobs={};bad_shapes=set();bad_blobs=set();bases={};run={};context_blocked=False
    def store_blob(scope,digest,value,*,strip=False):
        key=(scope,digest)
        if key in blobs:return
        stats=_canonical_walk(value,omit_cache_control=strip)
        if len(blobs)>=MAX_BLOBS:_fail('replay_resource_limit')
        slot=('blob',scope,digest);cost=retained.cost(slot,stats)
        retained.check(slot,cost)
        # Depth/node/byte and retained-state checks precede recursive copying.
        stored=_without_cache(value) if strip else value
        retained.put(slot,cost);blobs[key]=stored
    shape_allowed={'type','sessionId','agentId','shapeHash','timestamp','version','shape'}
    blob_allowed={'type','sessionId','agentId','hash','message'}
    request_allowed={'type','sessionId','agentId','id','timestamp','version','querySource','shapeHash','params','base','keep','tail','cacheControl','requestDigest','withheld','cut','cutRequestDigest'}
    for number,row in _rows(snapshot):
        kind=row.get('type')
        if kind in ('fork-context-ref','content-replacement'):
            context_blocked=True
        if kind=='system' and row.get('subtype')=='compact_boundary':
            shapes.clear();blobs.clear();bases.clear();run.clear()
            retained.drop_kinds({'shape','blob','base','run'})
        if kind in ('user','assistant'):
            scope=_scope(row,session_id)
            try:
                m=row.get('message')
                normalized=normalize_message(m);role=normalized['role'];ident=m.get('id')
                if ident is not None:_string(ident)
                previous=run.get(scope)
                if not previous or previous['role']!=role or (role!='user' and (not ident or ident!=previous['id'])):
                    prior=[]
                else:prior=blobs[previous['blob_key']]['content']
                if len(prior)+len(normalized['content'])>MAX_MESSAGES:_fail('replay_resource_limit')
                # Only bounded pointer lists are assembled before measuring. The
                # retained cumulative content is referenced via its immutable blob.
                value={'role':role,'content':prior+normalized['content']}
                digest=native_hash(value,omit_cache_control=True)
                run_value={'role':role,'id':ident,'blob_key':digest}
                slot=('run',scope,'latest');cost=retained.cost(slot,_canonical_walk(run_value),1)
                retained.check(slot,cost)
                store_blob(scope,digest,value,strip=True)
                retained.put(slot,cost);run[scope]={'role':role,'id':ident,'blob_key':(scope,digest)}
            except ReplayError as error:
                run.clear();retained.drop_kinds({'run'})
                if str(error)=='replay_resource_limit':
                    yield {'kind':'metadata','status':'blocked','line_number':number,'code':str(error)}
                    return
            continue
        if kind not in ('api-request-shape','api-request-blob','api-request'):continue
        scope=None;identity=None
        try:
            scope=_scope(row,session_id)
            if kind=='api-request-shape':
                identity=_sha(row.get('shapeHash'));value=row.get('shape')
                if set(row)-shape_allowed or type(row.get('version')) is not str or row.get('version') not in VERSIONS or type(value) is not dict or not value or set(value)-{'system','tools'}:
                    _fail('replay_invalid_shape')
                stats=_canonical_walk(value)
                if _has_cache(value):_fail('replay_invalid_shape')
                if native_hash(value)!=identity:_fail('replay_hash_mismatch')
                if (scope,identity) not in shapes and len(shapes)>=MAX_BLOBS:_fail('replay_resource_limit')
                slot=('shape',scope,identity);retained.put(slot,retained.cost(slot,stats))
                shapes[(scope,identity)]=value;continue
            if kind=='api-request-blob':
                identity=_sha(row.get('hash'));value=normalize_message(row.get('message'))
                if set(row)-blob_allowed or _has_cache(value):_fail('replay_invalid_blob')
                if native_hash(value)!=identity:_fail('replay_hash_mismatch')
                store_blob(scope,identity,value);continue
            identity=_string(row.get('id'));key=(scope,identity)
            if set(row)-request_allowed:_fail('replay_invalid_request')
            if request_counts[key]!=1:_fail('replay_duplicate_request_id')
            if type(row.get('version')) is not str or row.get('version') not in VERSIONS:_fail('replay_unsupported_version')
            if any(k in row for k in ('withheld','cut','cutRequestDigest')):_fail('replay_cut_or_withheld')
            if context_blocked:_fail('replay_unsupported_context_edit')
            shape_key=(scope,_sha(row.get('shapeHash')))
            if shape_key in bad_shapes or shape_key not in shapes:_fail('replay_missing_shape')
            keep=row.get('keep');tail=row.get('tail');params=row.get('params')
            if type(keep) is not int or keep<0 or type(tail) is not list or type(params) is not dict or set(params)&(OMITTED_BODY_KEYS|{'system','tools','messages'}):_fail('replay_invalid_request')
            if keep+len(tail)>MAX_MESSAGES:_fail('replay_resource_limit')
            base=row.get('base')
            if base is not None:_string(base)
            if keep:
                if (scope,base) not in bases:_fail('replay_missing_base')
                if keep>len(bases[(scope,base)]):_fail('replay_invalid_prefix')
                refs=bases[(scope,base)][:keep]
            else:
                if base is not None:_fail('replay_invalid_prefix')
                refs=[]
            refs=refs+[_sha(x) for x in tail]
            if len(refs)>MAX_MESSAGES:_fail('replay_resource_limit')
            if any((scope,h) in bad_blobs or (scope,h) not in blobs for h in refs):_fail('replay_missing_message')
            slot=('base',scope,identity)
            base_cost=retained.cost(slot,_Stats(2+67*len(refs)-bool(refs),1+len(refs),1),len(refs))
            retained.check(slot,base_cost)
            # Expand only references before preflight. Repeated large blobs are
            # counted on every occurrence without deepcopy or a rendered body.
            view={**params,**shapes[shape_key],'messages':[blobs[(scope,h)] for h in refs]}
            _cache_preflight(view,row.get('cacheControl'))
            body=copy.deepcopy(params);body.update(copy.deepcopy(shapes[shape_key]))
            body['messages']=[copy.deepcopy(blobs[(scope,h)]) for h in refs]
            _apply_cache(body,row.get('cacheControl'))
            digest=_sha(row.get('requestDigest'))
            if native_hash(body)!=digest:_fail('replay_hash_mismatch')
            retained.put(slot,base_cost);bases[key]=refs
            observed=responses.get(key,[])
            valid=bool(observed) and all(x['valid'] and x['line']>number for x in observed) and len({x['message_id'] for x in observed})==1
            binding_code=(None if valid else 'response_ref_missing' if not observed else 'response_metadata_invalid' if not all(x['valid'] for x in observed) else 'response_multiple_message_ids' if len({x['message_id'] for x in observed})!=1 else 'response_order_invalid')
            stops=[x['stop_reason'] for x in observed if x['stop_reason'] is not None]
            yield {'kind':'request','status':'recovered','line_number':number,'request_id':identity,
                   'normalized_request':body,'normalized_request_sha256':digest,'normalized_request_verified':True,
                   'original_wire_complete':False,'task_success_verified':False,
                   'normalization_limits':['native_omitted_body_metadata','billing_system_block_filtered','raw_wire_bytes_unavailable'],
                   'response_binding':{'request_ref_bound':valid,'code':binding_code,'fragment_count':len(observed),'source_lines':[x['line'] for x in observed],
                     'observed_stop_reasons':dict(Counter(stops)),'terminal_marker_observed':valid and bool(observed[-1]['stop_reason']),
                     'native_response_wire_complete':False}}
        except ReplayError as error:
            if str(error)=='replay_resource_limit':
                yield {'kind':'request' if kind=='api-request' else 'metadata','status':'blocked','line_number':number,'code':str(error)}
                return
            if scope and identity:
                if kind in ('api-request-shape','api-request-blob'):
                    slot=('bad_shape' if kind=='api-request-shape' else 'bad_blob',scope,identity)
                    retained.put(slot,retained.cost(slot,_Stats(4,1,0)))
                    (bad_shapes if kind=='api-request-shape' else bad_blobs).add((scope,identity))
            yield {'kind':'request' if kind=='api-request' else 'metadata','status':'blocked','line_number':number,'code':str(error)}
