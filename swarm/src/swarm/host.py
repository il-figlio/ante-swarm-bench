"""Host fingerprint, memory pressure and process-tree sampling.

Pressure follows the kernel's own signal, as Headroom does, never "RAM used":
Linux PSI (/proc/pressure/memory) or macOS kern.memorystatus_vm_pressure_level.
"""
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

PRESSURE_ORDER = {"normal": 0, "warn": 1, "critical": 2, "unknown": 3}
# Linux PSI thresholds (some avg10, percent). Declared, not tuned.
PSI_WARN = 5.0
PSI_CRITICAL = 20.0


def _sysctl(name: str) -> str | None:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def memory_state() -> dict:
    """Pressure level, available and total memory, swap used. Unknowns stay None."""
    system = platform.system()
    state = {"pressure": "unknown", "available_mb": None, "total_mb": None, "swap_used_mb": None, "pressure_source": None}
    if system == "Linux":
        try:
            info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            kb = lambda key: int(info[key].split()[0])  # noqa: E731
            state.update(total_mb=kb("MemTotal") // 1024, available_mb=kb("MemAvailable") // 1024,
                         swap_used_mb=(kb("SwapTotal") - kb("SwapFree")) // 1024)
        except (OSError, KeyError, ValueError):
            pass
        try:
            some = Path("/proc/pressure/memory").read_text().splitlines()[0]
            avg10 = float(re.search(r"avg10=([\d.]+)", some).group(1))
            state["psi_some_avg10"] = avg10
            state["pressure"] = "critical" if avg10 >= PSI_CRITICAL else "warn" if avg10 >= PSI_WARN else "normal"
            state["pressure_source"] = "linux-psi-some-avg10"
        except (OSError, AttributeError, ValueError, IndexError):
            pass
    elif system == "Darwin":
        level = _sysctl("kern.memorystatus_vm_pressure_level")
        state["pressure"] = {"1": "normal", "2": "warn", "4": "critical"}.get(level or "", "unknown")
        state["pressure_source"] = "darwin-memorystatus"
        total = _sysctl("hw.memsize")
        if total and total.isdigit():
            state["total_mb"] = int(total) // (1024 * 1024)
        try:
            vm = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^([^:]+):\s+(\d+)\.?$", vm, re.M)}
            free = pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0) \
                + pages.get("Pages purgeable", 0)
            state["available_mb"] = free * page // (1024 * 1024)
        except (OSError, AttributeError, ValueError, subprocess.TimeoutExpired):
            pass
        swap = _sysctl("vm.swapusage")
        match = re.search(r"used = ([\d.]+)M", swap or "")
        if match:
            state["swap_used_mb"] = int(float(match.group(1)))
    return state


def cpu_times() -> tuple[int, int] | None:
    """(busy, total) jiffies across all CPUs from /proc/stat; None where it does not exist (macOS)."""
    try:
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    return sum(parts) - idle, sum(parts)


def cpu_breakdown() -> dict | None:
    """Cumulative CPU seconds across all CPUs from /proc/stat: busy (not idle, iowait or steal), steal, total.
    Two readings over an interval give busy cores. None where it does not exist (macOS)."""
    try:
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        tick = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    if len(parts) < 4:
        return None
    steal = parts[7] if len(parts) > 7 else 0
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts[:8])  # guest time is already inside user and nice
    return {"busy": (total - idle - steal) / tick, "steal": steal / tick, "total": total / tick}


def psi_cpu_some_avg10() -> float | None:
    """Share of the last 10 s some task waited for a CPU (percent); None without /proc/pressure/cpu."""
    try:
        line = Path("/proc/pressure/cpu").read_text().splitlines()[0]
        return float(re.search(r"avg10=([\d.]+)", line).group(1))
    except (OSError, AttributeError, ValueError, IndexError):
        return None


def disk_used_mb(path: str = "/") -> int | None:
    try:
        import shutil
        return shutil.disk_usage(path).used // (1024 * 1024)
    except OSError:
        return None


