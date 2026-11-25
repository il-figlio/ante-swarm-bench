"""Execute a frozen plan into a bundle.

Cells run one after another in the plan's seeded order. Within a cell, N
attempts launch concurrently, each behind the admission gate. Nothing is
retried; every attempt, hold and sample is recorded.
"""
import errno
import hashlib
import hmac
import json
import os
import resource
import signal
import threading
import time
from pathlib import Path

from . import harnesses, host, limits as cgroup_limits
from .bundle import Bundle
from .plan import check_plan
from .report import write_report
from .sandbox import (ProcessSandbox, clean_root, execute, reap_escaped, remove_tree, runner_tag, tagged_processes, tree_kb,
                      write_marker)
from .relay import RelayModel
from .scripted import ScriptedModel
from .summary import quantile, summarize
from .tasks import load_task
from .util import append_jsonl, read_jsonl, snapshot, utc_now, write_json

FAILURE_ORDER = ["infra", "admission_timeout", "cancelled", "timeout", "output_limit", "oom_killed", "descendant_leak", "agent_error",
                 "request_budget_exhausted", "request_error", "request_incomplete", "script_unfinished",
                 "late_change", "check_failed", "off_target"]
SETTLE_S = 2.0
# Threads the runner itself spends per running attempt: the attempt thread,
# its output drain and the model server's handler. They count against the
# same task limit as the agents.
RUNNER_THREADS_PER_ATTEMPT = 3
# Below this much free disk an agent's failure says nothing about the agent: writes fail for everyone.
DISK_FLOOR_MB = 64
# Failures where the agent never ran or the runner stopped it: nothing here is the harness's doing.
NOT_RUN = ("infra", "admission_timeout", "cancelled")


class MazeUnavailable(RuntimeError):
    """The cell's maze service did not start: the attempt cannot run, and is recorded as infra."""


def maze_seed(secret: bytes | None, task_seed: int, cell_index: int, task_id: str) -> int:
    """The seed a cell's maze is generated from. With a run secret (the default), HMAC-SHA256 of the task seed, the
    cell and the task under a key that lives only in the runner's memory: an agent that reads [maze] from task.toml or
    plan.json and calls maze.generate itself gets a different maze. Without one (spec maze_seed = "plain", for trusted
    agents and the scripted-vs-simulation cross-check) it is the task seed itself."""
    if secret is None:
        return task_seed
    digest = hmac.new(secret, f"{task_seed}:{cell_index}:{task_id}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big")


class LimitsNotApplied(RuntimeError):
    """Limits are required and this cell has no cgroup: the attempt would run unlimited, so it does not run."""


DISK_SCOPE = "blocks the agent added to its sandbox (workdir + home) between launch and exit; the runner's setup and the checker's copy are apart"


