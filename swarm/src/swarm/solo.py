"""Swarm solo: Ante by itself, replayed as a small handheld game.

Replay only. The page reads Ante runs already stored under a serve data directory (`runs/<id>/run.json` and its
bundle) and draws each attempt's trace as a game. Nothing here starts a harness, calls a model or writes to the data.

What leaves the server is built field by field from an allow-list, never by copying a row: run receipts (preset,
size, hashes, pass counts, timings) and, per attempt, its events `{t, kind, arg?, ok?, bytes?}`. Free text (file
names, shell commands, the final message) goes through `clean()`, which drops absolute paths, long hex and
base64-like runs, e-mail addresses and URLs, and cuts the result short.
"""
import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUN_ID = re.compile(r"^[0-9a-f]{12}$")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
KINDS = {"read": "read", "write": "edit", "edit": "edit", "bash": "exec", "final": "done"}
TEST_CMD = re.compile(r"\b(pytest|unittest|test|check|verify|validate)\b|python3? \S+\.py\b")
MAX_ARG = 72

_SCRUB = [
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b\w+://\S+"), "<url>"),
    (re.compile(r"(?<![\w.])(~|/)[\w.@%+=:,-]*(/[\w.@%+=:,-]*)+"), "<path>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<hex>"),
    (re.compile(r"\b(sk|pk|ghp|gho|xox[bap])[-_][\w-]{8,}"), "<secret>"),
    (re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}"), "<blob>"),
]


def clean(text, limit: int = MAX_ARG) -> str | None:
    """A short, public form of free text from a trace, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    out = " ".join(text.split())
    for pattern, repl in _SCRUB:
        out = pattern.sub(repl, out)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def clean_file(name) -> str | None:
    """A file the task touches: relative, inside the work dir, plain characters. Anything else is hidden."""
    if not isinstance(name, str) or not re.fullmatch(r"[\w.*-]+(/[\w.*-]+){0,4}", name) or ".." in name.split("/"):
        return None
    return name[:MAX_ARG]


def kind_of(row: dict) -> str:
    kind = KINDS.get(row.get("op"), "model")
    if kind == "exec" and TEST_CMD.search(str(row.get("arg") or "")):
        return "test"
    return kind


def events(rows: list[dict], passed) -> list[dict]:
    """Request rows (one per model turn, each naming the tool it asked for) → the page's events, oldest first, with
    t in seconds from the first request. The checker's verdict closes the list as a `verify` event."""
    rows = sorted((r for r in rows if isinstance(r, dict) and isinstance(r.get("at"), (int, float))), key=lambda r: (r["at"], r.get("index", 0)))
    out = []
    t0 = rows[0]["at"] if rows else 0.0
    for r in rows:
        kind = kind_of(r)
        ev = {"t": round(r["at"] - t0, 3), "kind": kind}
        arg = clean_file(r.get("arg")) if kind in ("read", "edit") else clean(r.get("arg"))
        if arg:
            ev["arg"] = arg
        ok = r.get("status") == 200 and not r.get("error") and r.get("completed", True) is not False
        ev["ok"] = bool(ok)
        if isinstance(r.get("request_bytes"), int):
            ev["bytes"] = r["request_bytes"]
        out.append(ev)
    last = out[-1]["t"] if out else 0.0
    out.append({"t": round(last + 0.001, 3), "kind": "verify", "ok": passed is True})
    return out


