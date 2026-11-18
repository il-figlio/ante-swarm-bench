"""Process sandbox: one attempt in a fresh HOME and workdir under a clean root.

Portable (macOS and Linux) and deliberately modest. It isolates configuration,
credentials and instruction-file context. It is NOT a security boundary: every
attempt runs as the same user, shares the filesystem, network and signals, and
a hostile agent can interfere with other attempts. Use it for agents you trust;
use a namespaced or container sandbox for untrusted ones. Every record carries
this scope in `isolation`.
"""
import errno
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import stat
import tempfile
import threading
import time
from pathlib import Path

from .util import snapshot

OUTPUT_LIMIT = 8 * 1024 * 1024
ISOLATION = ("process: fresh HOME and workdir, clean environment, own session; same user id, shared filesystem, "
             "network and signals; not a security boundary")
CHECK_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def clean_root() -> Path:
    """A temp root with no AGENTS.md/CLAUDE.md anywhere above it."""
    base = Path(tempfile.gettempdir()).resolve()
    for parent in [base, *base.parents]:
        for name in ("AGENTS.md", "CLAUDE.md", ".git"):
            if (parent / name).exists():
                raise RuntimeError(f"{parent / name} would leak into agent context; set TMPDIR to a clean directory")
    root = Path(tempfile.mkdtemp(prefix="swarm-", dir=base))
    root.chmod(0o700)
    # Says this directory is a runner's sandbox root and which process owns it, so a lane reset can remove what a
    # crashed run left behind and never anything else.
    write_marker(root, {"pid": os.getpid(), "started": process_started(os.getpid()), "created": round(time.time(), 3)})
    return root


def write_marker(root: Path, doc: dict):
    """Atomically: a reset reading a half-written marker must not take a root that is just being born."""
    temp = Path(root) / (ROOT_MARKER + ".tmp")
    temp.write_text(json.dumps(doc))
    os.replace(temp, Path(root) / ROOT_MARKER)


def runner_tag() -> str:
    """SWARM_RUNNER for every process a runner starts: which runner, so a reset run by hand can tell the agents of
    a run in progress from leftovers."""
    return f"{os.getpid()}:{process_started(os.getpid()) or 0}"


ROOT_MARKER = ".swarm-root"
ROOT_NAME = re.compile(r"^swarm-[A-Za-z0-9_]{8}$")
ATTEMPT_DIR = re.compile(r"^c\d{3}-[a-z0-9-]+-n\d+-s\d{3,}$")


