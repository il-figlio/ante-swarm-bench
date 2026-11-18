"""cgroup v2 limits for the agents of one cell.

Each cell's agents run in one child cgroup of the runner's own cgroup, with cpu.max, memory.max and pids.max. The
runner, the model server and the maze service stay outside it, so the limit is what the agents get.

Needs the service unit to delegate its cgroup (`Delegate=yes` in the lane's systemd unit). cgroup v2's "no internal processes" rule means a cgroup that hands controllers to children may hold
no processes of its own, so entering limits does, once and idempotently:

  1. creates `<own>/runner` and moves this whole process into it;
  2. enables the cpu, memory and pids controllers in `<own>/cgroup.subtree_control`.

`release()` undoes both (the process moves back, the controllers are disabled) so a later run in the same
process is measured exactly as before. Anything that cannot be done raises LimitsUnavailable with the reason, and
`CellGroup.create` turns that into None plus `CellGroup.last_reason`. Nothing here is silent: the runner writes
the requested limits, whether they were applied and why not, into every cell result.
"""
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import host

CONTROLLERS = ("cpu", "memory", "pids")
PERIOD_US = 100000
LEAF = "runner"
FIX = "the service unit needs Delegate=yes (and a systemd that grants cpu, memory and pids)"


class LimitsUnavailable(Exception):
    """Limits cannot be applied here; the message is the reason and is safe to record."""


@dataclass(frozen=True)
class Limits:
    cpu: float | None = None          # cores; cpu.max = "<cores * 100000> 100000"
    memory_mb: int | None = None      # memory.max
    pids: int | None = None           # pids.max

    @classmethod
    def from_spec(cls, spec: dict | None) -> "Limits":
        spec = spec or {}
        return cls(cpu=spec.get("cpu"), memory_mb=spec.get("memory_mb"), pids=spec.get("pids"))

    def requested(self) -> dict:
        return {k: v for k, v in (("cpu", self.cpu), ("memory_mb", self.memory_mb), ("pids", self.pids)) if v is not None}

    def needed(self) -> list[str]:
        return [c for c, v in (("cpu", self.cpu), ("memory", self.memory_mb), ("pids", self.pids)) if v is not None]

    def files(self) -> dict[str, str]:
        """Controller file -> the exact text written to it."""
        out = {}
        if self.cpu is not None:
            out["cpu.max"] = cpu_max(self.cpu)
        if self.memory_mb is not None:
            out["memory.max"] = str(int(self.memory_mb) * 1024 * 1024)
        if self.pids is not None:
            out["pids.max"] = str(int(self.pids))
        return out


def cpu_max(cpu: float) -> str:
    return f"{int(cpu * PERIOD_US)} {PERIOD_US}"


