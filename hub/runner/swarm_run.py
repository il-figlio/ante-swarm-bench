#!/usr/bin/env python3
"""Swarm runner. Modes:
  local  N Ante instances on ONE cloned VM (default; density is the exhibit)
  vm     one Ante cell per VM, N cells (isolation / kill drill), capped by the hub
Work is persistent (brain ships a base repo, fetches each task/<id> branch back); cells are disposable.
Live files in the run dir: log.jsonl, samples.jsonl. `cancel` file => hard stop: kill ante on every cell,
no new dispatch, destroy cells. Every created cell is destroyed on any exit path.
Tokens come from Ante's own JSON event stream (UsageUpdate); cost needs Antix attribution and is left null.
"""
import argparse, concurrent.futures as cf, json, os, queue, re, shlex, signal, subprocess, sys, threading, time
from pathlib import Path

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
GOLDEN = os.environ.get("SWARM_GOLDEN", "ante-golden")
CELL_SUFFIX = "." + os.environ.get("SWARM_CELL_DOMAIN", "exe.xyz")  # exe.dev VM hostnames are <name>.exe.xyz
LOCK = threading.Lock()

# Harness registry: same prompt, same model, same check for every entry. Only the command and the process name differ.
HARNESSES = {
    "ante": {"proc": "ante", "match": "ante --effort", "cmd": lambda prompt, effort, model:
        f"ante --effort {effort} " + (f"-m {shlex.quote(model)} " if model else "") + f"--profile bare --no-session-save --yolo --output-format json -p {shlex.quote(prompt)}"},
    "hermes": {"proc": "hermes", "match": "hermes/.venv/bin/hermes", "cmd": lambda prompt, effort, model:
        f"hermes chat --quiet --oneshot --yolo --max-turns 40 --in \"$PWD\" " + (f"-m {shlex.quote(model)} " if model else "") + f"-q {shlex.quote(prompt)}"},
    "pi": {"proc": "node", "match": "pi-coding-agent/dist/cli.js", "cmd": lambda prompt, effort, model:
        f"pi -p --mode json --no-session --no-extensions --no-skills --thinking {'low' if effort in ('min','low','medium') else 'high'} " + (f"--model {shlex.quote(model)} " if model else "") + shlex.quote(prompt)},
}
TOKEN_SCRIPT = r"""import json,sys;i=o=c=0
def walk(x):
    global i,o,c
    if isinstance(x,dict):
        if 'input_tokens' in x or 'output_tokens' in x or 'input' in x and 'output' in x and all(isinstance(x.get(k),int) for k in ('input','output')):
            i+=int(x.get('input_tokens',x.get('input',0)) or 0);o+=int(x.get('output_tokens',x.get('output',0)) or 0);c+=int(x.get('cache_read_tokens',x.get('cacheRead',x.get('cache_read',0))) or 0);return
        for v in x.values(): walk(v)
    elif isinstance(x,list):
        for v in x: walk(v)
for l in open(sys.argv[1]):
    try: d=json.loads(l)
    except Exception: continue
    walk(d)
print(i,o,c)"""

def now(): return time.strftime("%H:%M:%S", time.gmtime())
def run(argv, timeout=600):
    t0 = time.time()
    try: p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out, err = e.stdout or "", e.stderr or ""
        if isinstance(out, bytes): out = out.decode("utf-8", "replace")
        if isinstance(err, bytes): err = err.decode("utf-8", "replace")
        p = subprocess.CompletedProcess(argv, 124, out, err + " timeout")
    return p, round(time.time() - t0, 2)
def ssh(host, cmd, timeout=600, tries=4):
    """ssh with retry on connection-level failure (exit 255): N simultaneous connections to a fresh VM hit sshd MaxStartups."""
    for attempt in range(tries):
        p, s = run(SSH + [host, cmd], timeout)
        if p.returncode != 255: return p, s
        time.sleep(0.5 * (attempt + 1) + 0.2 * attempt)
    return p, s
def rsync(src, dst, tries=4):
    for attempt in range(tries):
        p, s = run(["rsync", "-az", "-e", " ".join(SSH), src, dst])
        if p.returncode not in (255, 12, 30): return p, s
        time.sleep(0.5 * (attempt + 1))
    return p, s
