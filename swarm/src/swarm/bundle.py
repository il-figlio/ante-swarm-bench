"""Result bundles: hash-chained attempts, manifest and offline verification."""
import json
import os
from pathlib import Path

from .util import canonical, read_jsonl, sha256_bytes, sha256_file, write_json

GENESIS = "0" * 64


class Bundle:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.head = GENESIS
        self.count = 0

    @classmethod
    def create(cls, root: Path) -> "Bundle":
        root = Path(root)
        if root.exists():
            raise FileExistsError(f"{root} exists; a bundle is never reused")
        root.mkdir(parents=True)
        (root / "logs").mkdir()
        return cls(root)

    def append_attempt(self, record: dict) -> dict:
        record = dict(record, seq=self.count, prev_sha256=self.head)
        record["sha256"] = sha256_bytes(canonical({k: v for k, v in record.items() if k != "sha256"}))
        with (self.root / "attempts.jsonl").open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.head = record["sha256"]
        self.count += 1
        return record

    def seal(self):
        files = {str(p.relative_to(self.root)): sha256_file(p)
                 for p in sorted(self.root.rglob("*")) if p.is_file() and p.name != "MANIFEST.json"}
        write_json(self.root / "MANIFEST.json", {"chain_head": self.head, "attempts": self.count, "files": files})


def verify(root: Path) -> list[str]:
    """Problems found in a bundle. Empty list means it verifies.

    This detects corruption and inconsistency: missing, changed or extra files,
    a broken attempt chain, and records that disagree with the plan or summary.
    The bundle is not signed, so it does not prove who produced it: anyone who
    can write the directory can rebuild a consistent bundle.
    """
    from .plan import plan_digest
    from .runner import is_complete
    from .summary import summarize

    root = Path(root)
    problems = []
    manifest_path = root / "MANIFEST.json"
    if not manifest_path.exists():
        return ["MANIFEST.json missing (bundle not sealed: interrupted run?)"]
    manifest = json.loads(manifest_path.read_text())
    present = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.json"}
    for name, digest in manifest["files"].items():
        path = root / name
        if not path.exists():
            problems.append(f"missing file {name}")
        elif sha256_file(path) != digest:
            problems.append(f"modified file {name}")
    for extra in sorted(present - set(manifest["files"])):
        problems.append(f"unlisted file {extra}")
    head = GENESIS
    attempts = read_jsonl(root / "attempts.jsonl")
    for index, record in enumerate(attempts):
        if record.get("seq") != index or record.get("prev_sha256") != head:
            problems.append(f"attempt chain broken at seq {index}")
            break
        digest = sha256_bytes(canonical({k: v for k, v in record.items() if k != "sha256"}))
        if digest != record.get("sha256"):
            problems.append(f"attempt {index} hash mismatch")
            break
        head = digest
    if head != manifest.get("chain_head") or len(attempts) != manifest.get("attempts"):
        problems.append("chain head or attempt count differs from manifest")
    plan = json.loads((root / "plan.json").read_text()) if (root / "plan.json").exists() else None
    if plan is None or plan.get("plan_sha256") != plan_digest(plan):
        problems.append("plan.json missing or its hash does not match its contents")
    cells = read_jsonl(root / "cells.jsonl")
    skipped = read_jsonl(root / "skipped.jsonl")
    if plan is not None:
        planned = {c["index"]: c for c in plan["cells"]}
        for cell in cells + skipped:
            index = cell.get("cell", cell.get("index"))
            source = planned.get(index)
            if source is None or (source["harness"], source["level"], source["round"]) != (cell["harness"], cell["level"], cell["round"]):
                problems.append(f"cell {index} does not match the plan")
    known = {c["cell"] for c in cells}
    for record in attempts:
        if record.get("cell") not in known:
            problems.append(f"attempt {record.get('attempt')} belongs to no recorded cell")
            break
    per_cell = {}
    for record in attempts:
        per_cell[record.get("cell")] = per_cell.get(record.get("cell"), 0) + 1
    for cell in cells:
        if per_cell.get(cell["cell"], 0) != cell["level"]:
            problems.append(f"cell {cell['cell']} (n={cell['level']}) recorded {per_cell.get(cell['cell'], 0)} attempts")
    summary_path = root / "summary.json"
    if summary_path.exists():
        recorded = json.loads(summary_path.read_text())
        recomputed = summarize(attempts, cells, swarm_version=(plan or {}).get("swarm_version"))
        if canonical(recorded.get("groups")) != canonical(recomputed["groups"]):
            problems.append("summary.json does not match the recorded attempts")
        if plan is not None:
            complete = is_complete(plan, cells, attempts, skipped, recorded.get("cancelled", False), recorded.get("crashed"))
            if recorded.get("complete") != complete:
                problems.append(f"summary.json says complete={recorded.get('complete')}, records say {complete}")
            if recorded.get("cells_run") != len(cells):
                problems.append("summary.json cells_run does not match cells.jsonl")
    else:
        problems.append("summary.json missing")
    return problems
