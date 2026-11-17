"""Recover one exact observed Write pair against three matching artifact anchors.

No data writes, source replay, executable transcript commands or task approval.
Caller binds source bytes/consent, supplies trusted anchor metadata, scrubs the
returned candidate (including corpus secret literals) and assigns new lineage.
"""
import copy,hashlib,json,os,re,stat,subprocess
from pathlib import Path
import nous_privacy
from nous_transcript import parse_transcript_bytes, _check_bytes, _iter_lines, _loads

MAX_BYTES=1_000_000
MARKER='[historical tool payload omitted: workspace unavailable]'
OMITTED={'omitted':'historical_workspace_unavailable'}
class RecoveryError(ValueError):pass

def _fail(code):raise RecoveryError(code)
def _sha(value):return hashlib.sha256(value).hexdigest()
def _identity(path):
 try:
  chain=[path,*path.parents];result=[]
  for p in chain:
   s=os.lstat(p)
   if stat.S_ISLNK(s.st_mode):_fail('anchor_symlink')
   result.append((str(p),s.st_dev,s.st_ino))
  return result
 except OSError:_fail('anchor_unavailable')
def _read(path):
 before=_identity(path)
 try:
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  try:
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_BYTES:_fail('anchor_file_limits')
   chunks=[];n=0
   while True:
    part=os.read(fd,min(65536,MAX_BYTES+1-n))
    if not part:break
    chunks.append(part);n+=len(part)
    if n>MAX_BYTES:_fail('anchor_file_limits')
   end=os.fstat(fd)
   if (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)!=(end.st_dev,end.st_ino,end.st_size,end.st_mtime_ns,end.st_ctime_ns):_fail('anchor_changed')
  finally:os.close(fd)
 except OSError:_fail('anchor_unavailable')
 if _identity(path)!=before:_fail('anchor_changed')
 return b''.join(chunks),before

def _git(root,*args):
 try:
  p=subprocess.run(['git','-c','core.fsmonitor=false','-C',str(root),*args],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=3)
 except (OSError,subprocess.TimeoutExpired):_fail('anchor_git_unavailable')
 if p.returncode:_fail('anchor_git_mismatch')
 return p.stdout

def _omitted(record):
 count=0
 for m in record.get('messages',[]):
  content=m.get('content',[])
  if m.get('role')=='tool' and content==[{'type':'text','text':MARKER}]:count+=1
  if not isinstance(content,list):continue
  for b in content:
   if isinstance(b,dict) and ((b.get('type')=='tool_use' and b.get('input')==OMITTED) or (b.get('type')=='tool_result' and b.get('content')==MARKER)):count+=1
 return count

def _blocks(messages,kind,ident):
 return [(i,j,b) for i,m in enumerate(messages) for j,b in enumerate(m.get('content',[])) if isinstance(b,dict) and b.get('type')==kind and b.get('id' if kind=='tool_use' else 'tool_use_id')==ident]