def exe(cmd, timeout=120): return run(SSH + ["exe.dev", cmd], timeout)
CELL_PREFIX = os.environ.get("SWARM_CELL_PREFIX", "bench-c")  # VMs are bench capacity, not ante — names show up in receipts for every harness
def cell_name(i): return f"{CELL_PREFIX}-{i:02d}"

class Run:
    def __init__(self, workdir):
        self.dir = workdir; self.rows = []; self.samples = []; self.cells = []  # cells: names created by THIS run
        self.jl = workdir / "log.jsonl"; self.sj = workdir / "samples.jsonl"; self.jl.write_text(""); self.sj.write_text("")
        (workdir / "receipts.json").unlink(missing_ok=True); (workdir / "cancel").unlink(missing_ok=True)
        for f in workdir.glob("kill-*"): f.unlink()  # stale kill flags from a reused run id must not fire at this run's labels
        self.stop = threading.Event(); self.cancel_flag = threading.Event(); self.harness = "ante"
        self.instances = {}  # label -> (host, tid); enables per-task kill and live token totals
        self.killed = {}  # label -> task id terminated via the kill control; that attempt requeues as orphaned
        self.sampled = set()  # cell names with a sampler thread already running
        self.max_tokens = 0; self.live_tokens = {}  # cell -> {"input","output"}; summed across cells for the ceiling
    def cancelled(self): return self.cancel_flag.is_set() or (self.dir / "cancel").exists()
    def log(self, cell, action, secs, result, notes=""):
        row = {"t": now(), "cell": cell, "action": action, "secs": secs, "result": result, "notes": str(notes)[:240]}
        with LOCK:
            self.rows.append(row); print(f'{row["t"]} {cell:<14}{action:<22}{secs:>7}s  {result}  {str(notes)[:80]}', flush=True)
            with self.jl.open("a") as f: f.write(json.dumps(row) + "\n")
    def sample(self, row):
        with LOCK:
            self.samples.append(row)
            with self.sj.open("a") as f: f.write(json.dumps(row) + "\n")
    def track(self, name):
        with LOCK: self.cells.append(name)

def ensure_cell(name, R):
    p, s = ssh(name + CELL_SUFFIX, "true", timeout=15)
    if p.returncode == 0: R.track(name); R.log(name, "exists", s, "ok"); return True
    R.track(name)  # track before the call so a half-created VM is still destroyed
    p, s = exe(f"rm {name} --json"); R.log(name, "rm stale", s, "ok" if p.returncode == 0 else "FAIL", (p.stdout or p.stderr).strip()[:80])
    p, s = exe(f"cp {GOLDEN} {name} --json"); R.log(name, "cp golden", s, "ok" if p.returncode == 0 else "FAIL", (p.stdout or p.stderr).strip())
    if p.returncode != 0: return False
    t0 = time.time()
    while time.time() - t0 < 150 and not R.cancelled():
        p, _ = ssh(name + CELL_SUFFIX, "true", timeout=8)
        if p.returncode == 0: break
        time.sleep(1)
    ok = p.returncode == 0; R.log(name, "ssh ready", round(time.time() - t0, 2), "ok" if ok else "FAIL"); return ok

def destroy_all(R):
    names = sorted(set(R.cells))
    if not names: return
    def rm(name):
        p, s = exe(f"rm {name} --json"); R.log(name, "rm", s, "ok" if p.returncode == 0 else "FAIL", (p.stdout or p.stderr).strip()[:100])
    with cf.ThreadPoolExecutor(max_workers=max(1, len(names))) as ex: list(ex.map(rm, names))

EVENT_CMD = ("echo __EVENTS__; for f in /tmp/out-*; do [ -f \"$f\" ] || continue; o=/tmp/off-${f#/tmp/out-}; cur=$(stat -c%s \"$f\" 2>/dev/null || echo 0); "
             "prev=$(cat \"$o\" 2>/dev/null || echo 0); [ \"$cur\" -lt \"$prev\" ] && prev=0; bytes=$((cur - prev)); [ $bytes -gt 16384 ] && bytes=16384; "
             "echo \"@@F ${f#/tmp/out-} $(( $(date +%s) - $(stat -c%Y \"$f\") ))\"; [ $bytes -gt 0 ] && tail -c +$((prev+1)) \"$f\" | head -c $bytes; echo $((prev+bytes)) > \"$o\"; done")

