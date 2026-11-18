"""Live trail and hill: a human against real Ante processes, hosted by the live maze's round manager (play.py).

docs/live.md, "Games: trail and hill". A game is a small interface (`Game` below): a rules model holding the
authoritative state, a per-agent view (`look`), the `./<game>` client and its ops, the human's action, and the clock.
`Arcade` runs a round of one game on the PlayManager it is given and shares that manager's limits: the passcode
check and its rate limit, the token budgets, the sign-in or relay or stand-in model route, the cgroup limits and the
one-round-at-a-time slot (an arcade round is the manager's active round while it runs, so a maze round queues
behind it and starts when it ends; an arcade round asked for while anything runs is refused as busy).

Every Ante is a Swarm cell, as in the live maze: its own sandbox, the `./<game>` client over a GameService socket
whose token is bound to the Ante's process tree, the cell's cgroup when limits are set, and the round's model route.
Trail and hill have no hidden board: every viewer sees the whole state. The only secret is the seed (and with it the
future pieces in hill), committed as sha256("<seed>:<nonce>") at the start and revealed at the end.

Swarm styles (owner, 2026-11-17): a game declares how a swarm plays it and offers only the sizes that help.
  trail  "shared": the swarm's Antes are ONE team on the one grid (team score = the sum of its tokens), with shared
         plans in `look`, `./trail team` and a notes board private to the team; sizes 1 and 3.
  hill   "pooled": each Ante plays its own well with its own piece sequence, alone; the team's rows are pooled and
         compared per well with the human's; sizes 1 and 4.

HTTP, on the solo server, under /api/play/<game>/ for game in trail, hill:
  GET    status                      the manager's status plus {game, swarm_style, sizes, counts}
  POST   rounds                      {passcode, rival: {mode: single|swarm, count}} -> 201 {round, token, seed_hash}
  POST   rounds/<id>/act             X-Round-Token; trail {dir: N|E|S|W}; hill {col, rot}
  GET    rounds/<id>/events          SSE: state, frame, call, end
  DELETE rounds/<id>                 give up
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import queue
import re
import secrets
import threading
import time
import types
from pathlib import Path

from . import harnesses, hill as hl, trail as tr
from .gamesvc import GameService, client_source
from .sandbox import ProcessSandbox, clean_root, execute, reap_escaped, remove_tree, runner_tag, tagged_processes

ROUND_ID = re.compile(r"^[0-9a-f]{12}$")
MAX_READERS = 3
HEARTBEAT_S = 15.0
MAX_BODY = 4096
DIRS = ("N", "E", "S", "W")
READY_WAIT_S = 20.0   # the longest the clock waits for every Ante's first look
COUNTDOWN_S = 3.0     # then 3, 2, 1, go: the human gets the same start as the Antes


# ---- the games ----

class Game:
    """What a game gives the host. Subclasses fill these in; the model is created per round."""
    name = "game"
    ops: dict = {}
    free_ops = ("look",)  # ops an Ante may call before the go (the rest wait for it)
    usage = ""
    poll_s = 0.1          # how often the round thread runs `advance`
    swarm_style = "shared"   # how a swarm plays: "shared" (one team, one board) | "pooled" (own boards, pooled score)
    sizes = (1,)             # the Ante counts offered: 1 is "ante x1", more is a swarm

    def counts(self) -> dict:
        """mode: the counts it allows."""
        return {"single": (1,), "swarm": tuple(n for n in self.sizes if n > 1)}

    def teams(self, mode: str, names: list[str]) -> list[str]:
        """The team of every seat, "you" first: a lone Ante is its own team ("ante"), a swarm is the team "swarm"."""
        return ["you"] + (["swarm"] * len(names) if mode == "swarm" else list(names))

    def new_model(self, seed: int, names: list[str], teams: list[str] | None = None):
        raise NotImplementedError

    def prompt(self, mode: str, count: int) -> str:
        raise NotImplementedError

    def standin(self, index: int):
        """The scripted policy a stand-in Ante runs (no model)."""
        raise NotImplementedError

    def act(self, model, body: dict, now: float) -> tuple[int, dict]:
        """The human's action ("you"), under the service lock."""
        raise NotImplementedError

    def advance(self, model, elapsed: float) -> list[tuple[str, dict]]:
        """Time passes, under the service lock. Returns events to send."""
        return []

    def on_call(self, model, who, op, arg, result, elapsed: float) -> list[tuple[str, dict]]:
        """Events for one honoured agent call, under the service lock."""
        return [("call", {"who": who, "op": op if isinstance(op, str) else "?",
                          **({"arg": arg} if isinstance(arg, str) and len(arg) <= 12 else {})})]

    def frame(self, model) -> dict:
        return model.frame()

    def stats(self, model) -> dict:
        return {}

    def limit_s(self) -> float:
        """The longest a round can run by its own clock."""
        raise NotImplementedError