def _num(v, nd=3):
    return round(v, nd) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def load_run(run_dir: Path) -> dict | None:
    """One stored Ante run, public form, with every attempt's events. None when it is not a complete Ante run."""
    try:
        run = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(run, dict) or run.get("harness") != "ante" or not run.get("complete") or not RUN_ID.match(str(run.get("id", ""))):
        return None
    attempts = []
    for a in _read_jsonl(run_dir / "bundle" / "attempts.jsonl"):
        name = str(a.get("attempt", ""))
        if not re.fullmatch(r"[\w-]{1,64}", name):
            continue
        rows = _read_jsonl(run_dir / "bundle" / "logs" / name / "requests.jsonl")
        evs = events(rows, a.get("passed"))
        at = [r["at"] for r in rows if isinstance(r.get("at"), (int, float))]
        agent = a.get("agent") if isinstance(a.get("agent"), dict) else {}
        failure = a.get("failure")
        attempts.append({
            "id": name, "slot": a.get("slot") if isinstance(a.get("slot"), int) else len(attempts),
            "task": clean(a.get("task"), 40), "passed": a.get("passed") is True,
            "failure": clean(failure if isinstance(failure, str) else (failure or {}).get("kind") if isinstance(failure, dict) else None, 40),
            "wall_s": _num(agent.get("wall_s")), "cpu_s": _num(agent.get("cpu_s")),
            "rss_mb": _num((agent.get("max_process_rss_kb") or 0) / 1024, 1) if agent.get("max_process_rss_kb") else None,
            "start": min(at) if at else None, "end": max(at) if at else None,
            "steps": sum(1 for e in evs if e["kind"] != "verify"), "events": evs,
        })
    starts = [a["start"] for a in attempts if a["start"] is not None]
    t0 = min(starts) if starts else None
    for a in attempts:  # times relative to the run's first request; the absolute clock is not published
        for key in ("start", "end"):
            a[key] = round(a[key] - t0, 3) if a[key] is not None and t0 is not None else None
    attempts.sort(key=lambda a: (a["slot"], a["id"]))
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    tasks = sorted({a["task"] for a in attempts if a["task"]})
    return {
        "id": run["id"], "preset": clean(run.get("preset"), 32), "title": clean(run.get("title"), 60),
        "size": run.get("size") if isinstance(run.get("size"), int) else None,
        "created": clean(run.get("created"), 25), "harness_version": clean(run.get("harness_version"), 32),
        "plan": str(run.get("plan_sha256", ""))[:12] or None, "chain": str(run.get("chain_head", ""))[:12] or None,
        "verify": clean(run.get("verify"), 20), "tasks": tasks,
        "attempts_n": len(attempts), "passes": sum(1 for a in attempts if a["passed"]),
        "wall_s_median": _num(result.get("wall_s_median")), "max_running": result.get("max_running") if isinstance(result.get("max_running"), int) else None,
        "attempts": attempts,
    }


def receipt(run: dict) -> dict:
    """The list form: a run without its attempts."""
    return {k: v for k, v in run.items() if k != "attempts"}


class Library:
    """Ante runs under a data directory, reloaded when the directory's run list changes."""

    def __init__(self, data: Path):
        self.root = Path(data) / "runs"
        self.lock = threading.Lock()
        self.key = None
        self.runs: dict[str, dict] = {}

    def _scan_key(self):
        try:
            return tuple(sorted((p.name, (p / "run.json").stat().st_mtime) for p in self.root.iterdir() if (p / "run.json").exists()))
        except OSError:
            return ()

    def refresh(self) -> dict[str, dict]:
        key = self._scan_key()
        with self.lock:
            if key != self.key:
                runs = {}
                for name, _ in key:
                    if RUN_ID.match(name):
                        run = load_run(self.root / name)
                        if run:
                            runs[run["id"]] = run
                self.runs, self.key = runs, key
            return self.runs

    def listing(self) -> list[dict]:
        return sorted((receipt(r) for r in self.refresh().values()), key=lambda r: r["created"] or "", reverse=True)

    def get(self, rid: str) -> dict | None:
        return self.refresh().get(rid) if RUN_ID.match(rid) else None


# The maze race is a cartridge for the AX-1: a front-solo page (maze.html, with maze.css, maze.js and mazeplay.js beside
# it) that loads /maze/maze-core.js and gets the same strict CSP as every front-solo page. It is served at /maze/ and
# /maze/race. The analysis view (front-maze/prototype.html) is written as a Claude artifact: page content with no
# document skeleton and inline <style> and <script>. Served here it gets a skeleton and a CSP that allows exactly its
# own inline blocks by hash, so nothing else inline can run.
CARTRIDGE = {"": "maze.html", "race": "maze.html"}
# The trail and hill cartridges (live-capable games) and the replay console of stored runs.
GAME_PAGES = {"/": "trail.html", "": "trail.html", "/trail/": "trail.html", "/hill/": "hill.html", "/replay": "index.html"}
MAZE_PAGES = {"prototype": "prototype.html"}
MAZE_SKELETON = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>')
_BLOCK = re.compile(r"<(script|style)>(.*?)</\1>", re.S)