class Admission:
    """Launch only while the host has headroom: pressure at or below the limit and
    available memory minus pending launches above the reserve."""

    def __init__(self, config: dict, sampler: host.Sampler, sink, disk_paths=()):
        self.config = config
        # Filesystems the run writes to (sandboxes, bundle). Disk has no pressure signal and no OOM killer: a full
        # disk fails every attempt's setup at once, so launches are held while there is still room.
        self.disk_paths = tuple(disk_paths)
        self.disk_estimates: dict[str, float] = {}
        self.disk_committed: dict[str, float] = {}
        self.sampler = sampler
        self.sink = sink
        # gate: one admission decision at a time (held while waiting).
        # lock: shared counts; never held while sleeping, so release() is never blocked.
        self.gate = threading.Lock()
        self.lock = threading.Lock()
        self.pending: list[tuple[float, float, int, str]] = []  # (launched_at, estimate_mb, estimate_threads, attempt)
        self.estimates: dict[str, float] = {}
        self.thread_estimates: dict[str, int] = {}
        # Task slots committed to running attempts, from launch until they end:
        # agents grow threads lazily, so a fresh reading alone underestimates.
        self.committed: dict[str, int] = {}
        start = host.task_headroom()
        self.task_limit = start["tasks_limit"]
        self.task_baseline = None if start["tasks_free"] is None else start["tasks_limit"] - start["tasks_free"]

    def learn(self, harness: str, peak_mb: float, peak_threads: int = 0):
        if peak_mb:
            self.estimates[harness] = max(self.estimates.get(harness, 0.0), peak_mb)
        if peak_threads:
            self.thread_estimates[harness] = max(self.thread_estimates.get(harness, 0), peak_threads)

    def learn_disk(self, harness: str, mb: float):
        if mb:
            self.disk_estimates[harness] = max(self.disk_estimates.get(harness, 0.0), mb)

    def settled(self, attempt_id: str, written_mb: float):
        """An attempt's setup is on disk: a fresh reading now shows it, so only the rest stays committed."""
        with self.lock:
            if attempt_id in self.disk_committed:
                self.disk_committed[attempt_id] = max(0.0, self.disk_committed[attempt_id] - written_mb)

    def acquire(self, harness: str, attempt_id: str, cancel: threading.Event, disk_hint_mb: float = 0.0) -> dict:
        limit = host.PRESSURE_ORDER[self.config["max_pressure"]]
        reserve = self.config["reserve_mb"]
        started = time.monotonic()
        reasons = set()
        with self.gate:
            while True:
                now = time.monotonic()
                with self.lock:
                    self.pending = [entry for entry in self.pending if now - entry[0] < SETTLE_S]
                    pending = list(self.pending)
                    committed = sum(self.committed.values())
                    disk_committed = sum(self.disk_committed.values())
                # Task headroom is read fresh: under load the sampler can lag by
                # many seconds, and pids.max is a hard wall.
                state = dict(host.memory_state(), **host.task_headroom())
                estimate = self.estimates.get(harness, 0.0)
                threads = self.thread_estimates.get(harness, 0) + RUNNER_THREADS_PER_ATTEMPT
                pending_mb = sum(entry[1] for entry in pending)
                pending_threads = sum(entry[2] for entry in pending)
                held = []
                allow_unknown = self.config["allow_unknown"]
                if state["pressure"] == "unknown":
                    if not allow_unknown:
                        held.append("pressure_unknown")
                elif host.PRESSURE_ORDER[state["pressure"]] > limit:
                    held.append(f"pressure_{state['pressure']}")
                if state["available_mb"] is None:
                    if not allow_unknown:
                        held.append("memory_unknown")
                elif state["available_mb"] - pending_mb - estimate < reserve:
                    held.append("memory_reserve")
                # Each running agent holds processes and threads; the cgroup's
                # pids.max is a hard wall that memory pressure never shows.
                if state.get("tasks_free") is not None:
                    projected = None if self.task_baseline is None else \
                        self.task_limit - self.task_baseline - committed - threads
                    if state["tasks_free"] - pending_threads - threads < self.config["reserve_tasks"] or \
                            (projected is not None and projected < self.config["reserve_tasks"]):
                        held.append("task_reserve")
                # A plan frozen before the disk gate existed has no reserve_disk_mb and runs as it always did.
                disk_reserve = self.config.get("reserve_disk_mb")
                disk_estimate = max(self.disk_estimates.get(harness, 0.0), disk_hint_mb)
                disk_free = host.disk_free_mb(*self.disk_paths) if disk_reserve is not None and self.disk_paths else None
                if disk_reserve is not None and self.disk_paths:
                    if disk_free is None:
                        if not allow_unknown:
                            held.append("disk_unknown")
                    elif disk_free - disk_committed - disk_estimate < disk_reserve:
                        held.append("disk_reserve")
                if not held:
                    with self.lock:
                        self.pending.append((now, estimate, threads, attempt_id))
                        self.committed[attempt_id] = threads
                        self.disk_committed[attempt_id] = disk_estimate
                    decision = {"admitted": True, "wait_s": round(now - started, 3) if reasons else 0.0, "held_for": sorted(reasons),
                                "pressure": state["pressure"], "available_mb": state["available_mb"],
                                "estimate_mb": round(estimate, 1), "pressure_unknown": state["pressure"] == "unknown",
                                "tasks_free": state.get("tasks_free"), "estimate_threads": threads,
                                "committed_tasks": committed + threads}
                    if disk_reserve is not None and self.disk_paths:
                        decision.update(disk_free_mb=disk_free, estimate_disk_mb=round(disk_estimate, 1))
                    break
                reasons.update(held)
                if cancel.is_set() or now - started > self.config["wait_timeout_s"]:
                    decision = {"admitted": False, "wait_s": round(now - started, 3), "held_for": sorted(reasons),
                                "pressure": state["pressure"], "available_mb": state["available_mb"],
                                "estimate_mb": round(estimate, 1)}
                    if disk_reserve is not None and self.disk_paths:
                        decision.update(disk_free_mb=disk_free, estimate_disk_mb=round(disk_estimate, 1))
                    break
                time.sleep(0.25)
        self.sink(dict(decision, attempt=attempt_id, harness=harness, t=round(time.time(), 3)))
        return decision

    def release(self, attempt_id: str):
        with self.lock:
            self.committed.pop(attempt_id, None)
            self.disk_committed.pop(attempt_id, None)
            self.pending = [entry for entry in self.pending if entry[3] != attempt_id]


def classify(record: dict) -> str | None:
    agent, check, requests = record["agent"], record.get("check") or {}, record["requests"]
    found = set()
    if not record["admission"]["admitted"]:
        found.add("admission_timeout")
    if agent.get("termination") in ("cancelled", "timeout", "output_limit", "oom_killed", "descendant_leak"):
        found.add(agent["termination"])
    if agent.get("exit_code") not in (0, None) or (record["admission"]["admitted"] and agent.get("exit_code") is None):
        found.add("agent_error")
    if requests.get("budget_exhausted"):
        found.add("request_budget_exhausted")
    if requests.get("errors"):
        found.add("request_error")
    if record["admission"]["admitted"] and agent and (requests.get("count", 0) == 0
                                                      or requests.get("completed") != requests.get("count")):
        found.add("request_incomplete")
    if record["admission"]["admitted"] and agent and requests.get("final_served") is False:
        found.add("script_unfinished")
    if check and check.get("late_changes"):
        found.add("late_change")
    if check and check.get("check_exit") != 0:
        found.add("check_failed")
    if check and check.get("off_target"):
        found.add("off_target")
    return next((name for name in FAILURE_ORDER if name in found), None)