class TrailGame(Game):
    name = "trail"
    ops = tr.OPS
    free_ops = ("look", "team", "note")
    usage = tr.USAGE
    swarm_style = "shared"
    # x3, not x4: in scripted rounds on the 36x23 grid a fourth member raised the share of members that crash from
    # about 0.15 to 0.2 (five trails crowd the lanes), and a real x4 team costs a third more tokens per round.
    sizes = (1, 3)

    # Live pace: a model takes seconds per command, so the live tick is slow (0.8 s) and Antes steer with plans of
    # up to 12 moves (`./trail plan`); 225 ticks is a 180 s round, as hill. The scripted cartridge keeps its own pace.
    # A lone Ante plays on `size`; a swarm on `swarm_size`, the big grid, so a team of trails has room.
    def __init__(self, size: str = "m", tick_s: float = 0.8, clock_ticks: int = 225, swarm_size: str = "l"):
        self.size, self.tick_s, self.clock_ticks, self.swarm_size = size, tick_s, clock_ticks, swarm_size
        self.poll_s = min(0.05, tick_s / 4)

    def new_model(self, seed, names, teams=None):
        size = self.swarm_size if teams and len(set(teams)) < len(teams) else self.size
        return tr.TrailModel(tr.params(size, seed, len(names), clock=self.clock_ticks), names, teams)

    def prompt(self, mode, count):
        return tr.prompt(mode, count, self.tick_s)

    def standin(self, index):
        return tr.TrailPolicy(0, index)

    def act(self, model, body, now):
        d = body.get("dir")
        if d not in DIRS:
            return 422, {"error": "bad_dir"}
        ok = model.steer("you", d)
        pl = model.by["you"]
        return 200, {"ok": ok, "tick": model.tick, "next_heading": pl.pending or pl.heading, "alive": pl.alive}

    def advance(self, model, elapsed):
        want = min(int(elapsed / self.tick_s), model.clock)
        stepped = False
        while not model.over and model.tick < want:
            model.step()
            stepped = True
        return [("frame", model.frame())] if stepped else []

    def on_call(self, model, who, op, arg, result, elapsed):
        ev = {"who": who, "op": op if isinstance(op, str) else "?"}
        if op == "turn" and isinstance(arg, str) and arg.strip().upper() in ("L", "R", "S"):
            ev["arg"] = arg.strip().upper()
        elif op == "plan" and isinstance(arg, str) and re.fullmatch(r"[LRS]{1,%d}" % tr.MAX_PLAN, arg.strip().upper()):
            ev["arg"] = arg.strip().upper()
        # a note's text is the team's own: the feed shows only that a note was pinned
        return [("call", ev)]

    def stats(self, model):
        return {"ticks": model.tick, "tick_s": self.tick_s,
                "tokens": {pl.who: pl.tokens for pl in model.players},
                "teams": {t: model.team_tokens(t) for t in model.teams},
                "out": {pl.who: pl.died for pl in model.players if not pl.alive},
                "causes": {pl.who: {"cause": pl.cause, "hit": pl.hit} for pl in model.players if not pl.alive},
                "notes": {t: len(n) for t, n in model.notes.items() if n}}

    def reveal(self, model) -> dict:
        """What only the end shows (the page and the round log): the teams' notes boards, private during the round
        (as the live maze reveals its boards at the end)."""
        return {"notes": {t: model.note_lines(t) for t, n in model.notes.items() if n}}

    def limit_s(self):
        return self.clock_ticks * self.tick_s