def maze_page(source: str) -> tuple[bytes, str]:
    """Wrap one maze page and build its CSP from the hashes of its inline blocks."""
    html = MAZE_SKELETON + source + "</body></html>"
    hashes = {"script": [], "style": []}
    for kind, body in _BLOCK.findall(html):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        hashes[kind].append(f"'sha256-{digest}'")
    csp = ("default-src 'self'; script-src 'self' " + " ".join(hashes["script"])
           + "; style-src 'self' " + " ".join(hashes["style"])
           + "; font-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    return html.encode(), csp


def handler_for(lib: Library, static: Path, maze: Path | None = None, play=None):
    """`play` (a play.PlayAPI, or arcade.Router) adds the live games under /api/play/; without it those paths are 404."""
    static = static.resolve()
    maze = maze.resolve() if maze else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="application/json", cache="no-store", csp=None):
            payload = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp or "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                                        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            if not (play and play.route(self, "POST")):
                self.close_connection = True   # the body was not read
                self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if not (play and play.route(self, "DELETE")):
                self._send(404, {"error": "not found"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            parts = path.strip("/").split("/")
            if play and self.command == "GET" and play.route(self, "GET"):
                return
            if path == "/api/healthz":
                return self._send(200, {"ok": True})
            if path == "/api/runs":
                return self._send(200, {"runs": lib.listing()})
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                run = lib.get(parts[2])
                return self._send(200, run) if run else self._send(404, {"error": "no such run"})
            if path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if maze and parts[0] == "maze":
                # Exactly one segment after "/maze/", with no trailing slash: "/maze/race/" would load the page with
                # its relative "maze-core.js" pointing at /maze/race/maze-core.js, a 404 and a dead page.
                if path == "/maze":
                    return self._maze(None)
                rel = path[len("/maze/"):] if path.startswith("/maze/") else "/"
                return self._maze(rel) if "/" not in rel else self._send(404, b"not found", "text/plain")
            if len(parts) == 2 and parts[0] == "r" and RUN_ID.match(parts[1]):
                path = "/replay"
            if path in ("/trail", "/hill"):
                return self._redirect(path + "/")
            # The console opens on the trail cartridge; the replay console (stored runs on the trail and hill
            # replay boards) is at /replay and /r/<run>.
            rel = GAME_PAGES.get(path, path.lstrip("/"))
            target = (static / rel).resolve()
            if static not in target.parents or not target.is_file() or target.suffix not in TYPES:
                return self._send(404, b"not found", "text/plain")
            cache = "public, max-age=86400, immutable" if target.suffix == ".woff2" else "no-cache"
            return self._send(200, target.read_bytes(), TYPES[target.suffix], cache)

        def _redirect(self, location):
            self.send_response(308)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _maze(self, rel):
            if rel is None:  # "/maze" -> "/maze/", so the analysis view's relative script path resolves under /maze/
                return self._redirect("/maze/")
            if rel in CARTRIDGE:
                page = static / CARTRIDGE[rel]
                if page.is_file():
                    return self._send(200, page.read_bytes(), TYPES[".html"], "no-cache")
            if rel in MAZE_PAGES:
                page = maze / MAZE_PAGES[rel]
                if page.is_file():
                    body, csp = maze_page(page.read_text())
                    return self._send(200, body, TYPES[".html"], "no-cache", csp)
            if rel.endswith(".js"):
                target = (maze / rel).resolve()
                if maze in target.parents and target.is_file():
                    return self._send(200, target.read_bytes(), TYPES[".js"], "no-cache")
            return self._send(404, b"not found", "text/plain")

    return Handler


def serve_solo(data: Path, static: Path, host="127.0.0.1", port=8000, maze: Path | None = None, play_config=None):
    lib = Library(data)
    play = None
    if play_config is not None:
        from .arcade import Arcade, Router
        from .play import PlayAPI, PlayManager
        manager = PlayManager(play_config)
        play = PlayAPI(manager)
        if getattr(manager, "arcade", None) is None:   # until play.py hosts trail and hill itself (the arcade patch)
            play = Router(play, Arcade(manager))

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server((host, port), handler_for(lib, static, maze, play))
    live = "" if play is None else f" · live maze {'on' if play_config.passcode_sha256 else 'off (no passcode configured)'}" \
        f"{' (stand-in model)' if play_config.standin else ''}"
    print(f"swarm solo on {host}:{port} · {len(lib.refresh())} ante runs{live}", flush=True)
    server.serve_forever()
