"""swarm serve: a small public run service for live swarms on preset use cases.

Visitors pick a preset use case and a size from fixed lists; nothing they send
reaches a shell or a file path. Runs execute one at a time from a short queue,
with the scripted model only (no LLM spend, no network for agents), and each
run is a normal sealed, verified bundle. Progress streams as server-sent events.

Access: when SWARM_INGRESS_SECRET is set, every /api request except /api/healthz
must carry it in X-Swarm-Ingress (the public proxy injects it); otherwise only
loopback clients are served. The process sandbox is not a security boundary:
that is why visitors can choose presets and sizes, never tasks or commands.
"""
import hashlib
import hmac
import json
import os
import platform
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from datetime import datetime, timezone

from . import VERSION
from . import harnesses as harnesses_registry
from . import catalog, host
from . import export as run_export
from . import gate as lane_gate
from . import reset as lane_reset
from .bundle import verify
from .live import ATTEMPT, Tailer, clean, failure_detail, lifecycle, rss_mb, slot_of, transcript
from .plan import freeze
from .tasks import load_task
from .runner import run as run_plan
from .summary import quantile
from .util import canonical, read_jsonl, sha256_bytes, sha256_file, utc_now, write_json

MAX_BODY = 2048
SCRIPTED_ID = "swarm-scripted-1"
NAMES = {"ante": "Ante", "codex": "Codex CLI", "claude-code": "Claude Code", "nanocode": "nanocode", "hermes": "Hermes", "pi": "Pi"}
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
DEFAULT_HARNESS = "ante"
MAINTENANCE_FILE = "MAINTENANCE"
# What the durable ledger keeps of a finished run. Nothing about the visitor is ever stored here.
LEDGER_FIELDS = ("id", "preset", "title", "size", "harness", "created", "started", "finished", "state", "verify", "chain_head",
                 "plan_sha256", "harness_version", "cohort", "refused", "series")
LEDGER_RESULT = ("passes", "attempts", "max_running", "successes_per_minute", "wall_s_median", "peak_rss_mb_median",
                 "cpu_s_per_success", "admission_wait_s_total")
GATE_MODES = ("off", "observe", "enforce")
CONTROL_ORDERS = ("control_first", "control_last")
CONTROL_SEED = 1  # the control's plan is the same on every lane and every run
# Per-attempt figures the control keeps; each has its median in control.median.
CONTROL_VALUES = ("first_request_s", "step_gap_s", "cpu_s", "wall_s", "rss_mb", "setup_s", "request_bytes_first")
CONTROL_NOISY = 0.20  # IQR / median of the control's step gap above this: the lane was not steady enough to normalise by


CANARY_PARTS = ("tools_sha256", "system_sha256", "injected_sha256", "params_sha256")
FIRST_MEDIANS = ("first_request_bytes_median", "first_request_system_bytes_median", "first_request_tools_bytes_median",
                 "first_request_user_bytes_median", "first_request_instruction_bytes_median", "first_request_injected_bytes_median",
                 "first_request_other_bytes_median", "first_request_tool_count_median", "canary_sha256", "canary_distinct")


def lane_peaks(series: list, rss: list) -> tuple:
    """(agents_mem_mb_peak, mem_per_agent_mb) exactly as front/rank.js Rank.peaks: series rows are
    [t, running, agents_mem_mb, cpu_pct, available_mb]; rss holds each attempt's largest one-process RSS."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    one = max((v for v in rss if num(v)), default=None)
    sampled = max((x[2] for x in series if num(x[2])), default=None)
    per, top, mem = None, 0, 0
    for x in series:
        if num(x[1]) and x[1] > 0 and num(x[2]) and (x[1] > top or (x[1] == top and x[2] > mem)):
            top, mem, per = x[1], x[2], x[2] / x[1]
    peak = None if sampled is None and one is None else max(sampled or 0, one or 0)
    return peak, per


def adapter_sha256(harness: str) -> str | None:
    """12 hex over the harness's adapter file and harnesses/base.py: a change to either can move the numbers."""
    try:
        cls = harnesses_registry.REGISTRY[harness]
        files = [Path(sys.modules[cls.__module__].__file__), Path(harnesses_registry.base.__file__)]
        return sha256_bytes(b"".join(f.read_bytes() for f in files))[:12]
    except (KeyError, OSError, AttributeError):
        return None


def canary_of(attempts: list[dict]) -> dict | None:
    """The run's canary from its attempts' first requests; changed/parts_changed/binary_changed are filled by the store.
    Not stable (attempts disagree) means no sha and no verdict; None when no attempt reported one."""
    found = [((a.get("requests") or {}).get("first_request") or {}) for a in attempts]
    found = [f for f in found if (f.get("canary") or {}).get("sha256")]
    if not found:
        return None
    shas = {f["canary"]["sha256"] for f in found}
    out = {"sha256": None, "parts": None, "stable": len(shas) == 1, "distinct": len(shas), "changed": None,
           "parts_changed": [], "binary_changed": None, "wire": found[0].get("wire")}
    if len(shas) == 1:
        c = found[0]["canary"]
        out.update(sha256=c["sha256"], parts={k: c.get(k) for k in CANARY_PARTS})
    return out


def first_request_public(a: dict) -> dict | None:
    """Sizes, tool count and canary sha of an attempt's first request; no tool names or text."""
    first = (a.get("requests") or {}).get("first_request")
    if not isinstance(first, dict):
        return None
    keys = ("wire", "canonical_bytes", "system_bytes", "tools_bytes", "user_bytes", "instruction_bytes", "injected_bytes",
            "other_bytes", "tool_count", "mcp_tool_count", "request_bytes")
    return {k: first.get(k) for k in keys} | {"canary_sha256": (first.get("canary") or {}).get("sha256")}


def read_json_list(path: Path) -> list:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return doc if isinstance(doc, list) else []


def valid_series(series) -> dict | None:
    """{race: 12 hex, rep: 1..of, of: 1..64} exactly, or None."""
    if not isinstance(series, dict) or set(series) != {"race", "rep", "of"}:
        return None
    race, rep, of = series["race"], series["rep"], series["of"]
    if not (isinstance(race, str) and RUN_ID.match(race)):
        return None
    if not all(type(x) is int for x in (rep, of)) or not 1 <= rep <= of <= 64:
        return None
    return {"race": race, "rep": rep, "of": of}


