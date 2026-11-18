#!/usr/bin/env python3
"""MCP transport for pinned Hermes terminal/file tools; no model or account code."""
import json,os,sys,time
from pathlib import Path
if os.environ.get('BRIDGE_CGROUP'):(Path(os.environ['BRIDGE_CGROUP'])/'cgroup.procs').write_text(str(os.getpid()))
ROOT=Path(__file__).resolve().parent
SCHEMAS=json.loads((ROOT/'hermes-schemas-all.json').read_text())
SCHEMAS=[t for t in SCHEMAS if t['function']['name'] in ('read_file','write_file')]
ALLOWED={t['function']['name'] for t in SCHEMAS}
handler=None

def audit(event,**fields):
 path=os.environ.get('ANTE_HERMES_AUDIT')
 if path:
  with open(path,'a') as f:f.write(json.dumps({'event':event,'t':time.time(),**fields})+'\n')

def dispatch(msg):
 global handler
 method=msg.get('method');args=msg.get('params') or {}
 if method=='initialize':return {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'ante-hermes-tools','version':'0.1.0'}}
 if method=='ping':return {}
 if method=='tools/list':return {'tools':[{'name':t['function']['name'],'description':t['function'].get('description',''),'inputSchema':t['function']['parameters']} for t in SCHEMAS]}
 if method=='tools/call':
  name=args.get('name')
  if name not in ALLOWED:raise ValueError('Tool is outside terminal/file grant')
  if handler is None:
   audit('hermes_import_start');sys.path.insert(0,'/opt/swarm/hermes')
   # Libraries may print startup warnings. MCP stdout is exclusively JSON-RPC.
   from contextlib import redirect_stdout
   with redirect_stdout(sys.stderr):
    from model_tools import handle_function_call
   handler=handle_function_call;audit('hermes_import_ready')
  audit('hermes_tool_start',tool=name)
  from contextlib import redirect_stdout
  began=time.perf_counter_ns();cpu=time.process_time_ns()
  with redirect_stdout(sys.stderr):result=handler(name,args.get('arguments') or {},task_id='ante-hermes',enabled_tools=sorted(ALLOWED),enabled_toolsets=['terminal','file'])
  audit('hermes_tool_end',tool=name,wall_ns=time.perf_counter_ns()-began,cpu_ns=time.process_time_ns()-cpu,result=result)
  return {'content':[{'type':'text','text':result if isinstance(result,str) else json.dumps(result)}]}
 raise ValueError('Unsupported MCP method')

def main():
 for line in sys.stdin:
  try:
   msg=json.loads(line)
   if 'id' not in msg:continue
   try:response={'jsonrpc':'2.0','id':msg['id'],'result':dispatch(msg)}
   except Exception as error:response={'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32602,'message':str(error)}}
   print(json.dumps(response),flush=True)
  except ValueError:continue
if __name__=='__main__':main()
