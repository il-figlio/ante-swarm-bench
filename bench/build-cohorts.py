#!/usr/bin/env python3
"""Build the "2026 Q4 cohort" summary JSON (cohorts-2026q4.json) from a receipt package and progress logs.

Inputs (read-only):
  --runs DIR     publish package: DIR/rNNN/{receipts.json,meta.json,samples.json}
  --log FILE     progress log (repeatable); one line per finished cell:
                 "<utc> rNNN model=... harness=... level=xN repeat=N passed/attempted=P/A total_secs=S exit=0"
  --out FILE     where to write the JSON

Every number in the output is read from a receipt. The progress logs give the run order and
are cross-checked against the receipts (id, model, harness, level, repeat, passes, seconds);
any disagreement stops the build. `repeat` is not in the hub's public run fields, so a
reader takes it from this file instead of inferring it.

Gate runs are not part of any cohort here.
The output carries results; none is committed to this repository.
"""
import argparse, json, re, statistics, sys
from itertools import product
from pathlib import Path

LINE = re.compile(r"^(?P<ts>\S+) (?P<run>r\d+) model=(?P<model>\S+) harness=(?P<harness>\S+) level=x(?P<level>\d+) repeat=(?P<repeat>\d+) passed/attempted=(?P<passed>\d+)/(?P<attempted>\d+) total_secs=(?P<secs>[\d.]+) exit=(?P<exit>\d+)$")

MODEL_LABELS = {
    "openai/gpt-6.1-sol": "GPT-6.1 Sol",
    "openai/gpt-6.1-terra": "GPT-6.1 Terra",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
}
HARNESS_LABELS = {"ante": "Ante", "ante-pi": "Ante-Pi", "pi": "Pi", "hermes": "Hermes", "codex": "Codex CLI", "claude-code": "Claude Code"}

CAVEATS = [
    {"id": "versions", "text": "Latest harness releases at the time, not the 15 October pins: Ante 0.3.0, Pi 1.4.0, Hermes v0.22.5, Codex CLI 0.165.0. These numbers are not comparable with the earlier GPT-5.6 Terra protocol (shared-v1)."},
    {"id": "telemetry", "text": "Telemetry is off for every harness, Ante included. A telemetry-on Ante would be a separate cohort."},
    {"id": "host", "text": "One 2 vCPU, 8 GB machine. Cells ran one after another, never overlapping, so ×8 means eight copies sharing two CPUs."},
    {"id": "tasks", "text": "32 attempts per cell: four small Python toolbox tasks, eight times each, with visible unit tests the agent is told to run. This is a footprint and reliability comparison, not a coding-ability ranking."},
    {"id": "codex", "text": "Codex CLI runs with its own tool set as shipped. For these GPT-6.1 models it offers a JavaScript \"code mode\" exec tool that calls the shell, instead of plain shell and file tools, and this version cannot turn that off. Read the Codex rows as Codex as shipped, not as a tool-matched comparison."},
    {"id": "claude", "text": "Claude \"medium\" is an effort label, not an equal amount of reasoning: Claude and GPT effort scales differ, and Claude goes through a different provider route."},
    {"id": "ante-pi", "text": "Ante-Pi is this repository's adapter (bench/adapters/ante-pi): Ante's core driving Pi's native file and shell tools. It is not a shipped product."},
    {"id": "repeats", "text": "Each dot is one run. The bar marks the median of three repeats, and the range runs from the fastest to the slowest repeat; it is not a confidence interval. Provider routing, the model backend snapshot and upstream caching were not controlled."},
]


def sh(path):
    return json.loads(Path(path).read_text())


def short_version(harness, raw):
    raw = str(raw or "")
    m = re.search(r"release (v[\d.]+)", raw)
    if harness == "hermes" and m:
        return m.group(1)
    m = re.search(r"Ante-Pi tools (\d+\.\d+\.\d+)", raw)
    if harness == "ante-pi" and m:
        return "adapter " + m.group(1)
    m = re.search(r"(\d+\.\d+\.\d+)", raw)
    if harness in ("ante", "pi", "codex", "claude-code") and m:
        return m.group(1)
    return raw.split("\n")[0]