def run(plan_path: Path, out: Path, cancel: threading.Event | None = None) -> Path:
    plan = json.loads(Path(plan_path).read_text())
    problems = check_plan(plan)
    if problems:
        raise RuntimeError("plan cannot run: " + "; ".join(problems))
    spec = plan["spec"]
    cancel = cancel or threading.Event()
    # Each agent holds pipes and sockets; the usual 1024 descriptors run out first.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = hard if hard != resource.RLIM_INFINITY else 1 << 20
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, min(target, 1 << 20)), hard))
    except (ValueError, OSError):
        pass
    # Limits that cannot be applied refuse the run before anything is written, unless the plan says they are optional.
    wanted = cgroup_limits.Limits.from_spec(spec.get("limits"))
    limits_ctx = None
    if wanted.requested():
        required = spec["limits"].get("required", True)
        reason = cgroup_limits.probe(wanted)
        if reason and required:
            raise RuntimeError(f"limits cannot be applied: {reason} (set limits.required = false to run without them)")
        limits_ctx = {"limits": wanted, "requested": wanted.requested(), "required": required, "applied": False, "reason": reason}
    bundle = Bundle.create(out)
    write_json(bundle.root / "plan.json", plan)
    host_doc = {"fingerprint": host.fingerprint(), "started_at": utc_now(),
                "sandbox": spec["sandbox"], "isolation": ProcessSandbox.isolation}
    if limits_ctx:
        host_doc["limits"] = {k: limits_ctx[k] for k in ("requested", "required", "reason")}
    write_json(bundle.root / "host.json", host_doc)
    tasks = [load_task(Path(entry["path"])) for entry in plan["tasks"]]
    model_cfg = spec["model"]
    budget = spec["budget"]
    if model_cfg["source"] == "relay":
        # The gateway token comes from the lane's environment by name; the plan never holds a secret.
        key_env = model_cfg.get("upstream_key_env")
        key = os.environ.get(key_env) if key_env else None
        if key_env and not key:
            raise ValueError(f"model.upstream_key_env {key_env} is not set")
        model = RelayModel(model_cfg["upstream"], model_cfg["max_run_tokens"], upstream_key=key)
    else:
        model = ScriptedModel()
    sample_lock = threading.Lock()

    def sample_sink(row):
        with sample_lock:
            append_jsonl(bundle.root / "samples.jsonl", row)

    sampler = host.Sampler(spec["sample_interval_s"], sample_sink).start()
    root = clean_root()
    admission = Admission(spec["admission"], sampler, lambda row: append_jsonl(bundle.root / "admission.jsonl", row),
                          disk_paths=(root, bundle.root))
    sandbox = ProcessSandbox(root)
    chain_lock = threading.Lock()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, lambda *_: cancel.set())
        except ValueError:
            pass  # not on the main thread (tests); cancellation still works through the event
    cells = []
    stopped: dict[str, str] = {}
    ramp = spec.get("ramp")
    # Never written anywhere: each cell's effective maze seed is recorded once the cell is over, the key never.
    maze_secret = None if spec.get("maze_seed", "secret") == "plain" else os.urandom(32)
    crashed = None
    try:
        if limits_ctx and not limits_ctx["reason"]:
            # After the sampler took its cgroup: it reads the delegated parent, which holds the cell groups too.
            try:
                cgroup_limits.prepare(wanted)
                limits_ctx["applied"] = True
            except cgroup_limits.LimitsUnavailable as error:
                limits_ctx["reason"] = str(error)
                if limits_ctx["required"]:
                    raise
        for cell in plan["cells"]:
            if cancel.is_set():
                break
            if cell["harness"] in stopped:
                append_jsonl(bundle.root / "skipped.jsonl", dict(cell, reason=stopped[cell["harness"]]))
                continue
            cells.append(_run_cell(cell, plan, tasks, model, model_cfg, budget, sandbox, sampler, admission, bundle,
                                   chain_lock, cancel, limits_ctx, maze_secret))
            append_jsonl(bundle.root / "cells.jsonl", cells[-1])
            if ramp:
                reason = _ramp_stop(cells[-1], ramp)
                if reason:
                    stopped[cell["harness"]] = f"stopped after n={cell['level']}: {reason}"
    except Exception as error:  # noqa: BLE001 - seal what we have and say why
        crashed = f"{type(error).__name__}: {error}"[:500]
    finally:
        sampler.stop()
        model.close()
        if limits_ctx and limits_ctx["applied"]:
            undone = cgroup_limits.release()
            if undone:
                limits_ctx["release_problems"] = undone
        # Every sandbox should already be gone; whatever is not is removed here, and what cannot be is recorded.
        try:
            swept = remove_tree(root)
        except Exception as error:  # noqa: BLE001 - a cleanup failure is reported, never allowed to unseal the bundle
            swept = {"removed": False, "errors": [type(error).__name__]}
        if not swept.get("removed"):
            try:  # the run is over: a lane reset may take this root even though this process lives on
                write_marker(root, {"pid": os.getpid(), "finished": True})
            except OSError:
                pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    attempts = read_jsonl(bundle.root / "attempts.jsonl")
    summary = summarize(attempts, cells, swarm_version=plan.get("swarm_version"))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    summary.update(cancelled=cancel.is_set(), crashed=crashed,
                   complete=is_complete(plan, cells, attempts, read_jsonl(bundle.root / "skipped.jsonl"), cancel.is_set(), crashed),
                   runner_cpu_s=round(usage.ru_utime + usage.ru_stime, 3),
                   finished_at=utc_now(), ramp_stops=stopped,
                   cells_planned=len(plan["cells"]), cells_run=len(cells), plan_sha256=plan["plan_sha256"],
                   sandbox_root={k: swept[k] for k in ("removed", "errors", "leftover_kb") if k in swept},
                   **({"limits": {k: v for k, v in limits_ctx.items() if k != "limits"}} if limits_ctx else {}),
                   question=spec["question"], primary_outcome=spec["primary_outcome"],
                   token_scope="synthetic: scripted model counts request bytes / 4" if model_cfg["source"] == "scripted" else "provider")
    write_json(bundle.root / "summary.json", summary)
    try:
        write_report(bundle.root)
    except Exception as error:  # noqa: BLE001 - a report failure must not leave the bundle unsealed
        (bundle.root / "report.html").write_text(f"<!doctype html><title>Swarm report</title><p>Report failed: {type(error).__name__}</p>")
    bundle.seal()
    return bundle.root


