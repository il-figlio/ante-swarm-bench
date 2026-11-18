import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_task(base: Path, task_id: str, script: list[dict], allowed=("out.txt",), test="[ \"$(cat out.txt)\" = ok ]"):
    root = base / task_id
    (root / "workspace").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "workspace" / "seed.txt").write_text("seed\n")
    (root / "task.toml").write_text(f'[task]\nid = "{task_id}"\nallowed_changes = {json.dumps(list(allowed))}\n[agent]\ntimeout_sec = 60\n')
    (root / "instruction.md").write_text("Write ok to out.txt.\n")
    (root / "tests" / "test.sh").write_text(f"#!/bin/bash\nset -euo pipefail\n{test}\n")
    (root / "script.json").write_text(json.dumps(script))
    return root


OK_SCRIPT = [{"op": "read", "path": "seed.txt"}, {"op": "write", "path": "out.txt", "content": "ok"},
             {"op": "bash", "command": "cat out.txt"}, {"op": "final", "text": "done"}]


def make_spec(base: Path, harnesses=("mini",), levels=(1, 2), extra=""):
    spec = base / "spec.toml"
    spec.write_text(f'''question = "test"
primary_outcome = "successes_per_minute"
model = {{ source = "scripted", id = "m-1", effort = "medium", max_output_tokens = 16384 }}
harnesses = {json.dumps(list(harnesses))}
levels = {json.dumps(list(levels))}
tasks = ["tasks/*"]
seed = 3
stop_rule = "all cells once"
[budget]
attempt_timeout_s = 60
[admission]
reserve_mb = 0
max_pressure = "warn"
{extra}''')
    return spec


def tempdir():
    return tempfile.TemporaryDirectory(prefix="swarm-test-")