def process_started(pid: int) -> int | None:
    """A process's start time in clock ticks since boot (Linux), so a reused pid is not mistaken for its owner."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def remove_tree(path) -> dict:
    """Remove a directory tree and say what could not be removed.

    shutil.rmtree(ignore_errors=True) hid every failure, and one unwritable directory (a prepared home copied
    with its modes) or a file created while the walk ran left the whole sandbox behind without a trace.
    """
    path = Path(path)
    errors: list[str] = []

    def retry(function, target, _exc):
        # Unwritable directory: give ourselves write and search on it and its parent, then try once more.
        try:
            for fix in (os.path.dirname(target), target):
                inside = os.path.commonpath([os.path.abspath(fix), os.path.abspath(path)]) == os.path.abspath(path)
                if inside and os.path.isdir(fix) and not os.path.islink(fix):  # never a directory above the tree
                    os.chmod(fix, stat.S_IRWXU)
            function(target)
        except OSError as error:
            errors.append(errno.errorcode.get(error.errno, type(error).__name__))

    for _ in range(3):  # a process that was still writing can add entries during one walk
        if not os.path.lexists(path):
            break
        errors.clear()
        shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))
        if os.path.lexists(path):
            time.sleep(0.05)
    left = os.path.lexists(path)
    out = {"removed": not left}
    if left:
        out["errors"] = sorted(set(errors))[:5]
        out["leftover_kb"] = tree_kb(path)
    return out


def tree_kb(root, shared=frozenset()) -> int | None:
    """Disk blocks under root in KB. Each inode counts once; inodes in `shared` (files hard-linked from a shared
    prepared home) do not count, because this tree does not hold them."""
    total, seen, todo = 0, set(), [os.fspath(root)]
    try:
        while todo:
            # scandir + one stat per entry: a prepared home holds tens of thousands of files and this runs twice
            # per attempt, on the machine being measured.
            with os.scandir(todo.pop()) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            todo.append(entry.path)
                    except OSError:
                        continue
                    key = (info.st_dev, info.st_ino)
                    if key in seen or key in shared:
                        continue
                    seen.add(key)
                    total += info.st_blocks * 512
    except OSError:
        return None
    return total // 1024


def _kill_group(pgid: int):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def live_group_members(pgid: int) -> set[int]:
    """Live (non-zombie) processes in a process group."""
    members = set()
    if Path("/proc/self/stat").exists():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                    members.add(int(entry.name))
            except (OSError, ValueError, IndexError):
                pass
        return members
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {pgid}  # unknown: assume alive so the caller cleans up
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) == pgid \
                and not parts[2].startswith("Z"):
            members.add(int(parts[0]))
    return members


def tagged_processes(tag: str, prefix: bool = False) -> dict[int, int]:
    """pid -> rss_kb for live processes whose environment carries SWARM_ATTEMPT=tag.

    Daemons that detach into their own session still inherit the tag, so this
    finds what a process-group check misses.
    """
    needle = f"SWARM_ATTEMPT={tag}"
    found = {}
    if Path("/proc/self/environ").exists():
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environ = b"\0" + (entry / "environ").read_bytes() + b"\0"
                if (b"\0" + needle.encode()) in environ if prefix else (b"\0" + needle.encode() + b"\0") in environ:
                    found[int(entry.name)] = int((entry / "statm").read_text().split()[1]) * page_kb
            except (OSError, ValueError, IndexError):
                pass
        return found
    try:
        out = subprocess.run(["/bin/ps", "-E", "-A", "-o", "pid=,rss=,command="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return found
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        hit = f" {needle}" in f" {parts[2]}" if prefix else f" {needle} " in f" {parts[2]} "
        if hit:
            found[int(parts[0])] = int(parts[1])
    return found


def reap_escaped(tag: str, grace_s: float = 2.0, prefix: bool = False) -> dict:
    """Record and stop processes from this attempt (or cell, with prefix) that outlived it."""
    escaped = tagged_processes(tag, prefix)
    if not escaped:
        return {"escaped_processes": 0, "escaped_rss_kb": 0}
    for pid in escaped:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and tagged_processes(tag, prefix):
        time.sleep(0.1)
    for pid in tagged_processes(tag, prefix):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return {"escaped_processes": len(escaped), "escaped_rss_kb": sum(escaped.values()), "escaped_after_cleanup": len(tagged_processes(tag, prefix))}


TEARDOWN_SETTLE_S = 2.0
OOM_EXEC = True
OOM_PREFIX = 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"'


def oom_kills() -> int:
    """OOM kills so far in this service's cgroup (cgroup v2 memory.events), or 0 where unknown."""
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        for line in Path(f"/sys/fs/cgroup{rel}/memory.events").read_text().splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return 0