def is_complete(plan, cells, attempts, skipped, cancelled, crashed) -> bool:
    """Every planned cell ran or was skipped by the ramp rule, and every cell
    recorded exactly one attempt per instance."""
    if cancelled or crashed:
        return False
    per_cell = {}
    for record in attempts:
        per_cell[record["cell"]] = per_cell.get(record["cell"], 0) + 1
    return (len(cells) + len(skipped) == len(plan["cells"])
            and all(per_cell.get(c["cell"], 0) == c["level"] == c["attempted"] for c in cells)
            and len(attempts) == sum(c["level"] for c in cells))


def _run_cell(cell, plan, tasks, model, model_cfg, budget, sandbox, sampler, admission, bundle, chain_lock, cancel,
              limits_ctx=None, maze_secret=None):
    """One cell. Its maze services stop (writing their logs and summaries) and its cgroup closes on every path,
    including an exception anywhere between starting them and the cell's result."""
    held = {"mazes": {}, "group": None}
    try:
        return _run_cell_body(cell, plan, tasks, model, model_cfg, budget, sandbox, sampler, admission, bundle,
                              chain_lock, cancel, limits_ctx, maze_secret, held)
    finally:
        _release_cell(held)


def _release_cell(held: dict):
    """Stop whatever the cell still holds. On the normal path everything is already stopped and closed (both are
    idempotent); after an exception this is what stops the services, writes their logs and closes the group."""
    for entry in held["mazes"].values():
        if entry.get("service") is not None:
            try:
                entry["service"].stop()
            except Exception:  # noqa: BLE001 - never mask the cell's own exception
                pass
    group = held["group"]
    if group is not None and not group.closed:
        try:
            group.close()
        except Exception:  # noqa: BLE001
            pass