def split_events(out):
    """Split a sampler response into the numeric prefix and [(task_id, age_s, new_bytes)] event blocks."""
    body, _, events = out.partition("__EVENTS__\n")
    rows = []
    for block in events.split("@@F ")[1:]:
        head, _, data = block.partition("\n")
        tid, _, age = head.strip().partition(" ")
        try: rows.append((tid, int(age or 0), data))
        except ValueError: continue  # a tailed line starting with "@@F " must not sink the whole poll
    return body, rows

def sampler(name, R, every=2, proc="ante"):
    match = next((h["match"] for h in HARNESSES.values() if h["proc"] == proc), proc)
    tok = "python3 -c " + shlex.quote(TOKEN_SCRIPT.replace("for l in open(sys.argv[1]):", "import glob\nfor l in (line for f in glob.glob('/tmp/out-*') for line in open(f, errors='ignore')):")) + " x"
    cmd = (f"ps -eo rss=,pcpu=,args= | grep -F -- {shlex.quote(match)} | awk '$3 ~ /^(timeout|setsid|grep|bash|sh|awk|cat|tail)$/ {{next}} {{r+=$1;c+=$2;n++}} END{{printf \"%d %.1f %d\", r, c, n}}'; echo; "
           f"{tok} 2>/dev/null || echo 0 0 0; "
           "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print t-a}' /proc/meminfo; cut -d' ' -f1 /proc/loadavg; " + EVENT_CMD)
    evf = (R.dir / "events.jsonl").open("a")
    while not R.stop.is_set():
        try:
            p, _ = ssh(name + CELL_SUFFIX, cmd, timeout=15)
            body, rows = split_events(p.stdout); parts = body.split()
            if p.returncode == 0 and len(parts) >= 8:
                ti, to = int(parts[3]), int(parts[4])
                with LOCK: R.live_tokens[name] = {"input": ti, "output": to}
                R.sample({"t": round(time.time(), 1), "cell": name, "ante_rss_kb": int(parts[0]), "ante_cpu_pct": float(parts[1]), "ante_procs": int(parts[2]), "tokens_in": ti, "tokens_out": to, "vm_used_kb": int(parts[6]), "load1": float(parts[7])})
            else:
                R.sample({"t": round(time.time(), 1), "cell": name, "ante_rss_kb": -1, "ante_cpu_pct": 0, "ante_procs": -1, "tokens_in": 0, "tokens_out": 0, "vm_used_kb": -1, "load1": -1})
            for tid, age, data in rows:
                with LOCK:
                    evf.write(json.dumps({"t": round(time.time(), 1), "cell": name, "task": tid, "age_s": age, "data": data}) + "\n"); evf.flush()
        except Exception as e:
            R.log(name, "sample", 0, "FAIL", f"poll failed: {e}")
        R.stop.wait(every)
    evf.close()

def kill_instance(R, label):
    with LOCK:  # lookup+flag must be atomic vs run_one's instance pop — else a kill on a dead task could flag the next one
        inst = R.instances.get(label)
        if inst: R.killed[label] = inst[1]  # flag before the kill lands: run_one may return before this ssh does
    if not inst: R.log(label, "kill", 0, "FAIL", "no such running instance"); return
    host, tid = inst
    p, s = ssh(host, f"P=$(cat /tmp/pid-{tid} 2>/dev/null); [ -n \"$P\" ] || exit 1; kill -TERM -- -$P 2>/dev/null; sleep 1; kill -KILL -- -$P 2>/dev/null; true", timeout=20)
    if p.returncode != 0: R.killed.pop(label, None)
    R.log(label, "kill", s, "ok" if p.returncode == 0 else "FAIL", f"task {tid} process group terminated")

