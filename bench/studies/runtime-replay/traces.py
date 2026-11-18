"""Synthetic engine-independent fixtures. No provider, account or network access.

This validates a trace contract, not an engine. Engine shims remain unimplemented.
"""
import hashlib,json

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def trace(profile='sequential',steps=8):
 if profile not in ('text-only','sequential','parallel') or not 1<=steps<=64:raise ValueError('Invalid trace profile/length')
 tools=[] if profile=='text-only' else [{'name':'bench_echo','description':'Return the supplied synthetic payload unchanged.','parameters':{'type':'object','properties':{'step':{'type':'integer'},'payload':{'type':'string'}},'required':['step','payload'],'additionalProperties':False}}]
 calls=[{'id':f'call_{i:03d}','name':'bench_echo','arguments':{'step':i,'payload':'synthetic-'+str(i)}} for i in range(steps)] if tools else []
 batches=[calls] if profile=='parallel' else [[c] for c in calls]
 fixture={'revision':'core-trace-draft-v1','profile':profile,'system':'Execute only the fixed synthetic trace.','user':'Complete the benchmark trace.','tools':tools,'batches':batches,'final':'TRACE_COMPLETE'}
 fixture['case_sha256']=digest(fixture)
 return fixture

class TraceGate:
 """Reject skipped/changed tool work; require final completion after all batches."""
 def __init__(self,fixture):self.fixture=fixture;self.index=0;self.finished=False
 def expected(self):
  if self.finished:raise ValueError('Trace already completed')
  batches=self.fixture['batches']
  return {'tool_calls':batches[self.index]} if self.index<len(batches) else {'text':self.fixture['final']}
 def accept_results(self,results):
  expected=self.expected()
  if 'tool_calls' not in expected:raise ValueError('No tool batch pending')
  wanted={c['id']:c['arguments'] for c in expected['tool_calls']}
  # Order may differ for parallel execution, but duplicates/missing results cannot pass.
  actual={r['id']:r['result'] for r in results}
  if len(results)!=len(wanted) or actual!=wanted:raise ValueError('Missing, duplicate or changed tool result')
  self.index+=1
 def finish(self,text):
  if self.expected()!={'text':text}:raise ValueError('Incorrect or premature completion')
  self.finished=True
