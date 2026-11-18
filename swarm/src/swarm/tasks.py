"""Harbor-style task directories.

    <task>/task.toml        id, timeouts, allowed changes
    <task>/instruction.md   the prompt every harness receives, unchanged
    <task>/workspace/       starting files copied into each attempt's workdir
    <task>/tests/test.sh    run after the agent exits; exit 0 means pass
    <task>/script.json      optional: naive protocol, one tool call per step
    <task>/script.collapsed.json  optional: same files, one program
    task.toml [maze]        optional: a maze task (see docs/maze.md); needs no script.json, the scripted
                            model drives it with a per-attempt explorer policy

Tests are never visible to the agent: they are copied in only after it exits,
so a pass cannot come from edited tests.
"""
import glob
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import path_matches, tree_hash

SCRIPT_OPS = {"read", "write", "bash", "final"}


@dataclass
class Task:
    id: str
    root: Path
    instruction: str
    timeout_s: int
    allowed_changes: list[str]
    check_timeout_s: int = 120
    script: list[dict] | None = None
    collapsed: list[dict] | None = None
    tags: list[str] = field(default_factory=list)
    maze: dict | None = None  # validated [maze] parameters; None for an ordinary task

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def tests(self) -> Path:
        return self.root / "tests"

    def digest(self) -> str:
        return tree_hash(self.root)

    def change_allowed(self, relative: str) -> bool:
        return any(path_matches(relative, pattern) for pattern in self.allowed_changes)

    def script_for(self, protocol: str) -> list[dict] | None:
        """naive replays one tool call per step. collapsed does the same file effects in one program."""
        if protocol == "collapsed":
            return self.collapsed
        return self.script


def validate_script(steps, where: str) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{where}: script must be a non-empty list")
    for index, step in enumerate(steps):
        op = step.get("op") if isinstance(step, dict) else None
        if op not in SCRIPT_OPS:
            raise ValueError(f"{where}: step {index} has unknown op {op!r}")
        if op == "write" and not (isinstance(step.get("path"), str) and isinstance(step.get("content"), str)):
            raise ValueError(f"{where}: write step {index} needs path and content")
        if op == "read" and not isinstance(step.get("path"), str):
            raise ValueError(f"{where}: read step {index} needs path")
        if op in ("read", "write") and (step["path"].startswith("/") or ".." in Path(step["path"]).parts):
            raise ValueError(f"{where}: {op} step {index} path must stay inside the workdir")
        if op == "bash" and not isinstance(step.get("command"), str):
            raise ValueError(f"{where}: bash step {index} needs command")
    if steps[-1]["op"] != "final":
        raise ValueError(f"{where}: script must end with a final step")
    return steps


def load_task(root: Path) -> Task:
    root = Path(root).resolve()
    links = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_symlink()]
    if links:
        # Links are not covered by the task hash and could point anywhere.
        raise ValueError(f"{root}: symlinks are not allowed in tasks: {', '.join(sorted(links)[:5])}")
    meta = tomllib.loads((root / "task.toml").read_text())
    task_meta = meta.get("task", {})
    instruction = (root / "instruction.md").read_text().strip()
    if not instruction:
        raise ValueError(f"{root}: instruction.md is empty")
    if not (root / "workspace").is_dir():
        raise ValueError(f"{root}: missing workspace/")
    if not (root / "tests" / "test.sh").is_file():
        raise ValueError(f"{root}: missing tests/test.sh")
    script = None
    if (root / "script.json").exists():
        script = validate_script(json.loads((root / "script.json").read_text()), str(root / "script.json"))
    collapsed = None
    if (root / "script.collapsed.json").exists():
        collapsed = validate_script(json.loads((root / "script.collapsed.json").read_text()), str(root / "script.collapsed.json"))
    timeout = int(meta.get("agent", {}).get("timeout_sec", task_meta.get("timeout_s", 300)))
    maze = None
    if "maze" in meta:
        if not isinstance(meta["maze"], dict):
            raise ValueError(f"{root}: [maze] must be a table")
        from . import maze as maze_module  # lazy: tasks without [maze] never need it
        maze = maze_module.validate_params(dict(meta["maze"]))
    check_timeout = int(meta.get("verifier", {}).get("timeout_sec", 120))
    if "allowed_changes" not in task_meta or not isinstance(task_meta["allowed_changes"], list):
        raise ValueError(f"{root}: task.allowed_changes must list the paths the agent may change (use [\"**\"] for any)")
    return Task(
        id=task_meta.get("id", root.name),
        root=root,
        instruction=instruction,
        timeout_s=timeout,
        allowed_changes=list(task_meta["allowed_changes"]),
        check_timeout_s=check_timeout,
        script=script,
        collapsed=collapsed,
        tags=list(meta.get("metadata", {}).get("tags", [])),
        maze=maze,
    )


def load_tasks(patterns: list[str], base: Path) -> list[Task]:
    found = {}
    for pattern in patterns:
        # Absolute patterns are used as given; relative ones resolve against the spec.
        full = pattern if Path(pattern).is_absolute() else str(Path(base) / pattern)
        for path in map(Path, sorted(glob.glob(full))):
            if (path / "task.toml").is_file():
                task = load_task(path)
                if task.id in found and found[task.id].root != task.root:
                    raise ValueError(f"duplicate task id {task.id}")
                found[task.id] = task
    if not found:
        raise ValueError(f"no tasks matched {patterns}")
    return [found[key] for key in sorted(found)]
