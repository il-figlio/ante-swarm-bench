"""The numbers-only document for one lane run (GET /api/runs/<id>/export).

Everything here is an allow-list: a key is copied only when it is named below, and a string only when it is a short
code (a hash, id, version or tool name). No paths, hostnames, logs, tool arguments, prompt text or client data can get
through, whatever a bundle row happens to carry.
"""
import calendar
import json
import re
import time
from pathlib import Path

from . import stats
from .front import HOST, IPV4, IPV6
from .live import HOME, KEY
from .util import read_jsonl, sha256_bytes

# A short code: letters, digits and the few marks ids, versions, timestamps and tool names use. Never a path or a URL.
CODE = re.compile(r"^[A-Za-z0-9 ._:+()#|-]{1,80}$")
# Anything shaped like a domain name (any TLD, not only the lanes' own) or an IPv6 address is refused as well.
DOMAIN = re.compile(r"\b[a-z0-9-]+\.[a-z]{2,}(?![a-z0-9_])", re.I)

RUN_KEYS = ("id", "preset", "size", "protocol", "model", "effort", "harness", "harness_version", "harness_sha256",
            "adapter_sha256", "cohort", "plan_sha256", "state", "refused", "created", "started", "finished", "verify",
            "chain_head", "series", "settings", "warnings", "result", "gate")
CANARY_KEYS = ("sha256", "parts", "stable", "distinct", "changed", "parts_changed", "binary_changed")
CONTROL_KEYS = ("state", "reason", "key", "order", "harness", "harness_sha256", "python", "plan_sha256", "attempts",
                "passes", "values", "median", "noisy", "canary_sha256")
FIRST_KEYS = ("wire", "canonical_bytes", "system_bytes", "tools_bytes", "user_bytes", "user_text_bytes",
              "instruction_bytes", "injected_bytes", "other_bytes", "tool_count", "mcp_tool_count", "request_bytes")
REQUEST_IDS = ("request_id", "prev_request_id", "prev_source")
TOKENS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")