def disk_free_mb(*paths) -> int | None:
    """Free space, in MB, on the tightest of the filesystems holding these paths. None when none can be read."""
    import shutil
    free = None
    for path in paths:
        try:
            value = shutil.disk_usage(path).free // (1024 * 1024)
        except OSError:
            continue
        free = value if free is None else min(free, value)
    return free


# ---- memory the agents hold -------------------------------------------------------------------------------------
# Summed per-process RSS counts every shared page once per process that maps it: 256 agents of one binary showed
# 18 GB "in use" on a 7.7 GB machine with no swap, and a harness that forks helpers was charged more than one that
# threads. Three scopes, best first; every sample names the one it used.
MEM_CGROUP = ("cgroup: anon + mapped files + unreclaimable kernel memory of the runner's cgroup, less the runner's own anon "
              "and the idle baseline")
MEM_PSS = "pss: summed proportional set size of running attempts' process trees"
MEM_RSS = "rss-sum: summed RSS of running attempts' process trees (counts shared pages more than once)"
_KERNEL_PARTS = ("kernel_stack", "pagetables", "slab_unreclaimable", "sock", "percpu")


def cgroup_dir(proc: Path = Path("/proc"), sys_root: Path = Path("/sys/fs/cgroup")) -> Path | None:
    """This process's cgroup v2 directory, or None (cgroup v1, macOS)."""
    try:
        line = next(l for l in (proc / "self" / "cgroup").read_text().splitlines() if l.startswith("0::"))
    except (OSError, StopIteration):
        return None
    path = sys_root / line[3:].lstrip("/")
    return path if path.is_dir() else None


def cgroup_memory_kb(directory: Path | None) -> int | None:
    """Memory the cgroup holds that cannot simply be dropped: anonymous pages, file pages that are mapped (program
    text, shared libraries; once each, however many processes map them) and kernel memory (stacks, page tables).
    Left out, because the kernel frees them on demand and our own file copying fills them: unmapped page cache, and
    reclaimable slab (the directory-entry cache; linking a prepared home into 256 sandboxes alone makes over a GB)."""
    if directory is None:
        return None
    try:
        stat = dict(line.split()[:2] for line in (directory / "memory.stat").read_text().splitlines() if line.strip())
        kernel = max(0, int(stat["kernel"]) - int(stat.get("slab_reclaimable", 0))) if "kernel" in stat else \
            sum(int(stat.get(k, 0)) for k in _KERNEL_PARTS)
        return (int(stat["anon"]) + int(stat.get("file_mapped", 0)) + kernel) // 1024
    except (OSError, KeyError, ValueError):
        return None