def cancel_watcher(R):
    """Hard stop on `cancel`; targeted kill on `kill-<label>`; auto-cancel when the token ceiling is crossed."""
    seen = set()
    while not R.stop.is_set():
        if (R.dir / "cancel").exists():
            R.cancel_flag.set(); names = sorted(set(R.cells))
            for n in names: ssh(n + CELL_SUFFIX, KILL_GROUPS, timeout=20)
            R.log("brain", "hard cancel", 0, "ok", f"killed task process groups on {len(names)} cell(s)"); return
        for f in R.dir.glob("kill-*"):
            if f.name not in seen:
                seen.add(f.name); kill_instance(R, f.name[5:].replace("__", "/"))
        tot = {"input": sum(v["input"] for v in R.live_tokens.values()), "output": sum(v["output"] for v in R.live_tokens.values())}
        if R.max_tokens and (tot["input"] + tot["output"]) > R.max_tokens:
            (R.dir / "cancel").write_text(f"token ceiling {R.max_tokens} exceeded: {tot}")
            R.log("brain", "token ceiling", 0, "FAIL", f"{tot} > {R.max_tokens}; cancelling")
        time.sleep(1)

def ship_base(host, remote, workdir, R, label):
    ssh(host, f"rm -rf {remote} && mkdir -p {remote}")
    p, s = rsync(str(workdir / "base") + "/", f"{host}:{remote.replace('~/', '')}/")
    R.log(label, "ship base repo", s, "ok" if p.returncode == 0 else "FAIL", p.stderr.strip()[-100:]); return p.returncode == 0

KILL_GROUPS = ("for f in /tmp/pid-*; do P=$(cat \"$f\" 2>/dev/null); [ -n \"$P\" ] && kill -TERM -- -\"$P\" 2>/dev/null; done; sleep 2; "
               "for f in /tmp/pid-*; do P=$(cat \"$f\" 2>/dev/null); [ -n \"$P\" ] && kill -KILL -- -\"$P\" 2>/dev/null; done; true")

def ante_cmd(remote, tid, prompt, check, timeout, effort, model, harness="ante"):
    agent = HARNESSES[harness]["cmd"](prompt, effort, model)
    inner = f"echo $$ > /tmp/pid-{tid}; exec {agent}"  # quoted as one word so the remote shell cannot split the prompt or run its substitutions
    return (f"export ANTE_TELEMETRY=off PI_OFFLINE=1 PATH=$HOME/.ante/bin:/opt/swarm/node/bin:$PATH; cd {remote} && git checkout -q -B task/{tid} main 2>/dev/null; "
            f"/usr/bin/time -f 'peak_rss_kb=%M user_s=%U sys_s=%S wall_s=%e' -o /tmp/ru-{tid} "
            f"timeout -k 10 {timeout} setsid -w bash -c {shlex.quote(inner)} </dev/null >/tmp/out-{tid} 2>/tmp/err-{tid}; RC=$?; "
            # restore protected tracked files before judging: agents may add new files under tests/ but cannot rewrite checkers, the manifest, or the corpus
            f"for p in tests logs tools; do git checkout -q main -- \"$p\" 2>/dev/null; done; "
            f"git add -A; git -c user.name={harness}-cell -c user.email=cell@swarm -c commit.gpgsign=false commit -q -m 'task {tid} ({harness})' || true; "
            f"CHECK=$(python3 -m unittest {check} -q 2>&1 | tail -1); "
            f"TOK=$(python3 -c {shlex.quote(TOKEN_SCRIPT)} /tmp/out-{tid} 2>/dev/null); "
            f"echo RC=$RC; cat /tmp/ru-{tid}; echo CHECK=$CHECK; echo TOKENS=$TOK; echo FILES=$(git log --name-only --format='' main..HEAD 2>/dev/null | sort -u | tr '\\n' ' '); tail -c 300 /tmp/err-{tid}")

def parse(out):
    g = lambda k: next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith(k + "=")), "")
    tok = g("TOKENS").split(); tokens = {"input": int(tok[0]), "output": int(tok[1]), "cache_read": int(tok[2])} if len(tok) == 3 else None
    return {"rc": g("RC") or "?", "rusage": next((l for l in out.splitlines() if l.startswith("peak_rss_kb")), ""), "check": g("CHECK"), "files": g("FILES").strip(), "tokens": tokens}

