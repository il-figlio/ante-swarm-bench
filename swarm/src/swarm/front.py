"""swarm front: the public face for several private `swarm serve` lanes.

Each lane is one machine running `swarm serve` for one harness. The front end starts the same preset on
every lane at once (a race), relays each lane's live snapshots to the browser, and serves the page.

Browsers never learn where a lane runs. Lane URLs and secrets live only in the lanes file and the
environment; every relayed payload is scrubbed of hostnames, addresses, home paths and tokens on top
of the cleaning `swarm serve` already does.

Lanes file (JSON, keep it readable by the service user only):
    {"lanes": [{"id": "ante", "label": "Ante", "harness": "ante",
                "url": "https://lane-host.example", "secret_env": "SWARM_LANE_ANTE_SECRET"}]}
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

from . import catalog
from . import stats as race_stats_mod
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .live import HOME, KEY, TOKEN

LANE_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RACE_ID = re.compile(r"^[0-9a-f]{12}$")
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:exe\.xyz|exe\.dev|internal|local|ts\.net)\b", re.I)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I)
MAX_BODY = 4096
REPEATS = (1, 6, 10)
VISITOR_REPEATS = 6  # a visitor's series: harness presets at size 1 only; anything else above 1 is the owner's
ORDERS = ("control_first", "control_last")
REP_TIMEOUT_S = 1800  # one repetition on all lanes; a lane that never reports back must not hold the series
# What the race record keeps of a lane run, per repetition. All of it comes from the lane's public snapshot.
LANE_KEEP = ("state", "result", "control", "gate", "cohort", "canary", "refused", "harness_version", "harness_sha256",
             "adapter_sha256", "warnings", "verify")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}


class Lane:
    def __init__(self, d: dict):
        self.id, self.label, self.harness = d["id"], d.get("label", d["id"]), d["harness"]
        if not LANE_ID.match(self.id):
            raise ValueError(f"bad lane id {self.id!r}")
        self.url = d["url"].rstrip("/")
        self.secret = os.environ.get(d["secret_env"], "") if d.get("secret_env") else ""
        if d.get("secret_env") and not self.secret:
            raise ValueError(f"lane {self.id}: {d['secret_env']} is not set")
        # Optional: the most agents this lane's machine holds (a heavy harness can stall an 8 GB VM well before the size list ends).
        self.max_size = d.get("max_size") if isinstance(d.get("max_size"), int) and d["max_size"] > 0 else None
        # Real models this lane's relay offers (its serve enforces the same list); owner-only at launch.
        self.models = [m for m in d.get("models", []) if isinstance(m, str)]
        host = re.sub(r"^https?://", "", self.url).split("/")[0].split(":")[0]
        self.private = [s for s in {self.url, host} if s]

    def public(self) -> dict:
        out = {"id": self.id, "label": self.label, "harness": self.harness}
        if self.max_size:
            out["max_size"] = self.max_size
        if self.models:
            out["models"] = self.models
        return out


# Wrong owner passkeys allowed in total: the first try and two retries. The third wrong one locks real-model launches
# for everyone until the owner unlocks on the VM (`swarm.cli front-unlock`, with a PIN the front never loads). Nothing on the page or in any reply
# says whether a lock is on: a locked front answers exactly as it does to a wrong passkey.
PASSKEY_TRIES = 3
LOCK_FILE = "owner-lock.json"


def lock_state(data: Path) -> tuple[int, bool]:
    """(wrong passkeys since the last good one, locked). A missing or unreadable file reads as clear."""
    try:
        d = json.loads((Path(data) / LOCK_FILE).read_text())
        return int(d.get("failures", 0)), bool(d.get("locked"))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0, False


def write_lock(data: Path, failures: int, locked: bool):
    path = Path(data) / LOCK_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"failures": failures, "locked": locked, "t": time.time()}))
    os.replace(tmp, path)


class Front:
    def __init__(self, lanes_path: Path, data: Path, per_client_hour=3, passkey: str | None = None, timeout=8):
        doc = json.loads(Path(lanes_path).read_text())
        self.lanes = [Lane(d) for d in doc["lanes"]]
        self.notice = str(doc.get("notice", ""))[:300]
        if len({l.id for l in self.lanes}) != len(self.lanes):
            raise ValueError("duplicate lane ids")
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.per_client_hour, self.passkey, self.timeout = per_client_hour, passkey, timeout
        self.lock = threading.Lock()
        self.races: dict[str, dict] = {}
        self.hits: dict[str, deque] = {}
        self.status_cache: tuple[float, list] = (0.0, [])
        self.private = sorted({s for l in self.lanes for s in l.private}, key=len, reverse=True)
        self.poll_s, self.rep_timeout_s = 1.0, REP_TIMEOUT_S
        self.stats_cache: dict[str, tuple] = {}
        self.drivers: dict[str, threading.Thread] = {}
        self._load()

    # -- scrubbing -------------------------------------------------------------------------------
    def scrub(self, v):
        if isinstance(v, str):
            for s in self.private:
                v = v.replace(s, "[lane]")
            v = HOST.sub("[host]", v)
            v = IPV6.sub("[ip]", IPV4.sub("[ip]", v))
            v = KEY.sub("[key]", TOKEN.sub("[token]", HOME.sub("~", v)))
            return v
        if isinstance(v, list):
            return [self.scrub(x) for x in v]
        if isinstance(v, dict):
            return {self.scrub(k) if isinstance(k, str) else k: self.scrub(x) for k, x in v.items()}
        return v

    # -- upstream --------------------------------------------------------------------------------
    def _req(self, lane: Lane, path: str, body: dict | None = None, client: str | None = None):
        headers = {"Accept": "application/json"}
        if lane.secret:
            headers["X-Swarm-Ingress"] = lane.secret
        if client:
            headers["X-Real-IP"] = client
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(lane.url + path, data=data, headers=headers, method="POST" if data else "GET")

    def call(self, lane: Lane, path: str, body: dict | None = None, client: str | None = None) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self._req(lane, path, body, client), timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
            finally:
                e.close()
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}

    def _fan(self, fn, lanes=None):
        out, threads = {}, []
        for lane in (self.lanes if lanes is None else lanes):
            t = threading.Thread(target=lambda l=lane: out.__setitem__(l.id, fn(l)), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(self.timeout + 2)
        return out

    def lane_status(self) -> list[dict]:
        now = time.monotonic()
        with self.lock:
            if now - self.status_cache[0] < 2:
                return self.status_cache[1]
        got = self._fan(lambda l: self.call(l, "/api/status"))
        rows = []
        for lane in self.lanes:
            code, st = got.get(lane.id, (0, {}))
            # unclean: the lane's reset after its last run failed and it refuses launches until it is clean again.
            state = "offline" if code != 200 else "maintenance" if st.get("maintenance") else \
                "unclean" if st.get("lane") == "reset_failed" else "busy" if st.get("running") or st.get("queued") else "idle"
            rows.append({**lane.public(), "state": state})
        with self.lock:
            self.status_cache = (now, rows)
        return rows

    def presets(self) -> list[dict]:
        for lane in self.lanes:
            code, doc = self.call(lane, "/api/presets")
            if code == 200:
                return self.scrub(doc.get("presets", []))
        return []

    # -- races -----------------------------------------------------------------------------------
    def _load(self):
        path = self.data / "races.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    self.races[r["id"]] = r
                except (ValueError, KeyError):
                    continue
        # Only a driver thread moves a series on, and it died with the last process: what it left is kept, marked
        # interrupted, and never resumed (a resumed series would not be paired in time across lanes).
        for r in self.races.values():
            if r.get("state") in ("running", "calibrating"):
                r["state"] = "interrupted"
                self._save(r)

    def _save(self, race: dict):
        with self.lock:
            line = json.dumps(race)
        with (self.data / "races.jsonl").open("a") as f:
            f.write(line + "\n")

    def owner(self, passkey: str | None) -> bool:
        """True for the owner's passkey while no lock is on. Counts wrong ones; the last allowed miss locks."""
        with self.lock:
            failures, locked = lock_state(self.data)
            if locked or not self.passkey:
                return False
            if passkey and hmac.compare_digest(passkey.encode(), self.passkey.encode()):
                if failures:
                    write_lock(self.data, 0, False)
                return True
            if passkey:  # an empty field is not a guess
                write_lock(self.data, failures + 1, failures + 1 >= PASSKEY_TRIES)
            return False

    def _allow(self, client: str, owner: bool) -> bool:
        # The owner's extra launches follow a checked real-model launch only: a scripted launch never looks at the
        # passkey, so the hourly cap cannot be used to test guesses.
        cap = self.per_client_hour + (5 if owner else 0)
        now = time.time()
        q = self.hits.setdefault(client, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= cap:
            return False
        q.append(now)
        return True

    def catalog(self) -> dict:
        """Providers and models any lane offers, with their efforts, for the model menu."""
        offered = {m for lane in self.lanes for m in lane.models}
        models = [m for m in catalog.MODELS if m["id"] in offered]
        used = {m["provider"] for m in models}
        return {"providers": [p for p in catalog.PROVIDERS if p["id"] in used], "models": models}

    def start_race(self, preset: str, size, client: str, passkey: str | None = None, lanes=None, model=None,
                   effort=None, protocol=None, repeats=1) -> tuple[int, dict]:
        if not isinstance(size, int) or isinstance(size, bool):
            return 400, {"error": "size must be an integer"}
        if repeats is None:
            repeats = 1
        if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats not in REPEATS:
            return 400, {"error": f"repeats must be one of {list(REPEATS)}"}
        # lanes: the lane ids to race (one alone, a pair, or all). None means every lane.
        if lanes is None:
            chosen = list(self.lanes)
        elif isinstance(lanes, list) and lanes and len(lanes) <= len(self.lanes) and all(isinstance(x, str) for x in lanes) \
                and len(set(lanes)) == len(lanes) and all(self.lane(x) for x in lanes):
            chosen = [l for l in self.lanes if l.id in lanes]
        else:
            return 400, {"error": "lanes must list one or more known lane ids"}
        known = {p.get("id"): p for p in self.presets()}
        if preset not in known or size not in known[preset].get("sizes", []):
            return 400, {"error": "unknown preset or size"}
        allowed = known[preset].get("protocols") or ["naive"]
        protocol = protocol or "naive"
        if protocol not in allowed:
            return 400, {"error": f"protocol must be one of {allowed}"}
        kind = known[preset].get("kind") or "work"
        owner = False
        if model in (None, "", "scripted"):
            model = None
        elif not isinstance(model, str) or not self.owner(passkey):
            return 403, {"error": "real models are operator-only; visitors race the scripted model"}
        else:
            owner = True
            missing = [l.label for l in chosen if model not in l.models]
            if missing:
                return 400, {"error": f"{', '.join(missing)} cannot run {model}"}
        # A series ties up every chosen lane for its length: a visitor gets 6 short ones (harness preset, one agent);
        # anything longer goes through the owner's passkey, with the same count and lock as real models.
        if repeats > 1 and not owner and not (repeats == VISITOR_REPEATS and kind == "harness" and size == 1):
            if not self.owner(passkey):
                return 403, {"error": f"visitors may run {VISITOR_REPEATS} repetitions of a harness test at size 1; more needs the owner"}
            owner = True
        if model and effort is not None and effort not in (catalog.BY_ID.get(model) or {}).get("efforts", [effort]):
            return 400, {"error": f"{model} takes effort {', '.join(catalog.BY_ID[model]['efforts'])}"}
        # max_size is what a lane's machine is known to hold, not a limit: a larger race still runs, and the harness
        # is seen to fail. The race records who was over.
        over = [l.id for l in chosen if l.max_size and size > l.max_size]
        with self.lock:
            # Races on different lanes run side by side; a lane runs one race (or series) at a time.
            wanted = {l.id for l in chosen}
            busy = [r for r in self.races.values() if r.get("state") == "running" and wanted & set(r["lanes"])]
            if busy:
                return 409, {"error": "a race is already running on one of those lanes", "race": busy[0]["id"]}
            if not self._allow(client, owner):
                return 429, {"error": "hourly limit reached"}
            rid = secrets.token_hex(6)
            race = {"id": rid, "preset": preset, "size": size, "protocol": protocol, "t": time.time(), "kind": kind,
                    "state": "running", "model": model, "effort": effort, "repeats": repeats, "seed": rid, "rep": 0, "reps": [],
                    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "over": over,
                    # reserve the lanes before any call goes out; this is also the current repetition's view
                    "lanes": {l.id: {"run": None, "error": None, "state": "queued"} for l in chosen}}
            self.races[rid] = race
        self._save(race)
        launch = {"preset": preset, "size": size} | ({"protocol": protocol} if protocol not in (None, "naive") else {}) | (
            {"model": model} if model else {}) | ({"effort": effort} if model and effort else {})
        t = threading.Thread(target=self._drive, args=(race, chosen, launch, client), daemon=True)
        self.drivers[rid] = t
        t.start()
        return 202, self.public_race(race)

    # -- the series driver: one thread per race, repetition k on every lane together, then k + 1 --------------
    def _drive(self, race: dict, chosen: list, launch: dict, client: str):
        try:
            rng = random.Random(race["id"])
            for k in range(1, race["repeats"] + 1):
                if race["state"] != "running":
                    return
                order = rng.choice(ORDERS)
                if not self._repetition(race, chosen, launch, client, k, order) and k == 1:
                    with self.lock:
                        race["state"] = "failed"
                        q = self.hits.get(client)
                        if q:
                            q.pop()  # nothing ran, so the visitor keeps this race
                    self._save(race)
                    return
            self._end(race)
        except Exception as error:  # noqa: BLE001 - a broken driver ends its series as failed, never silently
            with self.lock:
                race["state"], race["error"] = "failed", f"driver: {type(error).__name__}"
            self._save(race)

    def _repetition(self, race, chosen, launch, client, k, order) -> bool:
        """Launch repetition k on every chosen lane, wait for all of them, keep what each lane reports. False when
        no lane accepted it."""
        rep = {"k": k, "order": order, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "finished": None,
               "lanes": {l.id: {"run": None, "error": None, "state": "queued"} for l in chosen}}
        with self.lock:
            race["reps"].append(rep)
            race["rep"], race["lanes"] = k, rep["lanes"]
        body = launch | {"series": {"race": race["id"], "rep": k, "of": race["repeats"]}, "control_order": order}
        if k > 1:
            body["meter"] = False  # one series is one launch against the lane's hourly cap
        got = self._fan(lambda l: self.call(l, "/api/runs", body | {"harness": l.harness}, client), chosen)
        with self.lock:
            for lane in chosen:
                code, out = got.get(lane.id, (0, {}))
                run = out.get("id") if code == 202 and RUN_ID.match(str(out.get("id", ""))) else None
                rep["lanes"][lane.id] = {"run": run, "harness": lane.harness, "state": "queued" if run else "failed",
                                         "error": None if run else ("offline" if code == 0 else "not clean after its last run"
                                                                    if out.get("lane") == "reset_failed" else f"refused ({code})")}
        self._save(race)
        if not any(v["run"] for v in rep["lanes"].values()):
            rep["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return False
        deadline = time.time() + self.rep_timeout_s
        pending = {lid for lid, v in rep["lanes"].items() if v["run"]}
        while pending and race["state"] == "running":
            got = self._fan(lambda l: self.call(l, f"/api/runs/{rep['lanes'][l.id]['run']}"), [self.lane(x) for x in pending])
            for lid in list(pending):
                code, doc = got.get(lid, (0, {}))
                run = (doc or {}).get("run") if code == 200 else None
                if not isinstance(run, dict):
                    continue
                entry = dict(rep["lanes"][lid], **{key: self.scrub(run.get(key)) for key in LANE_KEEP if key in run})
                if run.get("state") in ("done", "failed"):
                    if run.get("state") == "failed":
                        entry["error"] = self.scrub(run.get("error") or "failed")
                    entry["export"] = self._archive(race, k, self.lane(lid), entry["run"])
                    pending.discard(lid)
                with self.lock:
                    rep["lanes"][lid] = entry
            if pending and time.time() > deadline:
                with self.lock:
                    for lid in pending:
                        rep["lanes"][lid] = dict(rep["lanes"][lid], state="timeout", error=f"no result within {self.rep_timeout_s} s")
                break
            if pending:
                time.sleep(self.poll_s)
        with self.lock:
            rep["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save(race)
        return True

    def export_dir(self, race_id: str) -> Path:
        return self.data / "export" / "v1" / "races" / race_id

    def _archive(self, race: dict, k: int, lane: Lane, run: str) -> bool:
        """The lane's numbers-only document for this repetition, kept on the front (lanes prune their runs)."""
        code, doc = self.call(lane, f"/api/runs/{run}/export")
        if code != 200 or not isinstance(doc, dict):
            return False
        path = self.export_dir(race["id"]) / f"rep-{k}" / f"{lane.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.scrub(doc), sort_keys=True))
        return True

    def _end(self, race: dict):
        with self.lock:
            if race["state"] != "running":
                return
            race["state"] = "done"
        doc = self.race_stats(race)
        path = self.export_dir(race["id"]) / "stats.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")))
        self._save(race)

    def race_stats(self, race: dict) -> dict:
        """Paired statistics over the repetitions so far (stats.py), cached until another repetition finishes."""
        with self.lock:
            snapshot = json.loads(json.dumps({k: race.get(k) for k in ("id", "size", "repeats", "reps", "lanes")}))
        stamp = (len([r for r in snapshot["reps"] or [] if r.get("finished")]), race.get("state"))
        cached = self.stats_cache.get(race["id"])
        if cached and cached[0] == stamp:
            return cached[1]
        if snapshot["reps"]:
            snapshot["reps"] = [r for r in snapshot["reps"] if r.get("finished")]
        doc = race_stats_mod.race_stats(snapshot)
        self.stats_cache[race["id"]] = (stamp, doc)
        return doc

    @staticmethod
    def _public_lane(v: dict) -> dict:
        row = {"started": bool(v.get("run")), "error": v.get("error"), "state": v.get("state")}
        for key in ("result", "control", "gate", "cohort", "refused", "warnings"):
            if v.get(key) is not None:
                row[key] = v[key]
        canary = v.get("canary") or {}
        if canary:
            row["canary"] = {"changed": canary.get("changed"), "stable": canary.get("stable")}
        if v.get("baseline_s") is not None:
            row["baseline_s"] = v["baseline_s"]  # races from before the lane control
        return row

    def public_race(self, race: dict) -> dict:
        with self.lock:
            lanes = {k: self._public_lane(v) for k, v in race["lanes"].items()}
            reps = [{"k": r["k"], "order": r.get("order"), "started": r.get("started"), "finished": r.get("finished"),
                     "lanes": {k: self._public_lane(v) for k, v in r["lanes"].items()}} for r in race.get("reps") or []]
            out = {"id": race["id"], "preset": race["preset"], "size": race["size"], "protocol": race.get("protocol") or "naive",
                   "created": race["created"], "model": race.get("model"), "state": race["state"], "lanes": lanes,
                   "repeats": race.get("repeats", 1), "rep": race.get("rep"), "reps": reps}
        return self.scrub(out)

    def refresh(self, race: dict) -> dict:
        """The driver thread settles a race; a race with no driver (from before a restart) is already settled."""
        return race

    def finish(self, race: dict):
        with self.lock:
            if race["state"] == "running":
                race["state"] = "done"
        self._save(race)

    def lane(self, lid: str) -> Lane | None:
        return next((l for l in self.lanes if l.id == lid), None)

    def stream(self, race: dict, out: queue.Queue, stop: threading.Event, k: int | None = None) -> int:
        """One thread per lane run of repetition k (default: the current one): read the lane's SSE and push scrubbed
        snapshots, tagged with k, onto `out`. Returns how many streams started."""
        def pump(lane: Lane, run: str):
            try:
                with urllib.request.urlopen(self._req(lane, f"/api/runs/{run}/events"), timeout=60) as r:
                    for raw in r:
                        if stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data: "):
                            try:
                                snap = json.loads(line[6:])
                            except ValueError:
                                continue
                            out.put({"lane": lane.id, "rep": k, "snap": self.scrub(snap)})
            except (urllib.error.URLError, OSError):
                out.put({"lane": lane.id, "rep": k, "error": "lane stream interrupted"})
            finally:
                out.put({"lane": lane.id, "rep": k, "end": True})

        lanes = self.rep_lanes(race, k)
        started = 0
        for lid, v in lanes.items():
            lane = self.lane(lid)
            if lane and v.get("run"):
                threading.Thread(target=pump, args=(lane, v["run"]), daemon=True).start()
                started += 1
        return started

    def rep_lanes(self, race: dict, k: int | None) -> dict:
        """The lanes of repetition k (1-based), or the current repetition's (also every race from before series)."""
        with self.lock:
            reps = race.get("reps") or []
            if k is not None and 1 <= k <= len(reps):
                return dict(reps[k - 1]["lanes"])
            return dict(race["lanes"])

    def transcript(self, race: dict, lid: str, n: int, k: int | None = None) -> tuple[int, dict]:
        lane, v = self.lane(lid), self.rep_lanes(race, k).get(lid, {})
        if not lane or not v.get("run"):
            return 404, {"error": "no such lane"}
        code, doc = self.call(lane, f"/api/runs/{v['run']}/agents/{n}")
        return (200, self.scrub(doc)) if code == 200 else (404, {"error": "no such agent"})


