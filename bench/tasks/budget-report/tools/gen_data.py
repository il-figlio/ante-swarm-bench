#!/usr/bin/env python3
"""Generate the usage-export corpus and the ground-truth manifest.

Deterministic (fixed seed): re-running rewrites data/, caps.json,
prices.json and tests/manifest.json byte-identically.

    python3 tools/gen_data.py

Two weeks of daily exports sit under data/ as usage-YYYY-MM-DD.json —
one JSON array of call records per day: ts, key, team, model,
tokens_in, tokens_out, surface, and cost_usd on roughly two-thirds of
records (unpriced calls rate out from prices.json). caps.json is the
key registry: team, owner and the weekly USD cap per virtual key. The
manifest is the ground truth the checks read: aggregates, the over-cap
list, the top calls, and a sha256 of every input file.
"""
import hashlib
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261005

WEEK_START = date(2026, 10, 5)            # the current week, Monday
PREV_START = WEEK_START - timedelta(days=7)

# key -> team, owner, weekly USD cap. intern is registered but idle.
KEYS = {
    "vk-eng-alice":  {"team": "eng",  "owner": "alice",  "cap": 150.00},
    "vk-eng-bob":    {"team": "eng",  "owner": "bob",    "cap": 100.00},
    "vk-eng-priya":  {"team": "eng",  "owner": "priya",  "cap": 100.00},
    "vk-eng-intern": {"team": "eng",  "owner": "intern", "cap": 25.00},
    "vk-data-marta": {"team": "data", "owner": "marta",  "cap": 80.00},
    "vk-data-sam":   {"team": "data", "owner": "sam",    "cap": 60.00},
    "vk-ops-jules":  {"team": "ops",  "owner": "jules",  "cap": 40.00},
}

# model -> USD per million input / output tokens
PRICES = {
    "claude-sonnet-4":  {"in": 3.00, "out": 15.00},
    "claude-haiku-4":   {"in": 0.80, "out": 4.00},
    "gpt-4.1":          {"in": 2.00, "out": 8.00},
    "o4-mini":          {"in": 1.10, "out": 4.40},
    "gemini-2.5-pro":   {"in": 1.25, "out": 10.00},
}

# token ranges per model: (tokens_in lo/hi, tokens_out lo/hi)
SIZES = {
    "claude-sonnet-4":  (120_000, 600_000,  4_000, 40_000),
    "claude-haiku-4":   (30_000,  200_000,  2_000, 12_000),
    "gpt-4.1":          (200_000, 900_000,  3_000, 20_000),
    "o4-mini":          (25_000,  150_000,  2_000, 14_000),
    "gemini-2.5-pro":   (350_000, 1_100_000, 4_000, 28_000),
}

# per-key workload: calls/day range (weekday, weekend) and model mix
PROFILES = {
    "vk-eng-alice":  {"weekday": (22, 30), "weekend": (6, 10),
                      "mix": {"claude-sonnet-4": 70, "gemini-2.5-pro": 20,
                              "claude-haiku-4": 10}},
    "vk-eng-bob":    {"weekday": (10, 16), "weekend": (2, 5),
                      "mix": {"claude-sonnet-4": 40, "claude-haiku-4": 30,
                              "o4-mini": 30}},
    "vk-eng-priya":  {"weekday": (14, 20), "weekend": (3, 6),
                      "mix": {"claude-sonnet-4": 30, "o4-mini": 40,
                              "claude-haiku-4": 30}},
    "vk-eng-intern": {"weekday": (0, 0),   "weekend": (0, 0),
                      "mix": {}},
    "vk-data-marta": {"weekday": (16, 22), "weekend": (4, 8),
                      "mix": {"gpt-4.1": 30, "o4-mini": 45,
                              "claude-haiku-4": 25}},
    "vk-data-sam":   {"weekday": (12, 18), "weekend": (4, 8),
                      "mix": {"gpt-4.1": 55, "gemini-2.5-pro": 30,
                              "claude-sonnet-4": 15}},
    "vk-ops-jules":  {"weekday": (12, 20), "weekend": (1, 3),
                      "mix": {"claude-haiku-4": 60, "o4-mini": 40}},
}

SURFACES = {"ide": 55, "ci": 30, "chat": 15}
PRICED_FRACTION = 0.65        # share of records carrying cost_usd
PREV_SCALE = 0.82             # last week was quieter


def cost_of(rec):
    """Effective $ cost: cost_usd when the proxy priced the call,
    else rated from PRICES."""
    if "cost_usd" in rec:
        return rec["cost_usd"]
    p = PRICES[rec["model"]]
    return rec["tokens_in"] * p["in"] / 1e6 + rec["tokens_out"] * p["out"] / 1e6


