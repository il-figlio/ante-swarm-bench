"""Per (harness, level) statistics, recomputable from attempts and cells alone."""
import math
from collections import Counter


def wilson(passes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total == 0:
        return None
    p = passes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def quantile(values: list[float], q: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return round(values[low] + (values[high] - values[low]) * (position - low), 4)


def _anatomy(ran, rows) -> dict:
    firsts = [r["requests"]["first_request"] for r in ran if (r["requests"].get("first_request") or {}).get("canonical_bytes") is not None]
    out = {}
    if firsts:
        out["first_request_bytes_median"] = quantile([f["canonical_bytes"] for f in firsts], 0.5)
        for part in ("system", "tools", "user", "instruction", "injected", "other"):
            out[f"first_request_{part}_bytes_median"] = quantile([f.get(f"{part}_bytes") for f in firsts], 0.5)
        out["first_request_tool_count_median"] = quantile([f.get("tool_count") for f in firsts], 0.5)
        hashes = Counter(h for f in firsts if (h := (f.get("canary") or {}).get("sha256")))
        if hashes:
            # The commonest; ties go to the lower hash so recomputing gives the same answer.
            out["canary_sha256"] = min(hashes, key=lambda h: (-hashes[h], h))
            out["canary_distinct"] = len(hashes)
    written = [r["requests"]["cache_write_input_tokens"] for r in rows if isinstance(r["requests"].get("cache_write_input_tokens"), int)]
    if written:
        out["cache_write_input_tokens_total"] = sum(written)
    return out


def summarize(attempts: list[dict], cells: list[dict]) -> dict:
    groups = {}
    for record in attempts:
        key = f"{record['harness']}@{record['level']}"
        groups.setdefault(key, {"harness": record["harness"], "level": record["level"], "attempts": []})["attempts"].append(record)
    cell_walls = {}
    for cell in cells:
        cell_walls.setdefault(f"{cell['harness']}@{cell['level']}", []).append(cell)
    out = {}
    for key, group in sorted(groups.items(), key=lambda kv: (kv[1]["harness"], kv[1]["level"])):
        rows = group["attempts"]
        # Timing and resource figures come only from attempts whose agent ran.
        ran = [r for r in rows if r["agent"].get("wall_s") is not None]
        passes = sum(1 for r in rows if r["passed"])
        batch = cell_walls.get(key, [])
        batch_wall = sum(c["batch_wall_s"] for c in batch)
        # A throughput number from a wave that never reached its size, or that waited to be admitted, describes
        # the host's refusal. Leave it unmeasured. Timing medians still stand.
        reached = bool(batch) and all(c.get("max_running") == group["level"] for c in batch)
        waited = sum((r.get("admission") or {}).get("wait_s") or 0 for r in rows) > 0
        cpu = [r["agent"]["cpu_s"] for r in ran if r["agent"].get("cpu_s") is not None and not r["agent"].get("cpu_s_partial")]
        rss = [r["agent"]["max_process_rss_kb"] / 1024 for r in ran if r["agent"].get("max_process_rss_kb")]
        tree = [r["agent"]["peak_tree_rss_kb"] / 1024 for r in ran if r["agent"].get("peak_tree_rss_kb")]
        first_pass = [c["first_pass_s"] for c in batch if c.get("first_pass_s") is not None]
        out[key] = {
            "harness": group["harness"],
            "level": group["level"],
            "rounds": len(batch),
            "attempts": len(rows),
            "passes": passes,
            "pass_rate": round(passes / len(rows), 4) if rows else None,
            "pass_rate_wilson95": wilson(passes, len(rows)),
            "successes_per_minute": round(passes * 60 / batch_wall, 3) if batch_wall and reached and not waited else None,
            "batch_wall_s_total": round(batch_wall, 3),
            "wall_s_median": quantile([r["agent"]["wall_s"] for r in ran], 0.5),
            "wall_s_p90": quantile([r["agent"]["wall_s"] for r in ran], 0.9),
            "first_pass_s_median": quantile(first_pass, 0.5),
            "peak_rss_mb_median": quantile(rss, 0.5),
            "peak_rss_mb_max": round(max(rss), 2) if rss else None,
            "tree_rss_mb_median": quantile(tree, 0.5),
            "escaped_processes": sum(r["agent"].get("escaped_processes", 0) for r in ran),
            "escaped_rss_mb_median": quantile([r["agent"].get("escaped_rss_kb", 0) / 1024 for r in ran], 0.5),
            "tree_rss_sampled": len(tree),
            "cpu_s_total": round(sum(cpu), 4) if cpu else None,
            "cpu_s_per_success": round(sum(cpu) / passes, 4) if cpu and passes else None,
            "requests_median": quantile([r["requests"]["count"] for r in ran], 0.5),
            "admission_wait_s_total": round(sum(r["admission"]["wait_s"] for r in rows), 3),
            "max_running": max((c["max_running"] for c in batch), default=None),
            "failures": dict(sorted(Counter(r["failure"] for r in rows if r["failure"]).items())),
        }
        disk = [r["agent"]["disk_kb"] for r in ran if r["agent"].get("disk_kb") is not None]
        if disk:
            out[key].update({"disk_kb_median": quantile(disk, 0.5), "disk_kb_max": max(disk),
                             "cpu_s_median": quantile(cpu, 0.5), "tree_rss_mb_max": round(max(tree), 2) if tree else None})
        # Attempts the runner failed or stopped: never the harness's. Present only when there are any, so bundles
        # recorded before it keep their summary shape.
        not_run = sum(1 for r in rows if r["failure"] in ("infra", "cancelled"))
        if not_run and any("disk_free_mb_end" in r or "infra_reason" in r for r in rows):  # fields only new records have
            out[key]["not_run"] = not_run
            out[key]["infra_reasons"] = dict(sorted(Counter(r.get("infra_reason") or "runner_error" for r in rows if r["failure"] == "infra").items()))
        leftovers = sum(c.get("winddown", {}).get("sandbox_leftovers", 0) for c in batch)
        if leftovers:
            out[key]["sandbox_leftovers"] = leftovers
        if any(c.get("winddown", {}).get("shared_seed_changed") for c in batch):
            out[key]["shared_seed_changed"] = sum(c["winddown"].get("shared_seed_changed", 0) for c in batch)
        used = [r["requests"] for r in ran if r["requests"].get("output_tokens") is not None]
        if used:
            # Real model usage (relay runs), as the provider reported it.
            tokens = sum(u["input_tokens"] + u["output_tokens"] for u in used)
            out[key].update({"input_tokens_total": sum(u["input_tokens"] for u in used),
                             "cached_input_tokens_total": sum(u.get("cached_input_tokens", 0) for u in used),
                             "output_tokens_total": sum(u["output_tokens"] for u in used),
                             "tokens_per_success": round(tokens / passes) if passes else None})
        # First-request anatomy and cache writes: only attempts recorded with the fields (old bundles verify).
        out[key].update(_anatomy(ran, rows))
        # Startup and per-step timing. Bundles recorded before it existed have no "timing" and keep their summary shape.
        timed = [r["timing"] for r in ran if r.get("timing")]
        if timed:
            extra = {
                "first_request_s_median": quantile([t.get("first_request_s") for t in timed], 0.5),
                "first_request_s_p90": quantile([t.get("first_request_s") for t in timed], 0.9),
                "step_gap_s_median": quantile([t.get("step_gap_s_median") for t in timed], 0.5),
                "after_final_s_median": quantile([t.get("after_final_s") for t in timed], 0.5),
                "synthetic_input_tokens_median": quantile([r["requests"].get("synthetic_input_tokens") for r in ran], 0.5),
            }
            # Newer fields stay off bundles recorded before they existed, so verify still matches those summaries.
            for source, dest in (
                ("step_gap_s_p90", "step_gap_s_p90"),
                ("step_gap_s_early", "step_gap_s_early_median"),
                ("step_gap_s_late", "step_gap_s_late_median"),
                ("context_slope", "context_slope_median"),
                ("context_bytes_last", "context_bytes_last_median"),
            ):
                if any(source in t for t in timed):
                    extra[dest] = quantile([t.get(source) for t in timed], 0.5)
            if any("model_turns" in r.get("requests", {}) for r in ran):
                extra["model_turns_median"] = quantile([r["requests"].get("model_turns") for r in ran], 0.5)
            if any("probes" in r.get("requests", {}) for r in ran):
                extra["probes_median"] = quantile([r["requests"].get("probes") for r in ran], 0.5)
            out[key].update(extra)
        maze_summaries = [
            entry["summary"]
            for c in batch
            for entry in (c.get("maze") or {}).values()
            if isinstance(entry, dict) and isinstance(entry.get("summary"), dict)
        ]
        if maze_summaries:
            exits = [m["exit"]["t"] for m in maze_summaries if isinstance(m.get("exit"), dict) and m["exit"].get("t") is not None]
            out[key].update({
                "maze_runs": len(maze_summaries),
                "maze_exits": len(exits),
                "maze_exit_s_median": quantile(exits, 0.5),
                "maze_calls_median": quantile([m.get("calls") for m in maze_summaries], 0.5),
                "maze_unique_cells_median": quantile([m.get("unique_cells") for m in maze_summaries], 0.5),
                "maze_repeat_pct_median": quantile([m.get("repeat_pct") for m in maze_summaries], 0.5),
                "maze_credit_agents_median": quantile([len(m["credit"]) for m in maze_summaries if isinstance(m.get("credit"), dict)], 0.5),
            })
    return {"groups": out}