def self_anon_kb(proc: Path = Path("/proc")) -> int:
    """The runner's own anonymous memory. It grows with every attempt it tracks, and that is not the agents'."""
    try:
        for line in (proc / "self" / "status").read_text().splitlines():
            if line.startswith("RssAnon:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def pss_kb(pid: int, proc: Path = Path("/proc")) -> int | None:
    """Proportional set size of one process: each shared page divided among the processes that map it."""
    try:
        for line in (proc / str(pid) / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def fingerprint() -> dict:
    uname = platform.uname()
    info = {"system": uname.system, "release": uname.release, "machine": uname.machine, "cpus": os.cpu_count(),
            "python": platform.python_version()}
    if uname.system == "Darwin":
        info["model"] = _sysctl("hw.model")
        info["cpu_brand"] = _sysctl("machdep.cpu.brand_string")
    elif uname.system == "Linux":
        try:
            info["cgroup_v2"] = Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
        except OSError:
            info["cgroup_v2"] = None
        try:
            os_release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
            info["os"] = os_release.get("PRETTY_NAME", "").strip('"')
        except OSError:
            pass
    info["memory"] = {k: v for k, v in memory_state().items() if k in ("total_mb", "pressure_source")}
    return info


def task_headroom() -> dict:
    """Free task slots. Linux: the tightest cgroup pids.max on our path (counts
    threads). macOS: kern.maxprocperuid minus this user's processes."""
    if platform.system() == "Darwin":
        limit = _sysctl("kern.maxprocperuid")
        try:
            out = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid="], capture_output=True, text=True,
                                 timeout=5).stdout
            count = len(out.split())
        except (OSError, subprocess.TimeoutExpired):
            return {"tasks_free": None, "tasks_limit": None, "tasks_source": None}
        if not (limit and limit.isdigit()):
            return {"tasks_free": None, "tasks_limit": None, "tasks_source": None}
        return {"tasks_free": int(limit) - count, "tasks_limit": int(limit), "tasks_source": "darwin kern.maxprocperuid"}
    if not Path("/proc/self/cgroup").exists():
        return {"tasks_free": None, "tasks_limit": None, "tasks_source": None}
    try:
        line = next(l for l in Path("/proc/self/cgroup").read_text().splitlines() if l.startswith("0::"))
    except (OSError, StopIteration):
        return {"tasks_free": None, "tasks_limit": None, "tasks_source": None}
    path = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    best = {"tasks_free": None, "tasks_limit": None, "tasks_source": None}
    while True:
        try:
            limit = (path / "pids.max").read_text().strip()
            current = int((path / "pids.current").read_text())
            if limit != "max":
                free = int(limit) - current
                if best["tasks_free"] is None or free < best["tasks_free"]:
                    best = {"tasks_free": free, "tasks_limit": int(limit), "tasks_source": str(path / "pids.max")}
        except (OSError, ValueError):
            pass
        if path == Path("/sys/fs/cgroup") or path.parent == path:
            break
        path = path.parent
    return best


def process_table() -> dict[int, tuple[int, int, int | None]]:
    """pid -> (ppid, rss_kb, threads) for every process visible to us."""
    table = {}
    if Path("/proc/self/stat").exists():
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                table[int(entry.name)] = (int(fields[1]), int(fields[21]) * page_kb, int(fields[17]))
            except (OSError, ValueError, IndexError):
                pass
        return table
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return table
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), None)
    return table


def children_map(table: dict) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for child, (parent, *_rest) in table.items():
        children.setdefault(parent, []).append(child)
    return children