def scrub(value):
    """Numbers, bools, None and short codes, kept through lists and dicts; everything else is dropped."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):  # a hostname or address is short enough to look like a code, so it is refused by shape too
        return value if CODE.match(value) and not any(p.search(value) for p in (HOST, IPV4, IPV6, HOME, KEY, DOMAIN)) else None
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if isinstance(k, str) and scrub(k) is not None}
    return None


def pick(source, keys) -> dict:
    source = source if isinstance(source, dict) else {}
    return {k: scrub(source.get(k)) for k in keys if k in source}


def tools_of(first: dict) -> list:
    names = first.get("tools_offered")
    return [n for n in scrub(names) if isinstance(n, str)] if isinstance(names, list) else []


def first_request(a: dict) -> dict | None:
    first = (a.get("requests") or {}).get("first_request")
    if not isinstance(first, dict):
        return None
    canary = first.get("canary") if isinstance(first.get("canary"), dict) else {}
    return pick(first, FIRST_KEYS) | {"canary_sha256": scrub(canary.get("sha256")), "tools": tools_of(first)}


def row(a: dict, control: bool) -> dict:
    agent, timing, req, setup = (a.get(k) or {} for k in ("agent", "timing", "requests", "setup"))
    out = {"attempt": scrub(a.get("attempt")), "task": scrub(a.get("task")), "passed": a.get("passed"),
           "failure": scrub(a.get("failure")), "infra_reason": scrub(a.get("infra_reason")),
           "wall_s": scrub(agent.get("wall_s")), "cpu_s": None if agent.get("cpu_s_partial") else scrub(agent.get("cpu_s")),
           "rss_kb": scrub(agent.get("max_process_rss_kb")), "disk_kb": scrub(agent.get("disk_kb")),
           "setup_s": scrub(setup.get("setup_s")), "first_request_s": scrub(timing.get("first_request_s")),
           "step_gap_s_median": scrub(timing.get("step_gap_s_median")), "context_slope": scrub(timing.get("context_slope")),
           "model_turns": scrub(req.get("model_turns")), "probes": scrub(req.get("probes")),
           "tokens": pick(req, TOKENS), "first_request": first_request(a)}
    return out | ({"control": True} if control else {})


def rows(bundle: Path, control: bool = False) -> list[dict]:
    path = bundle / "attempts.jsonl"
    return [row(a, control) for a in read_jsonl(path)] if path.exists() else []


def request_row(r: dict, origin: float | None, attempt: str, control: bool) -> dict:
    def since(key):
        v = r.get(key)
        return round(v - origin, 6) if isinstance(v, (int, float)) and origin is not None else None
    # A provider's response id is the provider's, not ours to publish: only its hash, which still shows repeats.
    upstream = r.get("upstream_id")
    out = {"attempt": scrub(attempt), **pick(r, REQUEST_IDS),
           "upstream_sha12": sha256_bytes(upstream.encode())[:12] if isinstance(upstream, str) and upstream else None,
           "helper": bool(r.get("helper")), "kind": scrub(r.get("kind")),
           "model": scrub(r.get("model")), "request_bytes": scrub(r.get("request_bytes")), "tokens": pick(r, TOKENS),
           "recv_s": since("recv_mono"), "sent_s": since("sent_mono")}
    return out | ({"control": True} if control else {})


def requests(bundle: Path, control: bool = False) -> list[dict]:
    out = []
    for path in sorted((bundle / "logs").glob("*/requests.jsonl")):
        found = [r for r in read_jsonl(path) if isinstance(r, dict)]
        monos = [r["recv_mono"] for r in found if isinstance(r.get("recv_mono"), (int, float))]
        out += [request_row(r, min(monos) if monos else None, path.parent.name, control) for r in found]
    return out


def run_document(run_dir, rec: dict) -> dict:
    """One lane run as numbers: the run's fields, one row per attempt (the control's too) and one per model request."""
    run_dir = Path(run_dir)
    doc = pick(rec, RUN_KEYS)
    doc["canary"] = pick(rec.get("canary"), CANARY_KEYS) if rec.get("canary") else None
    doc["control"] = pick(rec.get("control"), CONTROL_KEYS) if rec.get("control") else None
    bundle, control = run_dir / "bundle", run_dir / "control" / "bundle"
    doc["rows"] = rows(bundle) + rows(control, True)
    doc["requests"] = requests(bundle) + requests(control, True)
    return doc


# ---- the export tree (swarm export) --------------------------------------------------------------------

STALE_S = 60 * 86400
ENTRY_KEYS = ("harness", "state", "error", "result", "control", "gate", "cohort", "canary", "refused", "harness_version",
              "harness_sha256", "adapter_sha256", "warnings", "verify")
LANE_FIELDS = ("attempts", "passes", "not_run") + tuple(dict.fromkeys(m[0] for m in stats.METRICS.values()))


def dump(value) -> str:
    """One deterministic form for every file, so a rebuild is byte-identical."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def load_races(front_data: Path) -> list[dict]:
    """races.jsonl is append-only: the last line of an id wins. Unreadable lines are skipped, as the front does."""
    races = {}
    path = front_data / "races.jsonl"
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            r = json.loads(line)
            races[r["id"]] = r
        except (ValueError, KeyError, TypeError):
            continue
    return [races[k] for k in sorted(races)]