def fetch(host, remote, tid, workdir, R, label):
    p, s = run(["git", "--git-dir", str(workdir / "work.git"), "fetch", "-q", f"ssh://{host}/~/{remote.replace('~/', '')}", f"+task/{tid}:{label}/task/{tid}"])
    R.log(label, "fetch branch", s, "ok" if p.returncode == 0 else "FAIL", p.stderr.strip()[-100:]); return p.returncode == 0

def run_one(host, label, remote, task, workdir, R, a, stagger=0.0):
    if stagger: time.sleep(stagger)
    if R.cancelled(): return None
    if not ship_base(host, remote, workdir, R, label):
        return {"task": task["id"], "cell": label, "host": host, "harness": a.harness, "passed": False, "failure": "infra", "requeue": "ship_failed"}
    with LOCK: R.instances[label] = (host, task["id"])
    p, s = ssh(host, ante_cmd(remote, task["id"], task["prompt"], task.get("check", ""), a.timeout, a.effort, a.model, a.harness), timeout=a.timeout + 60)
    with LOCK: R.instances.pop(label, None)  # task process is gone — a kill from here on must not hit a stale pid file
    r = parse(p.stdout); ok = r["rc"] == "0" and r["check"].startswith("OK")
    fail = "" if ok else ("cancelled" if R.cancelled() else "timeout" if r["rc"] == "124" else "check_failed" if r["rc"] == "0" else "ante_error")
    exp = set(task.get("expect_files") or []); touched = set(r["files"].split()); off = sorted(touched - exp) if exp else []
    R.log(label, f"ante task {task['id']}", s, "ok" if ok else fail, f"{r['rusage']} tokens={r['tokens']} files={r['files']}" + (f" off-target={off}" if off else ""))
    fetched = fetch(host, remote, task["id"], workdir, R, label) if not R.cancelled() else False
    requeue = None
    if R.killed.pop(label, None) == task["id"] and not ok: requeue = "killed"  # a kill that lands after the task passed is a no-op
    elif p.returncode == 255: requeue = "cell_lost"  # ssh dropped mid-task: the cell died or was removed
    elif not fetched and not R.cancelled(): requeue = "fetch_failed"
    return {"task": task["id"], "cell": label, "host": host, "harness": a.harness, "ante_rc": r["rc"], "check": r["check"], "passed": ok, "failure": fail, "ante_secs": s,
            "rusage": r["rusage"], "tokens": r["tokens"], "cost_usd": None, "files": r["files"], "off_target_files": off, "fetch_ok": fetched, "requeue": requeue}

def ensure_sampler(name, R, every, proc):
    with LOCK:
        if name in R.sampled: return
        R.sampled.add(name)
    threading.Thread(target=sampler, args=(name, R, every, proc), daemon=True).start()

def reconcile(tasks, nworkers, spawn, R):
    """Desired state: each task done once. Observed state: per-attempt results. Infra losses —
    cell lost, ship/fetch failed, operator kill — requeue at attempt+1 (max 2). Harness and
    task failures stay terminal: a bench never retries the model's mistakes."""
    pend = queue.Queue()
    for t in tasks: pend.put({"task": t, "attempt": 1})
    final, superseded = {}, []
    def work(i):
        while not R.cancelled():
            try: item = pend.get_nowait()
            except queue.Empty: return
            try:
                r = spawn(i, item)
            except Exception as e:  # an unguarded error must not lose the whole run's receipts
                R.log("brain", "spawn error", 0, "FAIL", f"{item['task']['id']}: {e!r}")
                r = {"task": item["task"]["id"], "cell": f"worker-{i}", "host": "", "harness": R.harness, "passed": False,
                     "failure": "infra", "requeue": "spawn_error", "ante_rc": "?", "check": "", "ante_secs": 0,
                     "rusage": "", "tokens": None, "files": "", "off_target_files": [], "fetch_ok": False}
            if r is None: continue
            if r.get("requeue") and item["attempt"] < 2 and not R.cancelled():
                superseded.append(r)
                R.log(r["cell"], "requeue", 0, "ok", f"{item['task']['id']} {r['requeue']}; attempt {item['attempt'] + 1}")
                pend.put({"task": item["task"], "attempt": item["attempt"] + 1})
                continue
            r["attempt"] = item["attempt"]; final[item["task"]["id"]] = r
    with cf.ThreadPoolExecutor(max_workers=nworkers) as ex: list(ex.map(work, range(nworkers)))
    return [final[k] for k in sorted(final)], superseded

