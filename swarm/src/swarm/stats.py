"""Repetition statistics for a race series (docs/evaluation.md, "Repetitions and ties"). Reads the race record and nothing else."""
from __future__ import annotations

import random
from bisect import bisect_left
from functools import lru_cache
from itertools import accumulate, combinations
from math import comb

from .summary import quantile

MIN_PAIRS, RESAMPLES = 6, 10_000

# page key -> (field in a lane run's `result`, better end, field in control.median or None, gated).
# Gated metrics are left out of a repetition where under half the agents passed (as the page's `thin`).
# Fields below marked (new) are additions to `result`; the rest exist in serve.py `_execute` today.
METRICS = {
    "pass":    ("passes", "high", None, False),
    "per":     ("mem_per_agent_mb", "low", None, True),             # (new)
    "rss":     ("peak_rss_mb_median", "low", None, True),
    "cpu":     ("cpu_s_median", "low", "cpu_s", True),               # (new)
    "disk":    ("disk_kb_median", "low", None, True),               # (new)
    "start":   ("first_request_s_median", "low", "first_request_s", False),
    "gap":     ("step_gap_s_median", "low", "step_gap_s", True),
    "slope":   ("context_slope_median", "low", None, True),
    "peakmem": ("agents_mem_mb_peak", "low", None, True),           # (new)
    # Agent wall, like the control's own wall_s (the batch wall of a 5-round run is not like with like).
    "wall":    ("wall_s_median", "low", "wall_s", True),            # control only at size 1
    "vh":      ("successes_per_minute", "high", None, False),       # x60 = passed per VM-hour
    "first":   ("first_request_bytes_median", "low", None, False),  # (new)
}
SCALE = {"vh": 60}
SIZE_ONE = {"wall"}  # normalised by the control only when the race had one agent


def sig(x, digits=6):
    """Six significant digits: the same bytes on every platform."""
    if isinstance(x, float):
        return float(f"{x:.{digits}g}")
    return x


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def spread(xs) -> dict:
    v = [x for x in xs if x is not None]
    return {"n": len(v), "median": quantile(v, 0.5), "p25": quantile(v, 0.25), "p75": quantile(v, 0.75),
            "min": min(v) if v else None, "max": max(v) if v else None}


# ---- repetitions -> values -----------------------------------------------------------------------------

def reps_of(race: dict) -> list[dict]:
    if race.get("reps"):
        return race["reps"]
    return [{"k": 0, "lanes": race.get("lanes") or {}}]


def is_floor(lane: str, entries: list[dict]) -> bool:
    """The mini harness is the floor, not a contender."""
    return lane == "mini" or any("mini" in (e.get("harness"), (e.get("result") or {}).get("harness")) for e in entries)


def cohort_changed(entries: list[dict]) -> bool:
    return len({e["cohort"] for e in entries if e.get("cohort")}) > 1


def control_reason(entry: dict, field: str) -> str | None:
    c = entry.get("control") or {}
    if not c:
        return "control_missing"
    if c.get("state") != "done":
        return "control_" + str(c.get("state") or "missing")
    if c.get("noisy"):
        return "control_noisy"
    v = (c.get("median") or {}).get(field)
    return None if v else "control_missing"  # a zero or absent control cannot be a divisor


def modal_key(reps: list[dict]):
    count = {}
    for rep in reps:
        for e in rep["lanes"].values():
            k = (e.get("control") or {}).get("key")
            if k:
                count[k] = count.get(k, 0) + 1
    return max(sorted(count), key=count.get) if count else None


def entry_reason(entry, gated: bool) -> str | None:
    if entry is None:
        return "missing"
    if entry.get("error"):
        return "error"
    result = entry.get("result")
    if entry.get("state") not in ("done", None) or (entry.get("state") is None and not result):
        return "not_done"
    if not result:
        return "no_result"
    attempts, passes = result.get("attempts"), result.get("passes")
    if gated and attempts and passes is not None and 2 * passes < attempts - (result.get("not_run") or 0):
        return "under_half_passed"
    return None