def race_time(race: dict) -> float | None:
    if isinstance(race.get("t"), (int, float)):
        return race["t"]
    try:
        return calendar.timegm(time.strptime(race.get("created"), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def entry_of(raw) -> dict:
    """A lane's record in a repetition, numbers and codes only. An error stays an error even when its text is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out = pick(raw, ENTRY_KEYS)
    out["canary"] = pick(raw.get("canary"), CANARY_KEYS) if isinstance(raw.get("canary"), dict) else None
    out["control"] = pick(raw.get("control"), CONTROL_KEYS) if isinstance(raw.get("control"), dict) else None
    if raw.get("error") and out.get("error") is None:
        out["error"] = "redacted"
    return out


def reps_of(race: dict) -> list[dict]:
    """Every repetition of the series; an old race without them is one repetition of its current lanes."""
    reps = race.get("reps") or [{"k": 0, "finished": race.get("created") or "legacy", "lanes": race.get("lanes")}]
    out = []
    for i, rep in enumerate(reps):
        rep = rep if isinstance(rep, dict) else {}
        lanes = rep.get("lanes") if isinstance(rep.get("lanes"), dict) else {}
        k = rep["k"] if isinstance(rep.get("k"), int) else i
        out.append({"k": k, "order": scrub(rep.get("order")), "started": scrub(rep.get("started")),
                    "finished": scrub(rep.get("finished")), "lanes": {l: entry_of(e) for l, e in sorted(lanes.items()) if scrub(l)}})
    return out


def lane_doc(front_data: Path, race: str, k: int, lane: str) -> dict | None:
    try:
        doc = json.loads((front_data / "export" / "v1" / "races" / race / f"rep-{k}" / f"{lane}.json").read_text())
    except (OSError, ValueError):
        return None
    return scrub(doc) if isinstance(doc, dict) else None


def lane_summary(reps: list[dict]) -> dict:
    """Each repetition's medians (the lane's own `result`); a repetition that gave none is null, never zero."""
    out = {}
    for rep in reps:
        for lane, e in rep["lanes"].items():
            r = e.get("result") if isinstance(e.get("result"), dict) else None
            out.setdefault(lane, {})[str(rep["k"])] = {"state": e.get("state"), "cohort": e.get("cohort"),
                                                         "values": {f: r.get(f) for f in LANE_FIELDS} if r else None}
    return out


def summary_of(race_id: str, size, repeats, reps: list[dict]) -> dict:
    """Everything here comes from the repetition records alone, so tools/rebuild_export.py can recompute it."""
    done = [r for r in reps if r.get("finished")]  # as the front: only finished repetitions count
    snapshot = {"id": race_id, "size": size, "repeats": repeats, "reps": done, "lanes": {}}
    return {"lanes": lane_summary(reps), "stats": stats.race_stats(snapshot)}


def lane_cohorts(reps: list[dict]) -> dict:
    out = {}
    for rep in reps:
        for lane, e in rep["lanes"].items():
            if e.get("cohort") and e["cohort"] not in out.setdefault(lane, []):
                out[lane].append(e["cohort"])
    return out


def superseded(infos: list[dict]) -> dict:
    """POLICY rule 3: per (race, lane), the newest cohort of that lane/preset/size/protocol/model supersedes older ones."""
    groups = {}
    for i in infos:
        for lane, cohorts in i["cohorts"].items():
            key = (lane, i["preset"], i["size"], i["protocol"], i["model"])
            groups.setdefault(key, []).append((race_time(i["race"]) or 0, i["id"], cohorts))
    out = {i["id"]: {} for i in infos}
    for key, found in groups.items():
        found.sort(key=lambda f: (f[0], f[1]))
        newest_id, newest = found[-1][1], found[-1][2][-1]
        for _, rid, cohorts in found[:-1]:
            if any(c != newest for c in cohorts):
                out[rid][key[0]] = newest
    return out


def manifest_of(race: dict, reps: list[dict], docs: dict) -> dict:
    lanes = {}
    for lane in sorted({l for r in reps for l in r["lanes"]}):
        entries = [(r["k"], r["lanes"][lane]) for r in reps if lane in r["lanes"]]
        k, e = entries[-1]
        doc = docs.get((k, lane)) or {}
        control = doc.get("control") or e.get("control") or {}
        settings = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
        sha = doc.get("harness_sha256") or e.get("harness_sha256")
        lanes[lane] = {"harness": e.get("harness"), "harness_version": doc.get("harness_version") or e.get("harness_version"),
                       "harness_sha256": sha[:12] if isinstance(sha, str) else None,
                       "adapter_sha256": doc.get("adapter_sha256") or e.get("adapter_sha256"),
                       "canary_sha256": (doc.get("canary") or e.get("canary") or {}).get("sha256"),
                       "control_key": control.get("key"), "gate_mode": scrub(settings.get("gate")), "gate_limits": scrub(settings.get("gate_limits")),
                       "plan_sha256": doc.get("plan_sha256"), "cohorts": lane_cohorts(reps).get(lane, [])}
    return {"id": scrub(race.get("id")), "preset": scrub(race.get("preset")), "size": scrub(race.get("size")),
            "protocol": scrub(race.get("protocol")), "model": scrub(race.get("model")), "effort": scrub(race.get("effort")),
            "kind": scrub(race.get("kind")), "repeats": scrub(race.get("repeats")), "seed": scrub(race.get("seed")),
            "state": scrub(race.get("state")), "interrupted": race.get("state") == "interrupted", "lanes": lanes,
            "times": {"created": scrub(race.get("created")),
                      "reps": [{"k": r["k"], "started": r["started"], "finished": r["finished"]} for r in reps]}}


def lines(items: list[dict]) -> str:
    return "".join(dump(i) for i in items)


def build(front_data, out, now: float | None = None) -> dict:
    """Write the numbers-only export tree for every race in a front data dir. `now` pins staleness for tests."""
    front_data, out = Path(front_data), Path(out)
    now = time.time() if now is None else now
    root, infos, counts = out / "v1", [], {"rows": 0, "requests": 0, "gates": 0}
    for race in load_races(front_data):
        rid = scrub(race.get("id"))
        if not rid:
            continue
        reps = reps_of(race)
        docs = {(r["k"], l): d for r in reps for l in r["lanes"] if (d := lane_doc(front_data, rid, r["k"], l))}
        rows, reqs, gates = [], [], []
        for (k, lane), doc in sorted(docs.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            tag = {"race": rid, "rep": k, "lane": lane}
            rows += [tag | r for r in doc.get("rows") or []]
            reqs += [tag | r for r in doc.get("requests") or []]
            g = doc.get("gate") if doc.get("gate") is not None else reps[[r["k"] for r in reps].index(k)]["lanes"][lane].get("gate")
            gates += [tag | r for r in (g if isinstance(g, list) else [g] if isinstance(g, dict) else []) if isinstance(r, dict)]
        manifest = manifest_of(race, reps, docs)
        base = root / "races" / rid
        write(base / "manifest.json", dump(manifest))
        write(base / "reps.jsonl", lines([{"k": r["k"], "order": r["order"], "started": r["started"], "finished": r["finished"],
                                           "lanes": r["lanes"]} for r in reps]))
        write(base / "rows.jsonl", lines(rows))
        write(base / "requests.jsonl", lines(reqs))
        write(base / "gates.jsonl", lines(gates))
        write(base / "summary.json", dump(summary_of(rid, manifest["size"], manifest["repeats"], reps)))
        counts["rows"] += len(rows)
        counts["requests"] += len(reqs)
        counts["gates"] += len(gates)
        t = race_time(race)
        infos.append({"id": rid, "race": race, "preset": manifest["preset"], "size": manifest["size"], "protocol": manifest["protocol"],
                      "model": manifest["model"], "repeats": manifest["repeats"], "state": manifest["state"], "created": manifest["times"]["created"],
                      "interrupted": manifest["interrupted"], "cohorts": lane_cohorts(reps),
                      "stale": t is not None and now - t > STALE_S})
    sup = superseded(infos)
    races = [{k: v for k, v in i.items() if k != "race"} | {"superseded_by": {l: sup[i["id"]].get(l) for l in i["cohorts"]}} for i in infos]
    cohorts = {}
    for i in infos:
        for lane, cs in i["cohorts"].items():
            for c in cs:
                cohorts.setdefault(c, {"lane": lane, "preset": i["preset"], "size": i["size"], "protocol": i["protocol"],
                                       "model": i["model"], "races": []})["races"].append(i["id"])
    write(root / "index.json", dump({"races": races, "cohorts": cohorts}))
    return {"out": str(out), "races": len(races), "interrupted": sum(1 for r in races if r["interrupted"]),
            "stale": sum(1 for r in races if r["stale"]), "superseded": sum(1 for r in races if any(r["superseded_by"].values())), **counts}
