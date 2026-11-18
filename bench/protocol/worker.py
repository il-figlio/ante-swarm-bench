#!/usr/bin/env python3
"""shared-v3 worker (copy of shared-v2 with Anthropic Messages cohort and Codex CLI cohort). One persistent, unprivileged host. No VM lifecycle calls or private credentials."""
import argparse, concurrent.futures, fcntl, filecmp, hashlib, json, os, re, shutil, signal, subprocess, time, threading
from pathlib import Path

def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and not p.name.startswith('._') and p.name!='.DS_Store' and '__pycache__' not in p.parts and p.suffix!='.pyc'}

ACTIVE={};ACTIVE_LOCK=threading.Lock()
def process_tree():
    table={}
    if not Path('/proc').exists():return table
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():continue
        try:
            fields=(item/'stat').read_text().rsplit(')',1)[1].split();table[int(item.name)]=(int(fields[1]),int(fields[21])*os.sysconf('SC_PAGE_SIZE')//1024)
        except (OSError,ValueError,IndexError):pass
    return table

def descendants(pid,table):
    found={pid};todo=[pid]
    while todo:
        parent=todo.pop()
        for child,(ppid,_) in table.items():
            if ppid==parent and child not in found:found.add(child);todo.append(child)
    return found

def gateway_pid(stats):
    # Hermes >= 0.22 starts a per-home daemon gateway (parent pid 1, so not a descendant of the CLI); count and clean it up with the task.
    try:return int(json.loads(Path(stats['gateway_pid_file']).read_text())['pid']) if stats and stats.get('gateway_pid_file') else None
    except (OSError,ValueError,KeyError,TypeError):return None

def stop_gateway(stats):
    pid=gateway_pid(stats)
    if pid:
        try:os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass

def measure_processes():
    table=process_tree()
    with ACTIVE_LOCK:
        for pid,stats in ACTIVE.items():
            extra=gateway_pid(stats);stats['peak_tree_rss_kb']=max(stats.get('peak_tree_rss_kb',0),sum(table.get(x,(0,0))[1] for x in descendants(pid,table))+(table.get(extra,(0,0))[1] if extra and extra not in descendants(pid,table) else 0))

def execute(argv,cwd,env,timeout,output,stats=None):
    with output.open('wb') as stream:
        p=subprocess.Popen(argv,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
        if stats is not None:
            with ACTIVE_LOCK:ACTIVE[p.pid]=stats
        end=time.monotonic()+timeout
        while p.poll() is None:
            if time.monotonic()>end or output.stat().st_size>8*1024*1024:
                if stats is not None:stats['termination_reason']='task_timeout' if time.monotonic()>end else 'output_limit'
                table=process_tree()
                for child in descendants(p.pid,table)-{p.pid}:
                    try:os.kill(child,signal.SIGKILL)
                    except ProcessLookupError:pass
                stop_gateway(stats);os.killpg(p.pid,signal.SIGKILL);p.wait()
                with ACTIVE_LOCK:ACTIVE.pop(p.pid,None)
                return 124
            time.sleep(.1)
        stop_gateway(stats)
        with ACTIVE_LOCK:ACTIVE.pop(p.pid,None)
        return p.returncode

# Base URLs handed to each harness for the Messages route (verified end to end against the fake upstream in tests/).
MESSAGES_BASE={'ante':'{root}/v1','pi':'{root}','hermes':'{root}'}
CODEX_FEATURES_OFF=['apps','browser_use','browser_use_external','browser_use_full_cdp_access','computer_use','daemon_auto_start','enable_request_compression','fast_mode','ultrafast_mode','hooks','image_generation','memories','multi_agent','multi_agent_v2','plugins','remote_plugin','tool_suggest','skill_search','sleep_tool','goals']

def codex_config(model,effort,base_url):
    off=''.join(f'{k} = false\n' for k in CODEX_FEATURES_OFF)
    return (f'model = {json.dumps(model)}\nmodel_provider = "relay"\nmodel_reasoning_effort = {json.dumps(effort)}\nweb_search = "disabled"\ncheck_for_update_on_startup = false\nproject_doc_max_bytes = 0\n'
            '[skills.bundled]\nenabled = false\n[analytics]\nenabled = false\n[feedback]\nenabled = false\n[history]\npersistence = "none"\n'
            f'[model_providers.relay]\nname = "relay"\nbase_url = {json.dumps(base_url)}\nwire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n[features]\n{off}')

def claude_env(model,root_url,home):
    # Every Claude Code model slot (including its background/small model) is pinned to the benchmark model so no request goes to another id.
    return {'ANTHROPIC_BASE_URL':root_url,'ANTHROPIC_API_KEY':'sk-ant-bench-dummy-not-a-credential','CLAUDE_CONFIG_DIR':str(home/'.claude'),
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_TELEMETRY':'1','DISABLE_AUTOUPDATER':'1','DISABLE_ERROR_REPORTING':'1','DISABLE_BUG_COMMAND':'1',
            'CLAUDE_CODE_DISABLE_AUTO_MEMORY':'1','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','ANTHROPIC_MODEL':model,'ANTHROPIC_DEFAULT_SONNET_MODEL':model,'ANTHROPIC_DEFAULT_OPUS_MODEL':model,
            'ANTHROPIC_DEFAULT_HAIKU_MODEL':model,'ANTHROPIC_DEFAULT_FABLE_MODEL':model,'ANTHROPIC_SMALL_FAST_MODEL':model,'CLAUDE_CODE_SUBAGENT_MODEL':model}

def usage_tokens(audit):
    """Total tokens over the task's generation requests (count_tokens calls excluded). 'input' is total input including cached tokens for both wires:
    Responses already reports it that way; for Anthropic Messages it is input_tokens + cache_read + cache_creation."""
    rows=[r for r in audit if r.get('route')!='count_tokens' and r.get('usage')]
    if not rows:return None
    def one(u):
        details=u.get('input_tokens_details') or {};cr=u.get('cache_read_input_tokens',details.get('cached_tokens',0) if isinstance(details,dict) else 0) or 0;cc=u.get('cache_creation_input_tokens') or 0
        return ((u.get('input_tokens') or 0)+(cr+cc if 'cache_read_input_tokens' in u or 'cache_creation_input_tokens' in u else 0),u.get('output_tokens') or 0,cr,cc)
    t=[one(r['usage']) for r in rows]
    out={'input':sum(x[0] for x in t),'output':sum(x[1] for x in t),'cache_read':sum(x[2] for x in t)}
    if any('cache_creation_input_tokens' in r['usage'] for r in rows):out['cache_creation']=sum(x[3] for x in t)
    return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--harness',choices=['ante','pi','hermes','claude-code','codex'],required=True);ap.add_argument('--protocol',choices=['shared-v3-claude','shared-v3-codex'],required=True);ap.add_argument('--task-count',type=int);ap.add_argument('--matrix-id',default='');ap.add_argument('--audit',action='store_true');ap.add_argument('--pack',choices=['toolbox'],default='toolbox');ap.add_argument('--instances',type=int,default=1);ap.add_argument('--run-id',required=True);ap.add_argument('--model',required=True);ap.add_argument('--effort',choices=['low','medium','high'],default='medium');ap.add_argument('--timeout',type=int,default=180);ap.add_argument('--root',default='/var/lib/ante-bench');ap.add_argument('--code',default='/opt/ante-bench/campaign-20261031');ap.add_argument('--time-bin',default='/usr/bin/time');ap.add_argument('--repeat',type=int,default=0)
    ap.add_argument('--node',default='/opt/swarm/node/bin/node');ap.add_argument('--python3',default='/usr/bin/python3');ap.add_argument('--gate-port',type=int,default=8766)
    ap.add_argument('--test-upstream',default='',help='LOCAL TESTS ONLY: replace the model gateway (BENCH_MODEL_UPSTREAM) with a fake upstream; requires a matrix id containing -test');a=ap.parse_args(); count=a.task_count or a.instances
    if not re.fullmatch(r'r\d{2,4}',a.run_id) or not 1<=a.instances<=32 or not 1<=count<=64 or not 30<=a.timeout<=600:ap.error('Invalid run ID, instance count or timeout')
    cohort_name=a.protocol.rsplit('-',1)[1];protocol=json.loads((Path(a.code)/'protocol.json').read_text());cohort=protocol['cohorts'][cohort_name]
    if not a.matrix_id.startswith(cohort['matrix_id']) or not 1<=a.repeat<=3:ap.error(f"This worker only runs matrix ids starting with {cohort['matrix_id']} and --repeat 1-3")
    if bool(a.test_upstream)!=('-test' in a.matrix_id):ap.error('--test-upstream and a matrix id containing -test must be used together (never for real receipts)')
    if a.harness not in cohort['harnesses'] or a.model not in cohort['models']:ap.error(f"{a.protocol} runs harnesses {cohort['harnesses']} and models {cohort['models']}")
    if not a.audit or a.timeout!=600 or a.effort!='medium' or a.instances not in (1,8) or count not in ((1,4,32) if a.test_upstream else (4,32)):ap.error('shared-v3 requires audit, 600s, medium effort, 1 or 8 instances and 4 (gate) or 32 attempts (1 only with --test-upstream)')
    free=shutil.disk_usage(a.root).free
    if free<6*2**30:ap.error(f'Disk preflight failed: {free/2**30:.2f} GiB free, need 6')
    root=Path(a.root);code=Path(a.code);lock=(root/'worker.lock').open('w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:ap.error('Another benchmark is active')
    d=root/'runs'/a.run_id;d.mkdir(parents=True,exist_ok=False);pack=code/'tasks'/a.pack;tasks=json.loads((pack/'tasks.json').read_text());hermes_python=str(code/'hermes-venv/bin/python');node=a.node;node_dir=str(Path(node).parent);pi_cli=str(code/'pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js');codex_bin=str(code/'codex/node_modules/.bin/codex');claude_bin=str(code/'claude-code/node_modules/.bin/claude')
    tool_path=node_dir+':/usr/bin:/bin'  # Codex and Claude Code are npm-installed; node (and its wrapper) must be on PATH
    if a.harness=='ante':version_cmd=[str(code/'ante'),'--version']
    elif a.harness=='pi':version_cmd=[node,pi_cli,'--version']
    elif a.harness=='codex':version_cmd=[codex_bin,'--version']
    elif a.harness=='claude-code':version_cmd=[claude_bin,'--version']
    else:version_cmd=[hermes_python,'-m','hermes_cli.main','--version']
    version=subprocess.check_output(version_cmd,text=True,env={**os.environ,'PATH':tool_path}).strip()
    if a.harness=='hermes':prep=json.loads((code/'hermes-preparation.json').read_text());version=version.splitlines()[0]+' · release '+prep['source_hermes_release']+' ('+prep['source_hermes_commit'][:7]+')'
    revision=hashlib.sha256(json.dumps(hashes(pack),sort_keys=True).encode()).hexdigest();start=time.monotonic()
    meta={'run_id':a.run_id,'mode':'local','cells':a.instances,'effort':a.effort,'model':a.model,'harness':a.harness,'task_count':count,'matrix_id':a.matrix_id,'harness_version':version,'task_revision':revision,'pack':a.pack,'started':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'pid':os.getpid(),'timeout_s':a.timeout,'protocol_revision':'shared-v3','protocol':a.protocol,'repeat':a.repeat,'provider_route':cohort['provider_route'],'provider_source':cohort['provider_route'],'wire':cohort['wire'],'effort_mapping':cohort['effort_mapping'],'test_upstream':a.test_upstream or None,'telemetry':'off','raw_homes_retained':True,'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'relay_sha256':hashlib.sha256((code/'model_gate.py').read_bytes()).hexdigest() if a.audit else None,'shared_protocol':protocol,'protocol_sha256':hashlib.sha256((code/'protocol.json').read_bytes()).hexdigest(),'preparation':json.loads((code/'hermes-preparation.json').read_text()) if a.harness=='hermes' else None};(d/'meta.json').write_text(json.dumps(meta))
    env={'PATH':'/usr/bin:/bin','HOME':str(root),'ANTE_HOME':str(root/'ante-home'),'ANTE_TELEMETRY':'off','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PI_OFFLINE':'1'}
    gate=None
    if a.audit:
        from model_gate import Gate, UPSTREAM
        gate=Gate(d,a.run_id,a.model,a.effort,upstream=a.test_upstream or UPSTREAM,port=a.gate_port)
    samples=[];stopping=threading.Event()
    def sample():
        while not stopping.is_set():
            try:
                measure_processes();mem={x.split(':')[0]:int(x.split()[1]) for x in Path('/proc/meminfo').read_text().splitlines() if x.split()[1].isdigit()};samples.append({'t':time.time(),'vm_used_kb':mem['MemTotal']-mem['MemAvailable'],'load1':os.getloadavg()[0]})
            except (OSError,KeyError):pass
            stopping.wait(.25)
    sampler=threading.Thread(target=sample,daemon=True);sampler.start()
    def one(i):
        t=tasks[i%len(tasks)];tid=f'{t["id"]}-{i+1:02d}';w=d/'workspaces'/tid;shutil.copytree(pack,w);before=hashes(w);log=d/(tid+'.jsonl');timing=d/(tid+'.time');ts=time.monotonic()
        home=d/'homes'/tid;home.mkdir(parents=True);task_env=dict(env,HOME=str(home),ANTE_HOME=str(home/'.ante'),HERMES_HOME=str(home/'.hermes'),PATH=tool_path)
        root_url=f'http://127.0.0.1:{a.gate_port}/{a.run_id}/{tid}';messages=cohort['wire']=='anthropic-messages'
        for name in ['.ante','.pi/agent','.hermes','.codex','.claude']:(home/name).mkdir(parents=True,exist_ok=True)
        # Base-URL conventions differ per client: OpenAI-style clients append /responses to a base ending in /v1, Anthropic SDK-style clients append /v1/messages to the bare root.
        (home/'.ante/catalog.json').write_text(json.dumps({'providers':{'exe':{'base_url':MESSAGES_BASE['ante'].format(root=root_url) if messages else root_url+'/v1','wire_style':'AnthropicMessage' if messages else 'OpenAiResponse','credential':None,'preferred_models':[{'id':a.model,'max_tokens':16384,'context_limit':128000}]}}}))
        (home/'.ante/settings.json').write_text(json.dumps({'has_completed_onboarding':True,'permission_mode':'yolo'}))
        (home/'.pi/agent/models.json').write_text(json.dumps({'providers':{'exe':{'baseUrl':MESSAGES_BASE['pi'].format(root=root_url) if messages else root_url+'/v1','api':'anthropic-messages' if messages else 'openai-responses','apiKey':'exe-integration','models':[{'id':a.model,'reasoning':True,'contextWindow':128000,'maxTokens':16384}]}}}))
        if a.harness=='hermes':
            for dirname in ('cache','bin'):shutil.copytree(code/'hermes-seed'/dirname,home/'.hermes'/dirname,dirs_exist_ok=True)
            task_env['PYTHONPATH']=str(code/'hermes-src')+(':'+str(code/'hermes-extra') if (code/'hermes-extra').is_dir() else '')
            (home/'.hermes/config.yaml').write_text(json.dumps({'model':{'provider':'custom:exe','default':a.model},'providers':{'exe':{'base_url':MESSAGES_BASE['hermes'].format(root=root_url) if messages else root_url+'/v1','api_key':'exe-integration','transport':'anthropic_messages' if messages else 'codex_responses'}},'terminal':{'backend':'local'},'agent':{'max_turns':40,'reasoning_effort':a.effort},'gateway':{'unmanaged_idle_exit_seconds':5}}))
        if a.harness=='codex':
            task_env['CODEX_HOME']=str(home/'.codex')
            (home/'.codex/config.toml').write_text(codex_config(a.model,a.effort,root_url+'/v1'))
        if a.harness=='claude-code':
            task_env.update(claude_env(a.model,root_url,home))
        cmd=[a.time_bin,'-v','-o',str(timing),str(code/'ante'),'--provider','exe','--model',a.model,'--effort',a.effort,'--no-skills','--disable-auto-memory','--no-session-save','--output-format','json','-p',t['prompt']]
        if a.harness=='pi':cmd=[a.time_bin,'-v','-o',str(timing),node,pi_cli,'--provider','exe','--model',a.model,'--thinking',a.effort,'-p','--mode','json','--no-session','--no-extensions','--no-skills','--no-context-files','--no-mcp',t['prompt']]
        elif a.harness=='hermes':cmd=[a.time_bin,'-v','-o',str(timing),hermes_python,'-m','hermes_cli.main','chat','--quiet','--oneshot','--yolo','--ignore-rules','--toolsets','terminal,file','--reasoning',a.effort,'--max-turns','40','--in',str(w),'-m',a.model,'-q',t['prompt']]
        elif a.harness=='codex':cmd=[a.time_bin,'-v','-o',str(timing),codex_bin,'exec','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox','--ephemeral','--ignore-rules','--json','-C',str(w),'-m',a.model,t['prompt']]
        elif a.harness=='claude-code':cmd=[a.time_bin,'-v','-o',str(timing),claude_bin,'-p','--dangerously-skip-permissions','--output-format','json','--model',a.model,'--effort',a.effort,'--no-session-persistence','--disable-slash-commands','--strict-mcp-config',t['prompt']]
        task_env['ANTE_BENCH_SYSTEM_PROMPT']='You are a coding agent. Use the supplied native terminal and file tools to complete the user task. Make only requested edits and verify them.'
        stats={'gateway_pid_file':str(home/'.hermes/gateway.pid')} if a.harness=='hermes' else {};ts=time.monotonic();rc=execute(cmd,w,task_env,a.timeout,log,stats);agent_secs=round(time.monotonic()-ts,2)
        for cache in w.rglob('__pycache__'):
            if cache.is_dir() and not cache.is_symlink():shutil.rmtree(cache)
        for bytecode in w.rglob('*.pyc'):bytecode.unlink()
        after=hashes(w);changed=sorted(k for k in set(before)|set(after) if before.get(k)!=after.get(k));off=[k for k in changed if k not in t.get('expect_files',[])];check=t.get('check','')
        if not re.fullmatch(r'tests\.[a-z0-9_]+',check):raise ValueError('Invalid task checker')
        # Checkers must be byte-identical to the frozen fixture before execution.
        pristine=all(after.get(k)==v for k,v in before.items() if k.startswith('tests/'))
        check_rc=execute([a.python3,'-m','unittest',check,'-q'],w,env,30,d/(tid+'.check')) if pristine else 1
        peak=re.search(r'Maximum resident set size \(kbytes\):\s*(\d+)',timing.read_text() if timing.exists() else '')
        audit=[r for r in gate.rows if r['task']==tid] if gate else []
        model_ok=not gate or bool(audit) and all(r['accepted'] for r in audit)
        passed=rc==0 and check_rc==0 and not off and model_ok
        return {'task':tid,'cell':f'worker/i{i+1:02d}','harness':a.harness,'passed':passed,'failure':'' if passed else 'output_limit' if stats.get('termination_reason')=='output_limit' else 'timeout' if rc==124 else 'check_failed' if check_rc else 'off_target' if off else 'ante_error','ante_rc':rc,'check':'OK' if check_rc==0 else 'FAILED','checker_pristine':pristine,'launch_argv':cmd[:cmd.index(t['prompt'])] if t['prompt'] in cmd else cmd,'ante_secs':agent_secs,'model_verified':model_ok,'request_audit':audit,'peak_process_tree_rss_kb':stats.get('peak_tree_rss_kb') or None,'peak_rss_kb':int(peak[1]) if peak else None,'files':' '.join(changed),'off_target_files':off,'tokens':usage_tokens(audit)}
    journal_lock=threading.Lock()
    def safe_one(i):
        try:
            result=one(i)
            with journal_lock:
                with (d/'task-results.jsonl').open('a') as journal:journal.write(json.dumps(result)+'\n')
            return result
        except Exception as error:return {'task':f"{tasks[i%len(tasks)]['id']}-{i+1:02d}",'cell':f'worker/i{i+1:02d}','harness':a.harness,'passed':False,'failure':'infra','error_type':type(error).__name__,'model_verified':False,'tokens':None,'peak_rss_kb':None,'check':'NOT RUN','files':''}
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.instances) as pool:results=list(pool.map(safe_one,range(count)))
    stopping.set();sampler.join();
    if gate:gate.close()
    elapsed=round(time.monotonic()-start,2);rss=[r['peak_rss_kb'] for r in results if r['peak_rss_kb'] is not None]
    receipt={**meta,'peak_process_tree_rss_kb':max((r.get('peak_process_tree_rss_kb') or 0 for r in results),default=0) or None,'total_secs':elapsed,'swarm_secs':elapsed,'passed':sum(r['passed'] for r in results),'attempted':len(results),'results':results,'cancelled':False,'checks':'per-task','tokens':{k:sum((r.get('tokens') or {}).get(k,0) for r in results) for k in ['input','output','cache_read']} if all(r.get('tokens') is not None for r in results) else None,'cost_usd':None,'peak_instance_rss_kb':max(rss) if rss else None,'cells_created':[],'peak_vm_used_kb':max((r['vm_used_kb'] for r in samples),default=None),'peak_load1':max((r['load1'] for r in samples),default=None),'sample_interval_s':.25,'model_verified':all(r['model_verified'] for r in results),'measurement_scope':'persistent host; fixture copy through final checks; no VM clone/teardown'}
    def durable(path,text):
        with path.open('w') as f:f.write(text);f.flush();os.fsync(f.fileno())
    durable(d/'samples.json',json.dumps(samples));durable(d/'receipts.json',json.dumps(receipt,indent=2));durable(d/'meta.json',json.dumps(meta))
    seed=code/'hermes-seed'
    def home_manifest():
        homes={}
        for home in sorted((d/'homes').iterdir()):
            rows=[];n=0;total=0
            for p in sorted(x for x in home.rglob('*') if x.is_file() and not x.is_symlink()):
                rel=str(p.relative_to(home));size=p.stat().st_size;n+=1;total+=size
                if a.harness=='hermes' and rel.startswith('.hermes/') and (seed/rel[len('.hermes/'):]).is_file() and (seed/rel[len('.hermes/'):]).stat().st_size==size and filecmp.cmp(p,seed/rel[len('.hermes/'):],shallow=False):digest='seed'
                else:digest=hashlib.sha256(p.read_bytes()).hexdigest()
                rows.append(f'{rel}\t{size}\t{digest}')
            homes[home.name]={'files':n,'bytes':total,'listing_sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
        return {'run_id':a.run_id,'note':'listing = sorted lines path<TAB>size<TAB>sha256 (or the word seed for files byte-identical to the hermes seed)','homes':homes}
    manifest_text=json.dumps(home_manifest(),indent=1,sort_keys=True);durable(d/'homes-manifest.json',manifest_text)
    manifest_sha=hashlib.sha256(manifest_text.encode()).hexdigest();shutil.rmtree(d/'homes')
    meta.update(raw_homes_retained=False,homes_manifest_sha256=manifest_sha);receipt.update(raw_homes_retained=False,homes_manifest_sha256=manifest_sha)
    durable(d/'meta.json',json.dumps(meta));durable(d/'receipts.json',json.dumps(receipt,indent=2))
    from usage_tracker import aggregate
    admin=root/'admin';admin.mkdir(exist_ok=True)
    with (admin/'usage.jsonl').open('a') as f:f.write(json.dumps({'completed_run':a.run_id,'cumulative':aggregate(root)})+'\n')
    print(json.dumps({'run_id':a.run_id,'passed':receipt['passed'],'attempted':receipt['attempted'],'wall_s':elapsed}))
if __name__=='__main__':main()
