"""Root driver in private netns. Runtime uid/cgroups isolated; server outside groups."""
import os,sys,json,time,threading,subprocess,selectors,hashlib,itertools,statistics,shutil
from pathlib import Path
from replay import Server,SYSTEM,norm_result,GOLD
from fixture import USER,payload,filename,unwrap,canonical,calls
ROOT=Path(__file__).resolve().parent;OUT=Path(sys.argv[1]);OUT.mkdir(exist_ok=True,parents=True);os.chmod(OUT,0o755)
uid=__import__('pwd').getpwnam('ante-bench').pw_uid;gid=__import__('pwd').getpwnam('ante-bench').pw_gid
server=Server();threading.Thread(target=server.serve_forever,daemon=True).start()
def cpu(cg):return int(dict(l.split() for l in (cg/'cpu.stat').read_text().splitlines())['usage_usec'])
def sample(cg):
 pids=[pid for p in [cg,*cg.glob('*')] if p.is_dir() for pid in (p/'cgroup.procs').read_text().split()];pss=rss=0
 for pid in pids:
  try:
   for l in Path('/proc',pid,'smaps_rollup').read_text().splitlines():
    if l.startswith('Pss:'):pss+=int(l.split()[1])
    if l.startswith('Rss:'):rss+=int(l.split()[1])
  except (FileNotFoundError,ProcessLookupError):pass
 return {'pss_kib':pss,'rss_kib':rss,'pids':len(pids),'cpu_usec':cpu(cg),'t':time.monotonic()}