def _recover(record,snapshot,anchor,original_root,target_root,backup_root,backup_manifest):
 if record.get('schema')!='nous.trajectory/1' or record.get('agent',{}).get('id')!='claude' or record.get('quality',{}).get('complete') is not False:_fail('anchor_parent_scope')
 rel=anchor.get('relative_path');expected=anchor.get('content_sha256')
 if not isinstance(rel,str) or not rel or any(c in rel for c in ('\0','\n','\r',':')) or Path(rel).is_absolute() or '..' in Path(rel).parts or str(Path(rel))!=rel:_fail('anchor_invalid_path')
 if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):_fail('anchor_invalid_digest')
 roots=[Path(p) for p in (original_root,target_root,backup_root)]
 if any(not p.is_absolute() or '..' in p.parts for p in roots):_fail('anchor_invalid_root')
 old,root,backup=roots;original=old/rel;target=root/rel;saved=backup/rel
 for part in reversed([old,*old.parents]):
  try:st=os.lstat(part)
  except FileNotFoundError:break
  except OSError:_fail('anchor_unavailable')
  if stat.S_ISLNK(st.st_mode):_fail('anchor_symlink')
 if nous_privacy.excluded_name(str(original)) or nous_privacy.excluded_name(str(target)) or nous_privacy.excluded_name(str(saved)):_fail('anchor_static_exclusion')
 root_sig=_identity(root);backup_sig=_identity(backup)
 _identity(root/'.git')
 if Path(_git(root,'rev-parse','--show-toplevel').decode().strip())!=root:_fail('anchor_git_root_mismatch')
 entry=backup_manifest.get('entries',{}).get(rel)
 if not isinstance(entry,dict) or entry.get('type')!='file' or entry.get('sha256')!=expected:_fail('anchor_manifest_mismatch')
 if not isinstance(entry.get('size'),int) or not 0<entry['size']<=MAX_BYTES:_fail('anchor_manifest_limits')
 _identity(target);_identity(saved)
 if nous_privacy.excluded(str(target),str(root),strict=True):_fail('anchor_current_exclusion')
 current,target_sig=_read(target);saved_bytes,saved_sig=_read(saved)
 if _sha(current)!=expected or _sha(saved_bytes)!=expected or len(current)!=entry['size'] or len(saved_bytes)!=entry['size']:_fail('anchor_content_mismatch')
 head=_git(root,'rev-parse','HEAD').decode().strip()
 if not re.fullmatch('[0-9a-f]{40,64}',head):_fail('anchor_git_mismatch')
 spec=head+':'+rel
 size=_git(root,'cat-file','-s',spec)
 if not size.strip().isdigit() or int(size)!=entry['size']:_fail('anchor_git_mismatch')
 blob=_git(root,'cat-file','blob',spec)
 if _sha(blob)!=expected:_fail('anchor_git_mismatch')
 # Full source checks are still required at the caller. Strict JSON here avoids
 # ambiguous duplicate keys, and raw block counts reject deduplicated replays.
 _check_bytes(snapshot)
 raw_calls=[];raw_results=[]
 for line in _iter_lines(snapshot):
  row=_loads(line.decode('utf-8'));m=row.get('message',{})
  if not isinstance(m,dict) or not isinstance(m.get('content'),list):continue
  for b in m['content']:
   if not isinstance(b,dict):continue
   if b.get('type')=='tool_use':raw_calls.append(b)
   if b.get('type')=='tool_result':raw_results.append(b)
 matching=[b for b in raw_calls if b.get('name')=='Write' and isinstance(b.get('input'),dict) and b['input'].get('file_path')==str(original)]
 if len(matching)!=1:_fail('anchor_call_ambiguous')
 raw=matching[0];inp=raw['input'];ident=raw.get('id')
 if set(inp)!={'file_path','content'} or not isinstance(inp['content'],str):_fail('anchor_input_contract')
 if not isinstance(ident,str) or not ident or sum(b.get('id')==ident for b in raw_calls)!=1:_fail('anchor_call_ambiguous')
 if _sha(inp['content'].encode())!=expected:_fail('anchor_observed_content_mismatch')
 if sum(b.get('tool_use_id')==ident for b in raw_results)!=1:_fail('anchor_result_ambiguous')
 parsed=parse_transcript_bytes(snapshot,agent='claude')
 calls=_blocks(parsed['messages'],'tool_use',ident);results=_blocks(parsed['messages'],'tool_result',ident)
 cached_calls=_blocks(record['messages'],'tool_use',ident);cached_results=_blocks(record['messages'],'tool_result',ident)
 if any(len(items)!=1 for items in (calls,results,cached_calls,cached_results)):_fail('anchor_linkage')
 ci,cj,call=calls[0];ri,rj,result=results[0];xi,xj,stored_call=cached_calls[0];yi,yj,stored_result=cached_results[0]
 if (ci,cj)!=(xi,xj) or (ri,rj)!=(yi,yj) or ri<=ci or parsed['messages'][ci]['role']!='assistant' or record['messages'][xi]['role']!='assistant':_fail('anchor_position_mismatch')
 if call.get('name')!='Write' or call.get('input')!=inp or stored_call.get('name')!='Write' or stored_call.get('input')!=OMITTED or stored_call.get('format')!=call.get('format'):_fail('anchor_cached_call_mismatch')
 if stored_result.get('content')!=MARKER or type(result.get('is_error')) is not bool or stored_result.get('is_error')!=result.get('is_error'):_fail('anchor_cached_result_mismatch')
 if not isinstance(result.get('content'),str) or not result['content'] or len(result['content'].encode())>8192:_fail('anchor_result_shape')
 # Recheck the exact files/roots/HEAD and exclusions after source analysis.
 if _identity(root)!=root_sig or _identity(backup)!=backup_sig or _identity(target)!=target_sig or _identity(saved)!=saved_sig:_fail('anchor_changed')
 if _git(root,'rev-parse','HEAD').decode().strip()!=head or _sha(_read(target)[0])!=expected or _sha(_read(saved)[0])!=expected:_fail('anchor_changed')
 if nous_privacy.excluded(str(target),str(root),strict=True):_fail('anchor_current_exclusion')
 result_record=copy.deepcopy(record);before=_omitted(record)
 result_record['messages'][xi]['content'][xj]['input']=copy.deepcopy(inp)
 result_record['messages'][yi]['content'][yj]['content']=result['content']
 after=_omitted(result_record)
 if before-after!=2:_fail('anchor_omission_count_mismatch')
 result_record.setdefault('provenance',{})['historical_tool_payloads_omitted']=after
 if 'historical_tool_payloads_omitted' in result_record.get('privacy',{}):result_record['privacy']['historical_tool_payloads_omitted']=after
 receipt={'status':'recovered_observed_pair','scope':'two_blocks_only_no_training_approval','relative_path_sha256':_sha(rel.encode()),'artifact_content_sha256':expected,'source_snapshot_sha256':_sha(snapshot),'call_id_sha256':_sha(ident.encode()),'call_location':[xi,xj],'result_location':[yi,yj],'git_head':head,'artifact_bytes':len(current),'input_body_restored':True,'result_body_restored':True,'result_is_error':result['is_error'],'omitted_payloads_before':before,'omitted_payloads_after':after,'parent_quality_unchanged':True,'task_evidence_unchanged':True,'requires_caller_rescrub_and_new_lineage':True}
 return result_record,receipt

def recover_anchored_write(record,snapshot,anchor,*,original_root,target_root,backup_root,backup_manifest):
 """Return (copy, safe receipt). Failure preserves the candidate exactly.

 Evidence must originate from the independently reviewed artifact match, not a
 tool name or model assertion. Success never clears the parent's quality gaps.
 """
 try:return _recover(record,snapshot,anchor,original_root,target_root,backup_root,backup_manifest)
 except RecoveryError as error:return copy.deepcopy(record),{'status':'not_recovered','code':str(error)}
 except Exception:return copy.deepcopy(record),{'status':'not_recovered','code':'anchor_recovery_failed'}
