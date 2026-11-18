import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import runner
for j,e in enumerate(('pi-direct','pi-mcp','hermes-direct','hermes-mcp','pi','ante-pi','hermes','ante-hermes')):
 r=runner.cell(e,1,j,33,.1)
 assert r['valid'],r['id']