def init_work(pack, workdir):
    bare = workdir / "work.git"
    if bare.exists(): return
    run(["git", "init", "-q", "--bare", str(bare)])
    tmp = workdir / "base"; run(["rm", "-rf", str(tmp)]); run(["cp", "-R", str(pack), str(tmp)]); run(["rm", "-rf", str(tmp / ".git")])
    for c in (["init", "-q", "-b", "main"], ["-c", "user.name=swarm", "-c", "user.email=swarm@ante", "add", "-A"],
              ["-c", "user.name=swarm", "-c", "user.email=swarm@ante", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"], ["push", "-q", str(bare), "main"]):
        run(["git", "-C", str(tmp)] + c)

def review(results, workdir, R):
    rev = workdir / "review"; run(["rm", "-rf", str(rev)]); run(["git", "clone", "-q", str(workdir / "work.git"), str(rev)])
    merged = []
    for r in results:
        if not r or not r["fetch_ok"]: continue
        ref = f"{r['cell']}/task/{r['task']}"; run(["git", "-C", str(rev), "fetch", "-q", "origin", f"{ref}:{ref}"])
        p, _ = run(["git", "-C", str(rev), "-c", "user.name=reviewer", "-c", "user.email=rev@ante", "-c", "commit.gpgsign=false", "merge", "-q", "--no-edit", ref])
        merged.append({"task": r["task"], "cell": r["cell"], "merged": p.returncode == 0})
        if p.returncode != 0: run(["git", "-C", str(rev), "merge", "--abort"])
    t0 = time.time(); p = subprocess.run(["python3", "-m", "unittest", "-q"], cwd=rev, capture_output=True, text=True)
    checks = (p.stderr.strip().splitlines() or [p.stdout.strip()[-100:]])[-1]
    R.log("reviewer", "merge+unittest", round(time.time() - t0, 2), "ok" if p.returncode == 0 else "FAIL", checks); return merged, checks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True); ap.add_argument("--tasks", required=True); ap.add_argument("--run-id", required=True)
    ap.add_argument("--mode", choices=["local", "vm"], default="local")
    ap.add_argument("--cells", type=int, default=8, help="local: instances on one VM; vm: number of VMs")
    ap.add_argument("--effort", default=os.environ.get("SWARM_EFFORT", "medium")); ap.add_argument("--model", default="")
    ap.add_argument("--harness", choices=sorted(HARNESSES), default="ante"); ap.add_argument("--max-tokens", type=int, default=0, help="cancel the run when input+output tokens exceed this (0 = no ceiling)")
    ap.add_argument("--timeout", type=int, default=300); ap.add_argument("--keep-cells", action="store_true"); ap.add_argument("--runs-dir", default="runs")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *x: sys.exit(143))  # SIGTERM still runs the finally that destroys cells (hub backstop killpg)
    pack = Path(a.pack).resolve(); tasks_all = json.loads(Path(a.tasks).read_text())
    workdir = Path(a.runs_dir) / a.run_id; workdir.mkdir(parents=True, exist_ok=True)
    R = Run(workdir); R.harness = a.harness; R.max_tokens = a.max_tokens; T0 = time.time(); init_work(pack, workdir)
    (workdir / "meta.json").write_text(json.dumps({"run_id": a.run_id, "mode": a.mode, "cells": a.cells, "effort": a.effort, "model": a.model or "golden default", "harness": a.harness, "max_tokens": a.max_tokens, "pack": pack.name, "started": now(), "pid": os.getpid()}))
    tasks = [dict(tasks_all[i % len(tasks_all)], id=f"{tasks_all[i % len(tasks_all)]['id']}-{i+1:02d}") if a.mode == "local" else tasks_all[i % len(tasks_all)] for i in range(a.cells)]
    if a.mode == "vm" and a.cells > len(tasks_all): sys.exit(f"vm mode needs one task per cell: {a.cells} cells > {len(tasks_all)} tasks")
    for t in tasks:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", t["id"]): sys.exit(f"bad task id {t['id']!r}")  # tid is interpolated into remote shell commands
    R.log("brain", "start", 0, "ok", f"harness={a.harness} mode={a.mode} n={a.cells} effort={a.effort} model={a.model or 'golden'}")
    threading.Thread(target=cancel_watcher, args=(R,), daemon=True).start()
    results, superseded, swarm_secs, merged, checks = [], [], None, [], "n/a"
    try:
        if a.mode == "vm":
            def spawn(i, item):
                name = cell_name(i + 1)
                if not ensure_cell(name, R):
                    return {"task": item["task"]["id"], "cell": name, "host": name + CELL_SUFFIX, "harness": a.harness, "passed": False, "failure": "infra", "requeue": "clone_failed"}
                ensure_sampler(name, R, 2, HARNESSES[a.harness]["proc"])
                return run_one(name + CELL_SUFFIX, name, f"~/workspace/{item['task']['id']}", item["task"], workdir, R, a)
            results, superseded = reconcile(tasks, a.cells, spawn, R)
        else:
            name = cell_name(1)
            if ensure_cell(name, R) and not R.cancelled():
                ensure_sampler(name, R, 1, HARNESSES[a.harness]["proc"])
                def spawn(i, item):
                    return run_one(name + CELL_SUFFIX, f"{name}/i{i+1:02d}", f"~/workspace/{item['task']['id']}", item["task"], workdir, R, a, stagger=0.15 * i if item["attempt"] == 1 else 0.0)
                results, superseded = reconcile(tasks, a.cells, spawn, R)
        swarm_secs = round(time.time() - T0, 2); R.log("brain", "all cells done", swarm_secs, "cancelled" if R.cancelled() else "ok")
        if a.mode == "vm" and not R.cancelled(): merged, checks = review(results, workdir, R)
        elif R.cancelled(): checks = "cancelled"
    except Exception as e:
        R.log("brain", "error", 0, "FAIL", repr(e)); checks = "error"
    finally:
        R.stop.set()
        if not a.keep_cells: destroy_all(R)
    total = round(time.time() - T0, 2); R.log("brain", "total", total, "cancelled" if R.cancelled() else "ok")
    done = [r for r in results if r]; tok = [r["tokens"] for r in done if r.get("tokens")]
    peak = max((int(r["rusage"].split("peak_rss_kb=")[1].split()[0]) for r in done if "peak_rss_kb=" in r["rusage"]), default=0)
    (workdir / "receipts.json").write_text(json.dumps({"run_id": a.run_id, "harness": a.harness, "mode": a.mode, "cells": a.cells, "effort": a.effort, "model": a.model or "golden default", "cancelled": R.cancelled(),
        "swarm_secs": swarm_secs or total, "total_secs": total, "results": done, "merged": merged, "checks": checks,
        "passed": sum(1 for r in done if r["passed"]), "attempted": len(done), "failures": {k: sum(1 for r in done if r["failure"] == k) for k in ("timeout", "check_failed", "ante_error", "infra", "cancelled")},
        "superseded_attempts": superseded,
        "tokens": {"input": sum(t["input"] for t in tok), "output": sum(t["output"] for t in tok), "cache_read": sum(t["cache_read"] for t in tok)} if tok else None, "cost_usd": None, "max_tokens": a.max_tokens,
        "cancel_reason": (workdir / "cancel").read_text()[:200] if (workdir / "cancel").exists() else None,
        "peak_instance_rss_kb": peak, "peak_vm_ante_rss_kb": max((s["ante_rss_kb"] for s in R.samples), default=0) if any(s["ante_rss_kb"] > 0 for s in R.samples) else 0, "peak_ante_procs": max((s["ante_procs"] for s in R.samples), default=0) if any(s["ante_procs"] > 0 for s in R.samples) else 0,
        "cells_created": sorted(set(R.cells))}, indent=1))
    (workdir / "log.json").write_text(json.dumps(R.rows, indent=1)); (workdir / "samples.json").write_text(json.dumps(R.samples))
    print(json.dumps({"total_secs": total, "passed": f"{sum(1 for r in done if r['passed'])}/{len(done)}", "checks": checks, "cells_created": sorted(set(R.cells))}))

if __name__ == "__main__": main()