def _run_cell_body(cell, plan, tasks, model, model_cfg, budget, sandbox, sampler, admission, bundle, chain_lock, cancel,
                   limits_ctx, maze_secret, held):
    harness = harnesses.get(cell["harness"], plan["spec"]["harness_options"].get(cell["harness"]))
    harness.frozen = plan["harnesses"][cell["harness"]]
    # The adapter's own turn ceiling follows this pack. A hardcoded cap would fail the long packs.
    harness.turn_cap = int(budget["max_requests_per_attempt"])
    binary = Path(plan["harnesses"][cell["harness"]]["path"])
    protocol = plan["spec"].get("protocol", "naive")
    level = cell["level"]
    # Rotate the task list by cell index so every task appears at every level.
    chosen = [tasks[(cell["index"] + i) % len(tasks)] for i in range(level)]
    running = {"now": 0, "max": 0}
    outcome = {"passed": 0, "attempted": 0, "admission_timeouts": 0}
    leftovers = []
    # What one attempt's setup will write before any has been measured: its workspace, and a prepared home if any.
    hint, workspace_kb = None, {}
    try:
        if getattr(harness, "plan_setup", None):
            harness.plan_setup(sandbox.root)
        hint = getattr(harness, "setup_disk_kb", None)
        workspace_kb = {t.id: 2 * (tree_kb(t.workspace) or 0) for t in {t.id: t for t in chosen}.values()}  # the workdir and the checker's copy
    except Exception:  # noqa: BLE001 - an unreadable seed fails each attempt's own setup, recorded there; never the cell
        hint = None
    running_lock = threading.Lock()
    first_pass = []
    mazes = held["mazes"] = _start_mazes(cell, chosen, bundle, maze_secret)
    group, group_reason = None, None
    if limits_ctx and limits_ctx["applied"]:
        group = held["group"] = cgroup_limits.CellGroup.create(f"c{cell['index']:03d}-{os.getpid()}", limits_ctx["limits"])
        group_reason = None if group else cgroup_limits.CellGroup.last_reason
    elif limits_ctx:
        group_reason = limits_ctx["reason"]
    # Required limits that this cell cannot get fail its attempts as infra (below); optional ones run unlimited, said in the result.
    no_group = bool(limits_ctx and limits_ctx["required"] and group is None)
    started = time.monotonic()

    def one(slot: int, task):
        attempt_id = f"c{cell['index']:03d}-{cell['harness']}-n{level}-s{slot:03d}"
        record = {"attempt": attempt_id, "cell": cell["index"], "harness": cell["harness"], "level": level,
                  "round": cell["round"], "slot": slot, "task": task.id, "started_at": utc_now(),
                  "isolation": sandbox.isolation,
                  "agent": {}, "check": None, "requests": {"count": 0, "errors": [], "budget_exhausted": False}}
        try:
            decision = admission.acquire(cell["harness"], attempt_id, cancel,
                                         ((hint() if hint else 0) + workspace_kb.get(task.id, 0)) / 1024)
            record["admission"] = decision
            if decision["admitted"]:
                if no_group:
                    raise LimitsNotApplied(f"limits are required but this cell has no cgroup: {group_reason}")
                _attempt(record, harness, binary, task, model, model_cfg, budget, sandbox, sampler, admission, bundle,
                         running, running_lock, cancel, protocol, maze=mazes.get(task.id), group=group)
        except Exception as error:  # noqa: BLE001 - every attempt is recorded, including our own failures
            record.setdefault("admission", {"admitted": True, "wait_s": 0.0, "held_for": []})
            record["infra_error"] = f"{type(error).__name__}: {error}"[:300]
            record["infra_reason"] = infra_reason(error, record.get("infra_stage"))
        finally:
            admission.release(attempt_id)
        try:
            record["failure"] = "infra" if record.get("infra_error") else classify(record)
            free = record.get("disk_free_mb_end")
            if record["failure"] not in (None, *NOT_RUN) and free is not None and free < DISK_FLOOR_MB:
                # The machine's disk was full when this agent failed. What it would have done with room to write is
                # unknown, so this is the runner's failure to provide a machine; the agent's own outcome is kept beside it.
                record.update(harness_failure=record["failure"], failure="infra", infra_reason="disk_full", infra_stage="run")
            record["passed"] = record["failure"] is None
            json.dumps(record).encode("utf-8", "surrogateescape")
        except Exception as error:  # noqa: BLE001
            record = {key: record[key] for key in ("attempt", "cell", "harness", "level", "round", "slot", "task", "started_at")}
            record.update(agent={}, check=None, requests={"count": 0, "errors": []}, admission={"admitted": True, "wait_s": 0.0, "held_for": []},
                          infra_error=f"record: {type(error).__name__}: {error}"[:300], failure="infra", passed=False)
        if record["passed"]:
            first_pass.append(time.monotonic() - started)
        with running_lock:
            outcome["attempted"] += 1
            outcome["passed"] += record["passed"]
            outcome["admission_timeouts"] += record["failure"] == "admission_timeout"
            if record.get("cleanup"):
                leftovers.append(record["attempt"])
        with chain_lock:
            bundle.append_attempt(record)

    available_before = host.memory_state()["available_mb"]
    threads = []
    for slot, task in enumerate(chosen):
        thread = threading.Thread(target=one, args=(slot, task))
        try:
            thread.start()
            threads.append(thread)
        except RuntimeError:
            # The runner itself is out of threads: wait for running attempts to
            # free some, then run this one here rather than drop it.
            for running_thread in threads:
                running_thread.join()
            threads = []
            one(slot, task)
    for thread in threads:
        thread.join()
    batch_wall = time.monotonic() - started
    # Every attempt of the cell has finished and been checked: the services (which the checks called) stop now.
    maze_result = _stop_mazes(mazes, bundle)
    # Wind-down: nothing from this cell may survive, and memory should come back.
    prefix = f"c{cell['index']:03d}-"
    sweep_started = time.monotonic()
    try:
        survivors = tagged_processes(prefix, prefix=True)
        reaped = reap_escaped(prefix, prefix=True) if survivors else {"escaped_processes": 0}
        recovery = _memory_recovery(available_before)
    except Exception as error:  # noqa: BLE001 - record the sweep failure instead of losing the cell
        survivors, reaped = [], {"sweep_error": f"{type(error).__name__}: {error}"[:200]}
        recovery = {"available_mb_after": None, "recovered_s": None}
    extra = {}
    if leftovers:
        extra["sandbox_leftovers"] = len(leftovers)
    if getattr(harness, "shared_changes", None):
        # A prepared home shared through hard links: one attempt writing through a link would reach every other.
        try:
            changed = harness.shared_changes()
            extra["shared_seed_changed"] = len(changed)
            if changed:
                extra["shared_seed_changed_files"] = changed[:5]
        except OSError as error:
            extra["shared_seed_check_error"] = type(error).__name__
    result_extra = {}
    if maze_result:
        result_extra["maze"] = maze_result
    if limits_ctx:
        result_extra["limits"] = _finish_group(group, limits_ctx, group_reason)
    return {"cell": cell["index"], "harness": cell["harness"], "level": level, "round": cell["round"],
            **result_extra,
            "batch_wall_s": round(batch_wall, 4), "max_running": running["max"],
            "first_pass_s": round(min(first_pass), 4) if first_pass else None, **outcome,
            "winddown": {"survivors": len(survivors), "survivors_after_cleanup": reaped.get("escaped_after_cleanup", 0),
                         "sweep_error": reaped.get("sweep_error"),
                         "sweep_s": round(time.monotonic() - sweep_started, 3),
                         "available_mb_before": available_before, **recovery, **extra}}


def _finish_group(group, ctx, reason) -> dict:
    """The cell's limits record: what was asked, whether it applied, why not, and what the group used."""
    out = {"requested": ctx["requested"], "applied": group is not None, "required": ctx["required"], "reason": None if group else reason}
    if group is not None:
        out["group"] = group.name
        out["stats"] = group.stats()  # before close: the files go with the group
        out["removed"] = group.close()
        if not out["removed"]:
            out["remove_error"] = group.close_error
    return out


