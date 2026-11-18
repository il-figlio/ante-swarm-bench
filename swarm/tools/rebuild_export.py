"""Recompute every summary.json in an export tree from its own repetition records; exit 1 listing any that differ."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from swarm import export  # noqa: E402


def check(race_dir: Path) -> str | None:
    try:
        manifest = json.loads((race_dir / "manifest.json").read_text())
        reps = [json.loads(line) for line in (race_dir / "reps.jsonl").read_text().splitlines() if line.strip()]
        want = export.dump(export.summary_of(race_dir.name, manifest.get("size"), manifest.get("repeats"), reps))
        return None if (race_dir / "summary.json").read_text() == want else "differs"
    except (OSError, ValueError) as error:
        return f"unreadable ({type(error).__name__})"


def main(argv) -> int:
    if len(argv) != 1:
        print("usage: rebuild_export.py <out>", file=sys.stderr)
        return 2
    races = sorted((Path(argv[0]) / "v1" / "races").glob("*/"))
    bad = [(d.name, why) for d in races if (why := check(d))]
    for name, why in bad:
        print(f"MISMATCH {name}: {why}")
    print(f"{len(races) - len(bad)} of {len(races)} summaries match")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