class HillGame(Game):
    name = "hill"
    ops = hl.OPS
    usage = hl.USAGE
    poll_s = 0.1
    swarm_style = "pooled"
    sizes = (1, 4)

    # fuse_s is the human's fuse as the page shows it (the page drops at fuse_s; the server allows player_grace_s
    # more). An Ante's fuse is ante_fuse_s, by default the human's server-side fuse (12 s). min_drop_s spaces an
    # Ante's drops, and every Ante drop must name the id of the piece it last looked at (hill.LiveHill).
    def __init__(self, clock_s: float = hl.CLOCK_S, fuse_s: float = 10.0, player_grace_s: float = 2.0,
                 ante_fuse_s: float | None = None, min_drop_s: float = 1.0):
        self.clock_s, self.fuse_s, self.grace = clock_s, fuse_s, player_grace_s
        self.ante_fuse_s = fuse_s + player_grace_s if ante_fuse_s is None else ante_fuse_s
        self.min_drop_s = min_drop_s

    def new_model(self, seed, names, teams=None):
        m = hl.LiveHill(seed, names, min_drop_s=self.min_drop_s, teams=teams)
        m.spawned = {w.who: 0.0 for w in m.wells}   # when each well's current piece appeared (round seconds)
        m.seen_k = {w.who: 0 for w in m.wells}
        return m

    def prompt(self, mode, count):
        return hl.prompt(mode, count, self.clock_s, self.ante_fuse_s, self.min_drop_s)

    def standin(self, index):
        return hl.HillPolicy(0, index)

    def _mark(self, model, now):
        for w in model.wells:
            if w.k != model.seen_k[w.who]:
                model.seen_k[w.who] = w.k
                model.spawned[w.who] = now

    def act(self, model, body, now):
        col, rot = body.get("col"), body.get("rot")
        if isinstance(col, bool) or isinstance(rot, bool) or not isinstance(col, int) or not isinstance(rot, int):
            return 422, {"error": "bad_drop"}
        result = model.drop("you", col, rot)
        self._mark(model, now)
        if "error" in result:
            return (410 if result.get("done") else 422), result
        return 200, result

    def advance(self, model, elapsed):
        self._mark(model, elapsed)
        changed = False
        for w in model.wells:
            if model.over or w.out:
                continue
            fuse = self.fuse_s + self.grace if w.who == "you" else self.ante_fuse_s
            if elapsed - model.spawned[w.who] >= fuse:
                col, rot = hl.spawn(model.piece(w.who))
                model.drop(w.who, col, rot)
                changed = True
        self._mark(model, elapsed)
        if not model.over and elapsed >= self.clock_s:
            model.finish("clock")
            changed = True
        return [("frame", self.frame(model))] if changed else []

    def on_call(self, model, who, op, arg, result, elapsed):
        self._mark(model, elapsed)
        ev = {"who": who, "op": op if isinstance(op, str) else "?"}
        got = re.fullmatch(r"(\d) (\d)(?: \S+)?", arg.strip()) if op == "drop" and isinstance(arg, str) else None
        if got:
            ev["arg"] = f"{got[1]} {got[2]}"
        if op == "drop" and "error" in result and not result.get("done"):
            ev["refused"] = True
        out = [("call", ev)]
        if op == "drop" and "error" not in result:
            out.append(("frame", self.frame(model)))
        return out

    def frame(self, model):
        f = model.frame()
        f["fuse_s"] = self.fuse_s
        f["ante_fuse_s"] = self.ante_fuse_s
        f["spawned"] = dict(model.spawned)
        return f

    def stats(self, model):
        return {"rows": {w.who: w.rows for w in model.wells}, "sent": {w.who: w.sent for w in model.wells},
                "pooled": {t["team"]: {"rows": t["rows"], "per_well": round(t["per_well"], 2), "wells": len(t["members"])}
                           for t in (model.team_public(t) for t in model.teams)},
                "drops": {w.who: w.drops for w in model.wells}, "clock_s": self.clock_s, "fuse_s": self.fuse_s,
                "ante_fuse_s": self.ante_fuse_s, "min_drop_s": self.min_drop_s,
                "refused": dict(getattr(model, "refused", {}))}

    def limit_s(self):
        return self.clock_s


GAMES = {"trail": TrailGame, "hill": HillGame}


# ---- a round ----

class Cell:
    def __init__(self, who: str, index: int):
        self.who, self.index = who, index
        self.key = ""
        self.token = None
        self.result: dict = {}
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.meter = None