def relative(race: dict, key: str) -> bool:
    return METRICS[key][2] is not None and (key not in SIZE_ONE or race.get("size") == 1)


def rep_values(race: dict):
    """-> ({metric: {lane: [value | None per repetition]}}, {metric: {lane: [reason | None]}})."""
    reps = reps_of(race)
    lanes = sorted({l for r in reps for l in r["lanes"]})
    entries = {l: [r["lanes"].get(l) for r in reps] for l in lanes}
    changed = {l for l in lanes if cohort_changed([e for e in entries[l] if e])}
    usual = modal_key(reps)
    values, reasons = {}, {}
    for key, (field, _, ctl, gated) in METRICS.items():
        values[key], reasons[key] = {}, {}
        for lane in lanes:
            vs, rs = [], []
            for e in entries[lane]:
                v, why = None, entry_reason(e, gated)
                if why is None and lane in changed:
                    why = "cohort_changed"
                if why is None:
                    v = (e["result"] or {}).get(field)
                    if v is not None:
                        v = v * SCALE.get(key, 1)
                    else:
                        why = "no_value"
                if why is None and relative(race, key):
                    why = control_reason(e, ctl)
                    if why is None and usual and e["control"].get("key") != usual:
                        why = "control_key"
                    if why is None:
                        v = v / e["control"]["median"][ctl]
                if why:
                    v = None
                vs.append(v)
                rs.append(why)
            values[key][lane], reasons[key][lane] = vs, rs
    return values, reasons


# ---- paired differences --------------------------------------------------------------------------------

def _tail(n: int, p: float, k: int) -> float:
    """P(Binomial(n, p) >= k)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(comb(n, c) * p ** c * (1 - p) ** (n - c) for c in range(k, n + 1))


@lru_cache(maxsize=None)
def _median_law(n: int):
    """Exact law of the median of a resample of n ranks: [(i, j, p)], the median is (x_i + x_j) / 2.

    A resample's median depends on the sorted sample only through which ranks it lands on, so the law is exact
    for every sample of size n. Drawing from it is the bootstrap with unlimited resamples, at a cost that fits
    10,000 draws x 1,000 pairs in pure Python."""
    if n % 2:
        m = (n + 1) // 2
        cdf = [_tail(n, (r + 1) / n, m) for r in range(n)]
        return tuple((r, r, max(0.0, cdf[r] - (cdf[r - 1] if r else 0.0))) for r in range(n))
    m = n // 2

    def G(a, b):  # P(x_(m) <= a, x_(m+1) <= b) over ranks, a = -1 gives 0
        if a < 0 or b < 0:
            return 0.0
        if a >= b:
            return _tail(n, (b + 1) / n, m + 1)
        pa, q = (a + 1) / n, (b - a) / (n - a - 1)
        return sum(comb(n, c) * pa ** c * (1 - pa) ** (n - c) * (1.0 if c > m else _tail(n - c, q, m + 1 - c))
                   for c in range(m, n + 1))
    g = [[G(a, b) for b in range(-1, n)] for a in range(-1, n)]  # index shifted by one
    law = []
    for i in range(n):
        for j in range(i, n):
            p = g[i + 1][j + 1] - g[i][j + 1] - g[i + 1][j] + g[i][j]
            law.append((i, j, max(0.0, p)))
    return tuple(law)


def boot_ci(d: list[float], rnd: random.Random, resamples: int = RESAMPLES) -> list[float]:
    """Percentile 95% interval of the median of resampled values; draws come from `rnd`."""
    s = sorted(d)
    cells = sorted(((s[i] + s[j]) / 2, p) for i, j, p in _median_law(len(s)))
    cum = list(accumulate(p for _, p in cells))
    total, vals = cum[-1], [v for v, _ in cells]
    u = sorted(rnd.random() * total for _ in range(resamples))  # sorted uniforms map to sorted medians
    at = lambda k: vals[min(bisect_left(cum, u[k]), len(vals) - 1)]
    out = []
    for q in (0.025, 0.975):
        pos = (resamples - 1) * q
        lo = int(pos)
        hi = min(lo + 1, resamples - 1)
        out.append(at(lo) + (at(hi) - at(lo)) * (pos - lo))
    return out


def gap_tie(a: float, b: float) -> bool:
    """The page's 10% rule (heat.js rank): a gap under 10% of the larger magnitude is not a difference."""
    span = max(abs(a), abs(b))
    return span == 0 or abs(a - b) / span < 0.1