def handler_for(front: Front, static: Path):
    static = static.resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def log_message(self, *args):
            pass

        def _headers(self, status, ctype, length=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()

        def _send(self, status, body, ctype="application/json"):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self._headers(status, ctype, len(payload))
            self.wfile.write(payload)

        def _client(self) -> str:
            # The hosting platform's HTTPS proxy appends the visitor's address; take the last hop it added.
            fwd = self.headers.get("X-Forwarded-For", "")
            return (fwd.split(",")[-1].strip() if fwd else "") or self.client_address[0]

        def _race(self, rid):
            return front.races.get(rid) if RACE_ID.match(rid) else None

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            parts = path.strip("/").split("/")
            if path == "/api/healthz":
                return self._send(200, {"ok": True})
            if path == "/api/lanes":
                return self._send(200, {"lanes": front.lane_status(), "notice": front.notice, "catalog": front.catalog()})
            if path == "/api/presets":
                return self._send(200, {"presets": front.presets()})
            if path == "/api/races":
                rows = sorted(front.races.values(), key=lambda r: r["t"], reverse=True)[:20]
                for r in rows:
                    front.refresh(r)
                return self._send(200, {"races": [front.public_race(r) for r in rows]})
            if len(parts) >= 3 and parts[:2] == ["api", "races"]:
                race = self._race(parts[2])
                if not race:
                    return self._send(404, {"error": "no such race"})
                if len(parts) == 3:
                    return self._send(200, front.public_race(front.refresh(race)))
                if len(parts) == 4 and parts[3] == "events":
                    return self._events(race)
                if len(parts) == 4 and parts[3] == "stats":
                    return self._send(200, front.race_stats(race))
                if len(parts) == 7 and parts[3] == "lanes" and parts[5] == "agents" and parts[6].isdigit() and len(parts[6]) <= 5:
                    m = re.search(r"[?&]rep=(\d{1,2})(?:&|$)", self.path)
                    return self._send(*front.transcript(race, parts[4], int(parts[6]), int(m.group(1)) if m else None))
                return self._send(404, {"error": "not found"})
            if path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            return self._static(path)

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (static / rel).resolve()
            if static not in target.parents or not target.is_file() or target.suffix not in STATIC_TYPES:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix])

        def _events(self, race):
            """Follows the series: {rep: k, start: true} opens repetition k, lane messages carry rep. At the 900 s
            limit a running series ends the stream with {more: true}, and the page reattaches."""
            self._headers(200, "text/event-stream")
            self.close_connection = True
            stop = threading.Event()
            self._emit({"race": front.public_race(race)})
            deadline = time.monotonic() + 900
            k = max(1, race.get("rep") or 1)
            try:
                while time.monotonic() < deadline:
                    # Wait for the driver to launch repetition k (its run ids), or for the race to settle.
                    while race.get("state") == "running" and (race.get("rep") or 0) < k and time.monotonic() < deadline:
                        self._idle(1)
                    lanes = front.rep_lanes(race, k if race.get("reps") else None)
                    while race.get("state") == "running" and all(v.get("state") == "queued" and not v.get("run") and not v.get("error")
                                                                 for v in lanes.values()) and time.monotonic() < deadline:
                        self._idle(0.5)
                        lanes = front.rep_lanes(race, k if race.get("reps") else None)
                    q = queue.Queue()
                    live = front.stream(race, q, stop, k if race.get("reps") else None)
                    self._emit({"rep": k, "start": True})
                    while live and time.monotonic() < deadline:
                        try:
                            msg = q.get(timeout=10)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if msg.get("end"):
                            live -= 1
                        self._emit(msg)
                    if live:
                        break  # out of time mid-repetition
                    if k < (race.get("repeats") or 1) and race.get("state") == "running":
                        k += 1
                        continue
                    # Last repetition streamed: wait for the driver to settle the race (results, stats).
                    while race.get("state") == "running" and time.monotonic() < deadline:
                        self._idle(1)
                    break
                if race.get("state") == "running":
                    self._emit({"race": front.public_race(race), "more": True})
                else:
                    self._emit({"race": front.public_race(race), "end": True})
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                stop.set()

        def _emit(self, msg):
            self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
            self.wfile.flush()

        def _idle(self, s):
            time.sleep(s)

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/api/races":
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
            return self._send(*front.start_race(str(body.get("preset", "")), body.get("size"), self._client(),
                                                str(key)[:128] if key else None, body.get("lanes"), body.get("model"),
                                                str(body["effort"])[:16] if body.get("effort") else None,
                                                str(body["protocol"])[:16] if body.get("protocol") else None,
                                                body.get("repeats", 1)))

    return Handler


def serve_front(lanes: Path, data: Path, static: Path, host="127.0.0.1", port=8090, **kw):
    front = Front(lanes, data, passkey=os.environ.get("SWARM_FRONT_PASSKEY") or None, **kw)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server((host, port), handler_for(front, static))
    print(f"swarm front on {host}:{port} · {len(front.lanes)} lanes: {', '.join(l.id for l in front.lanes)}", flush=True)
    server.serve_forever()
