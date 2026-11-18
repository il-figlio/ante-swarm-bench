import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from ..util import tree_hash
from .base import Harness


def _raise(error):
    raise error  # an unreadable seed directory must fail the setup, not yield a home with a hole in it


class Hermes(Harness):
    name = "hermes"
    binary_names = ("hermes",)
    # A fresh Hermes home installs Python dependencies on first use. "cold"
    # measures that install inside every attempt; "prepared" starts from a seed home.
    required_options = {"home": ("cold", "prepared"), "seed": None}
    # The part of a seed that every attempt can share: the installed dependency environments. Hermes reads them
    # and never rewrites them, and they are nearly all of a seed's size (444 MB of it on the lane). Copying the
    # whole seed into each of 256 attempts filled a 25 GB disk before one agent started. Everything else in the
    # seed (state, sessions, logs, the facts.json we repoint) is small and is copied, so attempts stay apart.
    SHARED_DIR = "installs"

    def validate_options(self):
        home = self.options.get("home")
        if home not in ("cold", "prepared"):
            raise ValueError("harness hermes: options.home must be stated explicitly, one of ['cold', 'prepared']")
        if home == "prepared":
            seed = Path(str(self.options.get("seed", ""))).expanduser()
            if not seed.is_dir():
                raise ValueError("harness hermes: home = 'prepared' needs options.seed, a prepared HERMES_HOME directory")
            # A seed is copied into every attempt: it must hold no credentials and no links out.
            risky = [str(p.relative_to(seed)) for p in seed.rglob("*")
                     if p.is_symlink() or p.name in (".env", "auth.json") or p.name.startswith(".env.")]
            if risky:
                raise ValueError(f"harness hermes: seed contains links or credential files: {', '.join(sorted(risky)[:5])}")
        unknown = set(self.options) - {"home", "seed"}
        if unknown:
            raise ValueError(f"harness hermes: unknown options {sorted(unknown)}")

    def _seed(self) -> Path:
        return Path(str(self.options["seed"])).expanduser().resolve()

    def _shareable(self, relative: Path) -> bool:
        parts = relative.parts
        return len(parts) > 1 and parts[0] == self.SHARED_DIR and not (len(parts) == 3 and parts[2] == "facts.json")

    def _scan(self) -> dict:
        """One pass over the seed, once per cell: what an attempt copies, what it can share, and how the shared
        files looked, so a write through a link is found afterwards."""
        with self._lock:
            if self._seed_scan is None:
                seed = self._seed()
                scan = {"copy_kb": 0, "shared_kb": 0, "inodes": set(), "files": {}, "foreign": False}
                for dirpath, dirnames, filenames in os.walk(seed, onerror=_raise):
                    for name in dirnames + filenames:
                        path = Path(dirpath) / name
                        info = path.lstat()
                        kb = info.st_blocks * 512 / 1024
                        if name in filenames and self._shareable(path.relative_to(seed)):
                            scan["shared_kb"] += kb
                            scan["inodes"].add((info.st_dev, info.st_ino))
                            scan["files"][str(path.relative_to(seed))] = (info.st_size, info.st_mtime_ns, info.st_mode)
                            # The kernel refuses to hard-link a file we do not own (fs.protected_hardlinks).
                            scan["foreign"] |= info.st_uid != os.getuid()
                        else:
                            scan["copy_kb"] += kb
                scan["inodes"] = frozenset(scan["inodes"])
                self._seed_scan = scan
            return self._seed_scan

    _lock = threading.Lock()
    _seed_scan = None
    _copied_shared = False  # a link was refused (another filesystem, kernel policy): shared files were copied instead

    @property
    def prepared_kb(self) -> int:
        """KB of the seed home each attempt holds for itself; the runner reports it apart from Hermes's own disk."""
        if self.options.get("home") != "prepared":
            return 0
        scan = self._scan()
        return int(scan["copy_kb"] + (scan["shared_kb"] if self._copied_shared else 0))

    @property
    def shared_kb(self) -> int:
        """KB of the seed home all attempts share through hard links: held once on the machine, not per attempt."""
        if self.options.get("home") != "prepared" or self._copied_shared:
            return 0
        return int(self._scan()["shared_kb"])

    @property
    def shared_inodes(self) -> frozenset:
        return self._scan()["inodes"] if self.options.get("home") == "prepared" else frozenset()

    def plan_setup(self, root: Path):
        """Before the first launch: find out whether the seed can be hard-linked into sandboxes under `root` (same
        filesystem, kernel allows it). Admission must know the real cost of an attempt before it admits a wave;
        learning it from the first attempt is too late, a wave is admitted in one breath."""
        if self.options.get("home") != "prepared":
            return
        scan = self._scan()
        if scan["foreign"]:
            self._copied_shared = True
            return
        first = next(iter(scan["files"]), None)
        probe = Path(root) / f".link-probe-{os.getpid()}"
        try:
            if first is not None:
                os.link(self._seed() / first, probe)
        except OSError:
            self._copied_shared = True
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def setup_disk_kb(self) -> int:
        """What admission should expect one attempt's setup to write, before the first has been measured."""
        return self.prepared_kb

    def prepared_method(self) -> str | None:
        if self.options.get("home") != "prepared":
            return None
        return "copy" if self._copied_shared else f"hard-link {self.SHARED_DIR}/, copy the rest"

    def shared_changes(self) -> list[str]:
        """Shared seed files whose size or modification time changed since the scan: something wrote through a
        link, so attempts were not isolated from each other. Empty when all is well."""
        if self.options.get("home") != "prepared" or self._seed_scan is None:
            return []
        seed, changed = self._seed(), []
        for relative, before in self._seed_scan["files"].items():
            try:
                info = (seed / relative).lstat()
                if (info.st_size, info.st_mtime_ns, info.st_mode) != before:
                    changed.append(relative)
            except OSError:
                changed.append(relative)
        return sorted(changed)

    def _materialise(self, seed: Path, config: Path):
        """Build an attempt's home from the seed: real directories, shared files hard-linked, the rest copied.
        Directories are made fresh and writable (a seed directory's read-only mode would block cleanup)."""
        self._scan()
        for dirpath, dirnames, filenames in os.walk(seed, onerror=_raise):
            relative = Path(dirpath).relative_to(seed)
            target = config / relative
            target.mkdir(exist_ok=True)
            for name in filenames:
                source, dest = Path(dirpath) / name, target / name
                if self._shareable(relative / name) and not self._copied_shared:
                    try:
                        os.link(source, dest)
                        continue
                    except OSError:
                        # plan_setup found links possible, so this is one odd file (link count at its limit): copy
                        # that file alone. The attempt's measured setup size tells admission what it cost.
                        pass
                shutil.copy2(source, dest)

    def fingerprint(self, binary):
        info = super().fingerprint(binary)
        if self.options.get("home") == "prepared":
            info["seed_sha256"] = tree_hash(Path(self.options["seed"]).expanduser())
        return info

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        config = home / ".hermes"
        if self.options.get("home") == "prepared":
            seed = self._seed()
            self._materialise(seed, config)
            # Hermes >= 0.23 records its dependency environment by absolute path in
            # installs/<key>/facts.json and refuses one outside the active home: repoint the copy.
            for facts in config.glob("installs/*/facts.json"):
                facts.write_text(facts.read_text().replace(str(seed), str(config)))
        config.mkdir(parents=True, exist_ok=True)
        mode = "chat_completions" if self.wire == "chat" else "codex_responses"
        cap = {"max_tokens": max_output_tokens} if self.wire == "chat" else {"max_output_tokens": max_output_tokens}
        settings = {
            "model": {"provider": "custom:swarm", "default": model, "base_url": base_url, "context_length": 128000,
                      "api_mode": mode},
            "providers": {"swarm": {"base_url": base_url, "api_key": "swarm-local", "transport": mode,
                                    "api_mode": mode, "extra_body": cap}},
            "terminal": {"backend": "local"},
            "agent": {"max_turns": self._turns(), "reasoning_effort": effort},
            "auxiliary": {"title_generation": {"enabled": False}},
            # Hermes >= 0.23: no passive update check (git ls-remote to GitHub) and no shared metrics.
            "updates": {"check": False},
            "telemetry": {"shared_metrics": {"enabled": False, "send": False}},
        }
        # YAML is a superset of JSON.
        (config / "config.yaml").write_text(json.dumps(settings))
        return {"HERMES_HOME": str(config)}

    def _turns(self) -> int:
        """Follow the pack's request budget. The old fixed cap of 40 made tool-loop, triage-60 and session-250 unwinnable."""
        try:
            turns = int(getattr(self, "turn_cap", 0) or 0)
        except (TypeError, ValueError):
            turns = 0
        return turns if turns > 0 else 300

    def argv(self, binary, prompt, workdir, model, effort):
        # The chat process is the measured process. Gateway stop happens in teardown, after after_final_s.
        return [str(binary), "chat", "--quiet", "--oneshot", "--yolo", "--ignore-rules", "--toolsets", "terminal,file",
                "--reasoning", effort, "--max-turns", str(self._turns()), "--in", str(workdir), "-m", model, "-q", prompt]

    def teardown(self, binary, env):
        """Stop this home's gateway after the attempt is timed. Never --all."""
        subprocess.run([str(binary), "gateway", "stop", "-y"], cwd=env.get("HOME") or None, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                       check=False)