def _start_mazes(cell, chosen, bundle, secret=None) -> dict:
    """One service per (cell, maze task), on the cell's effective seed (maze_seed). A task whose service fails to
    start maps to its error: each of its attempts fails as infra, none runs without its maze."""
    out = {}
    for task in chosen:
        if not task.maze or task.id in out:
            continue
        log = bundle.root / "logs" / f"maze-c{cell['index']:03d}-{task.id}.jsonl"
        seed = {"task": task.maze["seed"], "effective": maze_seed(secret, task.maze["seed"], cell["index"], task.id),
                "mode": "plain" if secret is None else "secret"}
        params = {**task.maze, "seed": seed["effective"]}
        entry = out[task.id] = {"service": None, "socket": None, "log": log, "error": None, "params": params, "seed": seed}
        try:
            from . import maze
            entry["service"] = maze.MazeService(maze.generate(params), params, log, seed_info=seed)
            entry["socket"] = entry["service"].start()
        except Exception as error:  # noqa: BLE001 - recorded on every attempt of the task and on the cell
            entry.update(service=None, socket=None, error=f"{type(error).__name__}: {error}"[:300])
    return out


def _stop_mazes(mazes, bundle) -> dict:
    result = {}
    for task_id, entry in mazes.items():
        row = {"log": str(entry["log"].relative_to(bundle.root)), "summary_file": str(entry["log"].with_suffix(".json").relative_to(bundle.root)),
               "seed": entry["seed"]}   # written after the cell: replay and re-simulate from seed.effective
        if entry["error"]:
            row["start_error"] = entry["error"]
        else:
            try:
                entry["service"].stop()
                row["summary"] = entry["service"].summary()
            except Exception as error:  # noqa: BLE001 - a service that cannot stop or summarise is recorded, not hidden
                row["stop_error"] = f"{type(error).__name__}: {error}"[:300]
        result[task_id] = row
    return result


def _memory_recovery(before, timeout_s=10.0, tolerance_mb=64):
    """Seconds until available memory is back within tolerance of its pre-cell value."""
    started = time.monotonic()
    after = host.memory_state()["available_mb"]
    if before is None or after is None:
        return {"available_mb_after": after, "recovered_s": None}
    while after < before - tolerance_mb and time.monotonic() - started < timeout_s:
        time.sleep(0.25)
        after = host.memory_state()["available_mb"]
    recovered = after >= before - tolerance_mb
    return {"available_mb_after": after, "recovered_s": round(time.monotonic() - started, 3) if recovered else None}


def _ramp_stop(cell: dict, ramp: dict) -> str | None:
    """Why a harness should stop climbing after this cell, or None."""
    if cell["attempted"] and cell["passed"] / cell["attempted"] < ramp["stop_pass_rate"]:
        return f"pass rate {cell['passed']}/{cell['attempted']} below {ramp['stop_pass_rate']}"
    if cell["admission_timeouts"]:
        return f"{cell['admission_timeouts']} launches never admitted"
    if cell["max_running"] < cell["level"] / 2:
        return f"only {cell['max_running']} of {cell['level']} ran at once"
    return None


def _teardown(harness, binary, env):
    """Adapter cleanup after the measured process has exited. Failures here stay off the attempt result."""
    fn = getattr(harness, "teardown", None)
    if fn is None or env is None:
        return
    try:
        fn(binary, env)
    except Exception:
        pass


def infra_reason(error: Exception, stage: str | None) -> str:
    """Why the runner failed an attempt, as a short code that is safe to show: never the exception text."""
    full = {errno.ENOSPC, errno.EDQUOT}
    if (isinstance(error, OSError) and error.errno in full) or "No space left on device" in str(error) \
            or "Disk quota exceeded" in str(error):
        return "disk_full"  # shutil.Error from a tree copy carries the errno only in its text
    if isinstance(error, MazeUnavailable):
        return "maze_unavailable"
    if isinstance(error, LimitsNotApplied):
        return "limits_unavailable"
    return "setup_failed" if stage in ("sandbox", "prepare") else "runner_error"