def gen_day(rng, day, scale):
    """The call records for one day, sorted by timestamp."""
    recs = []
    for key in sorted(KEYS):
        prof = PROFILES[key]
        lo, hi = prof["weekend" if day.weekday() >= 5 else "weekday"]
        n = int(round(rng.randint(lo, hi) * scale))
        for _ in range(n):
            models, weights = zip(*prof["mix"].items())
            model = rng.choices(models, weights=weights)[0]
            ti_lo, ti_hi, to_lo, to_hi = SIZES[model]
            tin = rng.randint(ti_lo, ti_hi)
            tout = rng.randint(to_lo, to_hi)
            ts = "%sT%02d:%02d:%02dZ" % (
                day.isoformat(), rng.randint(6, 22),
                rng.randint(0, 59), rng.randint(0, 59))
            rec = {
                "ts": ts,
                "key": key,
                "team": KEYS[key]["team"],
                "model": model,
                "tokens_in": tin,
                "tokens_out": tout,
                "surface": rng.choices(
                    list(SURFACES), weights=list(SURFACES.values()))[0],
            }
            if rng.random() < PRICED_FRACTION:
                rec["cost_usd"] = round(
                    tin * PRICES[model]["in"] / 1e6
                    + tout * PRICES[model]["out"] / 1e6, 4)
            recs.append(rec)
    recs.sort(key=lambda r: r["ts"])
    return recs


def money(x):
    return round(x + 0.0, 2)


def aggregates(records):
    """Per-key and per-team sums over a record list."""
    keys = {k: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "spend": 0.0}
            for k in KEYS}
    for rec in records:
        k = keys[rec["key"]]
        k["calls"] += 1
        k["tokens_in"] += rec["tokens_in"]
        k["tokens_out"] += rec["tokens_out"]
        k["spend"] += cost_of(rec)
    teams = {}
    for key, k in keys.items():
        t = teams.setdefault(
            KEYS[key]["team"],
            {"calls": 0, "tokens_in": 0, "tokens_out": 0, "spend": 0.0})
        for f in ("calls", "tokens_in", "tokens_out", "spend"):
            t[f] += k[f]
    return keys, teams


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, obj, sort=False):
    path.write_text(
        json.dumps(obj, indent=1, sort_keys=sort) + "\n")


