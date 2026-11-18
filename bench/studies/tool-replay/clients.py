"""Python native/Ante/MCP controls; original runtime study retained unchanged."""
import json,os,sys,time,subprocess,secrets
from pathlib import Path
from fixture import user,payload,calls,unwrap,filename
ROOT=Path(__file__).resolve().parent;cfg=json.loads(Path(sys.argv[1]).read_text())
def mark(kind,**kw):print(json.dumps({'mark':kind,'t':time.perf_counter(),**kw}),flush=True)
def done_turn(i):
 assert Path(filename(i)).read_text()==payload(i)
 mark('turn',index=i,text='TRACE_COMPLETE')
 if i==0:sys.stdin.readline()
 if i==cfg['turns']-1:sys.stdin.readline()
def ante():
 family=cfg['family'];home=Path(os.environ['ANTE_HOME']);home.mkdir(exist_ok=True)
 schemas=cfg['schemas'];names=['mcp__'+family+'__'+t['name'] for t in schemas]
 bridge=([os.environ['HERMES_BENCH_PYTHON'],str(ROOT/'hermes_bridge.py')] if family=='hermes' else ['/opt/swarm/node/bin/node',str(ROOT/'pi_bridge.mjs')])
 (home/'catalog.json').write_text(json.dumps({'providers':{'exe':{'base_url':cfg['url'],'wire_style':'OpenAiResponse','auth':None,'preferred_models':[{'id':cfg['model'],'max_tokens':16384,'context_limit':128000}]}}}))
 (home/'settings.json').write_text(json.dumps({'has_completed_onboarding':True,'permission_mode':'yolo','auto_memory':False,'skills':False,'session_save':False,'tools':names,'mcp_servers':{family:{'command':bridge[0],'args':bridge[1:],'env':{k:os.environ[k] for k in ('BRIDGE_CGROUP','HOME','HERMES_HOME','ANTE_PI_AUDIT','ANTE_HERMES_AUDIT','HERMES_BENCH_PYTHON','PYTHONPATH')}}}}))
 p=subprocess.Popen(['/opt/ante-bench/ante','serve','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
 def send(op):
  n=(int(time.time()*1000)<<80)|secrets.randbits(80);s='';abc='0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  for _ in range(26):s=abc[n&31]+s;n>>=5
  p.stdin.write(json.dumps({'id':'op_'+s,'op':op})+'\n');p.stdin.flush()
 send({'StartSession':{'model':cfg['model'],'provider':'exe','effort':'medium','cwd':os.getcwd(),'permission_mode':'yolo','tools':names,'no_skills':True,'enable_auto_memory':False,'save_session':False,'system_prompt':cfg['system']}})
 i=0;text='';sent=False;session=False
 try:
  for line in p.stdout:
   try:e=json.loads(line).get('event')
   except ValueError:continue
   if not isinstance(e,dict):continue
   if 'SessionStart' in e:session=True
   if session and 'ExtensionRefreshed' in e and not sent and any(x.get('tools') for x in e['ExtensionRefreshed'].get('mcp_servers',[])):
    mark('ready');send({'UserInput':user(0)});sent=True
   if 'AgentMessage' in e:text+=e['AgentMessage'].get('text',e['AgentMessage'].get('content','')) if isinstance(e['AgentMessage'],dict) else str(e['AgentMessage'])
   if 'Error' in e:raise RuntimeError(e)
   if 'TurnEnd' in e:
    assert text=='TRACE_COMPLETE' and e['TurnEnd']['status']=='Completed',(text,e)
    done_turn(i);i+=1;text=''
    if i==cfg['turns']:break
    send({'UserInput':user(i)})
  assert i==cfg['turns'],('early_exit',i)
 finally:
  send('Shutdown');p.stdin.close()
  try:p.wait(timeout=.5)
  except subprocess.TimeoutExpired:
   mark('cleanup',action='sigterm_after_0.5s');p.terminate()
   try:p.wait(timeout=2)
   except subprocess.TimeoutExpired:p.kill();p.wait()
 mark('child_exit',returncode=p.returncode)
def hermes():
 sys.path.insert(0,'/opt/swarm/hermes')
 from toolsets import create_custom_toolset
 create_custom_toolset('bench-file','Matched write/read trace',['write_file','read_file'])
 from run_agent import AIAgent
 class FixedPromptAgent(AIAgent):
  def _build_system_prompt(self,system_message=None):return cfg['system']
 agent=FixedPromptAgent(base_url=cfg['url'],api_key='offline',provider='custom',api_mode='codex_responses',model=cfg['model'],max_iterations=4,enabled_toolsets=['bench-file'],save_trajectories=False,quiet_mode=True,verbose_logging=False,max_tokens=16384,reasoning_config={'effort':'medium'},skip_context_files=True,skip_memory=True,skip_background_review=True,load_soul_identity=False,checkpoints_enabled=False)
 mark('ready');history=[]
 try:
  for i in range(cfg['turns']):
   r=agent.run_conversation(user(i),conversation_history=history)
   assert r['completed'] and r['final_response']=='TRACE_COMPLETE',r
   history=r['messages'];done_turn(i)
 finally:agent.close()
def control():
 if cfg['engine']=='hermes-direct':
  os.environ.pop('BRIDGE_CGROUP',None)
  from hermes_bridge import dispatch
  call=lambda msg:dispatch(msg)
  p=None
 else:
  p=subprocess.Popen([os.environ['HERMES_BENCH_PYTHON'],str(ROOT/'hermes_bridge.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
  serial=0
  def call(msg):
   nonlocal serial
   serial+=1;p.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,**msg})+'\n');p.stdin.flush();r=json.loads(p.stdout.readline());assert 'error' not in r,r;return r['result']
 try:
  call({'method':'initialize','params':{}});call({'method':'tools/list'});mark('ready')
  for i in range(cfg['turns']):
   for c in calls('hermes',i):
    r=call({'method':'tools/call','params':{'name':c['name'],'arguments':c['arguments']}});assert not r.get('isError'),r
    mark('tool_result',index=i,name=c['name'],result=unwrap(r))
   done_turn(i)
 finally:
  if p:
   p.stdin.close()
   try:p.wait(timeout=2)
   except subprocess.TimeoutExpired:p.terminate();p.wait(timeout=2)
if cfg['engine'].startswith('ante-'):ante()
elif cfg['engine']=='hermes':hermes()
else:control()
mark('done')