def _attempt(record, harness, binary, task, model, model_cfg, budget, sandbox, sampler, admission, bundle, running,
             running_lock, cancel, protocol="naive", maze=None, group=None):
    key = record["attempt"]
    maze_service = maze["service"] if maze else None
    maze_registered = False
    maze_error = []
    logs = bundle.root / "logs" / key
    box = env = None
    started_agent = False
    shared = getattr(harness, "shared_inodes", frozenset())
    stage = "sandbox"
    setup_started = time.monotonic()
    try:
        logs.mkdir(parents=True)
        # Written before any copying starts: the live view must not call an attempt alive while we are still
        # building its sandbox.
        write_json(logs / "lifecycle.live.json", {"phase": "preparing", "at": utc_now()})
        if maze and maze["error"]:
            raise MazeUnavailable(f"maze service did not start: {maze['error']}")
        box = sandbox.create(key, task)
        stage = "prepare"
        script = task.script_for(protocol)
        if maze:
            from . import maze as maze_module
            token = maze_service.register(key, record["slot"])
            maze_registered = True
            # The socket lives in the service's private directory, never in a workdir; the token works only from
            # this agent's process tree once started() binds it.
            (box["workdir"] / ".maze.json").write_text(json.dumps({"socket": maze["socket"], "token": token}))
            # The file is the runner's, like the task's own files: it must not read as a change the agent made.
            box["before"] = snapshot(box["workdir"])
            if model_cfg["source"] == "scripted":
                script = maze_module.Explorer(maze["params"]["seed"], record["slot"], task.maze["map"] == "shared")
        url = model.register(key, script, box["workdir"], model_cfg["id"], model_cfg["effort"], model_cfg["max_output_tokens"],
                             budget["max_requests_per_attempt"], trace=logs / "requests.live.jsonl",
                             instruction=task.instruction,
                             **({"helper": model_cfg["helper"]} if model_cfg.get("helper") else {}))
        env = harness.environment(binary, box["home"])
        env["SWARM_ATTEMPT"] = key
        env["SWARM_RUNNER"] = runner_tag()
        if model_cfg.get("wire"):
            harness.wire = model_cfg["wire"]  # e.g. chat for a Chat-Completions-only model; adapters default to their own
        harness.helper = model_cfg.get("helper")
        env.update(harness.prepare(box["home"], url, model_cfg["id"], model_cfg["effort"], model_cfg["max_output_tokens"]))
        argv = harness.argv(binary, task.instruction, box["workdir"], model_cfg["id"], model_cfg["effort"])
        if group is not None:
            argv = group.wrap(argv)  # joins the cell's cgroup itself, before it execs the harness
        # Everything on disk now is ours: the task's files, the adapter's config, a prepared home. Measured so the
        # agent's own writes can be told apart, and so admission knows what the next launch will cost.
        setup_kb = tree_kb(box["base"], shared)
        record["setup"] = {"setup_s": round(time.monotonic() - setup_started, 4), "disk_kb": setup_kb}
        admission.settled(key, (setup_kb or 0) / 1024)
        stage = "run"
        stats = {}
        timeout = min(task.timeout_s, budget["attempt_timeout_s"])
        write_json(logs / "lifecycle.live.json", {"phase": "launching", "at": utc_now(), "agent_limit_s": timeout})
        with running_lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        launched = time.monotonic()
        started_agent = True
        try:
            def started(pid):
                if maze_registered:
                    try:
                        maze_service.bind(key, pid)
                    except Exception as error:  # noqa: BLE001 - its calls are refused; said on the attempt, infra
                        maze_error.append(f"{type(error).__name__}: {error}")
                sampler.watch(key, pid, stats, lambda mb, threads: admission.learn(record["harness"], mb, threads))

            result = execute(argv, box["workdir"], env, timeout, logs / "agent.log", on_start=started,
                             cancel=cancel, on_exit=lambda: _teardown(harness, binary, env))
        finally:
            sampler.unwatch(key)
            with running_lock:
                running["now"] -= 1
        result.update(stats)
        if group is not None and cgroup_limits.join_failed(result.get("exit_code"), logs / "agent.log"):
            raise LimitsNotApplied("the agent could not join its cell's cgroup before it started (see agent.log)")
        if maze_error:
            raise MazeUnavailable(f"the agent's maze token could not be bound to it: {maze_error[0]}")
        # Before the checker copies the workdir into the sandbox: that copy is ours, and only attempts that reach
        # the check have one.
        end_kb = tree_kb(box["base"], shared)
        if end_kb is not None and setup_kb is not None:
            result["disk_kb"] = max(0, end_kb - setup_kb)
            result["disk_kb_setup"] = setup_kb
            result["disk_scope"] = DISK_SCOPE
            prepared = getattr(harness, "prepared_kb", 0) or 0
            if prepared:
                result["disk_kb_prepared"] = prepared  # the part of the setup that is a prepared home's own copy
            if getattr(harness, "shared_kb", 0):
                result["disk_kb_shared"] = harness.shared_kb  # hard-linked from the seed: held once, not per attempt
            if getattr(harness, "prepared_method", None) and harness.prepared_method():
                result["prepared_method"] = harness.prepared_method()
            admission.learn_disk(record["harness"], end_kb / 1024)
        record["agent"] = result
        write_json(logs / "lifecycle.live.json", {"phase": "agent_exited", "at": utc_now(), "agent_limit_s": timeout,
                   "agent": {k: result.get(k) for k in ("exit_code", "termination", "wall_s", "cpu_s", "max_process_rss_kb")}})
        # Learn from the larger of the sampled tree and the kernel's per-process peak.
        admission.learn(record["harness"], max(stats.get("peak_tree_rss_kb", 0), result.get("max_process_rss_kb") or 0) / 1024,
                        stats.get("peak_tree_threads", 0))
        rows, final_served = model.release(key)
        _requests(record, rows, budget["max_requests_per_attempt"])
        # A real model has no script to finish; only the scripted model can leave one unfinished.
        record["requests"]["final_served"] = final_served if model_cfg["source"] == "scripted" else None
        record["timing"] = timing(rows, launched, result.get("wall_s"))
        env = None  # stopped inside execute, before the leak check; finally must not stop it twice
        (logs / "requests.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        # Check whenever the agent exited by itself, so a leaked daemon and a
        # wrong answer stay distinguishable.
        if result["termination"] in ("exited", "descendant_leak") and not result.get("leaked_processes"):
            stage = "check"
            # The checker asks ./maze for the verdict from its own process: the token moves to it.
            bind = (lambda pid: maze_service.bind(key, pid)) if maze_registered else None
            record["check"] = sandbox.check(box, task, logs / "check.log", cancel, on_start=bind)
    except Exception:
        record["infra_stage"] = stage  # which of our steps failed; classified by the caller
        raise
    finally:
        # Every step below runs whatever the ones before it did: a sandbox is destroyed even when setup or
        # teardown raised, and one that cannot be removed is written on the attempt.
        if maze_registered:
            # After the check (it called ./maze verdict on the live service); recorded for every registered attempt,
            # failed ones included.
            try:
                record["maze"] = maze_service.verdict(key)
            except Exception as error:  # noqa: BLE001
                record["maze"] = {"error": f"{type(error).__name__}: {error}"[:200]}
        try:
            # Only for an agent that was launched: stopping a harness's daemon inside a half-built home starts the
            # harness there for nothing (256 times, on a full disk, on 2026-11-12).
            _teardown(harness, binary, env if started_agent else None)
            model.release(key, settle_s=0)
        finally:
            record["disk_free_mb_end"] = host.disk_free_mb(sandbox.root, bundle.root)
            if box is not None:
                try:
                    gone = sandbox.destroy(box)
                except Exception as error:  # noqa: BLE001 - recorded below
                    gone = {"removed": False, "errors": [type(error).__name__]}
                if not gone.get("removed"):
                    record["cleanup"] = {"sandbox_removed": False, "errors": gone.get("errors", []),
                                         "leftover_kb": gone.get("leftover_kb")}


def timing(rows, launched: float, wall_s) -> dict:
    """Harness overhead read from the scripted model, which shares this host's monotonic clock.

    first_request_s: launch to the first model request (cold start: runtime, config, tool registry, prompt build).
    step_gap_s_median: from one reply being sent to the next request arriving. It includes running the tool the
      reply asked for, so it is harness overhead only on tasks whose tools are trivial (tool-loop, session).
    after_final_s: from the final reply being sent to the process exiting (shutdown, flushes, telemetry).
    Unknown values stay None.
    """
    model = sorted((r for r in rows if r.get("recv_mono") is not None and r.get("kind") not in ("probe", "count_tokens")),
                   key=lambda r: r["recv_mono"])
    first = round(model[0]["recv_mono"] - launched, 4) if model else None
    gaps = [b["recv_mono"] - a["sent_mono"] for a, b in zip(model, model[1:]) if a.get("sent_mono") is not None]
    final = [r for r in model if r.get("op") == "final" and r.get("sent_mono") is not None]
    after = round(launched + wall_s - final[-1]["sent_mono"], 4) if final and wall_s is not None else None
    series = [r["request_bytes"] for r in model if isinstance(r.get("request_bytes"), int)]
    n = len(series)
    first_bytes = series[0] if n else None
    mid_bytes = series[n // 2] if n else None
    last_bytes = series[-1] if n else None
    # Subtract the first request (tool schemas plus the opening prompt) so the slope is growth, not catalog weight.
    net = (lambda v: None if v is None or first_bytes is None else v - first_bytes)
    return {
        "first_request_s": first,
        "step_gap_s_median": quantile(gaps, 0.5),
        "step_gap_s_p90": quantile(gaps, 0.9),
        "step_gap_s_early": quantile(gaps[:25], 0.5) if len(gaps) >= 25 else None,
        "step_gap_s_late": quantile(gaps[-25:], 0.5) if len(gaps) >= 25 else None,
        "step_gaps": len(gaps),
        "after_final_s": after,
        "request_bytes_first": first_bytes,
        "request_bytes_mid": mid_bytes,
        "request_bytes_last": last_bytes,
        "context_bytes_mid": net(mid_bytes),
        "context_bytes_last": net(last_bytes),
        "context_slope": round((last_bytes - first_bytes) / (n - 1), 2) if n >= 2 else None,
    }


def _first_request(rows) -> dict | None:
    """The first model row's anatomy with its wire size, tools and (relay) input tokens; None without anatomy."""
    row = next((r for r in rows if isinstance(r.get("anatomy"), dict)), None)
    if row is None:
        return None
    out = dict(row["anatomy"], request_bytes=row.get("request_bytes"), tools_offered=row.get("tools_offered"))
    if "input_tokens" in row:
        out["input_tokens"] = row["input_tokens"]
    return out


def _requests(record, rows, cap):
    errors = sorted({r["error"] for r in rows if r.get("error") and r["error"] != "request_budget_exhausted"})
    tools = sorted({r["tool"] for r in rows if r.get("tool")})
    tokens_in = sum(r.get("request_bytes", 0) // 4 for r in rows if r.get("completed"))
    model_kinds = {"responses", "messages", "chat"}
    record["requests"] = {
        "count": len(rows),
        "model_turns": sum(1 for r in rows if r.get("kind") in model_kinds or r.get("api") in model_kinds),
        "probes": sum(1 for r in rows if r.get("kind") in ("probe", "count_tokens")),
        "completed": sum(1 for r in rows if r.get("completed")),
        "errors": errors,
        "budget_exhausted": any(r.get("error") == "request_budget_exhausted" for r in rows),
        "tools_used": tools,
        "streamed": any(r.get("stream") for r in rows),
        "synthetic_input_tokens": tokens_in,
        # Requests where the harness asked for less output than the plan's cap (served; see scripted.py).
        "cap_lowered": sum(1 for r in rows if r.get("cap_lowered")),
    }
    if any("output_tokens" in r for r in rows):
        # Real usage, as the provider reported it (relay runs).
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            record["requests"][key] = sum(r.get(key, 0) for r in rows)
    written = [r["cache_write_input_tokens"] for r in rows if isinstance(r.get("cache_write_input_tokens"), int)]
    record["requests"]["cache_write_input_tokens"] = sum(written) if written else None
    record["requests"]["first_request"] = _first_request(rows)