class ArcadeRound:
    """One game round. Duck-types the parts of play.Round the manager reads (id, over, cancel, decide, cells)."""

    def __init__(self, game: Game, mode: str, count: int):
        self.game = game
        self.id = secrets.token_hex(6)
        self.token = secrets.token_hex(16)
        self.mode, self.count = mode, count
        self.seed = secrets.randbits(32)
        self.nonce = secrets.token_hex(16)
        self.seed_hash = hashlib.sha256(f"{self.seed}:{self.nonce}".encode()).hexdigest()
        names = ["ante"] if mode == "single" else [f"a{k + 1:02d}" for k in range(count)]
        self.cells = [Cell(who, k + 1) for k, who in enumerate(names)]
        self.teams = game.teams(mode, names)
        self.model = game.new_model(self.seed, ["you"] + names, self.teams)
        self.phase = "starting"
        self.created = time.monotonic()
        self.started: float | None = None
        self.ended: float | None = None
        self.lock = threading.Lock()
        self.readers: list[queue.Queue] = []
        self.no_reader_since: float | None = self.created
        self.cancel = threading.Event()
        self.over = threading.Event()
        self.go = threading.Event()        # set when the game's clock starts; acting ops wait for it
        self.go_at: float | None = None
        self.reason: str | None = None
        self.winner: str | None = None
        self.end_event: dict | None = None
        self.service: GameService | None = None
        self.budget = None
        self.signin: str | None = None
        self.signin_source: str | None = None
        self.calls: dict[str, int] = {c.who: 0 for c in self.cells}
        self.ops: dict[str, dict[str, int]] = {c.who: {} for c in self.cells}   # who: op: honoured calls
        self.acts = 0
        self.cleanup: dict = {}
        self.queue_pos = 0
        self.assist = False
        self.size = getattr(game, "size", None)

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.started else 0.0

    def emit(self, kind: str, data: dict):
        with self.lock:
            for q in self.readers:
                q.put((kind, data))

    def snapshot(self, model=None) -> dict:
        model = model or self.model
        go_in = max(0.0, self.go_at - time.monotonic()) if self.go_at and self.phase == "count" else None
        return {"round": self.id, "game": self.game.name, "phase": self.phase, "mode": self.mode, "count": self.count,
                "swarm_style": self.game.swarm_style,
                **({"go_in": round(go_in, 3)} if go_in is not None else {}),
                "you": "you", "rivals": [c.who for c in self.cells], "t": round(self.elapsed(), 3),
                "limit_s": self.game.limit_s(), "seed_hash": self.seed_hash, "frame": self.game.frame(model),
                **({"tick_s": self.game.tick_s} if hasattr(self.game, "tick_s") else {})}

    def subscribe(self):
        q: queue.Queue = queue.Queue()

        def add(model=None):
            snap = self.snapshot(model)
            with self.lock:
                if len(self.readers) >= MAX_READERS:
                    return None
                q.put(("state", snap))
                if self.end_event is not None:
                    q.put(("end", self.end_event))
                self.readers.append(q)
                self.no_reader_since = None
            return q

        if self.service is not None and self.service.running:
            return self.service.read(add)
        return add()

    def unsubscribe(self, q):
        with self.lock:
            if q in self.readers:
                self.readers.remove(q)
            if not self.readers:
                self.no_reader_since = time.monotonic()

    def on_call(self, who, op, arg, result, model):
        """GameService hook, lock held."""
        self.calls[who] = self.calls.get(who, 0) + 1
        if isinstance(op, str) and op in self.game.ops:
            per = self.ops.setdefault(who, {})
            per[op] = per.get(op, 0) + 1
        for kind, data in self.game.on_call(model, who, op, arg, result, self.elapsed()):
            self.emit(kind, data)
        if model.over:
            self.decide(model.reason or "over", model.winner)

    def decide(self, reason: str, winner: str | None = None) -> bool:
        with self.lock:
            if self.over.is_set():
                return False
            self.reason, self.winner = reason, winner
            self.over.set()
        self.go.set()   # calls waiting for the start return (the service is stopping)
        self.cancel.set()
        for c in self.cells:
            c.cancel.set()
        return True


# ---- the host ----

