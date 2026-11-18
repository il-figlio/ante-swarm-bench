"""Generate the Tier 0 task packs: $0 scripted-model tasks that measure the harness, not a model.

    python3 tools/make_tier0_tasks.py            # writes tasks/startup, trivial, tool-loop-50, session-250, triage-20, triage-60
    python3 tools/make_tier0_tasks.py OUT_DIR    # writes them under OUT_DIR instead (the tests compare the two)

Deterministic: the same seed gives byte-identical files, so a pack's hash only changes when this script does.
Re-running overwrites only these six directories.

  startup       one model request, no tools: launch to first request and final reply to exit. No collapsed script.
  trivial       naive: three model requests, two small writes, no shell. collapsed: one program writes both files.
  tool-loop-50  naive: 50 echo steps. collapsed: one program writes the same ticks.txt.
  session-250   naive: 250 echo steps. collapsed: one program writes the same ticks.txt.
  triage-20/60  naive: one read per log, then a planted answer.json. collapsed: one program counts ERROR lines.
                The naive path is a protocol, not evidence that a harness would have read the logs that way.
"""
import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "tasks"
SEED = 20261111
SERVICES = ["api", "auth", "billing", "cache", "gateway", "ingest", "mailer", "search", "scheduler", "worker"]
INFO = ["request ok", "cache hit", "job queued", "job finished", "health check ok", "connection reused", "config loaded"]
WARN = ["slow response", "retrying upstream", "queue depth high", "deprecated field used"]
ERRORS = ["upstream timeout", "connection refused", "invalid payload", "disk quota exceeded", "unhandled exception"]


def collapsed_loop(steps: int) -> str:
    """One program writes the same ticks.txt the naive echo loop writes."""
    return ("python3 -c \"open('ticks.txt','w').write(''.join(f'{n}\\n' for n in range(1, %d)))\"" % (steps + 1))


COLLAPSED_TRIVIAL = (
    "python3 -c \"open('note.txt','w').write('hello from agent\\n'); open('result.txt','w').write('done\\n')\""
)
COLLAPSED_TRIAGE = (
    "python3 -c \"import json,pathlib; p=pathlib.Path('logs'); "
    "counts={f.name:sum('ERROR' in line for line in f.read_text().splitlines()) for f in p.iterdir()}; "
    "best=max(counts,key=counts.get); "
    "pathlib.Path('answer.json').write_text(json.dumps({'file':best,'errors':counts[best]})+'\\n')\""
)


def write_task(name, *, out, instruction, script, allowed, test, timeout=120, files=None, tags=(), collapsed=None):
    root = out / name
    if root.exists():
        shutil.rmtree(root)
    (root / "workspace").mkdir(parents=True)
    (root / "tests").mkdir()
    for rel, content in (files or {"README.txt": "Workspace for the " + name + " task.\n"}).items():
        path = root / "workspace" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "instruction.md").write_text(instruction.strip() + "\n")
    (root / "script.json").write_text(json.dumps(script, indent=2) + "\n")
    if collapsed:
        (root / "script.collapsed.json").write_text(json.dumps(collapsed, indent=2) + "\n")
    tag_list = ", ".join(json.dumps(t) for t in ("tier0", *tags))
    allowed_list = ", ".join(json.dumps(a) for a in allowed)
    (root / "task.toml").write_text(
        f'[task]\nid = "{name}"\nallowed_changes = [{allowed_list}]\n\n[agent]\ntimeout_sec = {timeout}\n\n'
        f"[metadata]\ntags = [{tag_list}]\n")
    test_path = root / "tests" / "test.sh"
    test_path.write_text("#!/bin/bash\nset -euo pipefail\n" + test.strip() + "\n")
    test_path.chmod(0o755)


def loop_task(out, name, steps, timeout):
    write_task(
        name, out=out,
        instruction=f"Append the numbers 1 to {steps} to ticks.txt, one per line, in order. Use one shell command per "
                    f"number, `echo N >> ticks.txt`, run one at a time. Then report that you are done.",
        script=[{"op": "bash", "command": f"echo {n} >> ticks.txt"} for n in range(1, steps + 1)]
               + [{"op": "final", "text": f"Appended 1 to {steps} to ticks.txt."}],
        collapsed=[{"op": "bash", "command": collapsed_loop(steps)},
                   {"op": "final", "text": f"Appended 1 to {steps} to ticks.txt."}],
        allowed=["ticks.txt"],
        test=f'python3 -c "import sys; got = open(\'ticks.txt\').read().split(); '
             f'sys.exit(0 if got == [str(n) for n in range(1, {steps} + 1)] else \'ticks.txt is wrong\')" && echo PASS',
        timeout=timeout,
        tags=("overhead",),
    )


