#!/usr/bin/env python3
"""Swarm hub: owner-only control and observation service served under /swarm/.
Serves the observatory page and run data from RUNS_DIR. Identity comes from exe.dev's
proxy headers (X-ExeDev-Email); only ALLOWED_EMAILS may read. Stdlib only.
Env: SWARM_HUB_PORT (default 8003), SWARM_RUNS_DIR, SWARM_ALLOWED_EMAILS (comma list), SWARM_STATIC_DIR,
SWARM_ANNOTATIONS_DIR (default: sibling of the runs dir), SWARM_INGRESS_TOKEN (required for authed routes),
SWARM_RUNNER, SWARM_PACKS_DIR, SWARM_LOGIN_URL (where an anonymous visitor is sent; default is the exe.dev proxy login).
"""
import hmac, json, os, re, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("SWARM_HUB_PORT", "8003"))
RUNS = Path(os.environ.get("SWARM_RUNS_DIR", "runs")).resolve()
STATIC = Path(os.environ.get("SWARM_STATIC_DIR", Path(__file__).parent / "static")).resolve()
ALLOWED = {e.strip().lower() for e in os.environ.get("SWARM_ALLOWED_EMAILS", "").split(",") if e.strip()}
INGRESS = os.environ.get("SWARM_INGRESS_TOKEN", "")  # shared secret nginx injects; blocks local processes forging the identity header
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
RUNNER = Path(os.environ.get("SWARM_RUNNER", Path(__file__).parent.parent / "runner" / "swarm_run.py")).resolve()
PACKS = Path(os.environ.get("SWARM_PACKS_DIR", Path(__file__).resolve().parents[2] / "bench" / "tasks")).resolve()
ANNOTATIONS = Path(os.environ.get("SWARM_ANNOTATIONS_DIR", RUNS.parent / "annotations")).resolve()
LOGIN_URL = os.environ.get("SWARM_LOGIN_URL", "/__exe.dev/login?redirect=%2Fswarm%2F")
MAX_VMS = int(os.environ.get("SWARM_MAX_VMS", "2")); MAX_LOCAL = int(os.environ.get("SWARM_MAX_LOCAL", "32"))
MODELS = {"terra": "openai/gpt-5.6-terra", "luna": "openai/gpt-5.6-luna"}
HARNESSES = ("ante", "pi", "hermes")
USD_IN = float(os.environ.get("SWARM_USD_PER_MTOK_IN", "0") or 0); USD_OUT = float(os.environ.get("SWARM_USD_PER_MTOK_OUT", "0") or 0)
USD_CACHED = float(os.environ.get("SWARM_USD_PER_MTOK_CACHED", "0") or 0); PRICE_SOURCE = os.environ.get("SWARM_PRICE_SOURCE", "")

def shadow_cost(tokens):
    """List-price shadow cost in USD. The actual inference is a subscription; this is what the same tokens would cost at the configured API rate card."""
    if not tokens or not (USD_IN and USD_OUT): return None
    cached = tokens.get("cache_read", 0) or 0; fresh = max(0, (tokens.get("input", 0) or 0) - cached)
    return round((fresh * USD_IN + cached * (USD_CACHED if USD_CACHED else USD_IN) + (tokens.get("output", 0) or 0) * USD_OUT) / 1e6, 4)

