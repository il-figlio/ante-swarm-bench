"""Regression for error-status TurnEnd incorrectly treated as success."""
import importlib.util,io,json,os,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]/'bench/adapters'
class AdapterCompletion(unittest.TestCase):
 def wire(self,adapter,status,effort='medium'):
  spec=importlib.util.spec_from_file_location('new_adapter',ROOT/adapter/'entry.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as d:
   Path(d,'settings.json').write_text('{}');stream=io.StringIO()
   events=[{'SessionStart':{}},{'ExtensionRefreshed':{'mcp_servers':[{'tools':['read']}]}},{'TurnEnd':{'status':status}}]
   proc=types.SimpleNamespace(stdin=stream,stdout=[json.dumps({'event':e}) for e in events],poll=lambda:0,returncode=0)
   with patch.dict(os.environ,{'HOME':d,'ANTE_HOME':d,'HERMES_HOME':d}),patch.object(sys,'argv',['entry','--model','synthetic','--query','fixture','--effort',effort]),patch.object(m.subprocess,'Popen',return_value=proc):rc=m.main()
   return rc,[json.loads(x)['op'] for x in stream.getvalue().splitlines()]
 def test_terminal_status_controls_result(self):
  for a in ('ante-pi','ante-hermes'):
   for status in ('Completed',{'Error':{'kind':'unexpected_eof'}},'Cancelled',None,{}):
    with self.subTest(adapter=a,status=status):self.assertEqual(self.wire(a,status)[0],0 if status=='Completed' else 1)
 def test_requested_effort_reaches_engine(self):
  for a in ('ante-pi','ante-hermes'):
   for e in ('low','medium','high'):self.assertEqual(self.wire(a,'Completed',e)[1][0]['StartSession']['effort'],e)

class ShutdownFailure(unittest.TestCase):
 def outcome(self,family,mode,status='Completed'):
  spec=importlib.util.spec_from_file_location('shutdown_adapter',ROOT/family/'entry.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  class Input(io.StringIO):
   def close(self):self.flushed=self.getvalue();super().close()
  class Process:
   def __init__(self):
    self.stdin=Input();self.returncode=None;self.killed=False
    self.stdout=[json.dumps({'event':e}) for e in [{'SessionStart':{}},{'ExtensionRefreshed':{'mcp_servers':[{'tools':['read']}]}},{'TurnEnd':{'status':status}}]]
   def poll(self):return self.returncode
   def wait(self,timeout=None):
    if mode=='hang' and not self.killed:raise m.subprocess.TimeoutExpired('fake',timeout)
    self.returncode=-9 if self.killed else 7 if mode=='nonzero' else 0;return self.returncode
   def kill(self):self.killed=True
  proc=Process()
  with tempfile.TemporaryDirectory() as d:
   Path(d,'settings.json').write_text('{}');audit_path=Path(d,'audit.jsonl')
   with patch.dict(os.environ,{'HOME':d,'ANTE_HOME':d,'HERMES_HOME':d,'ANTE_PI_AUDIT':str(audit_path),'ANTE_HERMES_AUDIT':str(audit_path)}),patch.object(sys,'argv',['entry','--model','synthetic','--query','fixture','--effort','medium']),patch.object(m.subprocess,'Popen',return_value=proc):rc=m.main()
   events=[json.loads(x) for x in audit_path.read_text().splitlines()]
   return rc,events,proc
 def test_completed_turn_requires_clean_zero_shutdown(self):
  for family in ('ante-pi','ante-hermes'):
   for mode in ('clean','nonzero','hang'):
    with self.subTest(family=family,mode=mode):
     rc,events,proc=self.outcome(family,mode);self.assertEqual(rc,0 if mode=='clean' else 1);self.assertEqual(events[-1]['event'],'shutdown');self.assertEqual(events[-1]['forced'],mode=='hang');self.assertEqual(proc.killed,mode=='hang')
 def test_cancelled_turn_is_failure_even_after_clean_shutdown(self):
  for family in ('ante-pi','ante-hermes'):
   rc,events,_=self.outcome(family,'clean','Cancelled');self.assertEqual(rc,1);self.assertEqual(events[-1]['returncode'],0)
