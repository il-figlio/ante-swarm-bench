#!/usr/bin/env python3
"""Weekly spend report for the proxy's virtual-key usage exports.

    python3 report.py keys    per-key totals, printed to stdout

Daily exports land under data/ as usage-YYYY-MM-DD.json — one JSON
array of call records per day. caps.json is the key registry (team,
owner, weekly USD cap); prices.json holds the per-model USD rates for
calls the proxy didn't price.

TODO(fran): this only half works. `keys` reads every export under
data/ (there are two weeks in there now, so the totals are wrong) and
it silently drops calls with no cost_usd instead of rating them off
prices.json. The csv outputs I actually paste into the Friday update
aren't written yet.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# the week I report on, Monday through Sunday
WEEK_START = date(2026, 10, 5)
WEEK_DAYS = {(WEEK_START + timedelta(days=i)).isoformat()
             for i in range(7)}


def load_records(days=None):
    """Call records from the daily exports. `days` is an optional set
    of YYYY-MM-DD strings to keep — None reads everything on disk."""
    records = []
    for path in sorted(DATA.glob("usage-*.json")):
        day = path.stem[len("usage-"):]
        if days is not None and day not in days:
            continue
        for rec in json.loads(path.read_text()):
            rec["day"] = day
            records.append(rec)
    return records


def load_caps():
    """key -> {team, owner, weekly_cap_usd}."""
    return json.loads((ROOT / "caps.json").read_text())


def load_prices():
    """model -> {in_usd_per_mtok, out_usd_per_mtok}."""
    return json.loads((ROOT / "prices.json").read_text())


def cost_of(rec, prices):
    """Effective $ cost of a call."""
    if "cost_usd" in rec:
        return rec["cost_usd"]
    return 0.0  # TODO: rate from prices[rec["model"]] — under-reports


def cmd_keys():
    prices = load_prices()
    rows = {}
    for rec in load_records():  # BUG: no week filter, reads both weeks
        cost = cost_of(rec, prices)
        if cost == 0.0 and "cost_usd" not in rec:
            continue  # BUG: unpriced calls vanish from the report
        k = rows.setdefault(
            rec["key"], {"calls": 0, "tokens_in": 0,
                         "tokens_out": 0, "spend": 0.0})
        k["calls"] += 1
        k["tokens_in"] += rec["tokens_in"]
        k["tokens_out"] += rec["tokens_out"]
        k["spend"] += cost
    print("%-15s %6s %12s %12s %10s"
          % ("key", "calls", "tokens_in", "tokens_out", "spend_usd"))
    for key in sorted(rows):
        k = rows[key]
        print("%-15s %6d %12d %12d %10.2f"
              % (key, k["calls"], k["tokens_in"], k["tokens_out"],
                 k["spend"]))


COMMANDS = {"keys": cmd_keys}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        sys.stderr.write("usage: python3 report.py <%s>\n"
                         % "|".join(sorted(COMMANDS)))
        return 2
    COMMANDS[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