def parse_flat(text: str) -> dict[str, int]:
    """cpu.stat / memory.events: `key value` lines, integers only."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _int(path: Path) -> int | None:
    text = _read(path)
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None  # "max", or empty


def _write(path: Path, text: str, what: str):
    try:
        path.write_text(text)
    except OSError as error:
        raise LimitsUnavailable(f"cannot write {what} ({path}): {error.strerror or error}; {FIX}") from error


# ---- one-time entry into a delegated cgroup -----------------------------------------------------------------------

_lock = threading.Lock()
_state: dict | None = None


def probe(limits: Limits, own: Path | None = None) -> str | None:
    """Why limits cannot be applied here, or None. Reads only: nothing is moved or enabled."""
    needed = limits.needed()
    if not needed:
        return None
    with _lock:
        if _state is not None:
            return None
    own = own or host.cgroup_dir()
    if own is None:
        return "no cgroup v2 hierarchy for this process (macOS, cgroup v1 or no /sys/fs/cgroup)"
    available = (_read(own / "cgroup.controllers") or "").split()
    if not available:
        return f"{own} has no cgroup.controllers: cgroup v2 controllers are not delegated to it; {FIX}"
    missing = [c for c in needed if c not in available]
    if missing:
        return f"controller(s) {', '.join(missing)} not available in {own} (has: {', '.join(available)}); {FIX}"
    for name in ("cgroup.procs", "cgroup.subtree_control"):
        if not os.access(own / name, os.W_OK):
            return f"{own / name} is not writable by this user; {FIX}"
    if not os.access(own, os.W_OK | os.X_OK):
        return f"{own} is not writable by this user (cannot create child cgroups); {FIX}"
    return None


def prepare(limits: Limits, own: Path | None = None, pid: int | None = None) -> Path:
    """Make `own` ready to hold cell groups. Idempotent: a second call (same process) changes nothing. Returns the
    directory cell groups are created in. Raises LimitsUnavailable with the reason."""
    global _state
    needed = limits.needed()
    pid = os.getpid() if pid is None else pid
    with _lock:
        if _state is not None:
            return _state["own"]
    reason = probe(limits, own)
    if reason:
        raise LimitsUnavailable(reason)
    own = own or host.cgroup_dir()
    leaf = own / LEAF
    try:
        leaf.mkdir(exist_ok=True)
    except OSError as error:
        raise LimitsUnavailable(f"cannot create {leaf}: {error.strerror or error}; {FIX}") from error
    # A cgroup that enables controllers for children may hold no processes: this one moves into the leaf first.
    _write(leaf / "cgroup.procs", str(pid), "cgroup.procs of the runner's leaf")
    enabled = set((_read(own / "cgroup.subtree_control") or "").split())
    added = [c for c in needed if c not in enabled]
    try:
        if added:
            try:
                (own / "cgroup.subtree_control").write_text(" ".join(f"+{c}" for c in added))
            except OSError as error:
                reason = (f"cannot enable {', '.join(added)} in {own}/cgroup.subtree_control: {error.strerror or error}"
                          + ("; another process still lives in that cgroup (cgroup v2 allows no processes there once "
                             "controllers are enabled for children)" if error.errno == 16 else "") + f"; {FIX}")
                raise LimitsUnavailable(reason) from error
    except LimitsUnavailable:
        try:  # back where we were
            (own / "cgroup.procs").write_text(str(pid))
        except OSError:
            pass
        raise
    with _lock:
        _state = {"own": own, "leaf": leaf, "pid": pid, "added": added}
    return own


def release() -> dict:
    """Undo prepare(): the process returns to the original cgroup and the controllers this module enabled are
    disabled. Safe when nothing was prepared. Returns what could not be undone ({} when clean)."""
    global _state
    with _lock:
        state, _state = _state, None
    if state is None:
        return {}
    problems = {}
    own = state["own"]
    for controller in state["added"]:
        try:
            (own / "cgroup.subtree_control").write_text(f"-{controller}")
        except OSError as error:
            problems[f"disable_{controller}"] = error.strerror or str(error)
    try:
        (own / "cgroup.procs").write_text(str(state["pid"]))
        try:
            state["leaf"].rmdir()
        except OSError:
            pass
    except OSError as error:
        problems["move_back"] = error.strerror or str(error)
    return problems


# ---- joining a group before exec -------------------------------------------------------------------------------------

# The agent's first process writes its own pid into the group, then execs the harness (same pid). Adding the pid from
# the runner after Popen returned raced the harness: anything it forked first stayed outside the limits and outside
# cgroup.kill. preexec_fn would do the same in Python, but it is unsafe in a multithreaded runner.
JOIN_FAILED_EXIT = 125
JOIN_FAILED_MARK = "swarm: could not join the cell cgroup"
JOIN_SCRIPT = 'echo $$ > "$1" || { echo "' + JOIN_FAILED_MARK + '" >&2; exit ' + str(JOIN_FAILED_EXIT) + '; }; shift; exec "$@"'


def wrap_argv(procs: Path, argv) -> list[str]:
    """`sh -c JOIN_SCRIPT swarm-join <procs> argv...`: $0 is a name, $1 the group's cgroup.procs, the rest the
    command. The shell exits with JOIN_FAILED_EXIT (and the mark on stderr) instead of running the harness unlimited."""
    return ["/bin/sh", "-c", JOIN_SCRIPT, "swarm-join", str(procs), *[str(a) for a in argv]]


def join_failed(exit_code, log_path: Path) -> bool:
    """Whether a wrapped command stopped in the wrapper because it could not join its group."""
    if exit_code != JOIN_FAILED_EXIT:
        return False
    try:
        with open(log_path, "rb") as f:
            return JOIN_FAILED_MARK.encode() in f.read(4096)
    except OSError:
        return False


# ---- one cell's group ------------------------------------------------------------------------------------------------

class CellGroup:
    """A child cgroup holding one cell's agents."""

    last_reason: str | None = None  # why the latest create() returned None

    def __init__(self, path: Path, limits: Limits, sample_s: float = 0.25):
        self.path = Path(path)
        self.limits = limits
        self.name = self.path.name
        self.closed = False
        self.close_error: str | None = None
        self._peak_sampled_kb = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, args=(sample_s,), daemon=True)

    @classmethod
    def create(cls, name: str, limits: Limits, own: Path | None = None, pid: int | None = None) -> "CellGroup | None":
        """The cell's group, or None when limits cannot be applied (the reason is CellGroup.last_reason)."""
        try:
            return cls.try_create(name, limits, own, pid)
        except LimitsUnavailable as error:
            cls.last_reason = str(error)
            return None

    @classmethod
    def try_create(cls, name: str, limits: Limits, own: Path | None = None, pid: int | None = None) -> "CellGroup":
        if not limits.requested():
            raise LimitsUnavailable("no limits requested")
        parent = prepare(limits, own, pid)
        path = parent / name
        try:
            path.mkdir()
        except FileExistsError:
            # Left by a crashed run: reusable only when empty.
            if (_read(path / "cgroup.procs") or "").strip():
                raise LimitsUnavailable(f"{path} already exists and holds processes") from None
        except OSError as error:
            raise LimitsUnavailable(f"cannot create {path}: {error.strerror or error}; {FIX}") from error
        group = cls(path, limits)
        try:
            for filename, text in limits.files().items():
                _write(path / filename, text, filename)
        except LimitsUnavailable:
            try:
                path.rmdir()
            except OSError:
                pass
            raise
        group._thread.start()
        return group

    def wrap(self, argv) -> list[str]:
        """argv run so that the process joins this group before it execs the harness (see wrap_argv)."""
        return wrap_argv(self.path / "cgroup.procs", argv)

    def add(self, pid: int):
        """Move a process (and with it, whatever it starts afterwards) into the group. Raises OSError on failure."""
        (self.path / "cgroup.procs").write_text(str(pid))

    def pids(self) -> list[int]:
        return [int(p) for p in (_read(self.path / "cgroup.procs") or "").split() if p.isdigit()]

    def _sample(self, interval_s: float):
        while not self._stop.wait(interval_s):
            current = _int(self.path / "memory.current")
            if current is not None:
                self._peak_sampled_kb = max(self._peak_sampled_kb, current // 1024)

    def stats(self) -> dict:
        """What the group used. Unreadable values are None, never 0. memory_peak_source says where the peak came from:
        the kernel's memory.peak, or the largest memory.current seen by sampling (kernels before 5.19)."""
        cpu = parse_flat(_read(self.path / "cpu.stat") or "")
        events = parse_flat(_read(self.path / "memory.events") or "")
        current = _int(self.path / "memory.current")
        peak = _int(self.path / "memory.peak")
        source = "memory.peak"
        if peak is None:
            source = "sampled memory.current" if self._peak_sampled_kb or current is not None else None
            peak_kb = max(self._peak_sampled_kb, (current or 0) // 1024) if source else None
        else:
            peak_kb = peak // 1024
        pids_events = parse_flat(_read(self.path / "pids.events") or "")
        return {"cpu_usec": cpu.get("usage_usec"),
                "memory_peak_kb": peak_kb, "memory_peak_source": source,
                "memory_current_kb": None if current is None else current // 1024,
                "oom_kill": events.get("oom_kill"), "oom": events.get("oom"), "max_events": events.get("max"),
                "pids_max_events": pids_events.get("max")}

    def close(self, timeout_s: float = 3.0) -> bool:
        """Remove the group. Anything still in it is killed (cgroup.kill) first: processes of a finished cell have no
        business here. Returns False, with close_error set, when it could not be removed."""
        if self.closed:
            return True
        self._stop.set()
        self._thread.join(timeout=2)
        deadline = time.monotonic() + timeout_s
        killed = False
        while True:
            if self.pids() and not killed:
                try:
                    (self.path / "cgroup.kill").write_text("1")
                except OSError:
                    pass
                killed = True
            try:
                self.path.rmdir()
                self.closed = True
                return True
            except FileNotFoundError:
                self.closed = True
                return True
            except OSError as error:
                self.close_error = error.strerror or str(error)
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.05)
