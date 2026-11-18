"""Spec validation and freezing.

A spec says what to measure. A plan is the spec plus everything it resolved to
(task hashes, harness binary hashes and versions, Swarm's own source hash and
the seeded cell order), hashed. `swarm run` refuses a plan whose hash or
inputs no longer match.
"""
import random
import tomllib
from pathlib import Path

from . import VERSION, harnesses
from .tasks import load_tasks
from .util import canonical, sha256_bytes, tree_hash, utc_now

SOURCE_ROOT = Path(__file__).resolve().parent
OUTCOMES = {"successes_per_minute", "pass_rate", "peak_rss_mb", "cpu_s_per_success", "wall_s",
            "first_request_s", "after_final_s", "step_gap_s", "context_slope"}
PROTOCOLS = {"naive", "collapsed"}
SANDBOXES = {"process"}
SOURCES = {"scripted", "relay"}
MAX_LEVEL = 4096


def _require(condition, message):
    if not condition:
        raise ValueError(message)


TOP_KEYS = {"question", "primary_outcome", "protocol", "model", "harnesses", "harness_options", "levels", "ramp", "repeats", "tasks",
            "sandbox", "seed", "stop_rule", "budget", "admission", "sample_interval_s", "limits", "maze_seed"}
SECTION_KEYS = {
    "model": {"source", "id", "effort", "max_output_tokens", "upstream", "max_run_tokens", "upstream_key_env", "wire", "helper"},
    "ramp": {"start", "factor", "max", "stop_pass_rate"},
    "budget": {"max_requests_per_attempt", "attempt_timeout_s"},
    "limits": {"cpu", "memory_mb", "pids", "required"},
    "admission": {"reserve_mb", "reserve_tasks", "reserve_disk_mb", "max_pressure", "wait_timeout_s", "allow_unknown"},
}


def _check_keys(spec: dict):
    unknown = sorted(set(spec) - TOP_KEYS)
    _require(not unknown, f"unknown spec keys: {', '.join(unknown)}")
    for section, allowed in SECTION_KEYS.items():
        value = spec.get(section)
        if value is None:
            continue
        _require(isinstance(value, dict), f"{section} must be a table")
        extra = sorted(set(value) - allowed)
        _require(not extra, f"unknown keys in [{section}]: {', '.join(extra)}")


