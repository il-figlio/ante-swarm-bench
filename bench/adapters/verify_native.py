#!/usr/bin/env python3
"""Actual pinned native write/read and interrupted-stream gate. No live LLM.
Run as the unprivileged bench user on the bench VM; raw telemetry and wire captures stay in --output.
Needs the replay study code (--replay-code, e.g. ../studies/tool-replay) and a telemetry policy module (--policy-code).
"""
import argparse,hashlib,json,os,subprocess,sys,threading,time
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--replay-code',required=True);ap.add_argument('--policy-code',required=True);a=ap.parse_args()
sys.path.insert(0,a.replay_code);from replay import Server
from fixture import SYSTEM,USER,filename,payload
sys.path.insert(0,a.policy_code);from telemetry_policy import telemetry_environment
out=Path(a.output);out.mkdir(mode=0o700,parents=True,exist_ok=False);root=Path(__file__).resolve().parent
server=Server();threading.Thread(target=server.serve_forever,daemon=True).start();posts=[]
class Collector(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  b=self.rfile.read(int(self.headers.get('Content-Length',0)));p=out/('otel-%03d.bin'%len(posts));p.write_bytes(b);posts.append({'path':self.path,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()});self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
fault_requests=[]
class Broken(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length',0)));fault_requests.append({'at':time.monotonic()});b=b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_fault","status":"in_progress","output":[]}}\n\n';self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
collector=ThreadingHTTPServer(('127.0.0.1',0),Collector);broken=ThreadingHTTPServer(('127.0.0.1',0),Broken)
for s in (collector,broken):threading.Thread(target=s.serve_forever,daemon=True).start()
results=[]
try:
 for family in ('pi','hermes'):
  adapter='ante-'+family
  for mode in ('complete','interrupted'):
   d=out/(adapter+'-'+mode);d.mkdir(mode=0o700);home=d/'home';home.mkdir();w=d/'workspace';w.mkdir();(home/'.ante').mkdir();(home/'.hermes').mkdir()
   (home/'.ante/settings.json').write_text('{}');(home/'.hermes/config.yaml').write_text(json.dumps({'terminal':{'backend':'local'},'tools':{'tool_search':{'enabled':'off'}},'auxiliary':{'title_generation':{'enabled':False}}}))
   schemas=json.loads((root/adapter/'tool-schemas.json').read_text());schemas=[{'name':t['name'],'description':t['description'],'inputSchema':t['inputSchema']} for t in schemas] if family=='pi' else [{'name':t['function']['name'],'description':t['function']['description'],'inputSchema':t['function']['parameters']} for t in schemas];schemas.sort(key=lambda x:x['name'])
   cfg={'engine':adapter,'family':family,'model':'openai/gpt-5.6-terra','system':SYSTEM,'user':USER,'turns':1,'workspace':str(w),'schemas':schemas};key=adapter+'-'+mode;server.cases[key]={'cfg':cfg,'requests':[]}
   url=f'http://127.0.0.1:{server.server_port}/{key}/v1' if mode=='complete' else f'http://127.0.0.1:{broken.server_port}/v1'
   (home/'.ante/catalog.json').write_text(json.dumps({'providers':{'exe':{'base_url':url,'wire_style':'OpenAiResponse','auth':None,'preferred_models':[{'id':cfg['model'],'max_tokens':16384,'context_limit':128000}]}}}))
   env,policy=telemetry_environment({'PATH':'/opt/swarm/node/bin:/usr/bin:/bin','HOME':str(home),'ANTE_HOME':str(home/'.ante'),'HERMES_HOME':str(home/'.hermes'),'PYTHONPATH':'/opt/swarm/hermes','HERMES_BENCH_PYTHON':'/opt/swarm/hermes-prepared-venv/bin/python','ANTE_BENCH_SYSTEM_PROMPT':SYSTEM,'ANTE_PI_AUDIT':str(d/'adapter.jsonl'),'ANTE_HERMES_AUDIT':str(d/'adapter.jsonl'),'PYTHONDONTWRITEBYTECODE':'1'},endpoint=f'http://127.0.0.1:{collector.server_port}',environment='swarm-bench-adapter-gate')
   # Pi's v0.2 supervisor now honors the same explicit system-prompt override.
   start=len(posts);fault_start=len(fault_requests)
   with (d/'stdout.log').open('w') as output,(d/'stderr.log').open('w') as errors:
    p=subprocess.Popen(['/opt/swarm/hermes-prepared-venv/bin/python' if family=='hermes' else '/usr/bin/python3',str(root/adapter/'entry.py'),'--model',cfg['model'],'--effort','medium','--query',USER],cwd=w,env=env,stdout=output,stderr=errors,start_new_session=True)
    timed_out=False
    try:p.wait(timeout=90)
    except subprocess.TimeoutExpired:
     timed_out=True;os.killpg(p.pid,__import__('signal').SIGKILL);p.wait()
   requests=server.cases[key]['requests'];events=[json.loads(x) for x in (d/'adapter.jsonl').read_text().splitlines()];forced=any(e.get('event')=='shutdown' and e.get('forced') for e in events)
   valid=not timed_out and ((p.returncode==0 and len(requests)==3 and not any(r['errors'] for r in requests) and (w/filename(0)).read_text()==payload(0)) if mode=='complete' else p.returncode!=0)
   receipt={'adapter':adapter,'mode':mode,'valid':valid,'runtime_exit':p.returncode,'timed_out':timed_out,'forced_cleanup':forced,'native_write_read_verified':mode=='complete' and valid,'synthetic_requests':len(requests) if mode=='complete' else len(fault_requests)-fault_start,'live_requests':0,'telemetry':{**policy,'delivery_verified':len(posts)>start,'received_paths':[r['path'] for r in posts[start:]]},'tool_end_events':[e.get('tool') for e in events if e.get('event') in ('pi_tool_end','hermes_tool_end')]}
   results.append(receipt);(d/'wire-private.json').write_text(json.dumps(requests));(out/'receipt.json').write_text(json.dumps(results,indent=2));print(json.dumps(receipt),flush=True)
   if not valid or forced:raise RuntimeError('Adapter native/cleanup gate failed: '+adapter+' '+mode)
finally:
 for s in (server,collector,broken):s.shutdown();s.server_close()
