"""Private observed lower-bound usage. Never infer subscription dollars from tokens."""
import datetime,json,re
from pathlib import Path

def json_rows(path):
 try:
  for line in path.read_text().splitlines():
   try:
    row=json.loads(line)
    if isinstance(row,dict):yield row
   except ValueError:continue  # Retain earlier durable rows after a torn final write.
 except OSError:return

def aggregate(root, *, receipt_overrides=None):
 root=Path(root);runs=[];unfinished=[];sources=set()
 counts={k:0 for k in ('recorded_requests','requests_with_usage','requests_with_cache_usage','input_tokens','output_tokens','cached_input_tokens','tasks_without_request_audit','attempted_tasks','accepted_requests','forwarding_attempts','requests_with_upstream_headers','completed_responses','requests_without_forwarding_evidence','rejected_before_forward','failed_http_requests','requests_without_upstream_status','requests_without_final_record')}
 for d in sorted((root/'runs').glob('r[0-9]*'),key=lambda x:x.name):
  if not re.fullmatch(r'r\d+',d.name) or int(d.name[1:])<42:continue
  try:receipt=json.loads((d/'receipts.json').read_text());final=receipt.get('state')!='running'
  except (OSError,ValueError):receipt={};final=False
  if d.name in (receipt_overrides or {}):receipt=receipt_overrides[d.name];final=receipt.get('state')!='running'
  tasks={t['task']:t for t in json_rows(d/'task-results.jsonl') if 'task' in t}
  tasks.update({t.get('task',f'legacy-index-{i}'):t for i,t in enumerate(receipt.get('results',[]))})
  # Durable v2 snapshots include requests from setup failures and interrupted runs.
  snapshots={}
  for audit in sorted(d.glob('audit/*/request-audit-v2.jsonl')):
   for row in json_rows(audit):
    if row.get('request_id'):snapshots[row['request_id']]=row
  audit_rows=[]
  for task in tasks.values():
   historical=task.get('request_audit') or []
   for row in historical:
    if row.get('request_id'):snapshots.setdefault(row['request_id'],row)
    else:audit_rows.append(row)
  audit_rows.extend(snapshots.values())
  if not final and not tasks and not audit_rows:continue
  runs.append(d.name);sources.add(receipt.get('provider_source','ChatGPT subscription via the managed model gateway'))
  if not final:unfinished.append(d.name)
  started={tid for tid,t in tasks.items() if t.get('execution_started',True)}
  observed={r['task'] for r in audit_rows if r.get('task')}
  counts['attempted_tasks']+=max(len(started|observed),receipt.get('attempted',0))
  for tid,t in tasks.items():
   if t.get('execution_started',True) and not t.get('request_audit') and not any(r.get('task')==tid for r in audit_rows):counts['tasks_without_request_audit']+=1
  for row in audit_rows:
   counts['recorded_requests']+=1
   for counter,field in (('accepted_requests','accepted'),('forwarding_attempts','forwarding_attempted'),('requests_with_upstream_headers','headers_received'),('completed_responses','stream_complete')):counts[counter]+=int(row.get(field) is True)
   counts['requests_without_forwarding_evidence']+=int('forwarding_attempted' not in row)
   counts['requests_without_final_record']+=int('request_id' in row and row.get('finished') is not True)
   counts['rejected_before_forward']+=int(row.get('accepted') is False)
   counts['failed_http_requests']+=int(type(row.get('status')) is int and row['status']>=400)
   counts['requests_without_upstream_status']+=int(row.get('headers_received') is not True if 'headers_received' in row else row.get('status') is None)
   u=row.get('usage') or {}
   if not isinstance(u,dict):continue
   valid=lambda v:type(v) is int and v>=0
   if valid(u.get('input_tokens')) and valid(u.get('output_tokens')):
    counts['requests_with_usage']+=1;counts['input_tokens']+=u['input_tokens'];counts['output_tokens']+=u['output_tokens']
    details=u.get('input_tokens_details') or {};cached=u.get('cached_tokens',details.get('cached_tokens') if isinstance(details,dict) else None)
    if 'cache_read_input_tokens' in u or 'cache_creation_input_tokens' in u:  # shared-v3 Anthropic Messages usage: input_tokens excludes cached tokens
     cached=u.get('cache_read_input_tokens',0);counts['input_tokens']+=cached+(u.get('cache_creation_input_tokens') or 0)
    if valid(cached):counts['cached_input_tokens']+=cached;counts['requests_with_cache_usage']+=1
 quota=None
 for snapshot in json_rows(root/'admin/quota-headers.jsonl'):
  try:
   headers={k.lower():v for k,v in snapshot['headers'].items()};windows=[]
   for name in ('primary','secondary'):
    prefix='x-codex-'+name+'-';minutes=int(headers.get(prefix+'window-minutes','0'))
    if minutes>0:
     used=float(headers[prefix+'used-percent']);windows.append({'window_minutes':minutes,'used_percent':used,'remaining_percent':max(0,100-used),'reset_at':headers.get(prefix+'reset-at')})
   quota={'observed_at':snapshot['observed_at'],'active_limit':headers.get('x-codex-active-limit'),'windows':windows,'scope':'Account-wide gateway-reported limit, not isolated benchmark consumption'}
  except (ValueError,KeyError,TypeError):continue
 return {'updated':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Bench campaign r42 onward (shared-v3: Anthropic usage normalised to total input); final receipts plus durable v2 request/task journals','provider_source':'; '.join(sorted(sources)) or 'No observed provider traffic','cost_usd':None,'subscription_quota_remaining':quota,'coverage':'Observed lower bound; missing usage, unjournaled requests and charges remain unknown; historical forwarding stages unobserved','run_ids':runs,'unfinished_run_ids':unfinished,**counts}
if __name__=='__main__':
 import sys
 print(json.dumps(aggregate(sys.argv[1]),indent=2))
