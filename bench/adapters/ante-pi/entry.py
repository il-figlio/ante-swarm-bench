#!/usr/bin/env python3
"""One-shot Ante core session using actual pinned Pi tools over MCP; not the Pi CLI."""
import argparse,json,os,subprocess,sys,time,secrets
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PROMPT='You are a coding agent. Use the supplied native terminal and file tools to complete the user task. Make only requested edits and verify them.'
def op_id():
 n=(int(time.time()*1000)<<80)|secrets.randbits(80);out='';chars='0123456789ABCDEFGHJKMNPQRSTVWXYZ'
 for _ in range(26):out=chars[n&31]+out;n>>=5
 return 'op_'+out
def audit(event,**fields):
 if os.environ.get('ANTE_PI_AUDIT'):
  with open(os.environ['ANTE_PI_AUDIT'],'a') as f:f.write(json.dumps({'event':event,'t':time.time(),**fields})+'\n')
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--version',action='store_true');ap.add_argument('--model');ap.add_argument('--effort',choices=['low','medium','high'],required=not '--version' in sys.argv);ap.add_argument('--query');a=ap.parse_args()
 if a.version:print('Ante-Pi tools 0.3.0 / Ante0.2.11 / Pi0.85.1');return 0
 if not a.query or not a.model:ap.error('Fresh one-shot model and query required')
 names=['mcp__pi__'+t['name'] for t in json.loads((ROOT/'tool-schemas.json').read_text())]
 home=Path(os.environ['ANTE_HOME']);cfg=json.loads((home/'settings.json').read_text())
 cfg.update(tools=names,auto_memory=False,skills=False,session_save=False,mcp_servers={'pi':{'command':'/opt/swarm/node/bin/node','args':[str(ROOT/'bridge.mjs')],'env':{'ANTE_PI_AUDIT':os.environ.get('ANTE_PI_AUDIT',''),'HOME':os.environ['HOME'],'PI_OFFLINE':'1'}}});(home/'settings.json').write_text(json.dumps(cfg))
 p=subprocess.Popen([os.environ.get('ANTE_BENCH_BINARY','/opt/ante-bench/ante'),'serve','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=sys.stderr,text=True)
 def send(op):p.stdin.write(json.dumps({'op':op,'id':op_id()})+'\n');p.stdin.flush()
 sent=False;session=False;ended=False;failed=False;audit('ante_start')
 try:
  send({'StartSession':{'model':a.model,'provider':'exe','effort':a.effort,'cwd':os.getcwd(),'permission_mode':'yolo','tools':names,'no_skills':True,'enable_auto_memory':False,'save_session':False,'system_prompt':os.environ.get('ANTE_BENCH_SYSTEM_PROMPT',PROMPT)}})
  for line in p.stdout:
   try:event=json.loads(line).get('event')
   except ValueError:continue
   audit('ante_event',type=event if isinstance(event,str) else next(iter(event or {}),'unknown'))
   if not isinstance(event,dict):continue
   if 'SessionStart' in event:session=True
   if session and 'ExtensionRefreshed' in event and not sent:
    if any(x.get('tools') for x in event['ExtensionRefreshed'].get('mcp_servers',[])):send({'UserInput':a.query});sent=True;audit('task_input')
   if 'AgentMessage' in event:print(json.dumps({'message':event['AgentMessage']}),flush=True)
   if 'Error' in event:print(json.dumps({'error':event['Error']}),file=sys.stderr);failed=True;break
   if 'TurnEnd' in event:ended=True;failed=event['TurnEnd'].get('status')!='Completed';audit('turn_end',status=event['TurnEnd'].get('status'));break
 finally:
  forced=False
  if p.poll() is None:
   try:send('Shutdown');p.stdin.close();p.wait(timeout=10)
   except (OSError,subprocess.TimeoutExpired):
    forced=True;p.kill();p.wait()
  audit('shutdown',returncode=p.returncode,forced=forced)
 return 0 if sent and ended and not failed and not forced and p.returncode==0 else 1
if __name__=='__main__':sys.exit(main())
