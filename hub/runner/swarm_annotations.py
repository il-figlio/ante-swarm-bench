#!/usr/bin/env python3
"""Agentation annotation triage — strictly read-only. Reads the JSON files dropped by
POST /swarm/api/annotations (hub/server.py) and formats them for a human or agent. It never
modifies, moves, deletes, or executes anything: annotation content is data, printed, never evaluated.

Files: <dir>/<UTC-timestamp>-<rand>.json, each {"received": iso, "payload": body} where body is the
component's POST: {event, timestamp(ms), url, output?, annotations? | annotation?}. `submit` carries
the full annotations array plus rendered markdown `output`; the add/update/delete/clear events carry
one annotation each — every command prints whatever exists either way.

  list [--dir D] [--newer-than FILE|TS]  one row per file: name, UTC, count, page(s), first comment
  show FILE|GLOB... [--dir D]            full detail per annotation (whatever keys exist)
  export [--dir D] [--out FILE]          [{file,ts,url,selector,comment,extra}] to pipe into an agent
  summary [--dir D]                      counts per page URL, per day, total

--dir defaults to $SWARM_ANNOTATIONS_DIR else ./annotations. A missing or empty dir
prints "no annotations" and exits 0; malformed JSON files warn on stderr and are skipped.
"""
import argparse, calendar, json, os, sys, time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_DIR = "annotations"
NAME_TS = "%Y%m%dT%H%M%SZ"
KNOWN_KEYS = {"comment", "url", "elementPath", "selector", "element", "x", "y",
              "boundingBox", "selectedText", "nearbyText", "cssClasses", "reactComponents", "timestamp"}


def warn(msg: str) -> None:
    print(f"swarm-annotations: warning: {msg}", file=sys.stderr)


def cut(s: object, n: int) -> str:
    t = " ".join(str(s).split())
    return t if len(t) <= n else t[: n - 3] + "..."


def file_ts(path: Path) -> float:
    try:
        return calendar.timegm(time.strptime(path.name.split("-", 1)[0], NAME_TS))
    except ValueError:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def iso(v: object) -> str:
    if isinstance(v, (int, float)) and v > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v / 1000 if v > 1e11 else v))
    return v if isinstance(v, str) else ""


def collect_files(d: Path) -> List[Path]:
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.json") if p.is_file()), key=file_ts)


def load_doc(path: Path) -> Optional[dict]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as e:
        warn(f"{path.name}: unreadable, skipped ({e})")
        return None
    if not isinstance(doc, dict):
        warn(f"{path.name}: top level is not a JSON object, skipped")
        return None
    return doc


def payload_of(doc: dict) -> dict:
    p = doc.get("payload")
    return p if isinstance(p, dict) else doc


def annotations_of(doc: dict) -> List[dict]:
    p = payload_of(doc)
    anns = p.get("annotations")
    if isinstance(anns, list):
        return [a for a in anns if isinstance(a, dict)]
    a = p.get("annotation")
    return [a] if isinstance(a, dict) else []


def row_for(name: str, doc: dict, a: dict) -> dict:
    p = payload_of(doc)
    extra = {k: v for k, v in a.items() if k not in ("comment", "elementPath", "selector", "timestamp", "url")}
    for k in ("event", "output"):
        if p.get(k):
            extra[k] = p[k]
    return {"file": name,
            "ts": iso(a.get("timestamp") or p.get("timestamp") or doc.get("received")),
            "url": str(a.get("url") or p.get("url") or ""),
            "selector": str(a.get("elementPath") or a.get("selector") or ""),
            "comment": str(a.get("comment") or ""),
            "extra": extra}


def rows_of(name: str, doc: dict) -> List[dict]:
    return [row_for(name, doc, a) for a in annotations_of(doc)]


def all_rows(files: Iterable[Path]) -> List[dict]:
    rows: List[dict] = []
    for f in files:
        doc = load_doc(f)
        if doc is not None:
            rows += rows_of(f.name, doc)
    return rows


def empty(d: Path) -> int:
    print(f"no annotations: {d} does not exist" if not d.is_dir() else f"no annotations in {d}")
    return 0


def parse_newer_than(arg: str) -> float:
    p = Path(arg)
    if p.is_file():
        try:
            return p.stat().st_mtime
        except OSError:
            pass
    try:
        return float(arg)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", NAME_TS):
        try:
            return calendar.timegm(time.strptime(arg, fmt))
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(arg.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"not a file or timestamp: {arg}")


