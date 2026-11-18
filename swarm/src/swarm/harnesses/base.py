"""Harness plugin interface.

A harness knows how to point one agent CLI at a model endpoint inside a fresh
HOME, and how to launch it on a prompt. It never installs anything: the plan
records the binary path, version and hash, and the run refuses to start if the
binary changed.
"""
import os
import shutil
import subprocess
from pathlib import Path

from ..util import sha256_file

BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class Harness:
    name = ""
    binary_names: tuple[str, ...] = ()
    # Env vars the harness needs from the operator, never copied implicitly.
    passthrough: tuple[str, ...] = ()
    # Options a spec must state explicitly for this harness (no silent defaults).
    required_options: dict[str, tuple[str, ...]] = {}

    def __init__(self, options: dict | None = None):
        self.options = dict(options or {})
        # The plan's frozen fingerprint (path, sha256, version); set by the runner.
        self.frozen: dict = {}

    def validate_options(self) -> None:
        for key, allowed in self.required_options.items():
            if self.options.get(key) not in allowed:
                raise ValueError(f"harness {self.name}: options.{key} must be stated explicitly, one of {list(allowed)}")
        unknown = set(self.options) - set(self.required_options)
        if unknown:
            raise ValueError(f"harness {self.name}: unknown options {sorted(unknown)}")

    def locate(self, override: str | None = None) -> Path | None:
        if override:
            path = Path(override).expanduser()
            return path.resolve() if path.exists() else None
        for name in self.binary_names:
            found = shutil.which(name) or next(
                (str(p) for p in (Path.home() / ".local/bin" / name, Path.home() / f".{self.name}/bin" / name) if p.exists()),
                None,
            )
            if found:
                return Path(found).resolve()
        return None

    def version(self, binary: Path) -> str:
        try:
            out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL, env={"PATH": BASE_PATH, "HOME": os.environ.get("HOME", "/")})
            return (out.stdout or out.stderr).strip().splitlines()[0][:200]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return "unknown"

    def fingerprint(self, binary: Path) -> dict:
        return {"path": str(binary), "sha256": sha256_file(binary), "version": self.version(binary)}

    def environment(self, binary: Path, home: Path) -> dict:
        """A clean environment: no operator credentials, no inherited config."""
        env = {"PATH": f"{binary.parent}:{BASE_PATH}", "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "TMPDIR": str(home / "tmp"), "NO_COLOR": "1", "TERM": "dumb"}
        for key in self.passthrough:
            if key in os.environ:
                env[key] = os.environ[key]
        (home / "tmp").mkdir(parents=True, exist_ok=True)
        return env

    helper = None  # sub-agent model the relay also admits (model.helper); adapters that support one point it there
    wire = None  # set by the runner when the plan names one (model.wire); None means the adapter's own default

    def prepare(self, home: Path, base_url: str, model: str, effort: str, max_output_tokens: int) -> dict:
        """Write provider config into home; return extra env vars."""
        raise NotImplementedError

    def argv(self, binary: Path, prompt: str, workdir: Path, model: str, effort: str) -> list[str]:
        raise NotImplementedError
