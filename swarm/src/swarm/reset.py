"""Lane reset: put the machine back to a clean, ready state after a run, after a crash, or by hand.

A run that filled the disk left 52 sandboxes (8.5 GB) behind, and the next run would have started on that. The
reset removes every sandbox root a runner made, stops every process a runner started, then checks that disk,
processes and memory are back near where they were. It writes what it found and what it did.

It only ever touches what a runner made:
- directories in the temp dir named like a runner root (`swarm-` + 8 characters) that carry the runner's marker
  file, or, for roots made before the marker existed, that hold nothing but attempt directories;
- processes whose environment carries SWARM_ATTEMPT, which the runner sets on every agent and checker and which
  their children inherit. Never by user id, so the lane service, an SSH session or a tunnel cannot match.

A root whose runner is still alive and has not finished is in use: it is left alone, and so are all processes,
unless `force` is given. Where /proc is missing (macOS) processes and memory are not examined at all.
"""
import json
import os
import signal
import tempfile
import time
from pathlib import Path

from . import host
from .sandbox import ATTEMPT_DIR, ROOT_MARKER, ROOT_NAME, process_started, remove_tree, tagged_processes, tree_kb
from .util import append_jsonl, utc_now, write_json

RECORD = "lane-reset.json"
HISTORY = "lane-resets.jsonl"
HAS_PROC = Path("/proc/self/environ").exists()


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def _runner_live(pid, started) -> bool:
    """Is the runner that made this still the same live process? A reused pid has another start time; where start
    times are unknown (macOS), a live pid counts as the owner."""
    return _alive(pid) and (not started or process_started(pid) in (None, started))


def runner_roots(tmp: Path | None = None) -> list[dict]:
    """Sandbox roots in the temp dir: [{"path", "in_use"}]. in_use: its runner is alive and has not finished."""
    base = Path(tmp or tempfile.gettempdir()).resolve()
    found = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            if not ROOT_NAME.match(entry.name) or entry.is_symlink() or not entry.is_dir() or entry.stat().st_uid != os.getuid():
                continue
            marker = entry / ROOT_MARKER
            names = [child.name for child in entry.iterdir()]
            if marker.is_file() and not marker.is_symlink():
                try:
                    doc = json.loads(marker.read_text())
                except (OSError, ValueError):
                    doc = None
                if not isinstance(doc, dict) or "pid" not in doc:
                    # A marker we cannot read says nothing about its owner: in use while the root is young.
                    found.append({"path": entry, "in_use": time.time() - entry.stat().st_mtime < YOUNG_S})
                    continue
                found.append({"path": entry, "in_use": bool(not doc.get("finished") and _runner_live(doc["pid"], doc.get("started")))})
            elif ROOT_MARKER + ".tmp" in names or not names:
                # Being born (marker not yet in place), or empty: a new root for a few minutes, then nobody's.
                if ROOT_MARKER + ".tmp" in names:
                    found.append({"path": entry, "in_use": time.time() - entry.stat().st_mtime < YOUNG_S})
            elif all(ATTEMPT_DIR.match(name) for name in names):
                # Made before the marker existed: ours only if it holds attempt directories and nothing else.
                found.append({"path": entry, "in_use": False})
        except OSError:
            continue
    return found


YOUNG_S = 300


def agent_processes(own: bool = False) -> tuple[dict[int, int], int] | None:
    """(pid -> rss_kb of leftover processes a runner started, how many more belong to a run in progress); None
    where processes cannot be examined.

    A process whose SWARM_RUNNER names a runner that is still alive belongs to that runner: its run may be in
    progress, so it is not a leftover. `own`: the caller is that runner and says it has no run in progress."""
    if not HAS_PROC:
        return None
    mine = set()
    pid = os.getpid()
    while pid > 1:  # never ourselves or anything above us, whatever its environment says
        mine.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    leftovers, active = {}, 0
    for pid, rss in tagged_processes("", prefix=True).items():
        if pid in mine:
            continue
        try:
            env = dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)
            runner, _, started = env.get(b"SWARM_RUNNER", b"").decode().partition(":")
            runner_pid, runner_started = int(runner), int(started or 0)
        except (OSError, ValueError):
            runner_pid = runner_started = None  # started by a runner from before the tag, or already gone
        if runner_pid is not None and _runner_live(runner_pid, runner_started) and not (own and runner_pid == os.getpid()):
            active += 1
        else:
            leftovers[pid] = rss
    return leftovers, active