def cell(engine,n,index,turns=4,sampling=.1):
 cid=f'tool{index:03d}-{engine}-n{n}';folder=OUT/cid;folder.mkdir();os.chmod(folder,0o755)
 cg=Path('/sys/fs/cgroup')/cid;cg.mkdir();(cg/'cgroup.subtree_control').write_text('+cpu +memory');eng=cg/'engine';bridge=cg/'bridge';eng.mkdir();bridge.mkdir()
 for d in (cg,eng,bridge):os.chown(d/'cgroup.procs',uid,gid)
 procs=[];sel=selectors.DefaultSelector();requests=[];marks=[[] for _ in range(n)];logs=[b'' for _ in range(n)];peaks=[]
 start=time.monotonic();deadline=start+240;first=set();last=set();warm=None;warmcpu=None;failure=None;end=None;cpuend=None;warmcomponents=None;endcomponents=None;total_start_cpu=time.process_time()
 try:
  for i in range(n):
   home=folder/f'i{i}';home.mkdir();os.chown(home,uid,gid);(home/'.hermes').mkdir();os.chown(home/'.hermes',uid,gid)
   (home/'.hermes/config.yaml').write_text(json.dumps({'terminal':{'backend':'local'},'tools':{'tool_search':{'enabled':'off'}},'auxiliary':{'title_generation':{'enabled':False}}}))
   family='pi' if 'pi' in engine else 'hermes';control=engine.endswith(('-direct','-mcp'))
   rawschemas=json.loads((ROOT/('pi-schemas-all.json' if family=='pi' else 'hermes-schemas-all.json')).read_text())
   schemas=[{'name':t['name'],'description':t['description'],'inputSchema':t['inputSchema']} for t in rawschemas if t['name'] in ('read','write')] if family=='pi' else [{'name':t['function']['name'],'description':t['function']['description'],'inputSchema':t['function']['parameters']} for t in rawschemas if t['function']['name'] in ('read_file','write_file')]
   schemas.sort(key=lambda x:x['name'])
   key=cid+f'-i{i}';cfg={'engine':engine,'family':family,'url':f'http://127.0.0.1:{server.server_port}/{key}/v1','model':'openai/gpt-5.6-terra','system':SYSTEM,'user':USER,'turns':turns,'workspace':str(home),'schemas':schemas};cfgpath=home/'case.json';cfgpath.write_text(json.dumps(cfg));server.cases[key]={'cfg':cfg,'requests':[]};requests.append(server.cases[key])
   audit=str(home/'tool-audit.jsonl')
   env={'PATH':'/opt/swarm/node/bin:/usr/bin:/bin','HOME':str(home),'ANTE_HOME':str(home/'.ante'),'HERMES_HOME':str(home/'.hermes'),'PYTHONPATH':'/opt/swarm/hermes','PYTHONUNBUFFERED':'1','TERM':'dumb','NO_COLOR':'1','OPENAI_API_KEY':'offline','HERMES_DISABLE_UPDATE_CHECK':'1','BRIDGE_CGROUP':str(bridge),'HERMES_BENCH_PYTHON':'/opt/swarm/hermes-prepared-venv/bin/python','ANTE_HERMES_AUDIT':audit,'ANTE_PI_AUDIT':audit}
   cmd=['/opt/swarm/node/bin/node',str(ROOT/'pi.mjs'),str(cfgpath)] if engine in ('pi','pi-direct','pi-mcp') else ['/opt/swarm/hermes-prepared-venv/bin/python' if engine.startswith('hermes') else '/usr/bin/python3',str(ROOT/'clients.py'),str(cfgpath)]
   def setup():
    (eng/'cgroup.procs').write_text(str(os.getpid()));os.setgroups([]);os.setgid(gid);os.setuid(uid)
   err=open(home/'stderr.log','wb');p=subprocess.Popen(cmd,cwd=home,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,preexec_fn=setup);procs.append(p);err.close();sel.register(p.stdout,selectors.EVENT_READ,i)
  lastsample=0
  while sel.get_map():
   now=time.monotonic()
   if now>deadline:failure='deadline';break
   if sampling and now-lastsample>=sampling:
    point=sample(cg);point['engine']=sample(eng);point['bridge']=sample(bridge);peaks.append(point);lastsample=now
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
    warm=time.monotonic();warmcpu=cpu(cg);warmcomponents={'engine':cpu(eng),'bridge':cpu(bridge)}
    for p in procs:p.stdin.write(b'GO\n');p.stdin.flush()
   if len(last)==n and end is None:
    end=time.monotonic();cpuend=cpu(cg);endcomponents={'engine':cpu(eng),'bridge':cpu(bridge)};point=sample(cg);point['engine']=sample(eng);point['bridge']=sample(bridge);peaks.append(point)
    for p in procs:p.stdin.write(b'QUIT\n');p.stdin.flush()
  rc=[]
  for p in procs:
   try:rc.append(p.wait(timeout=2))
   except subprocess.TimeoutExpired:failure='exit_timeout';rc.append(None)
  cleanup_deadline=time.monotonic()+3
  while sample(cg)['pids'] and time.monotonic()<cleanup_deadline:time.sleep(.02)
  exit_t=time.monotonic();cpu_exit=cpu(cg)
  expected_requests=0 if control else turns*3
  files_ok=all(all((folder/f'i{i}'/filename(t)).is_file() and (folder/f'i{i}'/filename(t)).read_text()==payload(t) for t in range(turns)) for i in range(n))
  tools=[]
  for i in range(n):
   p=folder/f'i{i}'/'tool-audit.jsonl';tools.append([json.loads(l) for l in p.read_text().splitlines()] if p.exists() else [])
  toolcounts_ok=engine=='hermes' or all(len([a for a in audit if a.get('event') in (('pi_body_end',) if engine=='pi' else ('hermes_tool_end','pi_tool_end'))])==2*turns for audit in tools)
  tool_results_ok=True
  if control:
   for i,mm in enumerate(marks):
    actual=[[m['name'],norm_result(m['result'],requests[i]['cfg'])] for m in mm if m['mark']=='tool_result']
    wanted=[[c['name'],GOLD[family][t][k]] for t in range(turns) for k,c in enumerate(calls(family,t))]
    tool_results_ok &= actual==wanted
  valid=not failure and tool_results_ok and files_ok and toolcounts_ok and sample(cg)['pids']==0 and all(x==0 for x in rc) and all(len(c['requests'])==expected_requests and not any(r['errors'] for r in c['requests']) for c in requests) and all(len([m for m in mm if m['mark']=='turn'])==turns for mm in marks)
  result={'id':cid,'engine':engine,'instances':n,'turns_per_instance':turns,'valid':valid,'failure':failure,'returncodes':rc,'elapsed_s':end-start if end else None,'teardown_s':exit_t-end if end else None,'cpu_including_teardown_usec':cpu_exit,'driver_cpu_s':time.process_time()-total_start_cpu,'exit_group_empty':sample(cg)['pids']==0,'files_ok':files_ok,'tool_results_ok':tool_results_ok,'toolcounts_ok':toolcounts_ok,'tools':tools,'component_cold_cpu_usec':warmcomponents,'component_warm_cpu_usec':{k:endcomponents[k]-warmcomponents[k] for k in ('engine','bridge')} if endcomponents and warmcomponents else None,'first_success_s':max((m['driver_t'] for mm in marks for m in mm if m['mark']=='turn' and m['index']==0),default=None),'cpu_usec':cpuend,'cold_cpu_usec':warmcpu,'warm_cpu_usec':cpuend-warmcpu if warmcpu is not None and cpuend is not None else None,'warm_elapsed_s':end-warm if warm and end else None,'warm_start_t':warm,'measured_end_t':end,'peak_pss_kib':max((s['pss_kib'] for s in peaks),default=0),'peak_rss_kib':max((s['rss_kib'] for s in peaks),default=0),'marks':marks,'requests':requests,'samples':peaks,'sampling_s':sampling}
  (folder/'receipt.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in ('marks','requests','samples','tools')}),flush=True);return result
 finally:
  if sample(cg)['pids']:
   (cg/'cgroup.kill').write_text('1')
   cleanup_deadline=time.monotonic()+3
   while sample(cg)['pids'] and time.monotonic()<cleanup_deadline:time.sleep(.02)
  for p in procs:
   try:p.wait(timeout=3)
   except subprocess.TimeoutExpired:pass
  eng.rmdir();bridge.rmdir();cg.rmdir()
if __name__=='__main__':
 mode=sys.argv[2] if len(sys.argv)>2 else 'gate'
 if mode=='gate':
  for j,e in enumerate(('pi-direct','pi-mcp','hermes-direct','hermes-mcp','pi','ante-pi','hermes','ante-hermes')):
   r=cell(e,1,j,3,.1)
   if not r['valid']:raise RuntimeError('Calibration failed: '+r['id'])
 elif mode=='study':
  plan=json.loads((ROOT/'protocol.json').read_text());(OUT/'protocol.json').write_text(json.dumps(plan,indent=2))
  for name,expected in plan['source_sha256'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected,name
  for j,c in enumerate(plan['cells']):
   r=cell(c['engine'],c['instances'],j,plan['turns_per_instance'],plan['sampling_s'])
   with open(OUT/'summary.jsonl','a') as f:f.write(json.dumps({k:v for k,v in r.items() if k not in ('requests','marks','samples','tools')})+'\n')
   # Drop completed wire bodies from server RAM; receipts retain all evidence.
   for key in list(server.cases):
    if key.startswith(r['id']+'-'):del server.cases[key]
   if not r['valid']:raise RuntimeError('Invalid measured cell: '+r['id'])