def parse_log(paths):
    order = {}
    for p in paths:
        for line in Path(p).read_text().splitlines():
            m = LINE.match(line.strip())
            if m:
                d = m.groupdict()
                if d["run"] in order:
                    sys.exit(f"{d['run']} appears twice in the progress logs")
                order[d["run"]] = d
    return order


def cell_from(run_id, receipts, log, meta=None):
    r = receipts
    if r.get("model_verified") is not True:
        sys.exit(f"{run_id}: model_verified is {r.get('model_verified')!r}, refusing to publish")
    if r.get("cancelled"):
        sys.exit(f"{run_id}: cancelled run cannot enter a published cohort")
    if r.get("test_upstream"):
        sys.exit(f"{run_id}: test_upstream run cannot enter a published cohort")
    if isinstance(meta, dict):
        for k in ("run_id", "matrix_id", "model", "harness", "cells", "repeat"):
            if k in meta and meta[k] != r.get(k):
                sys.exit(f"{run_id}: meta.json {k}={meta[k]!r} disagrees with receipts.json {r.get(k)!r}")
    results = r.get("results")
    if isinstance(results, list) and results:
        if len(results) != int(r["attempted"]) or sum(1 for x in results if x.get("passed")) != int(r["passed"]):
            sys.exit(f"{run_id}: top-level passed/attempted disagrees with results list")
    rss_raw = r.get("peak_process_tree_rss_kb")
    cell = {
        "run_id": run_id,
        "model": r["model"],
        "harness": r["harness"],
        "level": int(r["cells"]),
        "repeat": int(r["repeat"]),
        "passed": int(r["passed"]),
        "attempted": int(r["attempted"]),
        "total_secs": float(r["total_secs"]),
        "peak_process_tree_rss_kb": int(rss_raw) if rss_raw is not None else None,
    }
    if log is None:
        sys.exit(f"{run_id}: no progress-log line")
    want = (log["model"], log["harness"], int(log["level"]), int(log["repeat"]), int(log["passed"]), int(log["attempted"]))
    got = (cell["model"], cell["harness"], cell["level"], cell["repeat"], cell["passed"], cell["attempted"])
    if want != got or abs(float(log["secs"]) - cell["total_secs"]) > 0.01 or log["exit"] != "0":
        sys.exit(f"{run_id}: receipt {got} {cell['total_secs']} disagrees with log {want} {log['secs']}")
    return cell