class Arcade:
    """Trail and hill rounds on a play.PlayManager (see the module doc for what is shared)."""

    def __init__(self, manager, games: dict | None = None):
        self.manager = manager
        self.games: dict[str, Game] = games or {name: cls() for name, cls in GAMES.items()}
        self.rounds: dict[str, ArcadeRound] = {}
        self.lock = threading.Lock()

    @property
    def config(self):
        return self.manager.config

    # -- status and admission --

    def status(self, game: str) -> dict:
        g = self.games[game]
        out = dict(self.manager.status())
        out.update(game=game, swarm_style=g.swarm_style, sizes=list(g.sizes),
                   counts={k: list(v) for k, v in g.counts().items()}, max_antes=max(g.sizes))
        return out

    def create(self, game: str, req: dict) -> tuple[int, dict]:
        mode, count = req.get("mode"), req.get("count")
        counts = self.games[game].counts()
        if mode not in counts:
            return 422, {"error": "bad_mode"}
        if not isinstance(count, int) or isinstance(count, bool) or count not in counts[mode]:
            return 422, {"error": "bad_count"}
        m = self.manager
        reason = m.unavailable()
        if reason:
            return 503, {"error": "unavailable", "reason": reason}
        with m.lock:
            if m.active is not None:
                return 409, {"error": "busy", "queue": len(m.waiting)}
            if not m._memory_ok(count):
                return 503, {"error": "unavailable", "reason": "memory"}
            r = ArcadeRound(self.games[game], mode, count)
            m.active = r
        with self.lock:
            self.rounds[r.id] = r
        threading.Thread(target=self._run, args=(r,), name=f"arcade-{r.id}", daemon=True).start()
        return 201, {"round": r.id, "token": r.token, "seed_hash": r.seed_hash, "queue": 0}

    def get(self, game: str, rid, token) -> tuple[ArcadeRound | None, int]:
        r = self.rounds.get(rid) if isinstance(rid, str) and ROUND_ID.match(rid) else None
        if r is None or r.game.name != game:
            return None, 404
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), r.token.encode()):
            return None, 403
        return r, 200

    def act(self, r: ArcadeRound, body: dict) -> tuple[int, dict]:
        service = r.service
        if r.phase in ("starting", "ready", "count"):
            return 409, {"error": "not_started"}
        if r.over.is_set() or service is None or not service.running:
            return 410, {"error": "over", "reason": r.reason}
        events: list = []

        def go(model):
            status, out = r.game.act(model, body, r.elapsed())
            if status == 200:
                r.acts += 1
                if r.game.name == "hill":
                    events.append(("frame", r.game.frame(model)))
            if model.over:
                r.decide(model.reason or "over", model.winner)
            return status, out

        status, out = service.read(go)
        for kind, data in events:
            r.emit(kind, data)
        return status, out

    def give_up(self, r: ArcadeRound):
        r.decide("gave_up", None)

    # -- the round's life --

    def _run(self, r: ArcadeRound):
        m = self.manager
        cfg = self.config
        root = group = None
        cleanup: dict = {}
        try:
            root = clean_root()
            r.prefix = f"g{r.id}-"
            workspace = root / "_workspace"
            workspace.mkdir()
            client = workspace / r.game.name
            client.write_text(client_source(r.game.name, r.game.ops, r.game.usage))
            client.chmod(0o755)
            sandbox = ProcessSandbox(root)
            r.service = GameService(r.model, r.game.name, on_call=r.on_call, gate=r.go, free_ops=r.game.free_ops)
            r.service.start()
            for c in r.cells:
                c.key = f"{r.prefix}{c.who}"
                c.token = r.service.register(c.who)
            if m.picker is not None:
                r.signin, reason, r.signin_source = m.picker.pick(evaluate=True)
                if reason:
                    r.decide(reason)
                    return
            r.model_route = m._make_model(r)
            if m.limits is not None:
                from . import limits as cgroup_limits
                group = cgroup_limits.CellGroup.create(f"arcade-{r.id}", m.limits)
                if group is None:
                    raise RuntimeError(f"limits not applied: {cgroup_limits.CellGroup.last_reason}")
            ws = types.SimpleNamespace(workspace=workspace)
            for c in r.cells:
                c.thread = threading.Thread(target=self._cell, args=(r, c, sandbox, ws, group), name=f"arcade-{c.key}",
                                            daemon=True)
                c.thread.start()
            # Ready: every Ante has looked once (or READY_WAIT_S passed); then a countdown everyone sees; then go.
            r.phase = "ready"
            r.emit("state", r.service.read(r.snapshot))
            deadline = time.monotonic() + READY_WAIT_S
            while not r.over.wait(0.1) and time.monotonic() < deadline:
                if all(r.calls.get(c.who, 0) > 0 or (c.thread is not None and not c.thread.is_alive()) for c in r.cells):
                    break
            if r.over.is_set():
                return
            r.phase, r.go_at = "count", time.monotonic() + COUNTDOWN_S
            r.emit("state", r.service.read(r.snapshot))
            if r.over.wait(COUNTDOWN_S):
                return
            r.phase = "running"
            r.started = time.monotonic()
            r.go.set()
            r.emit("state", r.service.read(r.snapshot))
            limit = r.game.limit_s() + 5
            while not r.over.wait(r.game.poll_s):
                elapsed = r.elapsed()
                events = r.service.read(lambda model: r.game.advance(model, elapsed))
                for kind, data in events:
                    r.emit(kind, data)
                if r.model.over:
                    r.decide(r.model.reason or "over", r.model.winner)
                elif elapsed > limit:
                    r.decide("timeout")
                elif r.budget is not None and r.budget.exhausted():
                    r.decide("tokens")
                elif r.no_reader_since is not None and time.monotonic() - r.no_reader_since > cfg.reader_grace_s:
                    r.decide("left")
        except Exception as error:  # noqa: BLE001 - a round that cannot run ends, says why, and is cleaned up
            r.decide("error")
            cleanup["error"] = type(error).__name__
        finally:
            r.cancel.set()
            try:
                cleanup.update(self._teardown(r, root, group))
            finally:
                r.phase = "over"
                r.ended = time.monotonic()
                try:
                    self._end(r, cleanup)
                finally:
                    if m.active is r:
                        m._next()

    def _cell(self, r: ArcadeRound, c: Cell, sandbox, ws, group):
        """One Ante, as PlayManager._cell runs a maze Ante: sandbox, `./<game>`, model route, until the round ends."""
        from . import play
        m, cfg = self.manager, self.config
        box = None
        try:
            if r.cancel.is_set():
                return
            box = sandbox.create(c.key, ws)
            (box["workdir"] / f".{r.game.name}.json").write_text(
                json.dumps({"socket": str(r.service.socket_path), "token": c.token}))
            harness = harnesses.get("ante", {"telemetry": "off"})
            harness.frozen = m.harness.frozen
            harness.wire = cfg.wire
            env = harness.environment(cfg.ante, box["home"])
            env["SWARM_ATTEMPT"] = c.key
            env["SWARM_RUNNER"] = runner_tag()
            argv = harness.argv(cfg.ante, r.game.prompt(r.mode, r.count), box["workdir"], cfg.model, cfg.effort)
            if m.picker is not None:
                env.update(m.subscription_home(box["home"], r.signin))
                argv[argv.index("--provider") + 1] = getattr(cfg, "ante_provider", None) or play.SUBSCRIPTION_PROVIDER
                c.meter = play.UsageMeter(r.budget.add if r.budget is not None else None)
            else:
                script = r.game.standin(c.index) if cfg.standin or cfg.model_factory else []
                url = r.model_route.register(c.key, script, box["workdir"], cfg.model, cfg.effort,
                                             cfg.max_output_tokens, cfg.max_requests)
                env.update(harness.prepare(box["home"], url, cfg.model, cfg.effort, cfg.max_output_tokens))
                catalog_path = Path(env["ANTE_HOME"]) / "catalog.json"
                catalog = json.loads(catalog_path.read_text())
                catalog["providers"]["swarm"]["wire_style"] = cfg.wire_style or play.WIRE_STYLES[cfg.wire]
                catalog_path.write_text(json.dumps(catalog))
            if group is not None:
                argv = group.wrap(argv)
            meter = c.meter or play.UsageMeter()

            def output(chunk):
                meter.feed(chunk)
                if m.picker is not None and meter.requests >= cfg.max_requests:
                    c.cancel.set()

            if r.cancel.is_set():
                c.cancel.set()
            result = execute(argv, box["workdir"], env, r.game.limit_s() + 60, box["base"] / "agent.log",
                             on_start=lambda pid: r.service.bind(c.who, pid), cancel=c.cancel, on_output=output)
            c.result = {k: result.get(k) for k in ("exit_code", "termination", "wall_s", "cpu_s")}
            rss = result.get("max_process_rss_kb")
            c.result["rss_mb"] = round(rss / 1024, 1) if isinstance(rss, int) else None
            if m.picker is not None:
                c.result.update({"requests": meter.requests, "tokens": meter.tokens, "error_kind": meter.error_kind})
                if hasattr(m, "_signin_verdict"):
                    m._signin_verdict(r, c, meter)
        except Exception as error:  # noqa: BLE001 - one Ante that cannot start is recorded; the round goes on
            c.result = {"error": type(error).__name__}
        finally:
            if box is not None:
                link = Path(box["home"]).joinpath(".ante", *play.SIGNIN_STORE)
                try:
                    if link.is_symlink():
                        link.unlink()   # first: removing the sandbox must never walk into the shared store
                except OSError:
                    c.result["store_link_left"] = True
            try:
                if getattr(r, "model_route", None) is None:
                    raise LookupError
                rows, _ = r.model_route.release(c.key, settle_s=0) if c.key else ([], False)
                c.result["requests"] = len(rows)
                for k in ("input_tokens", "output_tokens"):
                    if any(k in row for row in rows):
                        c.result[k] = sum(row.get(k, 0) for row in rows)
            except Exception:  # noqa: BLE001
                pass
            if box is not None and not c.result.get("store_link_left"):
                gone = sandbox.destroy(box)
                if not gone.get("removed"):
                    c.result["sandbox_removed"] = False

    def _teardown(self, r: ArcadeRound, root, group) -> dict:
        out: dict = {}
        for c in r.cells:
            if c.thread is not None:
                c.thread.join(timeout=20)
        out["threads_alive"] = sum(1 for c in r.cells if c.thread is not None and c.thread.is_alive())
        prefix = getattr(r, "prefix", None)
        if prefix:
            try:
                reaped = reap_escaped(prefix, prefix=True)
                out["escaped"] = reaped.get("escaped_processes", 0)
                out["survivors"] = len(tagged_processes(prefix, prefix=True))
            except Exception as error:  # noqa: BLE001
                out["sweep_error"] = type(error).__name__
        if r.service is not None:
            try:
                r.service.stop()
                out["forbidden"], out["stolen"] = r.service.forbidden, r.service.stolen
            except Exception as error:  # noqa: BLE001
                out["service_error"] = type(error).__name__
        route = getattr(r, "model_route", None)
        if route is not None:
            try:
                route.close()
            except Exception:  # noqa: BLE001
                pass
        if group is not None:
            try:
                out["limits"] = group.stats()
                group.close()
            except Exception:  # noqa: BLE001
                pass
        if root is not None:
            gone = remove_tree(root)
            out["sandboxes_removed"] = bool(gone.get("removed"))
        out["killed"] = out.get("survivors", 0) == 0 and out["threads_alive"] == 0
        try:
            self.manager.daily.save()
        except Exception:  # noqa: BLE001
            pass
        return out

    def _end(self, r: ArcadeRound, cleanup: dict):
        model = r.model
        # The game's own end (its clock, or trails/wells out) names the winner. A round given up, left or cut short
        # has none; `leader` still says who was ahead when it stopped.
        natural = model.over and r.reason == model.reason
        if not model.over:
            model.finish(r.reason or "error")
        r.winner = model.winner if natural else None
        tokens = r.budget.used if r.budget is not None else None
        if tokens is None:
            got = [c.result.get(k) for c in r.cells for k in ("input_tokens", "output_tokens")]
            tokens = sum(t for t in got if isinstance(t, int)) if any(isinstance(t, int) for t in got) else None
        stats = {"seconds": round((r.ended or time.monotonic()) - r.started, 3) if r.started else 0.0,
                 "calls": dict(r.calls), "ops": {k: dict(v) for k, v in r.ops.items()}, "acts": r.acts,
                 "tokens": tokens, "antes": len(r.cells), "swarm_style": r.game.swarm_style, **r.game.stats(model)}
        revealed = r.game.reveal(model) if hasattr(r.game, "reveal") else {}
        event = {"game": r.game.name, "winner": r.winner, "leader": model.winner, "reason": r.reason, "stats": stats,
                 **revealed,
                 "frame": r.game.frame(model), "seed": r.seed, "nonce": r.nonce, "seed_hash": r.seed_hash}
        r.cleanup = cleanup
        with r.lock:
            r.end_event = event
        r.emit("end", event)
        cfg = self.config
        row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "game": r.game.name,
               "round": r.id, "seed_hash": r.seed_hash, "mode": r.mode, "count": r.count, "wall_s": stats["seconds"],
               "winner": r.winner, "reason": r.reason, "calls": dict(r.calls), "ops": stats["ops"], "acts": r.acts,
               "tokens": tokens, "swarm_style": r.game.swarm_style, "result": r.game.stats(model),
               **revealed,
               "provider": cfg.provider, "model": cfg.model, "signin": r.signin,
               "antes": [{"who": c.who, **c.result} for c in r.cells], "cleanup": cleanup}
        if cfg.data is not None:
            try:
                with (cfg.data / "rounds.jsonl").open("a") as f:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
            except OSError:
                pass
        print(f"play {r.game.name} round {r.id} · {r.mode}x{r.count} · {r.reason} · winner {r.winner} · "
              f"{stats['seconds']} s · tokens {tokens} · {cfg.model} via {cfg.provider}", flush=True)
        with self.lock:
            cutoff = time.monotonic() - 600
            for rid in [k for k, v in self.rounds.items() if v.ended is not None and v.ended < cutoff]:
                del self.rounds[rid]

    # -- HTTP --

    def route(self, handler, method: str) -> bool:
        """Answer /api/play/trail/... and /api/play/hill/... on a solo Handler. False when the path is not ours."""
        path, _, query = handler.path.partition("?")
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[:2] != ["api", "play"] or parts[2] not in self.games:
            return False
        game, rest = parts[2], parts[3:]
        try:
            if method == "GET" and rest == ["status"]:
                handler._send(200, self.status(game))
            elif method == "POST" and rest == ["rounds"]:
                self._create(handler, game)
            elif len(rest) >= 2 and rest[0] == "rounds":
                self._round(handler, game, method, rest[1], rest[2:], query)
            else:
                handler._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    @staticmethod
    def _body(handler):
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            handler.close_connection = True
            handler._send(413 if length > MAX_BODY else 400, {"error": "bad_length"})
            return None
        try:
            body = json.loads(handler.rfile.read(length))
        except ValueError:
            handler._send(400, {"error": "bad_json"})
            return None
        if not isinstance(body, dict):
            handler._send(400, {"error": "bad_request"})
            return None
        return body

    def _create(self, handler, game):
        body = self._body(handler)
        if body is None:
            return
        m = self.manager
        if not m.config.passcode_sha256:
            return handler._send(503, {"error": "unavailable", "reason": "no_passcode_configured"})
        verdict = m.check_passcode(m._client(handler), body.pop("passcode", None))
        if verdict == 429:
            return handler._send(429, {"error": "too_many_tries"})
        if verdict == 403:
            return handler._send(403, {"error": "wrong_passcode"})
        rival = body.get("rival")
        if not isinstance(rival, dict):
            return handler._send(422, {"error": "bad_rival"})
        status, out = self.create(game, {"mode": rival.get("mode"), "count": rival.get("count")})
        handler._send(status, out)

    @staticmethod
    def _token(handler, query):
        token = handler.headers.get("X-Round-Token")
        if token:
            return token
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "t":
                return v
        return None

    def _round(self, handler, game, method, rid, rest, query):
        r, status = self.get(game, rid, self._token(handler, query))
        if r is None:
            return handler._send(status, {"error": "not found" if status == 404 else "forbidden"})
        if method == "DELETE" and not rest:
            self.give_up(r)
            return handler._send(200, {"ok": True, "reason": r.reason})
        if method == "POST" and rest == ["act"]:
            body = self._body(handler)
            if body is None:
                return
            status, out = self.act(r, body)
            return handler._send(status, out)
        if method == "GET" and rest == ["events"]:
            return self._events(handler, r)
        handler._send(404, {"error": "not found"})

    @staticmethod
    def _events(handler, r: ArcadeRound):
        q = r.subscribe()
        if q is None:
            return handler._send(429, {"error": "too_many_readers"})
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Accel-Buffering", "no")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            handler.wfile.write(b"retry: 2000\n\n")
            handler.wfile.flush()
            while True:
                try:
                    kind, data = q.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                    continue
                handler.wfile.write(f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())
                handler.wfile.flush()
                if kind == "end":
                    break
        except OSError:
            pass
        finally:
            r.unsubscribe(q)


class Router:
    """The solo server's /api/play/ router: trail and hill first, then the live maze. Used by solo.serve_solo until
    play.PlayAPI hosts the arcade itself (/tmp/games-patch/play-arcade.patch); then PlayAPI alone is enough."""

    def __init__(self, play_api, arcade: Arcade):
        self.play, self.arcade = play_api, arcade

    def route(self, handler, method: str) -> bool:
        return self.arcade.route(handler, method) or self.play.route(handler, method)