def log_file(rng, target_bytes, errors):
    """One ~target_bytes log with exactly `errors` lines containing ERROR, shuffled among INFO/WARN lines."""
    lines, clock = [], rng.randrange(0, 3600)
    error_lines = [f"ERROR {rng.choice(SERVICES)} {rng.choice(ERRORS)} id={rng.randrange(16**8):08x}" for _ in range(errors)]
    filler = []
    size = sum(len(e) + 22 for e in error_lines)
    while size < target_bytes:
        level, text = ("WARN", rng.choice(WARN)) if rng.random() < 0.15 else ("INFO", rng.choice(INFO))
        entry = f"{level} {rng.choice(SERVICES)} {text} id={rng.randrange(16**8):08x}"
        filler.append(entry)
        size += len(entry) + 22
    body = filler + error_lines
    rng.shuffle(body)
    for entry in body:
        clock += rng.randrange(1, 4)
        lines.append(f"2026-11-09T{clock // 3600 % 24:02d}:{clock // 60 % 60:02d}:{clock % 60:02d}Z {entry}")
    return "\n".join(lines) + "\n"


def triage_task(out, name, count, rng):
    files, counts = {}, {}
    # 1-40 ERROR lines per file keeps each near 8.7 KB; one file gets a strictly higher count, so exactly one answer.
    pool = [rng.randint(1, 40) for _ in range(count)]
    pool[rng.randrange(count)] = 41 + rng.randrange(10)
    for index, errors in enumerate(pool):
        rel = f"logs/node-{index + 1:02d}.log"
        files[rel] = log_file(rng, 8700, errors)
        counts[rel.removeprefix("logs/")] = sum("ERROR" in line for line in files[rel].splitlines())
    best = max(counts, key=counts.get)
    answer = {"file": best, "errors": counts[best]}
    write_task(
        name, out=out,
        instruction=f"logs/ holds {count} log files. Find which file has the most lines containing ERROR, and how many "
                    f"such lines it has. Write answer.json as {{\"file\": \"<file name, e.g. node-01.log>\", \"errors\": <count>}}.",
        script=[{"op": "read", "path": rel} for rel in sorted(files)]
               + [{"op": "write", "path": "answer.json", "content": json.dumps(answer) + "\n"},
                  {"op": "final", "text": f"{best} has the most ERROR lines: {counts[best]}."}],
        collapsed=[{"op": "bash", "command": COLLAPSED_TRIAGE},
                   {"op": "final", "text": f"{best} has the most ERROR lines: {counts[best]}."}],
        allowed=["answer.json"],
        test=f"python3 - <<'PY'\nimport json\ngot = json.load(open('answer.json'))\n"
             f"assert got == {answer!r}, f'answer.json is {{got}}'\nprint('PASS')\nPY",
        timeout=300 if count > 20 else 180,
        files=files,
        tags=("ops", "context"),
    )


def main(argv=sys.argv[1:]):
    out = Path(argv[0]).resolve() if argv else ROOT
    rng = random.Random(SEED)
    write_task(
        "startup", out=out,
        instruction="Reply with the single word ready. Do not run any tools or change any files.",
        script=[{"op": "final", "text": "ready"}],
        allowed=[],
        test="test \"$(cat README.txt)\" = 'Workspace for the startup task.' && echo PASS",
        tags=("overhead",),
    )
    write_task(
        "trivial", out=out,
        instruction="Write `hello from agent` to note.txt, then write `done` to result.txt. Do not run shell commands. Report when done.",
        script=[{"op": "write", "path": "note.txt", "content": "hello from agent\n"},
                {"op": "write", "path": "result.txt", "content": "done\n"},
                {"op": "final", "text": "Wrote note.txt and result.txt."}],
        collapsed=[{"op": "bash", "command": COLLAPSED_TRIVIAL},
                   {"op": "final", "text": "Wrote note.txt and result.txt."}],
        allowed=["note.txt", "result.txt"],
        test="test \"$(cat note.txt)\" = 'hello from agent' && test \"$(cat result.txt)\" = done && echo PASS",
        tags=("overhead",),
    )
    loop_task(out, "tool-loop-50", 50, 300)
    loop_task(out, "session-250", 250, 900)
    triage_task(out, "triage-20", 20, rng)
    triage_task(out, "triage-60", 60, rng)
    print("wrote", ", ".join(["startup", "trivial", "tool-loop-50", "session-250", "triage-20", "triage-60"]), "under", out)


if __name__ == "__main__":
    sys.exit(main())