def history():
    """Median tokens per passed task, per (harness, pack), from completed runs. Basis for the pre-run estimate."""
    per = {}
    for d in RUNS.iterdir() if RUNS.is_dir() else []:
        rec, meta = d / "receipts.json", d / "meta.json"
        if not (rec.exists() and meta.exists()): continue
        try: r = json.loads(rec.read_text()); m = json.loads(meta.read_text())
        except json.JSONDecodeError: continue
        key = f"{m.get('harness','ante')}:{m.get('pack','toolbox')}"
        for x in r.get("results", []):
            t = x.get("tokens")
            if t and x.get("passed") and isinstance(t.get("input"), int) and isinstance(t.get("output"), int): per.setdefault(key, []).append(t["input"] + t["output"])
    out = {}
    for k, v in per.items():
        v.sort(); out[k] = {"median_tokens_per_task": v[len(v) // 2], "p90_tokens_per_task": v[min(len(v) - 1, int(len(v) * 0.9))], "samples": len(v)}
    return out
ACTIVE = {}  # run_id -> Popen
ALOCK = threading.Lock()

def run_dirs():
    """Run directories in numeric order — a lexicographic sort would put r100 before r99."""
    if not RUNS.is_dir(): return []
    return sorted((d for d in RUNS.iterdir() if d.is_dir() and RUN_ID.match(d.name)),
                  key=lambda d: (0, int(d.name[1:])) if re.fullmatch(r"r\d+", d.name) else (1, d.name))

def next_run_id():
    ids = sorted(int(d.name[1:]) for d in RUNS.iterdir() if d.is_dir() and re.fullmatch(r"r\d{2,4}", d.name)) if RUNS.is_dir() else []
    return f"r{(ids[-1] + 1) if ids else 1:02d}"

def pid_alive(pid):
    try: os.kill(int(pid), 0); return True
    except (ProcessLookupError, ValueError, TypeError): return False
    except PermissionError: return True

def jload(path, default=None):
    """json.loads a file, returning default on missing/corrupt content — a torn write must not 500 a handler."""
    try: return json.loads(path.read_text()) if path.exists() else default
    except (json.JSONDecodeError, OSError): return default

def run_is_live(d):
    if (d / "receipts.json").exists(): return False
    meta = jload(d / "meta.json") or {}
    return pid_alive(meta.get("pid"))

def reap():
    with ALOCK:
        for rid, pr in list(ACTIVE.items()):
            if pr.poll() is not None: del ACTIVE[rid]

CHECK_MOD = re.compile(r"^tests\.check_[a-z0-9_]+$")

def custom_tasks(spec, pack):
    """Owner-written tasks for an ad-hoc run: one prompt per list entry, all on the pack's repo.
    Returns (tasks, error). The optional check must name a checker that already exists inside the
    pack — the cell runs `python3 -m unittest <check>`, so a free-form name would be arbitrary
    module execution."""
    raw = spec.get("prompts")
    if not isinstance(raw, list) or not raw: return None, "custom run needs a non-empty prompts list"
    prompts = []
    for p in raw[:64]:
        p = str(p).strip()
        if not p: return None, "empty prompt in custom task list"
        if len(p) > 4000: return None, "prompt over 4000 chars"
        prompts.append(p)
    if len(raw) > 64: return None, "at most 64 custom tasks"
    check = str(spec.get("check") or "").strip()
    if check:
        if not CHECK_MOD.match(check) or not (PACKS / pack / (check.replace(".", "/") + ".py")).exists():
            return None, "check must be an existing tests.check_* module in the pack"
    try: budget = max(30, min(int(spec.get("budget_s", 600) or 600), 3600))
    except (TypeError, ValueError): return None, "budget_s must be an integer"
    return [{"id": f"x{i+1:02d}", "persona": "custom", "title": p.split("\n", 1)[0][:80],
             "prompt": p, "check": check, "expect_files": [], "budget_s": budget,
             "tags": ["custom"]} for i, p in enumerate(prompts)], None

def start_run(spec):
    reap()
    mode = spec.get("mode", "vm"); pack = re.sub(r"[^a-z0-9-]", "", str(spec.get("pack", "toolbox")))
    try:
        n = int(spec.get("cells", 4)); timeout = max(30, min(int(spec.get("timeout", 300)), 3600)); max_tokens = max(0, int(spec.get("max_tokens", 0) or 0))
    except (TypeError, ValueError): return None, "cells/timeout/max_tokens must be integers"
    effort = spec.get("effort", "medium"); model = MODELS.get(spec.get("model", "terra"), MODELS["terra"])
    harness = spec.get("harness", "ante")
    if harness not in HARNESSES: return None, "unknown harness"
    if mode not in ("vm", "local") or effort not in ("min", "low", "medium", "high"): return None, "bad mode/effort"
    if mode == "vm" and not 1 <= n <= MAX_VMS: return None, f"vm mode allows 1-{MAX_VMS} cells (account headroom)"
    if mode == "local" and not 1 <= n <= MAX_LOCAL: return None, f"local mode allows 1-{MAX_LOCAL} instances"
    if not (PACKS / pack / "tasks.json").exists(): return None, "unknown task pack"
    tasks_file = PACKS / pack / "tasks.json"; gen = None
    if spec.get("custom"):
        tasks, err = custom_tasks(spec, pack)
        if err: return None, err
        if mode == "vm" and n > len(tasks): return None, f"vm mode needs one VM per task; {len(tasks)} tasks given"
        gen = tasks
    with ALOCK:  # check -> claim must be atomic; two POSTs must not spawn two runners into one dir
        live = [d.name for d in RUNS.iterdir() if d.is_dir() and run_is_live(d)] if RUNS.is_dir() else []
        if ACTIVE or live: return None, f"a run is already active ({', '.join(sorted(set(ACTIVE) | set(live)))}); stop it first"
        rid = next_run_id(); (RUNS / rid).mkdir(parents=True, exist_ok=True)
        if gen is not None:
            tasks_file = RUNS / rid / "tasks.json"
            tasks_file.write_text(json.dumps(gen, indent=1))  # kept in the run dir: the receipt includes exactly what was sent
        env = dict(os.environ, SWARM_EFFORT=effort, HOME=os.environ.get("HOME", str(RUNS.parent)))
        cmd = [sys.executable, str(RUNNER), "--pack", str(PACKS / pack), "--tasks", str(tasks_file), "--run-id", rid, "--mode", mode, "--cells", str(n), "--effort", effort, "--runs-dir", str(RUNS), "--timeout", str(timeout), "--model", model, "--harness", harness, "--max-tokens", str(max_tokens)]
        out = (RUNS / rid / "runner.out").open("ab")
        pr = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env, cwd=str(RUNS), start_new_session=True)
        out.close()  # Popen holds its own dup; the parent's copy would leak one fd per run
        ACTIVE[rid] = pr
    return rid, None

def stop_run(rid):
    reap()
    d = RUNS / rid
    if not d.is_dir(): return "no such run"
    if (d / "receipts.json").exists(): return "run already complete"  # a cancel marker on a finished run only confuses the record
    (d / "cancel").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    meta = jload(d / "meta.json") or {}
    pr = ACTIVE.get(rid); pid = pr.pid if pr else meta.get("pid")
    def hard():  # the runner's cancel watcher kills ante on the cells and destroys them; this is the backstop for a hung runner
        time.sleep(120)
        if pid and pid_alive(pid) and not (d / "receipts.json").exists():
            try: os.killpg(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError): pass
    threading.Thread(target=hard, daemon=True).start()
    return None

class H(BaseHTTPRequestHandler):
    server_version = "swarm-hub/0.1"
    def log_message(self, f, *a): sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), f % a))
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store"); self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; img-src 'self' data:")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def _authed(self):
        # Fail closed: without the shared ingress secret, a forged X-ExeDev-Email from any loopback process would authenticate.
        if not INGRESS or not hmac.compare_digest(self.headers.get("X-Swarm-Ingress", ""), INGRESS):
            self._send(403, b"bad ingress", "text/plain; charset=utf-8"); return False
        email = (self.headers.get("X-ExeDev-Email") or "").strip().lower()
        if not email:
            self._send(303, b"", "text/plain", {"Location": LOGIN_URL}); return False
        if not ALLOWED or email not in ALLOWED:
            self._send(403, b"This hub is private. Sign in with the authorized exe.dev account.", "text/plain; charset=utf-8"); return False
        return True
    PUBLIC_FIELDS = ("run_id", "harness", "mode", "cells", "effort", "model", "total_secs", "swarm_secs", "passed", "attempted", "failures", "tokens", "peak_instance_rss_kb", "peak_vm_ante_rss_kb", "peak_ante_procs", "cancelled", "cancel_reason", "checks", "harness_version", "task_revision", "measurement_scope", "matrix_id", "task_count", "model_verified", "peak_process_tree_rss_kb", "protocol_revision", "adapter_sha256", "preparation", "timeout_s")
    PEAKS_CACHE = {}
    def _peaks(self, s):
        """Peak fields from a run's samples.json, cached by (path, mtime) — anonymous callers must not re-parse MBs per hit."""
        try: key = (str(s), s.stat().st_mtime)
        except OSError: return {}
        if key not in self.PEAKS_CACHE:
            sm = jload(s) or []
            def peak_nonneg(field):
                vals = [x.get(field) for x in sm if isinstance(x, dict) and isinstance(x.get(field), (int, float)) and not isinstance(x.get(field), bool) and x.get(field) >= 0]
                return max(vals) if vals else None
            self.PEAKS_CACHE[key] = {"peak_vm_used_kb": peak_nonneg("vm_used_kb"),
                                     "peak_load1": peak_nonneg("load1"),
                                     "peak_ante_rss_kb": peak_nonneg("ante_rss_kb")}
            if len(self.PEAKS_CACHE) > 512: self.PEAKS_CACHE.clear()
        return self.PEAKS_CACHE[key]
    def _public(self, path):
        """Read-only, unauthenticated view of completed runs for the public site. No logs, no controls, summary fields only."""
        cors = {"Access-Control-Allow-Origin": "*"}
        if path == "/public/runs":
            runs = []
            for d in run_dirs():
                rec, meta = d / "receipts.json", d / "meta.json"
                if not rec.exists(): continue
                r = jload(rec); m = jload(meta) or {}
                if not isinstance(r, dict): continue
                row = {k: r.get(k) for k in self.PUBLIC_FIELDS}; row["id"] = d.name; row["started"] = m.get("started"); row["pack"] = m.get("pack")
                if r.get("matrix_id"): row["rss_measurements"] = sum(x.get("peak_rss_kb") is not None for x in r.get("results", []))
                row["shadow_cost_usd"] = None if r.get("matrix_id") else shadow_cost(r.get("tokens")); row["date"] = time.strftime("%Y-%m-%d", time.gmtime(d.stat().st_mtime))
                pk = self._peaks(d / "samples.json")
                row["peak_vm_used_kb"] = pk.get("peak_vm_used_kb") if pk.get("peak_vm_used_kb") is not None else r.get("peak_vm_used_kb")
                row["peak_load1"] = pk.get("peak_load1") if pk.get("peak_load1") is not None else r.get("peak_load1")
                if row.get("peak_vm_ante_rss_kb") is None or (isinstance(row.get("peak_vm_ante_rss_kb"), (int, float)) and row["peak_vm_ante_rss_kb"] < 0):
                    row["peak_vm_ante_rss_kb"] = pk.get("peak_ante_rss_kb")
                runs.append(row)
            live = [d.name for d in RUNS.iterdir() if d.is_dir() and RUN_ID.match(d.name) and run_is_live(d)] if RUNS.is_dir() else []
            return self._send(200, json.dumps({"runs": runs, "live": live, "price_source": PRICE_SOURCE, "usd_per_mtok": {"input": USD_IN or None, "cached_input": USD_CACHED or None, "output": USD_OUT or None}}).encode(), extra=cors)
        m = re.match(r"^/public/runs/([a-z0-9-]+)$", path)
        if m and RUN_ID.match(m.group(1)):
            d = RUNS / m.group(1); out = {"id": d.name}
            for name in ("receipts", "samples"):
                v = jload(d / f"{name}.json")
                if v is not None: out[name] = v
            m = jload(d / "meta.json")
            if isinstance(m, dict):
                out["meta"] = {k: m.get(k) for k in ("run_id", "mode", "cells", "effort", "model", "harness", "max_tokens", "pack", "started")}
            if not isinstance(out.get("receipts"), dict): return self._send(404, b'{"error":"not a completed run"}', extra=cors)
            out["receipts"]["shadow_cost_usd"] = None if out["receipts"].get("matrix_id") else shadow_cost(out["receipts"].get("tokens"))
            for x in out["receipts"].get("results", []) + out["receipts"].get("superseded_attempts", []):
                x.pop("host", None)
                for audit in x.get("request_audit", []):
                    if isinstance(audit.get("usage"), dict): audit["usage"].pop("attribution", None)
            out["live"] = run_is_live(d)
            return self._send(200, json.dumps(out).encode(), extra=cors)
        if m := re.match(r"^/public/live/([a-z0-9-]+)$", path):
            d = RUNS / m.group(1)
            if not (RUN_ID.match(m.group(1)) and d.is_dir()): return self._send(404, b'{}', extra=cors)
            sm = d / "samples.jsonl"; lines = [l for l in sm.read_text().splitlines() if l.strip()] if sm.exists() else []
            samples = []
            for l in lines[-120:]:
                try: samples.append(json.loads(l))
                except json.JSONDecodeError: pass
            return self._send(200, json.dumps({"id": d.name, "live": run_is_live(d), "samples": samples}).encode(), extra=cors)
        self._send(404, b'{"error":"not found"}', extra=cors)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        path = re.sub(r"^/swarm", "", path) or "/"
        if path == "/healthz": return self._send(200, b'{"ok":true}')
        if path.startswith("/public/"): return self._public(path)
        if not self._authed(): return
        if path in ("/", "/index.html", "/app.js"):
            name = "index.html" if path != "/app.js" else "app.js"
            ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/javascript; charset=utf-8"
            try: return self._send(200, (STATIC / name).read_bytes(), ctype)
            except OSError: return self._send(404, b'{"error":"not found"}')
        if path == "/api/runs":
            runs = []
            for d in run_dirs():
                rec = d / "receipts.json"; live = run_is_live(d)
                meta = jload(d / "meta.json") or {}
                runs.append({"id": d.name, "complete": rec.exists(), "live": live, "aborted": not live and not rec.exists(), "mtime": int(d.stat().st_mtime), "mode": meta.get("mode", "vm"), "cells": meta.get("cells"), "model": meta.get("model"), "harness": meta.get("harness", "ante"), "cancelled": (d / "cancel").exists()})
            return self._send(200, json.dumps(runs).encode())
        m = re.match(r"^/api/runs/([a-z0-9-]+)/events$", path)
        if m and RUN_ID.match(m.group(1)):
            d = RUNS / m.group(1)
            if not d.is_dir(): return self._send(404, b'{"error":"no such run"}')
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv) if "?" in self.path else {}
            try: ea = max(0, int(q.get("after", 0) or 0))
            except ValueError: ea = 0
            f = d / "events.jsonl"; lines = [l for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
            out = []
            for l in lines[ea:ea + 200]:
                try: out.append(json.loads(l))
                except json.JSONDecodeError: pass
            return self._send(200, json.dumps({"id": d.name, "events": out, "total": len(lines), "after": ea, "live": run_is_live(d)}).encode())
        m = re.match(r"^/api/runs/([a-z0-9-]+)$", path)
        if m and RUN_ID.match(m.group(1)):
            d = RUNS / m.group(1)
            if not d.is_dir(): return self._send(404, b'{"error":"no such run"}')
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv) if "?" in self.path else {}
            try: la, sa = int(q.get("log_after", 0) or 0), int(q.get("samples_after", 0) or 0)
            except ValueError: la = sa = 0
            out = {"id": d.name, "live": run_is_live(d)}
            meta = jload(d / "meta.json")
            if meta is not None: out["meta"] = meta
            for name in ("receipts", "samples", "log"):
                v = jload(d / f"{name}.json")
                if v is not None: out[name] = v
            if out.get("receipts"):
                rc = out["receipts"]; rc["shadow_cost_usd"] = shadow_cost(rc.get("tokens")); rc["price_source"] = PRICE_SOURCE
                for x in rc.get("results", []): x["shadow_cost_usd"] = shadow_cost(x.get("tokens"))
            def tail_lines(f, after):
                raw = [l for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
                rows = []
                for l in raw[after:]:
                    try: rows.append(json.loads(l))
                    except json.JSONDecodeError: pass
                return rows, len(raw)
            jl = d / "log.jsonl"
            if jl.exists() and (la or "log" not in out or out["live"]):
                rows, tot = tail_lines(jl, la); out["log"] = rows; out["log_total"] = tot; out["log_after"] = la
            sm = d / "samples.jsonl"
            if sm.exists() and (sa or "samples" not in out or out["live"]):
                rows, tot = tail_lines(sm, sa); out["samples"] = rows; out["samples_total"] = tot; out["samples_after"] = sa
            return self._send(200, json.dumps(out).encode())
        if path == "/healthz": return self._send(200, b'{"ok":true}')
        if path == "/api/status":
            reap(); packs = sorted(d.name for d in PACKS.iterdir() if (d / "tasks.json").exists()) if PACKS.is_dir() else []
            checks = {p: sorted("tests." + f.stem for f in (PACKS / p / "tests").glob("check_*.py")) for p in packs}
            return self._send(200, json.dumps({"active": sorted(ACTIVE), "max_vms": MAX_VMS, "max_local": MAX_LOCAL, "packs": packs, "checks": checks, "models": sorted(MODELS), "harnesses": list(HARNESSES), "history": history(), "usd_per_mtok": {"input": USD_IN or None, "cached_input": USD_CACHED or None, "output": USD_OUT or None, "source": PRICE_SOURCE}}).encode())
        self._send(404, b'{"error":"not found"}')
    def do_POST(self):
        path = re.sub(r"^/swarm", "", self.path.split("?", 1)[0]) or "/"
        if not self._authed(): return
        if self.headers.get("X-Swarm-Action") != "1": return self._send(403, b'{"error":"missing action header"}')
        try: n = int(self.headers.get("Content-Length") or 0)
        except ValueError: return self._send(400, b'{"error":"bad content-length"}')
        if n < 0: return self._send(400, b'{"error":"bad content-length"}')
        limit = 262144 if path == "/api/annotations" else 65536  # annotation payloads carry markdown + element context
        if n > limit: return self._send(413, b'{"error":"body too large"}')
        try: body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except json.JSONDecodeError: return self._send(400, b'{"error":"bad json"}')
        if not isinstance(body, dict):
            if path == "/api/annotations": return self._send(400, b'{"error":"expected a JSON object"}')
            body = {}
        if path == "/api/annotations":
            # Sink for Agentation (site/agentation): one JSON file per POST, read later by a local agent.
            anns = body.get("annotations")
            if isinstance(anns, list) and len(anns) > 256: body["annotations"] = anns[:256]  # one file must not grow without bound
            ANNOTATIONS.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(4).hex() + ".json"
            try: (ANNOTATIONS / name).write_text(json.dumps({"received": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "payload": body}, indent=1))
            except OSError: return self._send(500, b'{"error":"could not store annotation"}')
            return self._send(201, json.dumps({"stored": name}).encode())
        if path == "/api/swarms":
            rid, err = start_run(body)
            if err: return self._send(409, json.dumps({"error": err}).encode())
            return self._send(201, json.dumps({"id": rid}).encode())
        m = re.match(r"^/api/swarms/([a-z0-9-]+)/cancel$", path)
        if m and RUN_ID.match(m.group(1)):
            err = stop_run(m.group(1))
            return self._send(404 if err else 200, json.dumps({"error": err} if err else {"cancelled": m.group(1)}).encode())
        m = re.match(r"^/api/swarms/([a-z0-9-]+)/kill$", path)
        if m and RUN_ID.match(m.group(1)):
            label = re.sub(r"[^A-Za-z0-9_/-]", "", str(body.get("label", "")))[:60]
            d = RUNS / m.group(1)
            if not label or not d.is_dir() or not run_is_live(d): return self._send(404, b'{"error":"no live run/instance"}')
            (d / f"kill-{label.replace('/', '__')}").write_text(time.strftime("%H:%M:%SZ", time.gmtime()))
            return self._send(200, json.dumps({"kill": label}).encode())
        self._send(404, b'{"error":"not found"}')

if __name__ == "__main__":
    if not ALLOWED: sys.exit("SWARM_ALLOWED_EMAILS is required")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
