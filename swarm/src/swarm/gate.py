"""Quiet gate: is this lane quiet enough to measure? Every reading is taken fresh; unknown stays None.

The gate only judges. `ok` is the verdict and `mode` (off | observe | enforce) is recorded; whoever calls it decides
whether a failing verdict refuses the run. Reset = clean after, gate = quiet before, admission = headroom during.
"""
import os
import time

from . import host
from . import reset as lane_reset
from .util import utc_now

LIMITS = {"max_swap_mb": 0, "max_cpu_cores": 0.10, "quiet_s": 5, "wait_s": 120, "allow_unknown": False}
MODES = ("off", "observe", "enforce")
LEFTOVERS = ("agent_processes_left", "sandbox_roots_left")
WORDS = {
    "swap_in_use": "swap in use", "swap_unknown": "swap unknown",
    "cpu_busy": "other CPU busy", "cpu_steal": "CPU stolen by the host", "cpu_unknown": "CPU unknown",
    "memory_below_baseline": "memory below baseline", "memory_unknown": "memory unknown",
    "disk_low": "disk low", "disk_unknown": "disk unknown",
    "agent_processes_left": "leftover agent processes", "agent_processes_unknown": "agent processes unknown",
    "sandbox_roots_left": "leftover sandboxes",
    "pressure_warn": "memory pressure warn", "pressure_critical": "memory pressure critical",
    "pressure_unknown": "memory pressure unknown",
}


def limits(tolerance_mb: int, min_free_mb: int, **overrides) -> dict:
    """LIMITS plus the two the caller owns (the reset's tolerance; max(reset_min_free_mb, reserve_disk_mb))."""
    return {**LIMITS, "tolerance_mb": tolerance_mb, "min_free_mb": min_free_mb, **overrides}


def _own_cpu_s() -> float:
    t = os.times()
    return t.user + t.system


def _processes():
    found = lane_reset.agent_processes(own=True)
    return None if found is None else len(found[0])


def default_readers() -> dict:
    return {"memory_state": host.memory_state, "cpu_breakdown": host.cpu_breakdown, "own_cpu": _own_cpu_s,
            "psi": host.psi_cpu_some_avg10, "disk_free": lambda path: host.disk_free_mb(path),
            "agent_processes": _processes, "runner_roots": lambda tmp: len(lane_reset.runner_roots(tmp)),
            "clock": time.monotonic, "sleep": time.sleep}


def _count(value):
    return len(value) if isinstance(value, (list, tuple, dict, set)) else value  # counts only, never paths


def _cpu_window(r, quiet_s) -> dict:
    """Other, own and steal cores averaged over a window sampled each second (the endpoints give the mean)."""
    t0, c0, o0 = r["clock"](), r["cpu_breakdown"](), r["own_cpu"]()
    for _ in range(max(1, int(quiet_s))):
        r["sleep"](1)
    t1, c1, o1 = r["clock"](), r["cpu_breakdown"](), r["own_cpu"]()
    dt = t1 - t0
    if c0 is None or c1 is None or o0 is None or o1 is None or dt <= 0:
        return {"other": None, "own": None, "steal": None}
    busy, own = (c1["busy"] - c0["busy"]) / dt, (o1 - o0) / dt
    return {"other": round(max(0.0, busy - own), 3), "own": round(max(0.0, own), 3),
            "steal": round(max(0.0, (c1["steal"] - c0["steal"]) / dt), 3)}


def _readings(lim, baseline, tmp, data, r) -> dict:
    memory = r["memory_state"]()
    disks = [r["disk_free"](p) for p in (tmp, data) if p]
    cpu = _cpu_window(r, lim["quiet_s"])
    return {"swap_used_mb": memory.get("swap_used_mb"), "cpu_cores_other": cpu["other"], "cpu_cores_self": cpu["own"],
            "steal_cores": cpu["steal"], "psi_cpu_some_avg10": r["psi"](), "pressure": memory.get("pressure", "unknown"),
            "available_mb": memory.get("available_mb"), "baseline_available_mb": baseline,
            "disk_free_mb": None if not disks or None in disks else min(disks),
            "agent_processes": _count(r["agent_processes"]()), "sandbox_roots": _count(r["runner_roots"](tmp))}