def execute(argv, cwd, env, timeout_s, log_path: Path, on_start=None, cancel: threading.Event | None = None,
            on_exit=None, on_output=None) -> dict:
    """Run argv in its own session. Returns exit, timing, rusage and termination facts.

    on_output, when given, sees each chunk of output as it is logged (the live maze counts Ante's usage events).

    Never blocks past the deadline: output is drained with select, and a process
    that keeps the pipe open after the leader exits is recorded, not waited for.
    """
    result = {"exit_code": None, "termination": None, "output_bytes": 0, "forced_cleanup": False}
    if OOM_EXEC and Path("/proc/self/oom_score_adj").exists():
        # Under memory pressure the kernel should kill agents, never the runner that records them. The process sets
        # its own score and then execs the harness (same pid), so everything the harness starts inherits it.
        # Setting it from outside after the spawn raced the harness's own forks, and when the leader was a tiny
        # wrapper the kernel killed the wrapper and orphaned the real agent (Hermes x32, 2026-11-11).
        argv = ["/bin/sh", "-c", OOM_PREFIX, *argv]
    ooms_before = oom_kills()
    started = time.monotonic()
    with log_path.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        fd = proc.stdout.fileno()
        limit_hit = threading.Event()
        stop = threading.Event()

        def drain():
            # poll, not select: select() fails on descriptors above FD_SETSIZE (1024),
            # which a runner with hundreds of agents reaches.
            poller = select.poll()
            poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            size = 0
            while not stop.is_set():
                if not poller.poll(200):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                keep = chunk[: max(0, OUTPUT_LIMIT - size)]
                log.write(keep)
                size += len(keep)
                if on_output is not None and keep:
                    try:
                        on_output(keep)
                    except Exception:  # noqa: BLE001 - a watcher's failure never stops the drain
                        pass
                if len(keep) < len(chunk):
                    limit_hit.set()
                    _kill_group(proc.pid)
                    break
            result["output_bytes"] = size

        reader = threading.Thread(target=drain, daemon=True)
        try:
            reader.start()
        except RuntimeError:
            _kill_group(proc.pid)
            proc.wait()
            proc.stdout.close()
            raise
        if on_start:
            on_start(proc.pid)
        deadline = started + timeout_s
        while True:
            pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            if cancel is not None and cancel.is_set():
                result["termination"] = "cancelled"
            elif time.monotonic() > deadline:
                result["termination"] = "timeout"
            if result["termination"]:
                _kill_group(proc.pid)
                result["forced_cleanup"] = True
                _, status, rusage = os.wait4(proc.pid, 0)
                break
            time.sleep(0.02)
        proc.returncode = os.waitstatus_to_exitcode(status)
        result["wall_s"] = round(time.monotonic() - started, 4)
        if limit_hit.is_set():
            result["termination"] = "output_limit"
        exit_now = os.waitstatus_to_exitcode(status)
        if exit_now < 0 and result["termination"] is None:
            # Killed by a signal nobody here sent. SIGKILL while the service's OOM counter rose: out of memory.
            result["signal"] = -exit_now
            if -exit_now == signal.SIGKILL and oom_kills() > ooms_before:
                result["termination"] = "oom_killed"
        ooms = oom_kills() - ooms_before
        if ooms > 0:
            # The counter covers the whole service, not this attempt: the kernel killed something on this machine
            # while this agent ran. A crash recorded next to it may be the machine's doing, not the agent's.
            result["oom_kills_during"] = ooms
        # Anything still alive in the group after the leader exits is a leak.
        if live_group_members(proc.pid):
            result["leaked_processes"] = True
            result["forced_cleanup"] = True
            result["termination"] = result["termination"] or "descendant_leak"
        _kill_group(proc.pid)
        if on_exit is not None:
            # Adapter teardown (e.g. stopping a daemon the harness is designed to leave for its next run): after
            # wall_s is taken, before escaped processes are counted, so a documented stop is not a leak.
            t0 = time.monotonic()
            try:
                on_exit()
            except Exception:  # noqa: BLE001 - teardown failure leaves whatever it missed to the leak check
                pass
            # A stop request is a signal, not a wait: give what teardown stopped a moment to exit before counting,
            # or a daemon still shutting down is called a leak (flaky on Linux, 2026-11-12). Bounded; whatever is
            # still there after it is a leak as before.
            settle = time.monotonic() + TEARDOWN_SETTLE_S
            while env.get("SWARM_ATTEMPT") and tagged_processes(env["SWARM_ATTEMPT"]) and time.monotonic() < settle:
                time.sleep(0.05)
            result["teardown_s"] = round(time.monotonic() - t0, 4)
        tag = env.get("SWARM_ATTEMPT")
        if tag:
            result.update(reap_escaped(tag))
            if result["escaped_processes"]:
                result["forced_cleanup"] = True
                result["termination"] = result["termination"] or "descendant_leak"
        reader.join(timeout=2)
        if reader.is_alive():
            # Something we could not find still holds the output pipe.
            stop.set()
            reader.join(timeout=2)
            result["output_pipe_held"] = True
            result["forced_cleanup"] = True
            result["termination"] = result["termination"] or "descendant_leak"
        proc.stdout.close()
    result["exit_code"] = proc.returncode
    # rusage covers the leader and every descendant it waited for.
    result["cpu_s"] = round(rusage.ru_utime + rusage.ru_stime, 4)
    if result.get("signal") or result.get("leaked_processes"):
        # A killed leader never waited for its children, so rusage misses them: not a CPU figure to compare.
        result["cpu_s_partial"] = True
    result["cpu_scope"] = "rusage: leader + reaped descendants"
    # Largest single process among the leader and reaped descendants. macOS
    # reports bytes, Linux KiB.
    result["max_process_rss_kb"] = rusage.ru_maxrss // 1024 if sys.platform == "darwin" else rusage.ru_maxrss
    result["termination"] = result["termination"] or "exited"
    return result


