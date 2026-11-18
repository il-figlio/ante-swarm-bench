"""Root driver in private netns. Runtime uid/cgroups isolated; server outside groups."""
import os,sys,json,time,threading,subprocess,selectors,hashlib,itertools,statistics,shutil
from pathlib import Path
from replay import Server,SYSTEM
ROOT=Path(__file__).resolve().parent;OUT=Path(sys.argv[1]);OUT.mkdir(exist_ok=True,parents=True);os.chmod(OUT,0o755)
uid=__import__('pwd').getpwnam('ante-bench').pw_uid;gid=__import__('pwd').getpwnam('ante-bench').pw_gid
server=Server();threading.Thread(target=server.serve_forever,daemon=True).start()
def cpu(cg):return int(dict(l.split() for l in (cg/'cpu.stat').read_text().splitlines())['usage_usec'])
def sample(cg):
 pids=(cg/'cgroup.procs').read_text().split();pss=rss=0
 for pid in pids:
  try:
   for l in Path('/proc',pid,'smaps_rollup').read_text().splitlines():
    if l.startswith('Pss:'):pss+=int(l.split()[1])
    if l.startswith('Rss:'):rss+=int(l.split()[1])
  except (FileNotFoundError,ProcessLookupError):pass
 return {'pss_kib':pss,'rss_kib':rss,'pids':len(pids),'cpu_usec':cpu(cg),'t':time.monotonic()}
