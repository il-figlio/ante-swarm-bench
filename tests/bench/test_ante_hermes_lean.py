import importlib.util,io,json,os,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]/'bench/adapters/ante-hermes-lean'
class LeanAdapterTest(unittest.TestCase):
 def run_wire(self,effort,events):
  spec=importlib.util.spec_from_file_location('lean',ROOT/'entry.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as d:
   (Path(d)/'settings.json').write_text('{}');stream=io.StringIO();proc=types.SimpleNamespace(stdin=stream,stdout=[json.dumps({'event':e})+'\n' for e in events],poll=lambda:0)
   with patch.dict(os.environ,{'ANTE_HOME':d,'HOME':d,'HERMES_HOME':d}),patch.object(sys,'argv',['entry','--effort',effort,'--model','test','--query','task']),patch.object(m.subprocess,'Popen',return_value=proc) as launch:
    rc=m.main()
   return rc,[json.loads(x)['op'] for x in stream.getvalue().splitlines()],launch.call_args[0][0]
 def test_effort_wire_and_no_cli_launch(self):
  for effort in ('low','medium','high'):
   rc,ops,argv=self.run_wire(effort,[{'SessionStart':{}},{'ExtensionRefreshed':{'mcp_servers':[{'tools':['terminal']}] }},{'TurnEnd':{}}]);self.assertEqual(rc,0);self.assertEqual(ops[0]['StartSession']['effort'],effort);self.assertEqual(ops[1],{'UserInput':'task'});self.assertEqual(argv,['/opt/ante-bench/ante','serve','--stdio'])
 def test_early_eof_and_error_fail(self):
  for events in [[{'SessionStart':{}}],[{'Error':{'message':'failure'}}]]:self.assertEqual(self.run_wire('medium',events)[0],1)
 def test_bridge_and_schemas_identical_to_full(self):
  for name in ('bridge.py','tool-schemas.json'):self.assertEqual((ROOT/name).read_bytes(),(ROOT.parent/'ante-hermes'/name).read_bytes())