def _reasons(v, lim) -> tuple[list, list]:
    """(hold reasons, unknown readings). Unknown readings are reasons too, until the caller allows them."""
    reasons, unknown = [], []

    def need(name, value, reason):
        if value is None:
            unknown.append(name)
            if not lim["allow_unknown"]:
                reasons.append(reason)
        return value is not None

    if need("swap_used_mb", v["swap_used_mb"], "swap_unknown") and v["swap_used_mb"] > lim["max_swap_mb"]:
        reasons.append("swap_in_use")
    if need("cpu_cores_other", v["cpu_cores_other"], "cpu_unknown"):
        if v["cpu_cores_other"] >= lim["max_cpu_cores"]:
            reasons.append("cpu_busy")
        elif v["steal_cores"] is not None and v["steal_cores"] >= lim["max_cpu_cores"]:
            reasons.append("cpu_steal")
    floor = None if v["baseline_available_mb"] is None else v["baseline_available_mb"] - lim["tolerance_mb"]
    if need("available_mb", None if floor is None else v["available_mb"], "memory_unknown") and v["available_mb"] < floor:
        reasons.append("memory_below_baseline")
    if need("disk_free_mb", v["disk_free_mb"], "disk_unknown") and v["disk_free_mb"] < lim["min_free_mb"]:
        reasons.append("disk_low")
    if need("agent_processes", v["agent_processes"], "agent_processes_unknown") and v["agent_processes"]:
        reasons.append("agent_processes_left")
    if v["sandbox_roots"]:
        reasons.append("sandbox_roots_left")
    if v["pressure"] == "unknown":
        unknown.append("pressure")
        if not lim["allow_unknown"]:
            reasons.append("pressure_unknown")
    elif v["pressure"] != "normal":
        reasons.append(f"pressure_{v['pressure']}")
    return reasons, unknown


def check(lim: dict, baseline_available_mb, tmp, data, readers=None, stage=None, mode="observe") -> dict:
    """One evaluation, including the quiet_s CPU window. `readers` overrides any of default_readers() by name."""
    lim = {**LIMITS, "tolerance_mb": 512, "min_free_mb": 0, **lim}
    r = {**default_readers(), **(readers or {})}
    values = _readings(lim, baseline_available_mb, tmp, data, r)
    reasons, unknown = _reasons(values, lim)
    return {"stage": stage, "at": utc_now(), "mode": mode, "ok": not reasons, "held_for": list(reasons),
            "reasons": reasons, "wait_s": 0, "quiet_s": lim["quiet_s"], "values": values,
            "limits": {k: lim[k] for k in ("max_swap_mb", "max_cpu_cores", "tolerance_mb", "min_free_mb", "wait_s")},
            "unknown": unknown}


def wait(lim: dict, baseline_available_mb, tmp, data, readers=None, on_leftovers=None, stage=None, mode="observe") -> dict:
    """Poll check until it passes or wait_s has passed. held_for gathers every reason seen along the way."""
    lim = {**LIMITS, **lim}
    r = {**default_readers(), **(readers or {})}
    start, held, called = r["clock"](), [], False
    while True:
        record = check(lim, baseline_available_mb, tmp, data, r, stage, mode)
        held += [x for x in record["reasons"] if x not in held]
        waited = r["clock"]() - start
        if record["ok"] or waited >= lim["wait_s"]:
            break
        if not called and on_leftovers and any(x in LEFTOVERS for x in record["reasons"]):
            called = True
            on_leftovers()
            continue  # look again at once, the cleanup is the point
        r["sleep"](1)
    record.update(held_for=held, wait_s=round(waited, 1))
    record["limits"]["wait_s"] = lim["wait_s"]
    return record


def words(reason: str) -> str:
    return WORDS.get(reason, reason.replace("_", " "))


def sentence(record: dict) -> str:
    """The reasons in words, with the figure where there is one: 'other CPU busy (0.6 cores), swap in use (120 MB)'."""
    v, out = record["values"], []
    for reason in record.get("reasons") or record.get("held_for") or []:
        figure = {"cpu_busy": f"{v['cpu_cores_other']} cores", "cpu_steal": f"{v['steal_cores']} cores",
                  "swap_in_use": f"{v['swap_used_mb']} MB", "disk_low": f"{v['disk_free_mb']} MB free",
                  "memory_below_baseline": f"{v['available_mb']} MB available",
                  "agent_processes_left": f"{v['agent_processes']}", "sandbox_roots_left": f"{v['sandbox_roots']}"}.get(reason)
        out.append(f"{words(reason)} ({figure})" if figure else words(reason))
    return ", ".join(out)


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(x) for k, x in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(x) for x in value]
    if isinstance(value, str) and (len(value) > 40 or "/" in value or value.startswith("~")):
        return None  # a path or a long string is not a code
    return value


def public(record: dict) -> dict:
    """The record for the page and run.json: numbers, booleans and short codes only."""
    return _clean(record)
