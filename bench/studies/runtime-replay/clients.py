"""Direct API replay clients. Control/supervisor overhead stays inside measurement."""
import json,os,sys,time,subprocess,secrets
from pathlib import Path
cfg=json.loads(Path(sys.argv[1]).read_text()); start=time.perf_counter()
def mark(kind,**kw): print(json.dumps({'mark':kind,'t':time.perf_counter(),**kw}),flush=True)
def user(i):return cfg['user'] if i==0 else f'Continue fixed trace {i}.'
def ante():
 home=Path(os.environ['ANTE_HOME']);home.mkdir(exist_ok=True)
 (home/'catalog.json').write_text(json.dumps({'providers':{'exe':{'base_url':cfg['url'],'wire_style':'OpenAiResponse','auth':None,'preferred_models':[{'id':cfg['model'],'max_tokens':16384,'context_limit':128000}]}}}))
 (home/'settings.json').write_text(json.dumps({'has_completed_onboarding':True,'permission_mode':'yolo','auto_memory':False,'skills':False,'session_save':False}))
 p=subprocess.Popen(['/opt/ante-bench/ante','serve','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
 def send(op):
  n=(int(time.time()*1000)<<80)|secrets.randbits(80);s='';abc='0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  for _ in range(26):s=abc[n&31]+s;n>>=5
  p.stdin.write(json.dumps({'id':'op_'+s,'op':op})+'\n');p.stdin.flush()
 send({'StartSession':{'model':cfg['model'],'provider':'exe','effort':'medium','cwd':os.getcwd(),'permission_mode':'yolo','tools':[],'no_skills':True,'enable_auto_memory':False,'save_session':False,'system_prompt':cfg['system']}})
 i=0;text='';sent=False
 try:
  for line in p.stdout:
   try:e=json.loads(line).get('event')
   except ValueError:continue
   if not isinstance(e,dict):continue
   if 'SessionStart' in e and not sent:
    mark('ready');send({'UserInput':user(0)});sent=True
   if 'AgentMessage' in e:text+=e['AgentMessage'].get('text',e['AgentMessage'].get('content','')) if isinstance(e['AgentMessage'],dict) else str(e['AgentMessage'])
   if 'Error' in e:raise RuntimeError(e)
   if 'TurnEnd' in e:
    assert text=='TRACE_COMPLETE' and e['TurnEnd']['status']=='Completed',(text,e)
    mark('turn',index=i,text=text,event=e)
    if i==0:sys.stdin.readline()
    i+=1;text=''
    if i==cfg['turns']:
     sys.stdin.readline();break
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
 from run_agent import AIAgent
 class FixedPromptAgent(AIAgent):
  # ephemeral_system_prompt is additive; override only construction of the prompt.
  def _build_system_prompt(self,system_message=None):return cfg['system']
 agent=FixedPromptAgent(base_url=cfg['url'],api_key='offline',provider='custom',api_mode='codex_responses',model=cfg['model'],max_iterations=1,enabled_toolsets=[],disabled_toolsets=['all'],save_trajectories=False,quiet_mode=True,verbose_logging=False,max_tokens=16384,reasoning_config={'effort':'medium'},skip_context_files=True,skip_memory=True,skip_background_review=True,load_soul_identity=False,checkpoints_enabled=False)
 mark('ready');history=[]
 try:
  for i in range(cfg['turns']):
   r=agent.run_conversation(user(i),conversation_history=history)
   assert r['completed'] and r['final_response']=='TRACE_COMPLETE',r
   history=r['messages'];mark('turn',index=i,text=r['final_response'])
   if i==0:sys.stdin.readline()
 finally:
  sys.stdin.readline();agent.close()
if cfg['engine']=='ante':ante()
else:hermes()
mark('done')