def _figures(tmp, data, own=False) -> dict:
    memory = host.memory_state()
    processes = agent_processes(own)
    roots = runner_roots(tmp)
    return {"disk_free_mb": host.disk_free_mb(*[p for p in (tmp or tempfile.gettempdir(), data) if p]),
            "available_mb": memory["available_mb"] if HAS_PROC else None,
            "agent_processes": None if processes is None else len(processes[0]),  # leftovers; a live run's agents are not
            "sandbox_roots": len(roots),
            "sandbox_kb": sum(tree_kb(r["path"]) or 0 for r in roots)}


def reset(data: Path | None = None, min_free_mb: int = 0, baseline_available_mb: int | None = None,
          tolerance_mb: int = 512, settle_s: float = 10.0, force: bool = False, tmp: Path | None = None,
          run: str | None = None, own: bool = False) -> dict:
    """Clean the lane and check it. Returns the record (also written under `data` when given).

    ok is true only when no leftover root or agent process remains, free disk is at least min_free_mb and, when a
    baseline is known, available memory is back within tolerance_mb of it. Safe to run again at any time.

    own: the caller is the lane service, with no run of its own in progress. Processes its own runs left are then
    leftovers, and another runner's live run on the machine is that runner's business, not a failure of this lane.
    By hand (own false) a live run is reported as `runner_active` and nothing of it is touched, unless forced.
    """
    started = time.monotonic()
    before = _figures(tmp, data, own)
    problems = []
    roots = runner_roots(tmp)
    busy = [r for r in roots if r["in_use"]]
    scan = agent_processes(own)
    processes, active = scan if scan is not None else ({}, 0)
    if force and scan is not None:
        processes, active = {**processes, **agent_processes_all()}, 0
    if (busy or active) and not force and not own:
        problems.append("runner_active")  # a run is in progress: its sandboxes and its agents are not leftovers
    found, killed = len(processes), 0
    if processes:
        for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            for pid in processes:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + wait
            left = _still(processes)
            while left and time.monotonic() < deadline:
                time.sleep(0.1)
                left = _still(processes)
            processes = left
            if not processes:
                break
        killed = found - len(processes)
    removed = failed = 0
    for root in roots:
        if root["in_use"] and not force:
            continue
        if remove_tree(root["path"]).get("removed"):
            removed += 1
        else:
            failed += 1
    if failed:
        problems.append("sandbox_not_removed")
    # Memory comes back a moment after the processes go; wait for it rather than fail a clean lane.
    after = _figures(tmp, data, own)
    deadline = time.monotonic() + settle_s
    while baseline_available_mb is not None and after["available_mb"] is not None \
            and after["available_mb"] < baseline_available_mb - tolerance_mb and time.monotonic() < deadline:
        time.sleep(0.5)
        after = _figures(tmp, data, own)
    if after["agent_processes"]:
        problems.append("agent_processes_left")
    if after["disk_free_mb"] is not None and after["disk_free_mb"] < min_free_mb:
        problems.append("disk_low")
    if baseline_available_mb is not None and after["available_mb"] is not None \
            and after["available_mb"] < baseline_available_mb - tolerance_mb:
        problems.append("memory_not_recovered")
    record = {"at": utc_now(), "t": round(time.time(), 3), "ok": not problems, "problems": problems, "run": run, "forced": bool(force),
              "before": before, "after": after, "sandbox_roots_removed": removed, "sandbox_roots_failed": failed,
              "sandbox_roots_in_use": 0 if force else len(busy), "processes_found": found, "processes_killed": killed,
              "processes_in_live_runs": active, "processes_examined": HAS_PROC, "min_free_mb": min_free_mb,
              "baseline_available_mb": baseline_available_mb, "tolerance_mb": tolerance_mb,
              "took_s": round(time.monotonic() - started, 3)}
    if data is not None:
        data = Path(data)
        data.mkdir(parents=True, exist_ok=True)
        write_json(data / RECORD, record)
        append_jsonl(data / HISTORY, record)
    return record


def agent_processes_all() -> dict[int, int]:
    """Forced: every process a runner started, live run or not (still never ourselves or our ancestors)."""
    leftovers, _ = agent_processes(own=False) or ({}, 0)
    everything = tagged_processes("", prefix=True)
    pid, mine = os.getpid(), set()
    while pid > 1:
        mine.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return {**leftovers, **{p: rss for p, rss in everything.items() if p not in mine}}


def _still(processes: dict) -> dict:
    """Those of `processes` that are still alive and still carry the runner's tag (a reused pid does not)."""
    now = tagged_processes("", prefix=True)
    return {pid: rss for pid, rss in processes.items() if pid in now}


def last(data: Path) -> dict | None:
    try:
        return json.loads((Path(data) / RECORD).read_text())
    except (OSError, ValueError):
        return None