def tree(pid: int, table: dict, children: dict | None = None) -> set[int]:
    children = children if children is not None else children_map(table)
    found, todo = {pid}, [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


class Sampler:
    """Samples host memory state, the memory all agents hold together, and each running attempt's process-tree RSS.

    agents_mem_mb is the figure to chart: it can never exceed physical memory (see MEM_CGROUP above for the scopes).
    Per-attempt process-tree RSS double-counts shared pages; it is labelled as such and only steers admission, where
    erring high is safe. Gaps longer than 3x the interval are recorded so they read as unobserved time.
    """

    def __init__(self, interval_s: float, sink):
        self.interval_s = interval_s
        self.sink = sink
        self.watched: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.sealed = False
        self.errors = 0
        self.latest = dict(memory_state(), **task_headroom())
        self.cpu_last = cpu_times()
        # Taken before any agent starts: whatever else shares the cgroup (a shell, the runner's own files) is not theirs.
        self.cgroup = cgroup_dir()
        self.mem_baseline_kb = self._cgroup_net_kb()
        self.has_pss = Path("/proc/self/smaps_rollup").exists()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def watch(self, key: str, pid: int, stats: dict, on_sample=None):
        """Track an attempt's process tree. on_sample(peak_mb, peak_threads) is
        called each tick so admission can learn while attempts are running."""
        with self.lock:
            self.watched[key] = {"pid": pid, "stats": stats, "on_sample": on_sample}

    def unwatch(self, key: str):
        with self.lock:
            self.watched.pop(key, None)

    def stop(self):
        """Stop sampling. After this returns no further rows are written."""
        self.stop_event.set()
        self.thread.join(timeout=30)
        self.sealed = True

    def _cgroup_net_kb(self) -> int | None:
        held = cgroup_memory_kb(self.cgroup)
        return None if held is None else held - self_anon_kb()

    def agents_memory(self, members: set[int], rss_kb: int) -> tuple[float | None, str]:
        """(MB, scope) held by all running agents together."""
        if self.mem_baseline_kb is not None:
            # One scope for the whole run: a read that fails is an unknown sample, not a switch to another measure.
            net = self._cgroup_net_kb()
            return (None if net is None else round(max(0, net - self.mem_baseline_kb) / 1024, 1)), MEM_CGROUP
        if not self.has_pss:
            return round(rss_kb / 1024, 1), MEM_RSS  # no smaps_rollup (macOS): the old sum, marked as overcounting
        return round(sum(pss_kb(pid) or 0 for pid in members) / 1024, 1), MEM_PSS

    def _loop(self):
        last = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            gap = now - last
            last = now
            try:
                self._tick(gap)
            except Exception as error:  # noqa: BLE001 - a failed tick is an unobserved interval, not a dead sampler
                self.errors += 1
                if not self.sealed:
                    try:
                        self.sink({"t": round(time.time(), 3), "gap_s": round(gap, 3), "unobserved": True,
                                   "error": f"{type(error).__name__}: {error}"[:200]})
                    except OSError:
                        pass
            self.stop_event.wait(self.interval_s)

    def _tick(self, gap):
        state = dict(memory_state(), **task_headroom())
        self.latest = state
        table = process_table()
        children = children_map(table)
        with self.lock:
            watched = list(self.watched.items())
        per_attempt = {}
        everyone: set[int] = set()
        for key, entry in watched:
            members = tree(entry["pid"], table, children) if entry["pid"] in table else set()
            everyone |= members
            rss = sum(table[m][1] for m in members if m in table)
            stats = entry["stats"]
            stats["peak_tree_rss_kb"] = max(stats.get("peak_tree_rss_kb", 0), rss)
            stats["peak_tree_processes"] = max(stats.get("peak_tree_processes", 0), len(members))
            threads = [table[m][2] for m in members if m in table and table[m][2] is not None]
            if threads:
                stats["peak_tree_threads"] = max(stats.get("peak_tree_threads", 0), sum(threads))
            per_attempt[key] = rss
            if entry.get("on_sample"):
                entry["on_sample"](stats["peak_tree_rss_kb"] / 1024, stats.get("peak_tree_threads", 0))
        cpu, cpu_pct = cpu_times(), None
        if cpu and self.cpu_last and cpu[1] > self.cpu_last[1]:
            cpu_pct = round(100 * (cpu[0] - self.cpu_last[0]) / (cpu[1] - self.cpu_last[1]), 1)
        self.cpu_last = cpu
        if self.sealed or self.stop_event.is_set():
            return
        # cpu_pct: whole host, all cores, since the last sample (None off Linux). agents_mem_mb: memory all agents hold
        # together, in the scope named by agents_mem_scope. agents_rss_mb: the old summed process-tree RSS, kept as
        # raw data only; it double-counts shared pages and is not shown.
        mem_mb, mem_scope = self.agents_memory(everyone, sum(per_attempt.values()))
        self.sink({"t": round(time.time(), 3), "gap_s": round(gap, 3), "unobserved": gap > 3 * self.interval_s,
                   "pressure": state["pressure"], "available_mb": state["available_mb"], "total_mb": state.get("total_mb"),
                   "swap_used_mb": state["swap_used_mb"], "tasks_free": state["tasks_free"], "running": len(per_attempt),
                   "cpu_pct": cpu_pct, "disk_used_mb": disk_used_mb(), "agents_mem_mb": mem_mb, "agents_mem_scope": mem_scope,
                   "agents_rss_mb": round(sum(per_attempt.values()) / 1024, 1),
                   "attempt_rss_kb": per_attempt})