class ProcessSandbox:
    name = "process"
    isolation = ISOLATION

    def __init__(self, root: Path):
        self.root = Path(root)

    def create(self, key: str, task) -> dict:
        base = self.root / key
        workdir = base / "work"
        home = base / "home"
        base.mkdir(mode=0o700)
        try:
            shutil.copytree(task.workspace, workdir, symlinks=True)
            home.mkdir()
            return {"base": base, "key": key, "workdir": workdir.resolve(), "home": home.resolve(), "before": snapshot(workdir)}
        except BaseException:
            # A half-made sandbox (disk full mid-copy) has no box to destroy later: remove it here.
            remove_tree(base)
            raise

    def check(self, box: dict, task, log_path: Path, cancel: threading.Event | None = None, on_start=None) -> dict:
        """Run the task's hidden tests on a private copy of the agent's workdir.

        The copy, a fresh HOME and Python's user site disabled keep files the
        agent left behind (or a process still running) from changing the
        verdict. Changes made to the agent workdir after the agent exited are
        recorded as late changes. on_start gets the checker's pid (a maze task binds its token to it).
        """
        after = snapshot(box["workdir"])
        changed = sorted(p for p in set(box["before"]) | set(after) if box["before"].get(p) != after.get(p))
        off_target = [p for p in changed if not task.change_allowed(p)]
        check_dir = box["base"] / f"check-{secrets.token_hex(8)}"
        check_dir.mkdir(mode=0o700)
        shutil.copytree(box["workdir"], check_dir / "work", symlinks=True)
        shutil.copytree(task.tests, check_dir / "tests")
        (check_dir / "home").mkdir()
        env = {"PATH": CHECK_PATH, "HOME": str(check_dir / "home"), "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(check_dir / "home"),
               "SWARM_ATTEMPT": box["key"] + "-check", "SWARM_RUNNER": runner_tag(), "SWARM_TESTS_DIR": str(check_dir / "tests"),
               "SWARM_WORKDIR": str(check_dir / "work")}
        run = execute(["/bin/bash", str(check_dir / "tests" / "test.sh")], check_dir / "work", env,
                      task.check_timeout_s, log_path, cancel=cancel, on_start=on_start)
        late = snapshot(box["workdir"]) != after
        return {"changed": changed, "off_target": off_target, "check_exit": run["exit_code"],
                "check_termination": run["termination"], "check_wall_s": run["wall_s"], "late_changes": late,
                "check_escaped_processes": run.get("escaped_processes", 0)}

    def destroy(self, box: dict) -> dict:
        """Remove the sandbox. Returns {"removed": bool, ...}; what stays behind is recorded, never silent."""
        return remove_tree(box["base"])