def paired(a: list, b: list, seed: str, resamples: int = RESAMPLES, min_pairs: int = MIN_PAIRS) -> dict:
    ok = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(ok)
    out = {"n": n, "diff": [x - y for x, y in ok], "diff_median": None, "ci95": None, "tie": None, "rule": "gap10"}
    if not n:
        return out
    out["diff_median"] = median(out["diff"])
    if n >= min_pairs:
        lo, hi = boot_ci(out["diff"], random.Random(seed), resamples)
        out.update(ci95=[lo, hi], tie=lo <= 0 <= hi, rule="bootstrap")
    else:
        out["tie"] = gap_tie(median([x for x, _ in ok]), median([y for _, y in ok]))
    return out


# ---- the /stats document -------------------------------------------------------------------------------

def vs_control(race: dict, key: str, lane: str, vals: list, resamples: int) -> dict | None:
    """The lane's ratio to its control is 1 when it matches: the difference is ratio - 1."""
    if not relative(race, key):
        return None
    d = [v - 1 for v in vals if v is not None]
    if not d:
        return {"diff_median": None, "ci95": None}
    ci = boot_ci(d, random.Random(f"{race.get('id')}:{key}:{lane}:control"), resamples) if len(d) >= MIN_PAIRS else None
    return {"diff_median": median(d), "ci95": ci}


def race_stats(race: dict, resamples: int = RESAMPLES) -> dict:
    reps = reps_of(race)
    values, reasons = rep_values(race)
    lanes = sorted(values["pass"])
    entries = {l: [r["lanes"].get(l) for r in reps] for l in lanes}
    floor = {l for l in lanes if is_floor(l, [e for e in entries[l] if e])}
    out, rules = {}, []
    for key in METRICS:
        rel = relative(race, key)
        lane_out = {}
        for lane in lanes:
            vals, why = values[key][lane], reasons[key][lane]
            sp = spread(vals)
            bad = "floor: not ranked" if lane in floor else None
            if "cohort_changed" in why:
                bad, sp = "cohort_changed", spread([])
            elif not sp["n"] and not bad:
                bad = next((r for r in why if r), None)
            lane_out[lane] = {"values": [sig(v) for v in (vals if bad != "cohort_changed" else [None] * len(vals))],
                              **{k: sig(v) for k, v in sp.items()},
                              "vs_control": vs_control(race, key, lane, vals, resamples) if not bad else None,
                              "excluded": bad, "reasons": why}
            vc = lane_out[lane]["vs_control"]
            if vc:
                vc["diff_median"], vc["ci95"] = sig(vc["diff_median"]), vc["ci95"] and [sig(x) for x in vc["ci95"]]
        ranked = [l for l in lanes if not lane_out[l]["excluded"]]
        pairs = []
        for a, b in combinations(ranked, 2):
            p = paired(values[key][a], values[key][b], f"{race.get('id')}:{key}:{a}:{b}", resamples)
            rules.append(p["rule"])
            pairs.append({"a": a, "b": b, "n": p["n"], "diff_median": sig(p["diff_median"]),
                          "ci95": p["ci95"] and [sig(x) for x in p["ci95"]], "tie": p["tie"]})
        out[key] = {"relative_to_control": rel, "lanes": lane_out, "pairs": pairs}
    rule = "bootstrap" if rules and all(r == "bootstrap" for r in rules) else "gap10"
    return {"n": len(reps), "rule": rule, "min_pairs": MIN_PAIRS, "resamples": resamples, "metrics": out}