class Service:
    def __init__(self, presets_path: Path, data: Path, max_queue=5, per_client_hour=3, keep=40,
                 telemetry="on", telemetry_env="swarm-bench-live", attempt_timeout_s=120, binary=None, harness="ante",
                 passkey=None, passkey_bonus=5, passkey_tries_hour=10, binaries=None, harness_options=None, max_size=None, live=None,
                 reset=True, reset_min_free_mb=0, reset_tolerance_mb=512, reset_settle_s=10.0, reset_retry_s=60.0, reset_tmp=None,
                 reserve_disk_mb=1024, gate="off", gate_limits=None, control=False, gate_readers=None):
        self.presets_path = Path(presets_path).resolve()
        doc = json.loads(self.presets_path.read_text())
        base = self.presets_path.parent
        self.presets = {}
        for p in doc["presets"]:
            tasks = [str((base / t).resolve()) for t in p["tasks"]]
            sizes = sorted({int(s) for s in p["sizes"] if 1 <= int(s) <= 1024})
            self.presets[p["id"]] = dict(p, tasks=tasks, sizes=sizes)
        self.data = Path(data).resolve()
        (self.data / "runs").mkdir(parents=True, exist_ok=True)
        self.max_queue, self.per_client_hour, self.keep = max_queue, per_client_hour, keep
        self.max_size = max_size  # the most agents this machine holds; larger launches are refused
        # Real models through the relay: {models: {id: {upstream, key_env}}, max_size, max_tokens, effort}; a plain list
        # of ids uses live["upstream"]. None means scripted only.
        if live and isinstance(live.get("models"), list):
            live = dict(live, models={m: {"upstream": live.get("upstream"), "key_env": None} for m in live["models"]})
        if live:
            live = dict(live, models={m: v for m, v in (live.get("models") or {}).items() if v.get("upstream")})
        self.live = live if live and live["models"] else None
        # A shared passkey raises one client's hourly cap by passkey_bonus; wrong guesses have their own cap.
        self.passkey, self.passkey_bonus, self.passkey_tries_hour = passkey or None, passkey_bonus, passkey_tries_hour
        self.misses: dict[str, list[float]] = {}
        self.tailers: dict[str, Tailer] = {}      # live console for the run in progress
        self.consoles: dict[str, list[dict]] = {}  # small cache of finished runs' consoles
        self.telemetry, self.telemetry_env, self.attempt_timeout_s = telemetry, telemetry_env, attempt_timeout_s
        self.binary, self.harness = binary, harness
        # Options other harnesses must state (e.g. hermes home/seed); Ante's come from telemetry above.
        self.harness_options = {k: dict(v) for k, v in (harness_options or {}).items()}
        self.maintenance_poll_s = 2.0
        # Harness binaries by name; "binary" is the older way to say where ante is. A preset's "harnesses" list
        # says which harnesses a visitor may pick; only those whose binary is found on this machine are offered.
        self.binaries = dict(binaries or {})
        if binary:
            self.binaries.setdefault(harness, binary)
        self.available = {name for name in harnesses_registry.REGISTRY if name in self._wanted()
                          and harnesses_registry.get(name).locate(self.binaries.get(name)) is not None}
        self.available.add(harness)  # the service's own default is always offered, as before
        for p in self.presets.values():
            p["harnesses"] = [h for h in p.get("harnesses") or [harness] if h in self.available]
            if harness not in p["harnesses"]:
                p["harnesses"].insert(0, harness)
        self.lock = threading.Lock()
        self.q: queue.Queue[str] = queue.Queue()
        self.order: list[str] = []          # queued run ids, in order
        self.current: str | None = None
        self.recent: dict[str, list[float]] = {}  # client hash -> launch times
        # The lane is reset after every run and once here, so a run never starts on what a crash or the last run left.
        # lane_state: ready | failed. While failed, launches are refused and a retry runs every reset_retry_s.
        self.reset_on, self.reset_min_free_mb, self.reset_tolerance_mb = reset, reset_min_free_mb, reset_tolerance_mb
        self.reset_settle_s, self.reset_retry_s, self.reset_tmp = reset_settle_s, reset_retry_s, reset_tmp
        self.reserve_disk_mb = int(reserve_disk_mb)
        self.reset_lock = threading.Lock()   # one reset at a time
        self.state_lock = threading.Lock()   # lane_state / last_reset; never held while a reset runs
        self.busy_lock = threading.Lock()    # held by the worker for a run and its reset: no other reset may overlap one
        self.closing = threading.Event()
        self.lane_state, self.last_reset, self.baseline_available_mb = "ready", None, None
        # Quiet gate before the control and before the run (gate.py), and the lane control (preset kind "control").
        # Both are off by default so a library caller and macOS keep the old flow; `swarm serve` turns them on.
        if gate not in GATE_MODES:
            raise ValueError(f"gate must be one of {GATE_MODES}")
        self.gate_mode, self.control_on, self.gate_readers = gate, bool(control), gate_readers
        self.gate_limits = dict(lane_gate.LIMITS, **(gate_limits or {}))
        self.last_gate, self.last_control = None, None
        if reset:
            self.reset_lane(None)
            # What "recovered" means for later resets: available memory with the lane clean and nothing running.
            self.baseline_available_mb = host.memory_state()["available_mb"] if lane_reset.HAS_PROC else None
        self.ledger_lock = threading.Lock()
        self.ledger_path = self.data / "ledger.jsonl"
        self._backfill_ledger()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    def _wanted(self) -> set[str]:
        return {h for p in self.presets.values() for h in (p.get("harnesses") or [])}

    def close(self, timeout=60):
        """Stop taking work; wait for the run in progress to seal its bundle."""
        self.closing.set()
        self.q.put(None)
        self.worker.join(timeout)

    # ---- lane reset ----
    def reset_lane(self, rid: str | None, name: str = "reset.json") -> dict:
        """Clean the machine and check it (see reset.py). One at a time; the outcome decides whether launches are taken."""
        with self.reset_lock:
            try:
                record = lane_reset.reset(self.data, self.reset_min_free_mb, self.baseline_available_mb, self.reset_tolerance_mb,
                                          self.reset_settle_s, tmp=self.reset_tmp, run=rid, own=True)
            except Exception as error:  # noqa: BLE001 - a reset that cannot run is a failed reset, not a dead service
                record = {"at": utc_now(), "t": time.time(), "ok": False, "problems": ["reset_error"], "run": rid, "error": type(error).__name__}
            with self.state_lock:
                self.last_reset = record
                was, self.lane_state = self.lane_state, "ready" if record["ok"] else "failed"
        if rid and (self.run_dir(rid) / "run.json").exists():
            write_json(self.run_dir(rid) / name, record)
        if not record["ok"] and was != "failed":
            threading.Thread(target=self._reset_retry, daemon=True).start()
        return record

    def _reset_retry(self):
        """A failed reset is tried again until the lane is clean: memory may simply need longer to come back."""
        while not self.closing.wait(self.reset_retry_s):
            if self.lane_clean():
                return
            # Only between runs: the worker holds busy_lock from a run's start to the end of its own reset.
            if not self.busy_lock.acquire(blocking=False):
                continue
            try:
                if self.reset_lane(None)["ok"]:
                    return
            finally:
                self.busy_lock.release()

    def lane_clean(self) -> bool:
        """True when launches may be taken. A reset run by hand (`swarm lane-reset`) that succeeded also clears a failure."""
        if self.lane_state == "ready":
            return True
        by_hand = lane_reset.last(self.data)
        with self.state_lock:
            if self.lane_state == "ready":
                return True
            if by_hand and by_hand.get("ok") and by_hand.get("t", 0) > (self.last_reset or {}).get("t", 0):
                self.last_reset, self.lane_state = by_hand, "ready"
                # The operator vouched for the machine as it is now: that is the level later resets must return to.
                if lane_reset.HAS_PROC:
                    self.baseline_available_mb = host.memory_state()["available_mb"]
                return True
        return False

    @staticmethod
    def public_reset(record: dict | None) -> dict | None:
        """What a visitor may see of a reset: counts and figures, no paths."""
        if not record:
            return None
        keys = ("at", "ok", "problems", "sandbox_roots_removed", "sandbox_roots_failed", "processes_killed", "took_s")
        out = {k: record.get(k) for k in keys}
        for when in ("before", "after"):
            out[when] = {k: (record.get(when) or {}).get(k) for k in ("disk_free_mb", "available_mb", "agent_processes", "sandbox_roots")}
        return out

    # ---- records ----
    def run_dir(self, rid: str) -> Path:
        return self.data / "runs" / rid

    def record(self, rid: str) -> dict | None:
        path = self.run_dir(rid) / "run.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, rid: str, rec: dict):
        write_json(self.run_dir(rid) / "run.json", rec)

    def public_presets(self):
        keys = ("id", "title", "blurb", "sizes", "harnesses", "kind", "protocols", "primary_outcome")
        return [{k: p[k] for k in keys if k in p} | {"tasks": [Path(t).name for t in p["tasks"]]}
                for p in self.presets.values() if p.get("kind") not in ("baseline", "control")]

    # ---- maintenance: while <data>/MAINTENANCE exists, the machine belongs to a benchmark ----
    def maintenance(self) -> dict | None:
        """None, or {"until": ISO time or None, "minutes": whole minutes left or None}. The file may hold an ISO
        time (or JSON {"until": ...}); an empty or unreadable one still counts as maintenance, with no end time."""
        path = self.data / MAINTENANCE_FILE
        if not path.exists():
            return None
        until = None
        try:
            text = path.read_text().strip()
            if text.startswith("{"):
                text = str(json.loads(text).get("until") or "")
            if text:
                until = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, AttributeError):
            until = None
        if until is None:
            return {"until": None, "minutes": None}
        left = (until - datetime.now(timezone.utc)).total_seconds()
        return {"until": until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "minutes": max(1, int(left // 60) + 1) if left > 0 else 0}

    # ---- launching ----
    def run_tokens(self, size: int) -> int:
        """The run's token budget: per agent when the lane sets one, so a 256-agent run is not starved by a budget
        sized for 8 and its failures stay the harness's own. Otherwise the lane's fixed run budget."""
        per_agent = int(self.live.get("agent_tokens") or 0)
        return per_agent * max(1, int(size)) if per_agent else int(self.live["max_tokens"])

    def launch(self, preset: str, size, client: str, passkey: str | None = None, harness: str | None = None,
               model: str | None = None, effort: str | None = None, protocol: str | None = None,
               meter: bool = True, series=None, control_order=None) -> tuple[int, dict]:
        maintenance = self.maintenance()
        if maintenance:
            return 503, {"error": "benchmark in progress", "until": maintenance["until"], "minutes": maintenance["minutes"]}
        if not self.lane_clean():
            # Never start a run on a machine the last run left dirty: its numbers would carry that run's leftovers.
            return 503, {"error": "lane is not clean: the reset after the last run failed", "lane": "reset_failed",
                         "problems": (self.last_reset or {}).get("problems", []), "retry_after_s": int(self.reset_retry_s)}
        p = self.presets.get(preset)
        if p is None:
            return 400, {"error": "unknown preset"}
        if series is not None and valid_series(series) is None:
            return 400, {"error": "series must be {race: 12 hex, rep: 1..of, of: up to 64}"}
        if control_order is not None and control_order not in CONTROL_ORDERS:
            return 400, {"error": f"control_order must be one of {list(CONTROL_ORDERS)}"}
        if harness is None:
            harness = self.harness
        if not isinstance(harness, str) or harness not in p["harnesses"]:
            return 400, {"error": f"harness must be one of {p['harnesses']}"}
        allowed = p.get("protocols") or ["naive"]
        protocol = protocol or "naive"
        if protocol not in allowed:
            return 400, {"error": f"protocol must be one of {allowed}"}
        try:
            size = int(size)
        except (TypeError, ValueError):
            return 400, {"error": "size must be a number"}
        if size not in p["sizes"]:
            return 400, {"error": f"size must be one of {p['sizes']}"}
        if self.max_size and size > self.max_size:
            return 400, {"error": f"this lane runs at most {self.max_size} agents"}
        if model in (None, "", "scripted", SCRIPTED_ID):
            model = None
        elif not self.live or model not in self.live["models"]:
            return 400, {"error": f"model must be scripted or one of {sorted(self.live['models']) if self.live else []}"}
        elif p.get("kind", "work") != "work":
            return 400, {"error": "real models run work tasks; harness tests are scripted only"}
        elif self.live.get("max_size") and size > self.live["max_size"]:  # unset on the public lanes: no size cap
            return 400, {"error": f"real-model runs are capped at {self.live['max_size']} agents"}
        elif catalog.wire_of(model) and catalog.wire_of(model) not in catalog.HARNESS_WIRES.get(harness, ("responses",)):
            return 400, {"error": f"{NAMES.get(harness, harness)} cannot speak {catalog.wire_of(model)} for {model}"}
        if model and effort is None:
            effort = (catalog.BY_ID.get(model) or {}).get("default_effort") or self.live["effort"]
        if model and effort not in ((catalog.BY_ID.get(model) or {}).get("efforts") or [self.live["effort"]]):
            return 400, {"error": f"effort for {model} must be one of {(catalog.BY_ID.get(model) or {}).get('efforts') or [self.live['effort']]}"}
        key = hashlib.sha256(client.encode()).hexdigest()[:16]
        now = time.time()
        with self.lock:
            cap = self.per_client_hour
            if passkey:
                misses = [t for t in self.misses.get(key, []) if now - t < 3600]
                if len(misses) >= self.passkey_tries_hour:
                    return 429, {"error": "too many passkey attempts", "retry_after_s": int(3600 - (now - misses[0]))}
                if not (self.passkey and hmac.compare_digest(passkey.encode()[:128], self.passkey.encode())):
                    self.misses[key] = misses + [now]
                    return 403, {"error": "wrong passkey"}
                cap += self.passkey_bonus
            times = [t for t in self.recent.get(key, []) if now - t < 3600]
            if meter and len(times) >= cap:
                return 429, {"error": "rate limited", "retry_after_s": int(3600 - (now - times[-cap])),
                             "passkey": bool(self.passkey) and not passkey}
            if len(self.order) >= self.max_queue:
                return 503, {"error": "queue full, try again shortly"}
            rid = secrets.token_hex(6)
            self.run_dir(rid).mkdir(parents=True)
            repeats = 5 if p.get("kind") == "harness" and int(size) == 1 else 1
            rec = {"id": rid, "preset": preset, "title": p["title"], "size": size, "harness": harness,
                   "protocol": protocol, "repeats": repeats, "state": "queued", "created": utc_now()}
            if model:
                rec["model"], rec["effort"] = model, effort
            if series is not None:
                rec["series"] = valid_series(series)
            rec["control_order"] = control_order or "control_first"
            self.save(rid, rec)
            if meter:
                self.recent[key] = times + [now]
            self.order.append(rid)
            position = len(self.order) + (1 if self.current else 0)
        self.q.put(rid)
        return 202, {"id": rid, "position": position}

    def position(self, rid: str) -> int | None:
        with self.lock:
            if rid == self.current:
                return 0
            return self.order.index(rid) + 1 + (1 if self.current else 0) if rid in self.order else None

    # ---- the worker: one run at a time ----
    def _worker(self):
        while True:
            rid = self.q.get()
            if rid is None:
                return
            # A queued run waits out a benchmark; the run already in progress never does (it finished first).
            while self.maintenance():
                time.sleep(self.maintenance_poll_s)
            with self.lock:
                if rid in self.order:
                    self.order.remove(rid)
                self.current = rid
            self._work(rid)

    def _work(self, rid: str):
        """One queued run: gate, control, reset, gate, run, reset (control_last: gate, run, reset, gate, control,
        reset), all under busy_lock so no other run or reset can come between a run and its control. Refuse it on a
        dirty or (enforce) not-quiet lane. The terminal state and ledger line are written here, after the control."""
        ran = False
        self.busy_lock.acquire()
        try:
            rec = self.record(rid) or {"id": rid}
            rec["settings"] = self.settings()
            self.save(rid, rec)
            # A run accepted before a reset failed still must not start on a dirty lane: one more try, then refuse.
            if self.reset_on and not self.lane_clean() and not self.reset_lane(None)["ok"]:
                self._refuse(rid, "lane_not_clean", "lane not clean: " + ", ".join(self.last_reset.get("problems", [])))
            else:
                ran = True
                self._flow(rid)
        except Exception as error:  # noqa: BLE001 - recorded on the run, the service keeps going
            rec = self.record(rid) or {"id": rid}
            rec.update(state="failed", error=f"{type(error).__name__}: {error}"[:300], finished=utc_now())
            if (rec.get("control") or {}).get("state") == "pending":
                rec["control"] = dict(rec["control"], state="skipped", reason="run_error")  # never left "in progress"
            self.save(rid, rec)
            self._ledger_append(rec)
        finally:
            # Still "current" while the lane is put back, so the next launch queues behind the reset.
            try:
                if ran and self.reset_on:
                    self.reset_lane(rid)
            finally:
                self.busy_lock.release()
                with self.lock:
                    self.current = None
                self._prune()

    def settings(self) -> dict:
        """What this lane measures with: recorded in every run, since each changes what the numbers mean."""
        keys = ("max_swap_mb", "max_cpu_cores", "quiet_s", "wait_s", "allow_unknown")
        return {"gate": self.gate_mode, "gate_limits": {k: self.gate_limits.get(k) for k in keys} if self.gate_mode != "off" else None,
                "control": self.control_on, "reset": self.reset_on}

    def _refuse(self, rid: str, why: str, error: str):
        rec = self.record(rid) or {"id": rid}
        rec.update(state="failed", refused=why, finished=utc_now(), error=error)
        self.save(rid, rec)
        self._ledger_append(rec)

    def _flow(self, rid: str):
        rec = self.record(rid)
        order = rec.get("control_order") or "control_first"
        with_control = self._control_preset(rec) is not None
        if not with_control:
            rec["control"] = {"state": "off", "reason": "not_configured" if self.control_on else "off"}
            self.save(rid, rec)
            if self._gate(rid, "run"):
                self._execute(rid)
                self._finish(rid)
            return
        rec["control"] = {"state": "pending", "order": order}
        self.save(rid, rec)
        if order == "control_first":
            if not self._gate(rid, "control"):
                return
            self._control(rid)
            # The lane is put back between the control and the run, and checked again before the run.
            if self.reset_on and not self.reset_lane(rid, "reset-control.json")["ok"]:
                return self._refuse(rid, "lane_not_clean", "lane not clean after the control: " + ", ".join(self.last_reset.get("problems", [])))
            if self._gate(rid, "run"):
                self._execute(rid)
                self._finish(rid)
        else:
            if not self._gate(rid, "run"):
                return
            self._execute(rid)
            if self.reset_on and not self.reset_lane(rid, "reset-run.json")["ok"]:
                self._skip_control(rid, "lane_not_clean")
            elif self._gate(rid, "control", refuse=False):
                self._control(rid)
            else:
                self._skip_control(rid, "not_quiet")
            self._finish(rid)

    def _finish(self, rid: str):
        """The run's terminal state: only once its control (either order) is recorded."""
        rec = self.record(rid)
        control = rec.get("control") or {}
        if control.get("state") == "failed":
            rec.setdefault("warnings", []).append("control_failed")
        if control.get("noisy"):
            rec.setdefault("warnings", []).append("control_noisy")
        rec.update(state="done", finished=utc_now(), cohort=self.cohort(rec))
        self.save(rid, rec)
        self._ledger_append(rec)

    # ---- the quiet gate ----
    def _gate(self, rid: str, stage: str, refuse=True) -> bool:
        """True when the stage may go ahead. off: no check. observe: checked and recorded, never refused. enforce: a
        lane that stays busy for wait_s refuses the run (or, refuse=False, tells the caller to skip the stage)."""
        if self.gate_mode == "off":
            return True
        rec = self.record(rid)
        rec["state"] = "checking"
        self.save(rid, rec)
        lim = lane_gate.limits(self.reset_tolerance_mb, max(self.reset_min_free_mb, self.reserve_disk_mb), **self.gate_limits)
        if self.gate_mode == "observe":
            # Observe changes nothing about the run: one reading, no holding, no extra reset. It is the data the
            # enforce limits will be set from.
            record = lane_gate.check(lim, self.baseline_available_mb, self.reset_tmp, self.data, readers=self.gate_readers,
                                     stage=stage, mode=self.gate_mode)
        else:
            record = lane_gate.wait(lim, self.baseline_available_mb, self.reset_tmp, self.data, readers=self.gate_readers,
                                    on_leftovers=lambda: self.reset_lane(None), stage=stage, mode=self.gate_mode)
        record["run"] = rid
        records = read_json_list(self.run_dir(rid) / "gate.json") + [record]
        write_json(self.run_dir(rid) / "gate.json", records)
        with self.ledger_lock, (self.data / "gates.jsonl").open("a") as f:
            f.write(json.dumps(lane_gate.public(record) | {"run": rid}, separators=(",", ":")) + "\n")
        self.last_gate = record
        rec = self.record(rid)
        rec["gate"] = [lane_gate.public(r) for r in records]
        self.save(rid, rec)
        if record["ok"] or self.gate_mode != "enforce":
            return True
        if refuse:
            self._refuse(rid, "not_quiet", "not quiet: " + ", ".join(record.get("reasons") or record.get("held_for") or []))
        return False

    # ---- the lane control: mini on a fixed task, on this machine, inside the run's own job ----
    def _control_preset(self, rec: dict) -> dict | None:
        if not self.control_on:
            return None
        if (self.presets.get(rec.get("preset")) or {}).get("kind") in ("control", "baseline"):
            return None
        return next((p for p in self.presets.values() if p.get("kind") == "control"), None)

    def _configured_control_key(self, rec: dict) -> str | None:
        p = self._control_preset(rec)
        if p is None:
            return None
        try:
            return self.control_key(p)
        except (OSError, ValueError, KeyError):
            return (rec.get("control") or {}).get("key")

    def _skip_control(self, rid: str, why: str):
        rec = self.record(rid)
        rec["control"] = dict(rec.get("control") or {}, state="skipped", reason=why)
        self.save(rid, rec)

    def _control(self, rid: str):
        rec = self.record(rid)
        p = self._control_preset(rec)
        d = self.run_dir(rid) / "control"
        d.mkdir(exist_ok=True)
        rec["state"] = "control"
        self.save(rid, rec)
        control = dict(rec.get("control") or {}, harness="mini", python=platform.python_version())
        try:
            tasks = ", ".join(json.dumps(t) for t in p["tasks"])
            (d / "spec.toml").write_text(f'''question = "Lane control: mini on {Path(p["tasks"][0]).name}, size 1, on this machine."
primary_outcome = {json.dumps(p.get("primary_outcome") or "step_gap_s")}
protocol = "naive"
model = {{ source = "scripted", id = "{SCRIPTED_ID}", effort = "medium", max_output_tokens = 16384 }}
harnesses = ["mini"]
levels = [1]
repeats = {int(p.get("repeats", 3))}
tasks = [{tasks}]
sandbox = "process"
seed = {CONTROL_SEED}
stop_rule = "The lane control: every round once, every attempt recorded."
[budget]
max_requests_per_attempt = {int(p.get("max_requests", 60))}
attempt_timeout_s = {int(p.get("timeout_s", 120))}
[admission]
reserve_mb = 512
reserve_tasks = 256
reserve_disk_mb = {self.reserve_disk_mb}
max_pressure = "warn"
wait_timeout_s = 120
''')
            plan = freeze(d / "spec.toml", None)
            write_json(d / "plan.json", plan)
            bundle = run_plan(d / "plan.json", d / "bundle")
            problems = verify(bundle)
            attempts = read_jsonl(bundle / "attempts.jsonl")
            control.update(self._control_record(p, plan, attempts, problems))
        except Exception as error:  # noqa: BLE001 - a control that cannot run is a failed control, the run still goes on
            control.update(state="failed", reason=f"control_error: {type(error).__name__}")
        rec = self.record(rid)
        rec["control"] = control
        self.save(rid, rec)
        self.last_control = {"key": control.get("key"), "median": control.get("median"), "at": utc_now(), "state": control.get("state")}

    @staticmethod
    def control_key(p: dict, plan: dict | None = None) -> str:
        """Same key, same control: preset fields, task hash, mini's agent file, Python version and VERSION. From the
        configuration alone (plan=None), so a control that was skipped or broke still names the same control."""
        fields = {k: p.get(k) for k in ("id", "kind", "sizes", "repeats", "max_requests", "timeout_s", "primary_outcome")}
        hashes = [t["sha256"] for t in plan.get("tasks", [])] if plan else [load_task(Path(t)).digest() for t in p["tasks"]]
        return sha256_bytes(canonical({"preset": fields, "tasks": hashes,
                                       "mini_agent": sha256_file(harnesses_registry.mini.AGENT),
                                       "python": platform.python_version(), "version": VERSION}))[:12]

    def _control_record(self, p: dict, plan: dict, attempts: list[dict], problems: list) -> dict:
        values = {k: [] for k in CONTROL_VALUES}
        for a in attempts:
            agent, timing = a.get("agent") or {}, a.get("timing") or {}
            values["first_request_s"].append(timing.get("first_request_s"))
            values["step_gap_s"].append(timing.get("step_gap_s_median"))
            values["cpu_s"].append(None if agent.get("cpu_s_partial") else agent.get("cpu_s"))
            values["wall_s"].append(agent.get("wall_s"))
            values["rss_mb"].append(rss_mb(agent))
            values["setup_s"].append((a.get("setup") or {}).get("setup_s"))
            values["request_bytes_first"].append(timing.get("request_bytes_first"))
        median = {k: quantile(v, 0.5) for k, v in values.items()}
        gaps = [g for g in values["step_gap_s"] if g is not None]
        # Noisy: the spread of the control's own step gap is too wide for a ratio to mean anything.
        noisy = bool(len(gaps) >= 2 and median["step_gap_s"] and
                     (quantile(gaps, 0.75) - quantile(gaps, 0.25)) / median["step_gap_s"] > CONTROL_NOISY)
        passes = sum(1 for a in attempts if a.get("passed"))
        canaries = [((a.get("requests") or {}).get("first_request") or {}).get("canary", {}).get("sha256") for a in attempts]
        ok = not problems and attempts and passes == len(attempts)
        return {"state": "done" if ok else "failed",
                "reason": None if ok else ("verify_failed" if problems else "no_attempts" if not attempts else "attempts_failed"),
                "key": self.control_key(p, plan), "harness_sha256": plan["harnesses"]["mini"]["sha256"][:12],
                "plan_sha256": plan["plan_sha256"], "verify": "verified" if not problems else problems[:5],
                "attempts": len(attempts), "passes": passes, "values": values, "median": median, "noisy": noisy,
                "canary_sha256": canaries[0] if canaries and len(set(canaries)) == 1 else None}

    def _execute(self, rid: str):
        rec = self.record(rid)
        p = self.presets[rec["preset"]]
        d = self.run_dir(rid)
        spec = d / "spec.toml"
        tasks = ", ".join(json.dumps(t) for t in p["tasks"])
        harness = rec.get("harness") or self.harness
        options = ""
        if harness == "ante":
            env = f'\ntelemetry_env = "{self.telemetry_env}"' if self.telemetry == "on" else ""
            options = f'[harness_options.ante]\ntelemetry = "{self.telemetry}"{env}\n'
        elif self.harness_options.get(harness):
            options = f"[harness_options.{json.dumps(harness)}]\n" + "".join(
                f"{key} = {json.dumps(value)}\n" for key, value in sorted(self.harness_options[harness].items()))
        # A preset may need more steps or time than the default (tier 0 packs run 50 to 250 steps).
        max_requests = int(p.get("max_requests", 40))
        timeout_s = int(p.get("timeout_s", self.attempt_timeout_s))
        if rec.get("model") and self.live:
            # A real model through the relay: the lane's gateway, a token budget for the whole run, and real-model time.
            route = self.live["models"][rec["model"]]
            key = f', upstream_key_env = {json.dumps(route["key_env"])}' if route.get("key_env") else ""
            wire = catalog.wire_of(rec["model"])
            wire = f', wire = {json.dumps(wire)}' if wire else ""
            if catalog.helper_of(rec["model"]):
                wire += f', helper = {json.dumps(catalog.helper_of(rec["model"]))}'
            model_line = (f'model = {{ source = "relay", id = {json.dumps(rec["model"])}, effort = {json.dumps(rec.get("effort") or self.live["effort"])}, '
                          f'max_output_tokens = 16384, upstream = {json.dumps(route["upstream"])}, max_run_tokens = {self.run_tokens(rec["size"])}{key}{wire} }}')
            max_requests, timeout_s = max(max_requests, 80), max(timeout_s, 600)
        else:
            model_line = f'model = {{ source = "scripted", id = "{SCRIPTED_ID}", effort = "medium", max_output_tokens = 16384 }}'
        protocol = rec.get("protocol") or "naive"
        primary = p.get("primary_outcome") or "successes_per_minute"
        repeats = int(rec.get("repeats") or 1)
        who = NAMES.get(harness, harness)
        question = (f"{p['title']}: {rec['size']} {who} agents, protocol {protocol}. "
                    + ("Live model." if rec.get("model") else
                       "Scripted replay of a fixed tool script. A pass is conformance to that script, not a solved task."))
        spec.write_text(f'''question = {json.dumps(question)}
primary_outcome = {json.dumps(primary)}
protocol = {json.dumps(protocol)}
{model_line}
harnesses = ["{harness}"]
levels = [{rec["size"]}]
repeats = {repeats}
tasks = [{tasks}]
sandbox = "process"
seed = {int(rid[:6], 16)}
stop_rule = "One wave of the requested size; size 1 harness packs run five rounds. Every attempt recorded."
[budget]
max_requests_per_attempt = {max_requests}
attempt_timeout_s = {timeout_s}
[admission]
reserve_mb = 512
reserve_tasks = 256
reserve_disk_mb = {self.reserve_disk_mb}
max_pressure = "warn"
wait_timeout_s = 300
{options}''')
        plan = freeze(spec, {harness: self.binaries[harness]} if self.binaries.get(harness) else None)
        write_json(d / "plan.json", plan)
        rec.update(state="running", started=utc_now(), plan_sha256=plan["plan_sha256"])
        self.save(rid, rec)
        tailer = self.tailers[rid] = Tailer(d / "bundle")
        try:
            bundle = run_plan(d / "plan.json", d / "bundle")
        finally:
            tailer.finish()
            events, _ = tailer.since(0, limit=tailer.cap)
            write_json(d / "console.json", {"dropped": tailer.dropped, "events": events})
            self.tailers.pop(rid, None)
        summary = json.loads((bundle / "summary.json").read_text())
        # Things about the run itself a reader must know before trusting its numbers.
        cells = read_jsonl(bundle / "cells.jsonl")
        warnings = []
        if not (summary.get("sandbox_root") or {"removed": True}).get("removed") or any(c.get("winddown", {}).get("sandbox_leftovers") for c in cells):
            warnings.append("sandbox_leftover")
        if any(c.get("winddown", {}).get("shared_seed_changed") for c in cells):
            warnings.append("shared_seed_changed")
        rec["warnings"] = warnings
        manifest = json.loads((bundle / "MANIFEST.json").read_text())
        group = next(iter(summary["groups"].values()), {})
        harness_info = plan["harnesses"][harness]
        attempts = read_jsonl(bundle / "attempts.jsonl")
        peak, per = lane_peaks(self.series(bundle, cap=1 << 30), [rss_mb(a.get("agent") or {}) for a in attempts])
        walls = [c["batch_wall_s"] for c in cells if isinstance(c.get("batch_wall_s"), (int, float))]
        rec["adapter_sha256"] = adapter_sha256(harness)
        rec["canary"] = self._canary(rid, rec, canary_of(attempts), harness_info, warnings)
        rec.update(run_finished=utc_now(), verify="verified" if not (problems := verify(bundle)) else problems[:5], chain_head=manifest["chain_head"][:12],
                   result={k: group.get(k) for k in ("attempts", "passes", "pass_rate", "successes_per_minute", "peak_rss_mb_median",
                                                       "wall_s_median", "cpu_s_per_success", "max_running", "admission_wait_s_total", "failures",
                                                       "first_request_s_median", "after_final_s_median", "step_gap_s_median", "step_gap_s_p90",
                                                       "context_slope_median", "model_turns_median", "probes_median", "not_run", "infra_reasons") + FIRST_MEDIANS}
                   | {"cpu_s_median": quantile([None if (a.get("agent") or {}).get("cpu_s_partial") else (a.get("agent") or {}).get("cpu_s") for a in attempts], 0.5),
                      "disk_kb_median": quantile([(a.get("agent") or {}).get("disk_kb") for a in attempts], 0.5),
                      "batch_wall_s_total": round(sum(walls), 2) if walls else None,
                      "agents_mem_mb_peak": peak, "mem_per_agent_mb": per},
                   complete=summary.get("complete"), harness=harness, harness_version=harness_info["version"],
                   harness_sha256=harness_info["sha256"][:12])
        self.save(rid, rec)  # the terminal state and ledger line come from _work, after the control

    # ---- the canary store: the last stable first-request fingerprint per harness, wire and source ----
    def _canary(self, rid: str, rec: dict, canary: dict | None, info: dict, warnings: list) -> dict | None:
        """Compare this run's canary with the lane's last one. Unstable or missing: no verdict, the store is left alone."""
        if canary is None:
            return None
        wire = canary.pop("wire", None) or "unknown"
        if not canary["stable"]:
            return canary
        harness = rec.get("harness") or self.harness
        # The model is part of the key: a relay lane switching models must not read as the harness changing.
        key = f"{harness}|{wire}|{rec.get('model') or 'scripted'}"
        now = {"sha256": canary["sha256"], "parts": canary["parts"], "harness_sha256": info["sha256"][:12],
               "harness_version": info["version"], "adapter_sha256": rec.get("adapter_sha256"), "since": utc_now(), "run": rid}
        path = self.data / "canary.json"
        with self.ledger_lock:
            try:
                store = json.loads(path.read_text())
            except (OSError, ValueError):
                store = {}
            before = store.get(key)
            canary["changed"] = False
            if before is None:
                store[key] = now
            elif any(before.get(k) != now[k] for k in ("sha256", "harness_sha256", "adapter_sha256")):
                canary["changed"] = True
                canary["binary_changed"] = before.get("harness_sha256") != now["harness_sha256"]
                canary["parts_changed"] = sorted(k for k in CANARY_PARTS if (before.get("parts") or {}).get(k) != now["parts"].get(k))
                store[key] = now
                line = {"at": now["since"], "key": key, "run": rid, "parts_changed": canary["parts_changed"],
                        "binary_changed": canary["binary_changed"], "from": before, "to": now}
                with (self.data / "canary.jsonl").open("a") as f:
                    f.write(json.dumps(line, separators=(",", ":")) + "\n")
                warnings.append("harness_changed")
            else:
                canary["binary_changed"] = False
            write_json(path, store)
        return canary

    def _stored_canary(self, rec: dict) -> str | None:
        """A run whose own canary is unknown (unstable, not recorded) is not a new cohort: it takes the lane's last
        known canary for the same harness and model."""
        try:
            store = json.loads((self.data / "canary.json").read_text())
        except (OSError, ValueError):
            return None
        harness, source = rec.get("harness") or self.harness, rec.get("model") or "scripted"
        mine = [v for k, v in store.items() if isinstance(v, dict) and k.split("|")[0] == harness and k.split("|")[-1] == source]
        latest = max(mine, key=lambda v: v.get("since") or "", default=None)
        return latest.get("sha256") if latest else None

    def canary_status(self) -> dict | None:
        """{harness: sha12, since} for this lane's own harness: its latest entry in the store."""
        try:
            store = json.loads((self.data / "canary.json").read_text())
        except (OSError, ValueError):
            return None
        mine = [v for k, v in store.items() if k.split("|", 1)[0] == self.harness and isinstance(v, dict)]
        latest = max(mine, key=lambda v: v.get("since") or "", default=None)
        return {self.harness: (latest["sha256"] or "")[:12], "since": latest.get("since")} if latest else None

    def cohort(self, rec: dict) -> str | None:
        """12 hex naming what may be compared with this run (docs/evaluation.md, "Cohorts"), from the frozen plan and the run's record."""
        try:
            plan = json.loads((self.run_dir(rec["id"]) / "plan.json").read_text())
            harness = rec.get("harness") or self.harness
            spec = plan["spec"]
            fields = {"preset": rec.get("preset"), "size": rec.get("size"), "protocol": spec.get("protocol"),
                      "model": spec["model"].get("id"), "effort": spec["model"].get("effort"),
                      "tasks": [t["sha256"] for t in plan["tasks"]], "harness_sha256": plan["harnesses"][harness]["sha256"],
                      # the binary and whatever else the plan fingerprints with it (mini's agent file, a seed home, node)
                      "harness_parts": {k: v for k, v in plan["harnesses"][harness].items() if k.endswith("_sha256")},
                      "harness_options": (spec.get("harness_options") or {}).get(harness),
                      "adapter_sha256": rec.get("adapter_sha256"), "canary": (rec.get("canary") or {}).get("sha256") or self._stored_canary(rec),
                      # the control the lane is configured with, run or not: a skipped control is not a new cohort
                      "control_key": self._configured_control_key(rec), "gate": rec.get("settings"), "version": VERSION}
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return sha256_bytes(canonical(fields))[:12]

    # ---- the ledger: one compact line per finished run, kept after the run's bundle is pruned ----
    @staticmethod
    def ledger_entry(rec: dict) -> dict:
        out = {k: rec.get(k) for k in LEDGER_FIELDS}
        out["harness"] = rec.get("harness") or DEFAULT_HARNESS  # runs from before the picker were all Ante
        out["control_key"] = (rec.get("control") or {}).get("key")
        gates = [g for g in rec.get("gate") or [] if isinstance(g, dict)]
        out["gate_ok"] = all(g.get("ok") for g in gates) if gates else None
        result = rec.get("result") or {}
        out.update({k: result.get(k) for k in LEDGER_RESULT})
        return out

    def _ledger_append(self, rec: dict):
        line = json.dumps(self.ledger_entry(rec), separators=(",", ":")) + "\n"
        with self.ledger_lock, self.ledger_path.open("a") as f:
            f.write(line)

    def _backfill_ledger(self):
        if self.ledger_path.exists():
            return
        recs = [r for d in (self.data / "runs").iterdir() if d.is_dir() and (r := self._safe_record(d.name))
                and r.get("state") in ("done", "failed")]
        recs.sort(key=lambda r: r.get("finished") or "")
        with self.ledger_lock:
            self.ledger_path.write_text("".join(json.dumps(self.ledger_entry(r), separators=(",", ":")) + "\n" for r in recs))

    def _safe_record(self, rid: str) -> dict | None:
        try:
            return self.record(rid)
        except ValueError:
            return None

    def ledger(self, limit=50) -> list[dict]:
        """Newest first. bundle_kept says whether the full run (and so /swarm/?run=<id>) still exists."""
        limit = max(1, min(int(limit), 500))
        try:
            lines = self.ledger_path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            e["bundle_kept"] = bool(RUN_ID.match(str(e.get("id"))) and (self.run_dir(e["id"]) / "run.json").exists())
            out.append(e)
            if len(out) >= limit:
                break
        return out

    # ---- the tests behind each preset, as written ----
    def tasks(self) -> list[dict]:
        used: dict[str, list[str]] = {}
        titles: dict[str, str] = {}
        paths: dict[str, Path] = {}
        for pid, p in self.presets.items():
            for t in p["tasks"]:
                name = Path(t).name
                paths[name] = Path(t)
                used.setdefault(name, []).append(pid)
                if len(p["tasks"]) == 1:
                    titles.setdefault(name, p["title"])
        out = []
        for name, root in paths.items():
            try:
                meta = tomllib.loads((root / "task.toml").read_text())
                files = sorted((str(f.relative_to(root / "workspace")), f.stat().st_size)
                               for f in (root / "workspace").rglob("*") if f.is_file())
                out.append({"name": name, "title": titles.get(name, name),
                            "instruction": clean((root / "instruction.md").read_text(), 4000),
                            "test": clean((root / "tests" / "test.sh").read_text(), 6000),
                            "allowed_changes": [clean(a, 200) for a in meta.get("task", {}).get("allowed_changes", [])],
                            "workspace": [{"name": clean(n, 200), "bytes": b} for n, b in files],
                            "presets": used[name]})
            except (OSError, ValueError):
                continue
        return out

    def _prune(self):
        runs = sorted((p for p in (self.data / "runs").iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in runs[:-self.keep]:
            rec = self.record(old.name) or {}
            if rec.get("state") in ("done", "failed"):
                shutil.rmtree(old, ignore_errors=True)

    # ---- live view of a run, read from its bundle files ----
    def console(self, rid: str, since: int, limit=400) -> tuple[list[dict], int]:
        tailer = self.tailers.get(rid)
        if tailer:
            return tailer.since(since, limit)
        if rid not in self.consoles:
            path = self.run_dir(rid) / "console.json"
            if not path.exists():
                return [], since
            doc = json.loads(path.read_text())
            if len(self.consoles) > 8:
                self.consoles.pop(next(iter(self.consoles)))
            self.consoles[rid] = doc["events"]
        events = self.consoles[rid]
        chunk = events[since:since + limit]
        return chunk, since + len(chunk)

    def spec_text(self, rid: str) -> str | None:
        path = self.run_dir(rid) / "spec.toml"
        if not path.exists():
            return None
        text = path.read_text()
        for p in self.presets.values():
            for t in p["tasks"]:
                text = text.replace(json.dumps(t), json.dumps("tasks/" + Path(t).name))
        return clean(text, 4000)

    def snapshot(self, rid: str, since: int, csince: int | None = None) -> dict:
        rec = self.record(rid)
        b = self.run_dir(rid) / "bundle"
        attempts = read_jsonl(b / "attempts.jsonl") if b.exists() else []
        decisions = read_jsonl(b / "admission.jsonl") if b.exists() else []
        admitted = sum(1 for a in decisions if a.get("admitted"))
        sample = {}
        if (b / "samples.jsonl").exists():
            with (b / "samples.jsonl").open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                lines = [line for line in f.read().splitlines() if line.strip()]
            for line in reversed(lines):
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if "running" in s:
                    sample = {k: s.get(k) for k in ("running", "available_mb", "total_mb", "pressure", "tasks_free", "t",
                                                    "cpu_pct", "disk_used_mb", "agents_mem_mb")}
                    break
        new = [{"slot": int(a["attempt"].rsplit("-s", 1)[-1]) if "-s" in a.get("attempt", "") else None, "task": a.get("task"), "passed": a.get("passed"), "failure": a.get("failure"),
                # ran: an agent process was launched. infra: why the runner failed the attempt (codes only).
                "ran": a.get("agent", {}).get("wall_s") is not None,
                "infra": {"reason": a.get("infra_reason") or "runner_error", "stage": a.get("infra_stage")} if a.get("failure") == "infra" else None,
                "termination": a.get("agent", {}).get("termination"),
                "setup_s": (a.get("setup") or {}).get("setup_s"),
                "wall_s": a.get("agent", {}).get("wall_s"), "rss_mb": rss_mb(a.get("agent", {})),
                "cpu_s": None if a.get("agent", {}).get("cpu_s_partial") else a.get("agent", {}).get("cpu_s"),
                "disk_kb": a.get("agent", {}).get("disk_kb"),
                "tree_rss_mb": round(a["agent"]["peak_tree_rss_kb"] / 1024, 1) if a.get("agent", {}).get("peak_tree_rss_kb") else None,
                "threads": a.get("agent", {}).get("peak_tree_threads"),
                "first_request_s": (a.get("timing") or {}).get("first_request_s"),
                "step_gap_s": (a.get("timing") or {}).get("step_gap_s_median"),
                "context_slope": (a.get("timing") or {}).get("context_slope"),
                "model_turns": (a.get("requests") or {}).get("model_turns"),
                "probes": (a.get("requests") or {}).get("probes"),
                "input_tokens": (a.get("requests") or {}).get("input_tokens"),
                "output_tokens": (a.get("requests") or {}).get("output_tokens"),
                "first_request": first_request_public(a),
                "failure_detail": failure_detail(a)}
               for a in attempts[since:]]
        public = dict(rec)
        if public.get("error"):
            # Raw service exceptions can contain paths or provider text. Keep those private.
            gates = read_json_list(self.run_dir(rid) / "gate.json")
            public["error"] = ("This lane was not clean after its last run, so this run did not start."
                               if public.get("refused") == "lane_not_clean" else
                               "This lane was not quiet enough to measure: " + (lane_gate.sentence(gates[-1]) if gates else "unknown") + "."
                               if public.get("refused") == "not_quiet" else
                               "The service stopped before this run could finish. Partial attempts are retained.")
        try:
            public["reset"] = self.public_reset(json.loads((self.run_dir(rid) / "reset.json").read_text()))
        except (OSError, ValueError):
            pass
        out = {"run": public, "position": self.position(rid), "admitted": admitted, "finished": len(attempts), "sample": sample, "attempts": new,
               "progress": self.progress(rid, rec, attempts, decisions, sample)}
        if csince is not None:
            out["console"], out["console_next"] = self.console(rid, csince)
        if since == 0:
            out["series"] = self.series(b)
        out["mem_scope"] = self.mem_scope(b)
        return out

    @staticmethod
    def mem_scope(bundle: Path) -> str | None:
        """Short code for how this run's "memory in use" was measured: cgroup, pss, rss-sum (overcounts) or
        host (a run recorded before the measure changed: host memory in use above the run's first sample)."""
        path = bundle / "samples.jsonl"
        if not path.exists():
            return None
        try:
            with path.open() as f:
                first = next((json.loads(line) for line in f if '"running"' in line), None)
        except (OSError, ValueError):
            return None
        if first is None:
            return None
        return str(first["agents_mem_scope"]).split(":", 1)[0] if first.get("agents_mem_scope") else "host"

    @staticmethod
    def series(bundle: Path, cap: int = 600) -> list[list]:
        """The run's host samples so far, for charts: [t, running, agents_mem_mb, cpu_pct, available_mb], at most
        `cap` rows (evenly thinned). Sent once, on a viewer's first snapshot; live samples follow one by one.

        A run recorded before agents_mem_mb existed only has summed process RSS, which counts shared pages many
        times over (18 GB on a 7.7 GB machine). That is never charted: such a run shows host memory in use above
        its first sample instead, which the old samples do hold."""
        rows, floor = [], None
        for s in read_jsonl(bundle / "samples.jsonl") if (bundle / "samples.jsonl").exists() else []:
            if "running" in s:
                mem = s.get("agents_mem_mb")
                if "agents_mem_mb" not in s and s.get("total_mb") is not None and s.get("available_mb") is not None:
                    used = s["total_mb"] - s["available_mb"]
                    floor = used if floor is None else floor
                    mem = max(0, used - floor)
                rows.append([s.get("t"), s.get("running"), mem, s.get("cpu_pct"), s.get("available_mb")])
        if len(rows) > cap:
            step = len(rows) / cap
            rows = [rows[int(i * step)] for i in range(cap - 1)] + [rows[-1]]
        return rows

    def progress(self, rid, rec, attempts, decisions, sample):
        """Read-only observations. Quiet log files do not prove a process is stalled."""
        now = time.time()
        finished = {slot_of(a.get("attempt", "")): a for a in attempts}
        admitted = {slot_of(a.get("attempt", "")) for a in decisions if a.get("admitted")}
        logs = self.run_dir(rid) / "bundle" / "logs"
        dirs = {slot_of(d.name): d for d in logs.iterdir() if d.is_dir() and ATTEMPT.fullmatch(d.name)} if logs.exists() else {}
        agents, activity = [], []
        for slot in range(min(int(rec.get("size") or 0), 1024)):
            record, directory = finished.get(slot), dirs.get(slot)
            stamps = []
            checking = False
            if directory:
                for name in ("agent.log", "requests.live.jsonl", "check.log", "lifecycle.live.json"):
                    try:
                        stat = (directory / name).stat()
                        if stat.st_size:
                            stamps.append(stat.st_mtime)
                        checking |= name == "check.log"
                    except OSError:
                        pass
            if stamps:
                activity.extend(stamps)
            observed = lifecycle(directory) if directory else {}
            # Every state here has a name in the page's legend. preparing: the runner is building the sandbox and no
            # agent exists yet. starting: the agent's process runs but has made no model request. notrun: the runner
            # failed or stopped the attempt, which is not the harness failing.
            phase = observed.get("phase")
            if record:
                state = "passed" if record.get("passed") else "notrun" if record.get("failure") in ("infra", "cancelled", "admission_timeout") else "failed"
            elif checking or phase == "agent_exited":
                state = "checking"
            elif phase == "launching":
                try:
                    state = "working" if (directory / "requests.live.jsonl").stat().st_size else "starting"
                except OSError:
                    state = "starting"
            elif directory or slot in admitted:
                state = "preparing"
            else:
                state = "waiting"
            agents.append({"slot": slot, "state": state,
                           "quiet_s": round(max(0, now - max(stamps)), 1) if stamps else None,
                           "failure_detail": failure_detail(record) if record else None})
            if not record and observed.get("phase") == "agent_exited":
                agents[-1]["observed_exit"] = observed.get("agent")
        start = rec.get("started") or rec.get("created")
        end = rec.get("finished")
        try:
            elapsed = max(0, (datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() if end else now)
                          - datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp())
        except (ValueError, AttributeError):
            elapsed = None
        plan = self.run_dir(rid) / "plan.json"
        try:
            spec = json.loads(plan.read_text()).get("spec", {})
        except (OSError, ValueError):
            spec = {}
        try:
            memory_total = json.loads((self.run_dir(rid) / "bundle" / "host.json").read_text()).get("fingerprint", {}).get("memory", {}).get("total_mb")
        except (OSError, ValueError):
            memory_total = None
        # The batch wall is the swarm itself, without the plan freeze and verify around it; it exists once cells are written.
        try:
            batch_wall = sum(json.loads(line)["batch_wall_s"] for line in (self.run_dir(rid) / "bundle" / "cells.jsonl").read_text().splitlines() if line.strip())
        except (OSError, ValueError, KeyError, TypeError):
            batch_wall = None
        return {"observed_at": utc_now(), "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
                "batch_wall_s": round(batch_wall, 2) if batch_wall else None,
                "last_activity_age_s": round(max(0, now - max(activity)), 1) if activity else None,
                "sample_age_s": round(max(0, now - sample["t"]), 1) if isinstance(sample.get("t"), (int, float)) else None,
                "quiet_warning_s": 30, "agents": agents,
                "memory_total_mb": memory_total,
                "agent_limit_s": spec.get("budget", {}).get("attempt_timeout_s"),
                "admission_limit_s": spec.get("admission", {}).get("wait_timeout_s"),
                "maintenance": self.maintenance() if rec.get("state") == "queued" else None}


def handler_for(svc: Service, secret: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="application/json"):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _allowed(self) -> bool:
            if secret:
                got = self.headers.get("X-Swarm-Ingress", "")
                return hmac.compare_digest(got.encode(), secret.encode())
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _client(self) -> str:
            # X-Real-IP is trusted only behind the authenticated proxy.
            return (self.headers.get("X-Real-IP") if secret else None) or self.client_address[0]

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/healthz":
                return self._send(200, {"ok": True})
            if not self._allowed():
                return self._send(403, {"error": "forbidden"})
            if path == "/api/presets":
                return self._send(200, {"presets": svc.public_presets()})
            if path == "/api/tasks":
                return self._send(200, {"tasks": svc.tasks()})
            if path == "/api/ledger":
                m = re.search(r"[?&]limit=(\d+)", self.path)
                limit = min(int(m.group(1)), 500) if m and len(m.group(1)) <= 6 else 50
                return self._send(200, {"runs": svc.ledger(limit or 50)})
            if path == "/api/status":
                with svc.lock:
                    current, queued = svc.current, len(svc.order)
                rec = svc.record(current) if current else None
                return self._send(200, {"running": {k: rec.get(k) for k in ("id", "title", "size", "harness")} if rec else None, "queued": queued,
                                        "maintenance": svc.maintenance(),
                                        "lane": "ready" if svc.lane_clean() else "reset_failed",
                                        "reset": svc.public_reset(svc.last_reset),
                                        "gate": lane_gate.public(svc.last_gate) if svc.last_gate else None,
                                        "control": svc.last_control, "canary": svc.canary_status(),
                                        "settings": {"gate": svc.gate_mode, "control": svc.control_on}})
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "runs"] and RUN_ID.match(parts[2]) and svc.record(parts[2]):
                rid = parts[2]
                if len(parts) == 3:
                    return self._send(200, svc.snapshot(rid, 0))
                if len(parts) == 4 and parts[3] == "events":
                    return self._events(rid)
                if len(parts) == 4 and parts[3] == "export":
                    return self._send(200, run_export.run_document(svc.run_dir(rid), svc.record(rid)))
                if len(parts) == 4 and parts[3] == "spec":
                    return self._send(200, {"spec": svc.spec_text(rid)})
                if len(parts) == 5 and parts[3] == "agents" and parts[4].isdigit() and len(parts[4]) <= 5:
                    t = transcript(svc.run_dir(rid) / "bundle", int(parts[4])) if (svc.run_dir(rid) / "bundle").exists() else None
                    return self._send(200, t) if t else self._send(404, {"error": "no such agent"})
            return self._send(404, {"error": "not found"})

        def _events(self, rid):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            since, csince, deadline = 0, 0, time.monotonic() + 900
            try:
                while time.monotonic() < deadline:
                    snap = svc.snapshot(rid, since, csince)
                    since += len(snap["attempts"])
                    csince = snap["console_next"]
                    self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                    self.wfile.flush()
                    if snap["run"]["state"] in ("done", "failed") and not snap["console"]:
                        break
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self._allowed():
                return self._send(403, {"error": "forbidden"})
            if self.path.split("?", 1)[0] != "/api/runs":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": "bad length"})
            try:
                body = json.loads(self.rfile.read(length))
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                return self._send(400, {"error": "invalid json"})
            key = body.get("passkey")
            status, out = svc.launch(str(body.get("preset", "")), body.get("size"), self._client(),
                                     str(key)[:128] if key else None, body.get("harness"),
                                     str(body["model"])[:80] if body.get("model") else None,
                                     str(body["effort"])[:16] if body.get("effort") else None,
                                     str(body["protocol"])[:16] if body.get("protocol") else None,
                                     body.get("meter", True) is not False,
                                     body.get("series"), body.get("control_order"))
            return self._send(status, out)

    return Handler


def serve(presets: Path, data: Path, host="127.0.0.1", port=8080, **kw):
    secret = os.environ.get("SWARM_INGRESS_SECRET") or None
    svc = Service(presets, data, passkey=os.environ.get("SWARM_PASSKEY") or None, **kw)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server((host, port), handler_for(svc, secret))
    print(f"swarm serve on {host}:{port} · presets {', '.join(svc.presets)} · ingress {'secret' if secret else 'loopback only'}", flush=True)
    server.serve_forever()