def cmd_list(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    if not d.is_dir():
        return empty(d)
    files = collect_files(d)
    if args.newer_than is not None:
        files = [f for f in files if file_ts(f) > args.newer_than]
    if not files:
        return empty(d)
    rows = []
    for f in files:
        doc = load_doc(f)
        if doc is None:
            continue
        anns = annotations_of(doc)
        p = payload_of(doc)
        urls: List[str] = []
        for u in [str(a.get("url") or "") for a in anns] + [str(p.get("url") or "")]:
            if u and u not in urls:
                urls.append(u)
        first = next((str(a.get("comment")) for a in anns if a.get("comment")), "")
        rows.append((f.name, iso(file_ts(f)), str(len(anns)), cut(", ".join(urls), 46), cut(first, 50)))
    if not rows:
        return empty(d)
    head = ("FILE", "UTC", "N", "PAGE(S)", "FIRST COMMENT")
    w = [max(len(r[i]) for r in rows + [head]) for i in range(5)]
    print(f"{head[0]:<{w[0]}}  {head[1]:<{w[1]}}  {head[2]:>{w[2]}}  {head[3]:<{w[3]}}  {head[4]}")
    for r in rows:
        print(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:>{w[2]}}  {r[3]:<{w[3]}}  {r[4]}")
    return 0


def show_file(path: Path) -> None:
    doc = load_doc(path)
    if doc is None:
        return
    p = payload_of(doc)
    print(f"=== {path.name}")
    for label, v in (("event", p.get("event")), ("received", doc.get("received")), ("url", p.get("url"))):
        if v:
            print(f"  {label}: {v}")
    if p.get("output"):
        print(f"  output: {cut(p['output'], 120)}")
    anns = annotations_of(doc)
    if not anns:
        print("  (no annotations)")
    for i, a in enumerate(anns, 1):
        print(f"  [{i}] {cut(a.get('comment') or '(no comment)', 200)}")
        if a.get("url") and a.get("url") != p.get("url"):
            print(f"      url: {a['url']}")
        sel = a.get("elementPath") or a.get("selector")
        if sel:
            print(f"      selector: {sel}")
        if a.get("element"):
            print(f"      element: {a['element']}")
        pos = " ".join(f"{k}={a[k]}" for k in ("x", "y") if a.get(k) is not None)
        if pos:
            print(f"      pos: {pos}")
        for k in ("boundingBox", "selectedText", "nearbyText", "cssClasses", "reactComponents"):
            if a.get(k):
                v = a[k]
                print(f"      {k}: {cut(v if isinstance(v, str) else json.dumps(v, default=str), 160)}")
        if a.get("timestamp"):
            print(f"      ts: {iso(a['timestamp'])}")
        for k in sorted(set(a) - KNOWN_KEYS):
            print(f"      {k}: {cut(json.dumps(a[k], default=str), 160)}")


def cmd_show(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    files: List[Path] = []
    for pat in args.files:
        p = Path(pat)
        if p.is_file():
            cands = [p]
        elif (d / pat).is_file():
            cands = [d / pat]
        elif d.is_dir():
            cands = sorted(d.glob(pat))
        else:
            cands = []
        if not cands:
            warn(f"{pat}: no matching annotation files")
        for c in cands:
            if c not in files:
                files.append(c)
    if not files:
        return empty(d) if not collect_files(d) else 1
    for f in sorted(files, key=file_ts):
        show_file(f)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    if not d.is_dir() or not collect_files(d):
        return empty(d)
    rows = all_rows(collect_files(d))
    out = json.dumps(rows, indent=1, default=str)
    if args.out:
        try:
            Path(args.out).write_text(out + "\n")
        except OSError as e:
            warn(f"cannot write {args.out}: {e}")
            return 1
        print(f"wrote {len(rows)} annotation(s) to {args.out}")
    else:
        print(out)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    if not d.is_dir() or not collect_files(d):
        return empty(d)
    files = collect_files(d)
    rows = all_rows(files)
    per_url: dict = {}
    per_day: dict = {}
    for r in rows:
        per_url[r["url"] or "(no url)"] = per_url.get(r["url"] or "(no url)", 0) + 1
        day = (r["ts"] or "")[:10] or "(unknown)"
        per_day[day] = per_day.get(day, 0) + 1
    print(f"dir: {d}")
    print("per page:")
    for u, n in sorted(per_url.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:>4}  {u}")
    print("per day:")
    for day, n in sorted(per_day.items()):
        print(f"  {n:>4}  {day}")
    print(f"total: {len(rows)} annotation(s) across {len(files)} file(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=os.environ.get("SWARM_ANNOTATIONS_DIR", DEFAULT_DIR),
                        help="annotations dir (default: $SWARM_ANNOTATIONS_DIR or %(default)s)")
    ap = argparse.ArgumentParser(prog="swarm_annotations",
                                 description="Read-only triage for Agentation annotation files.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", parents=[common], help="table of annotation files")
    p.add_argument("--newer-than", type=parse_newer_than, metavar="FILE|TIMESTAMP",
                   help="only files newer than a file's mtime or a timestamp (ISO, epoch, or file-name UTC)")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show", parents=[common], help="pretty-print annotations in file(s)")
    p.add_argument("files", nargs="+", metavar="FILE|GLOB")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("export", parents=[common], help="normalized JSON array of every annotation")
    p.add_argument("--out", metavar="FILE", help="write here instead of stdout")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("summary", parents=[common], help="counts per page, per day, total")
    p.set_defaults(fn=cmd_summary)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