def main():
    rng = random.Random(SEED)

    week = [WEEK_START + timedelta(days=i) for i in range(7)]
    prev = [PREV_START + timedelta(days=i) for i in range(7)]

    for old in (ROOT / "data").glob("usage-*.json"):
        old.unlink()
    days = {}
    for d in prev:
        days[d] = gen_day(rng, d, PREV_SCALE)
    for d in week:
        days[d] = gen_day(rng, d, 1.0)
    for d, recs in days.items():
        write_json(ROOT / "data" / ("usage-%s.json" % d.isoformat()),
                   recs)

    write_json(ROOT / "caps.json", {
        k: {"team": v["team"], "owner": v["owner"],
            "weekly_cap_usd": v["cap"]}
        for k, v in sorted(KEYS.items())}, sort=True)
    write_json(ROOT / "prices.json", {
        m: {"in_usd_per_mtok": p["in"], "out_usd_per_mtok": p["out"]}
        for m, p in sorted(PRICES.items())}, sort=True)

    # ---- ground truth --------------------------------------------------
    cur = [r for d in week for r in days[d]]
    prv = [r for d in prev for r in days[d]]
    keys, teams = aggregates(cur)
    pkeys, pteams = aggregates(prv)

    total = sum(k["spend"] for k in keys.values())
    ptotal = sum(k["spend"] for k in pkeys.values())
    cap_total = sum(v["cap"] for v in KEYS.values())

    man_keys = {}
    for key in sorted(KEYS):
        k, cap = keys[key], KEYS[key]["cap"]
        calls = k["calls"]
        man_keys[key] = {
            "team": KEYS[key]["team"],
            "owner": KEYS[key]["owner"],
            "cap_usd": money(cap),
            "calls": calls,
            "tokens_in": k["tokens_in"],
            "tokens_out": k["tokens_out"],
            "spend_usd": money(k["spend"]),
            "prev_spend_usd": money(pkeys[key]["spend"]),
            "avg_call_usd": money(k["spend"] / calls) if calls else 0.0,
            "daily_rate_usd": money(k["spend"] / 7),
            "cap_pct": round(k["spend"] / cap * 100, 2),
            "share_pct": round(k["spend"] / total * 100, 2),
            "wow_delta_pct": (round(
                (k["spend"] - pkeys[key]["spend"])
                / pkeys[key]["spend"] * 100, 2)
                if pkeys[key]["spend"] else None),
            "overage_usd": money(max(0.0, k["spend"] - cap)),
            "headroom_usd": money(cap - k["spend"]),
        }

    man_teams = {}
    for team in sorted(teams):
        t = teams[team]
        tcap = sum(v["cap"] for v in KEYS.values() if v["team"] == team)
        man_teams[team] = {
            "calls": t["calls"],
            "tokens_in": t["tokens_in"],
            "tokens_out": t["tokens_out"],
            "spend_usd": money(t["spend"]),
            "cap_usd": money(tcap),
            "prev_spend_usd": money(pteams[team]["spend"]),
            "avg_call_usd": money(t["spend"] / t["calls"]),
            "cap_pct": round(t["spend"] / tcap * 100, 2),
            "share_pct": round(t["spend"] / total * 100, 2),
            "wow_delta_pct": round(
                (t["spend"] - pteams[team]["spend"])
                / pteams[team]["spend"] * 100, 2),
            "overage_usd": money(max(0.0, t["spend"] - tcap)),
            "headroom_usd": money(tcap - t["spend"]),
        }

    calls = sum(k["calls"] for k in keys.values())
    manifest = {
        "note": "generated by tools/gen_data.py — do not edit",
        "corpus": {
            "week_start": WEEK_START.isoformat(),
            "week_end": week[-1].isoformat(),
            "prev_week_start": PREV_START.isoformat(),
            "prev_week_end": prev[-1].isoformat(),
            "week_days": [d.isoformat() for d in week],
            "prev_week_days": [d.isoformat() for d in prev],
            "seed": SEED,
            "generator": "tools/gen_data.py",
        },
        "keys": man_keys,
        "teams": man_teams,
        "totals": {
            "calls": calls,
            "tokens_in": sum(k["tokens_in"] for k in keys.values()),
            "tokens_out": sum(k["tokens_out"] for k in keys.values()),
            "spend_usd": money(total),
            "cap_usd": money(cap_total),
            "prev_spend_usd": money(ptotal),
            "wow_delta_usd": money(total - ptotal),
            "wow_delta_pct": round((total - ptotal) / ptotal * 100, 2),
            "cap_pct": round(total / cap_total * 100, 2),
            "daily_rate_usd": money(total / 7),
            "avg_call_usd": money(total / calls),
            "projected_october_usd": money(total / 7 * 31),
        },
        "daily_spend_usd": {
            d.isoformat(): money(sum(cost_of(r) for r in days[d]))
            for d in week},
        "over_cap": [
            {"key": key, "team": KEYS[key]["team"],
             "spend_usd": money(keys[key]["spend"]),
             "cap_usd": money(KEYS[key]["cap"]),
             "overage_usd": money(keys[key]["spend"] - KEYS[key]["cap"])}
            for key in sorted(KEYS)
            if keys[key]["spend"] > KEYS[key]["cap"]],
        "top_calls": [
            dict({"rank": i + 1},
                 **{f: rec[f] for f in
                    ("key", "team", "model", "ts",
                     "tokens_in", "tokens_out")},
                 cost_usd=money(cost_of(rec)))
            for i, rec in enumerate(
                sorted(cur, key=cost_of, reverse=True)[:3])],
        "input_hashes": {
            **{f"data/usage-{d.isoformat()}.json": sha256(
                ROOT / "data" / f"usage-{d.isoformat()}.json")
               for d in sorted(days)},
            "caps.json": sha256(ROOT / "caps.json"),
            "prices.json": sha256(ROOT / "prices.json"),
        },
    }
    write_json(ROOT / "tests" / "manifest.json", manifest, sort=True)

    print("wrote %d daily exports, %d calls this week" % (len(days), calls))
    for key in sorted(KEYS):
        k = man_keys[key]
        flag = " OVER" if k["spend_usd"] > k["cap_usd"] else ""
        print("  %-14s %-4s calls=%3d spend=%8.2f cap=%6.2f%s"
              % (key, k["team"], k["calls"], k["spend_usd"],
                 k["cap_usd"], flag))
    print("  total=%.2f prev=%.2f delta=%+.2f (%+.1f%%)"
          % (manifest["totals"]["spend_usd"],
             manifest["totals"]["prev_spend_usd"],
             manifest["totals"]["wow_delta_usd"],
             manifest["totals"]["wow_delta_pct"]))
    print("  top calls: " + ", ".join(
        "$%.2f %s" % (c["cost_usd"], c["key"]) for c in manifest["top_calls"]))


if __name__ == "__main__":
    sys.exit(main())
