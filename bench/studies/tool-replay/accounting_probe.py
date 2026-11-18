"""Validate that cgroup CPU includes an already exited busy child."""
import os,subprocess,json,time
from pathlib import Path
cg=Path('/sys/fs/cgroup/core-cpu-validation');cg.mkdir()
def setup():(cg/'cgroup.procs').write_text(str(os.getpid()))
code='''import subprocess,resource,json,time
subprocess.run(['python3','-c','import time; t=time.process_time(); exec("while time.process_time()-t < .5: pass")'],check=True)
r=resource.getrusage(resource.RUSAGE_CHILDREN)
print(json.dumps({'exited_child_cpu_s':r.ru_utime+r.ru_stime}))
'''
p=subprocess.run(['python3','-c',code],preexec_fn=setup,capture_output=True,text=True,check=True);r=json.loads(p.stdout);r['cgroup_cpu_s']=int(dict(l.split() for l in (cg/'cpu.stat').read_text().splitlines())['usage_usec'])/1e6;r['passed']=.48<r['exited_child_cpu_s']<.8 and r['cgroup_cpu_s']>=r['exited_child_cpu_s'] and r['cgroup_cpu_s']<r['exited_child_cpu_s']+.15
print(json.dumps(r));cg.rmdir();assert r['passed']