def _check_limits(limits: dict):
    """[limits]: cgroup v2 limits for each cell's agents. required (default true) makes a run refuse to start where
    they cannot be applied; required = false runs unlimited there and says so in every cell."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    whole = lambda v: isinstance(v, int) and not isinstance(v, bool)
    _require(limits.get("cpu") is None or (number(limits["cpu"]) and 0 < limits["cpu"] <= 1024), "limits.cpu must be a number of cores, above 0")
    _require(limits.get("memory_mb") is None or (whole(limits["memory_mb"]) and limits["memory_mb"] >= 16),
             "limits.memory_mb must be a whole number of MB, 16 or more")
    _require(limits.get("pids") is None or (whole(limits["pids"]) and limits["pids"] >= 1), "limits.pids must be a whole number, 1 or more")
    limits.setdefault("required", True)
    _require(isinstance(limits["required"], bool), "limits.required must be true or false")
    _require(any(limits.get(k) is not None for k in ("cpu", "memory_mb", "pids")),
             "[limits] must set at least one of cpu, memory_mb, pids")


def load_spec(path: Path) -> dict:
    spec = tomllib.loads(Path(path).read_text())
    _check_keys(spec)
    _require(isinstance(spec.get("question"), str) and spec["question"].strip(), "spec.question is required")
    _require(spec.get("primary_outcome") in OUTCOMES, f"spec.primary_outcome must be one of {sorted(OUTCOMES)}")
    protocol = spec.setdefault("protocol", "naive")
    _require(protocol in PROTOCOLS, f"protocol must be one of {sorted(PROTOCOLS)}")
    model = spec.get("model") or {}
    _require(model.get("source") in SOURCES, f"model.source must be one of {sorted(SOURCES)}")
    if model.get("source") == "relay":
        # A real model through the relay: an HTTPS gateway that holds the credentials, and a run token budget.
        _require(isinstance(model.get("upstream"), str) and model["upstream"].startswith(("https://", "http://127.0.0.1")),
                 "relay model needs model.upstream (an https:// gateway)")
        _require(isinstance(model.get("max_run_tokens"), int) and model["max_run_tokens"] > 0,
                 "relay model needs model.max_run_tokens (input + output tokens across the run)")
    else:
        _require(not {"upstream", "max_run_tokens", "upstream_key_env"} & set(model), "upstream settings are for model.source = relay")
    _require(isinstance(model.get("id"), str) and model.get("effort") in ("low", "medium", "high"), "model.id and model.effort are required")
    _require(model.get("wire") in (None, "responses", "messages", "chat"), "model.wire must be responses, messages or chat")
    _require(model.get("wire") != "chat" or model.get("source") == "relay", "the chat wire is served by the relay only")
    # A sub-agent model the harness may call besides the planned one (Claude Code's Haiku tier); relay only.
    _require(model.get("helper") is None or (isinstance(model["helper"], str) and model["helper"] != model.get("id")
                                             and model.get("source") == "relay"),
             "model.helper must be another model id, with model.source = relay")
    model.setdefault("max_output_tokens", 16384)
    names = spec.get("harnesses")
    _require(isinstance(names, list) and names and len(set(names)) == len(names), "harnesses must be a non-empty list without repeats")
    options = spec.setdefault("harness_options", {})
    _require(isinstance(options, dict) and set(options) <= set(names), "harness_options keys must be listed harnesses")
    for name in names:
        harnesses.get(name, options.get(name)).validate_options()
    ramp = spec.get("ramp")
    if ramp is not None:
        _require(isinstance(ramp, dict) and "levels" not in spec, "use either levels or [ramp], not both")
        ramp.setdefault("start", 1)
        ramp.setdefault("factor", 2)
        ramp.setdefault("max", 1024)
        ramp.setdefault("stop_pass_rate", 0.95)
        _require(isinstance(ramp["start"], int) and isinstance(ramp["max"], int) and 1 <= ramp["start"] <= ramp["max"] <= MAX_LEVEL,
                 f"ramp start/max must be integers from 1 to {MAX_LEVEL}")
        _require(isinstance(ramp["factor"], int) and ramp["factor"] >= 2, "ramp.factor must be an integer >= 2")
        _require(0 < ramp["stop_pass_rate"] <= 1, "ramp.stop_pass_rate must be in (0, 1]")
        levels, n = [], ramp["start"]
        while n <= ramp["max"]:
            levels.append(n)
            n *= ramp["factor"]
        spec["levels"] = levels
        spec["ramp"] = ramp
    levels = spec.get("levels")
    _require(isinstance(levels, list) and levels and all(isinstance(n, int) and 1 <= n <= MAX_LEVEL for n in levels),
             f"levels must be integers from 1 to {MAX_LEVEL}")
    _require(isinstance(spec.get("repeats", 1), int) and 1 <= spec.get("repeats", 1) <= 50, "repeats must be 1-50")
    _require(spec.get("sandbox", "process") in SANDBOXES, f"sandbox must be one of {sorted(SANDBOXES)}")
    _require(isinstance(spec.get("tasks"), list) and spec["tasks"], "tasks must list task directory globs")
    _require(isinstance(spec.get("stop_rule"), str) and spec["stop_rule"].strip(), "stop_rule is required: say when the run ends")
    budget = spec.setdefault("budget", {})
    budget.setdefault("max_requests_per_attempt", 40)
    budget.setdefault("attempt_timeout_s", 300)
    admission = spec.setdefault("admission", {})
    admission.setdefault("reserve_mb", 1024)
    admission.setdefault("max_pressure", "normal")
    admission.setdefault("reserve_tasks", 512)
    # Free disk to keep on the filesystems holding the sandboxes and the bundle; launches are held below it.
    admission.setdefault("reserve_disk_mb", 1024)
    _require(isinstance(admission["reserve_disk_mb"], int) and admission["reserve_disk_mb"] >= 0,
             "admission.reserve_disk_mb must be a whole number of MB, 0 or more")
    # Unknown pressure or memory readings hold launches unless the spec says otherwise.
    admission.setdefault("allow_unknown", False)
    _require(isinstance(admission["allow_unknown"], bool), "admission.allow_unknown must be true or false")
    admission.setdefault("wait_timeout_s", budget["attempt_timeout_s"])
    _require(admission["max_pressure"] in ("normal", "warn"), "admission.max_pressure must be normal or warn")
    if spec.get("limits") is not None:
        _check_limits(spec["limits"])
    # Maze tasks: "secret" (default) generates each cell's maze from the task seed and a per-run key no agent can read;
    # "plain" uses the task seed as written, so anyone holding task.toml can generate the maze. Plain is for trusted
    # agents only: the scripted cross-check against maze.simulate and tests. See docs/maze.md.
    spec.setdefault("maze_seed", "secret")
    _require(spec["maze_seed"] in ("secret", "plain"), "maze_seed must be secret or plain")
    spec.setdefault("repeats", 1)
    spec.setdefault("sandbox", "process")
    spec.setdefault("seed", 0)
    spec.setdefault("sample_interval_s", 0.5)
    return spec


def freeze(spec_path: Path, binaries: dict[str, str] | None = None) -> dict:
    spec_path = Path(spec_path).resolve()
    spec = load_spec(spec_path)
    tasks = load_tasks(spec["tasks"], spec_path.parent)
    if spec["model"]["source"] == "scripted":
        tasks_needing_script = [t for t in tasks if not t.maze]  # a maze task is driven by a policy, not a script
        if spec.get("protocol") == "collapsed":
            missing = [t.id for t in tasks_needing_script if not t.collapsed]
            _require(not missing, f"collapsed protocol needs script.collapsed.json in: {', '.join(missing)}")
        else:
            missing = [t.id for t in tasks_needing_script if not t.script]
            _require(not missing, f"scripted model needs script.json in: {', '.join(missing)}")
    resolved = {}
    for name in spec["harnesses"]:
        harness = harnesses.get(name, spec["harness_options"].get(name))
        binary = harness.locate((binaries or {}).get(name))
        _require(binary is not None, f"harness {name}: binary not found (pass --binary {name}=PATH)")
        resolved[name] = harness.fingerprint(binary)
    rng = random.Random(spec["seed"])
    if spec.get("ramp"):
        # A ramp must climb, so levels stay in order; harness order is shuffled
        # within each level. A harness stops climbing at its stop rule.
        cells = []
        for n in spec["levels"]:
            for r in range(spec["repeats"]):
                order = list(spec["harnesses"])
                rng.shuffle(order)
                cells += [{"harness": h, "level": n, "round": r} for h in order]
    else:
        cells = [{"harness": h, "level": n, "round": r}
                 for r in range(spec["repeats"]) for h in spec["harnesses"] for n in spec["levels"]]
        # Interleave harnesses and levels in a seeded random order so time-of-run
        # effects do not line up with any one treatment.
        rng.shuffle(cells)
    for index, cell in enumerate(cells):
        cell["index"] = index
    plan = {
        "swarm_version": VERSION,
        "created_at": utc_now(),
        "spec": spec,
        "spec_path": str(spec_path),
        "spec_sha256": sha256_bytes(spec_path.read_bytes()),
        "source_sha256": tree_hash(SOURCE_ROOT),
        "tasks": [{"id": t.id, "path": str(t.root), "sha256": t.digest(), "timeout_s": t.timeout_s,
                   "check_timeout_s": t.check_timeout_s, "allowed_changes": t.allowed_changes,
                   **({"maze": t.maze} if t.maze else {})} for t in tasks],
        "harnesses": resolved,
        "cells": cells,
    }
    plan["plan_sha256"] = plan_digest(plan)
    return plan


def plan_digest(plan: dict) -> str:
    return sha256_bytes(canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))


def check_plan(plan: dict) -> list[str]:
    """Problems that make a plan unsafe to run now. Empty list means go."""
    problems = []
    if plan.get("plan_sha256") != plan_digest(plan):
        problems.append("plan hash does not match its contents")
    if plan.get("source_sha256") != tree_hash(SOURCE_ROOT):
        problems.append("swarm source changed since the plan was frozen")
    from .tasks import load_task
    for entry in plan.get("tasks", []):
        try:
            if load_task(Path(entry["path"])).digest() != entry["sha256"]:
                problems.append(f"task {entry['id']} changed")
        except (OSError, ValueError) as error:
            problems.append(f"task {entry['id']}: {error}")
    for name, info in plan.get("harnesses", {}).items():
        harness = harnesses.get(name, plan["spec"].get("harness_options", {}).get(name))
        binary = Path(info["path"])
        if not binary.exists() or harness.fingerprint(binary)["sha256"] != info["sha256"]:
            problems.append(f"harness {name} binary changed or missing")
        else:
            now = harness.fingerprint(binary)
            for extra in ("agent_sha256", "seed_sha256", "node_sha256"):
                if extra in info and now.get(extra) != info[extra]:
                    problems.append(f"harness {name} {extra.removesuffix('_sha256')} changed")
    return problems