def cell(engine,n,index,turns=4,sampling=.1):
 cid=f'core{index:03d}-{engine}-n{n}';folder=OUT/cid;folder.mkdir();os.chmod(folder,0o755)
 cg=Path('/sys/fs/cgroup')/cid;cg.mkdir();procs=[];sel=selectors.DefaultSelector();requests=[];marks=[[] for _ in range(n)];logs=[b'' for _ in range(n)];peaks=[]
 start=time.monotonic();deadline=start+180;first=set();last=set();warm=None;warmcpu=None;failure=None;end=None;cpuend=None;total_start_cpu=time.process_time()
 try:
  for i in range(n):
   home=folder/f'i{i}';home.mkdir();os.chown(home,uid,gid);(home/'.hermes').mkdir();os.chown(home/'.hermes',uid,gid)
   (home/'.hermes/config.yaml').write_text(json.dumps({'tools':{'tool_search':{'enabled':'off'}},'auxiliary':{'title_generation':{'enabled':False}}}))
   key=cid+f'-i{i}';cfg={'engine':engine,'url':f'http://127.0.0.1:{server.server_port}/{key}/v1','model':'openai/gpt-5.6-terra','system':SYSTEM,'user':'Complete the benchmark trace.\n'+'x'*(4096-len('Complete the benchmark trace.\n')),'turns':turns};cfgpath=home/'case.json';cfgpath.write_text(json.dumps(cfg));server.cases[key]={'cfg':cfg,'requests':[]};requests.append(server.cases[key])
   env={'PATH':'/opt/swarm/node/bin:/usr/bin:/bin','HOME':str(home),'ANTE_HOME':str(home/'.ante'),'HERMES_HOME':str(home/'.hermes'),'PYTHONPATH':'/opt/swarm/hermes','PYTHONUNBUFFERED':'1','TERM':'dumb','NO_COLOR':'1','OPENAI_API_KEY':'offline','HERMES_DISABLE_UPDATE_CHECK':'1'}
   cmd=['/opt/swarm/node/bin/node',str(ROOT/'pi.mjs'),str(cfgpath)] if engine=='pi' else ['/opt/swarm/hermes-prepared-venv/bin/python' if engine=='hermes' else '/usr/bin/python3',str(ROOT/'clients.py'),str(cfgpath)]
   def setup():
    (cg/'cgroup.procs').write_text(str(os.getpid()));os.setgroups([]);os.setgid(gid);os.setuid(uid)
   err=open(home/'stderr.log','wb');p=subprocess.Popen(cmd,cwd=home,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,preexec_fn=setup);procs.append(p);err.close();sel.register(p.stdout,selectors.EVENT_READ,i)
  lastsample=0
  while sel.get_map():
   now=time.monotonic()
   if now>deadline:failure='deadline';break
   if sampling and now-lastsample>=sampling:peaks.append(sample(cg));lastsample=now
   for key,_ in sel.select(.01):
    i=key.data;chunk=os.read(key.fileobj.fileno(),65536)
    if not chunk:sel.unregister(key.fileobj);continue
    logs[i]+=chunk
    while b'\n' in logs[i]:
     line,logs[i]=logs[i].split(b'\n',1)
     with open(folder/f'i{i}/stdout.log','ab') as f:f.write(line+b'\n')
     try:m=json.loads(line)
     except ValueError:continue
     if 'mark' not in m:continue
     m['driver_t']=time.monotonic()-start;marks[i].append(m)
     if m['mark']=='turn' and m['index']==0:first.add(i)
     if m['mark']=='turn' and m['index']==turns-1:last.add(i)
   if len(first)==n and warm is None:
    warm=time.monotonic();warmcpu=cpu(cg)
    for p in procs:p.stdin.write(b'GO\n');p.stdin.flush()
   if len(last)==n and end is None:
    end=time.monotonic();cpuend=cpu(cg);peaks.append(sample(cg))
    for p in procs:p.stdin.write(b'QUIT\n');p.stdin.flush()
  rc=[]
  for p in procs:
   try:rc.append(p.wait(timeout=2))
   except subprocess.TimeoutExpired:failure='exit_timeout';rc.append(None)
  exit_t=time.monotonic();cpu_exit=cpu(cg)
  valid=not failure and not (cg/'cgroup.procs').read_text().strip() and all(x==0 for x in rc) and all(len(c['requests'])==turns and not any(r['errors'] for r in c['requests']) for c in requests) and all(len([m for m in mm if m['mark']=='turn'])==turns for mm in marks)
  result={'id':cid,'engine':engine,'instances':n,'turns_per_instance':turns,'valid':valid,'failure':failure,'returncodes':rc,'elapsed_s':end-start if end else None,'teardown_s':exit_t-end if end else None,'cpu_including_teardown_usec':cpu_exit,'driver_cpu_s':time.process_time()-total_start_cpu,'exit_group_empty':not (cg/'cgroup.procs').read_text().strip(),'first_success_s':max((m['driver_t'] for mm in marks for m in mm if m['mark']=='turn' and m['index']==0),default=None),'cpu_usec':cpuend,'cold_cpu_usec':warmcpu,'warm_cpu_usec':cpuend-warmcpu if warmcpu is not None and cpuend is not None else None,'warm_elapsed_s':end-warm if warm and end else None,'peak_pss_kib':max((s['pss_kib'] for s in peaks),default=0),'peak_rss_kib':max((s['rss_kib'] for s in peaks),default=0),'marks':marks,'requests':requests,'samples':peaks,'sampling_s':sampling}
  (folder/'receipt.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in ('marks','requests','samples')}),flush=True);return result
 finally:
  if (cg/'cgroup.procs').read_text().strip():
   (cg/'cgroup.kill').write_text('1')
  for p in procs:
   try:p.wait(timeout=3)
   except subprocess.TimeoutExpired:pass
  cg.rmdir()
if __name__=='__main__':
 mode=sys.argv[2] if len(sys.argv)>2 else 'gate'
 if mode=='gate':
  for j,e in enumerate(('ante','pi','hermes')):cell(e,1,j)
 elif mode=='observer':
  j=0
  for order in ((0,.02),(.02,0),(0,.02)):
   for rate in order:
    for e in ('ante','pi','hermes'):
     r=cell(e,1,j,65,rate);j+=1
     if not r['valid']:raise RuntimeError('Observer gate failed')
 elif mode=='study':
  plan=json.loads((ROOT/'protocol.json').read_text());assert plan['turns_per_instance']==65
  for j,c in enumerate(plan['cells']):
   r=cell(c['engine'],c['instances'],j,plan['turns_per_instance'],plan['sampling_s'])
   with open(OUT/'summary.jsonl','a') as f:f.write(json.dumps({k:v for k,v in r.items() if k not in ('requests','marks','samples')})+'\n')
   if not r['valid']:raise RuntimeError('Invalid measured cell: '+r['id'])