def expected_grid(matrix_id, revision):
    protocol = sh(Path(__file__).parent / "protocol" / "protocol.json")
    if revision == protocol["protocol_revision"]:
        for cohort in protocol["cohorts"].values():
            if cohort["matrix_id"] == matrix_id:
                return (set(product(cohort["models"], cohort["harnesses"], protocol["concurrency"],
                                    range(1, protocol["repeats"] + 1))), protocol["attempts_per_cell"])
    # The predecessor matrix is documented in protocol/README.md.
    if (matrix_id, revision) == ("shared-v2-20261030", "shared-v2"):
        return (set(product(("openai/gpt-6.1-sol", "openai/gpt-6.1-terra"),
                            ("ante", "pi", "hermes", "ante-pi"), (1, 8), (1, 2, 3))), 32)
    sys.exit(f"{matrix_id}: no frozen cohort for protocol revision {revision!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--log", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    runs = Path(a.runs)
    order = parse_log(a.log)

    by_matrix = {}
    for d in sorted((p for p in runs.iterdir() if re.fullmatch(r"r\d+", p.name)), key=lambda p: int(p.name[1:])):
        r = sh(d / "receipts.json")
        m = sh(d / "meta.json")
        sh(d / "samples.json")
        by_matrix.setdefault(r["matrix_id"], []).append((d.name, r, m))
    unlogged = [rid for rows in by_matrix.values() for rid, _, _ in rows if rid not in order]
    if unlogged:
        sys.exit(f"runs without a progress-log line: {unlogged}")
    extra = [rid for rid in order if not any(rid == x for rows in by_matrix.values() for x, _, _ in rows)]
    if extra:
        sys.exit(f"progress-log runs missing from the package: {extra}")

    cohorts = []
    for matrix_id, rows in by_matrix.items():
        first = rows[0][1]
        cells = [cell_from(rid, r, order.get(rid), meta=m) for rid, r, m in rows]
        cells.sort(key=lambda c: int(c["run_id"][1:]))  # progress order
        for rid, r, _ in rows:
            for k in ("effort", "provider_route", "telemetry", "protocol_revision", "protocol", "pack", "task_revision", "protocol_sha256"):
                if r.get(k) != first.get(k):
                    sys.exit(f"{rid}: {k} differs inside {matrix_id}")
        harnesses = []
        versions = {}
        for rid, r, _ in rows:
            if r["harness"] not in harnesses:
                harnesses.append(r["harness"])
            versions.setdefault(r["harness"], {"version": short_version(r["harness"], r["harness_version"]), "raw": str(r["harness_version"]).split("\n")[0]})
            if versions[r["harness"]]["raw"] != str(r["harness_version"]).split("\n")[0]:
                sys.exit(f"{rid}: harness_version differs inside {matrix_id}")
        models = []
        for c in cells:
            if c["model"] not in models:
                models.append(c["model"])
        want_grid, attempts_per_cell = expected_grid(matrix_id, first["protocol_revision"])
        got_grid = [(c["model"], c["harness"], c["level"], c["repeat"]) for c in cells]
        if len(set(got_grid)) != len(got_grid):
            sys.exit(f"{matrix_id}: duplicate cell")
        if set(got_grid) != want_grid or any(c["attempted"] != attempts_per_cell for c in cells):
            sys.exit(f"{matrix_id}: {len(cells)} cells, expected frozen grid of {len(want_grid)} cells with {attempts_per_cell} attempts each")
        cohorts.append({
            "id": matrix_id,
            "status": "published",
            "protocol_revision": first["protocol_revision"],
            "effort": first["effort"],
            "provider_route": first["provider_route"],
            "telemetry": first["telemetry"],
            "attempts_per_cell": attempts_per_cell,
            "repeats": 3,
            "levels": [1, 8],
            "models": [{"id": m, "label": MODEL_LABELS.get(m, m)} for m in models],
            "harnesses": [{"id": h, "label": HARNESS_LABELS[h], "version": versions[h]["version"], "version_raw": versions[h]["raw"]} for h in harnesses],
            "cells": cells,
        })

    all_models = list(dict.fromkeys(m["label"] for co in cohorts for m in co["models"]))
    model_summary = ", ".join(all_models) if all_models else "unknown models"
    doc = {
        "schema": 1,
        "title": "2026 Q4 cohort · current models",
        "protocol_line": f"2026 Q4 cohort · {model_summary} · medium · 3 repeats · 32 attempts per cell · one 2 vCPU, 8 GB host",
        "levels": [1, 8],
        "harness_order": ["ante", "ante-pi", "pi", "hermes", "codex", "claude-code"],
        "harness_labels": HARNESS_LABELS,
        "caveats": CAVEATS,
        "excluded": "Gate runs (r200-r209, r262-r263: four tasks, ×1, used to check the setup) are not published and are not part of any cohort.",
        "sources": {"progress_logs": [Path(p).name for p in a.log], "receipt_package": "runs-20261101.tar.gz", "run_count": sum(len(co["cells"]) for co in cohorts)},
        "cohorts": cohorts,
    }
    Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"{a.out}: {doc['sources']['run_count']} runs, {len(cohorts)} cohorts")


if __name__ == "__main__":
    main()
