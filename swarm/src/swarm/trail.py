"""Trail: the AX-1's snake-genre cartridge as a rules model, its scripted policy, and the `./trail` client.

Every player steers a trail on one shared grid. Each tick every trail still running moves one cell; a trail that
eats a token grows by one; a trail that leaves the grid or hits any trail (its own included), or meets another head
on the same cell, is out and leaves the board. The round ends when the clock runs out or at most one trail is left
(when it started with more than one). The winner has the most tokens; ties go to the one still running, then the
one that lasted longer; a tie after that is a draw.

Teams (swarm style "shared"): every player belongs to a team; by default each player is a team of one, which is the
plain every-trail-for-itself game. A swarm's Antes are ONE team: the team's score is the sum of its members' tokens,
standings compare teams (tokens first, then how long the team lasted: a member that crashes keeps its tokens for the
team), and the round ends when at most one team still has a trail running (or none), or at the clock. Teammates see
each other's heads, headings and queued plans in `look`, the whole team in `team`, and a notes board private to the
team (`note`, at most NOTES notes of NOTE_LEN characters).

The browser runs the same rules in front-solo/trail-core.js and must agree tick for tick (tests/test_trail.py runs
both on seeded games). Change one, change the other.

Steering: the human steers absolutely (`steer`). An agent steers relative to its heading, either one turn at the
next tick (`turn`), or a plan of up to MAX_PLAN relative moves (`plan`), one taken per tick; when the plan runs out
the trail drives straight. A `turn` overrides the plan and clears it; a new `plan` replaces the old one.

Seats: trails start in lanes, seat k of n at row rows*(k+1)//(n+1), even seats heading east from the left fifth,
odd seats heading west from the right fifth, so no two trails start on a collision course and every trail has most
of the grid's width ahead of it.

Cell layout: index = y * cols + x, y grows downward. A heading is N, E, S or W.
"""
from __future__ import annotations

import json
import re

from .maze import MASK32, mulberry32

SIZES = {"s": (20, 13), "m": (28, 18), "l": (36, 23)}
HEADINGS = ("N", "E", "S", "W")
DELTA = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}
LEFT = {"N": "W", "W": "S", "S": "E", "E": "N"}
RIGHT = {v: k for k, v in LEFT.items()}
OPPOSITE = {"N": "S", "E": "W", "S": "N", "W": "E"}
MAX_PLAYERS = 5
START_LEN = 3
MAX_PLAN = 12      # relative moves an agent can queue with `./trail plan`
DANGER_CAP = 40    # how far `look` follows the planned path before it calls it safe
WARN_SAFE = 8      # below this many safe ticks, `look` adds a plain-words warning
FLOOD_CAP = 48
DEFAULT_CLOCK = 300   # ticks
NOTES = 20         # a team's notes board keeps the newest NOTES notes
NOTE_LEN = 120     # characters per note, after sanitising
RECENT = 5         # tokens per member listed by `./trail team`
LOOK_NOTES = 6     # the newest notes `look` lists (`team` lists them all)


def params(size: str = "m", seed: int = 1, players: int = 2, clock: int = DEFAULT_CLOCK, tokens: int | None = None) -> dict:
    if size not in SIZES:
        raise ValueError(f"trail size must be one of {', '.join(SIZES)}")
    if not isinstance(players, int) or not 1 <= players <= MAX_PLAYERS:
        raise ValueError(f"trail players must be 1..{MAX_PLAYERS}")
    if not isinstance(seed, int) or not 0 <= seed <= MASK32:
        raise ValueError("trail seed must be a 32-bit integer")
    if not isinstance(clock, int) or not 1 <= clock <= 100_000:
        raise ValueError("trail clock must be 1..100000 ticks")
    cols, rows = SIZES[size]
    return {"size": size, "cols": cols, "rows": rows, "seed": seed, "players": players, "clock": clock,
            "tokens": tokens if tokens is not None else players + 1}


def seat(cols: int, rows: int, k: int, n: int) -> tuple[int, int, str]:
    """Seat k of n: (x, y, heading) of the head. Lanes across the grid, alternating east and west."""
    y = rows * (k + 1) // (n + 1)
    margin = cols * 2 // 10
    return (margin, y, "E") if k % 2 == 0 else (cols - 1 - margin, y, "W")


class Player:
    def __init__(self, who: str, seat: int, body: list[int], heading: str, team: str | None = None):
        self.who, self.seat, self.body, self.heading = who, seat, body, heading
        self.team = team or who
        self.pending: str | None = None
        self.plan: list[str] = []      # queued relative moves (L, R, S), one taken per tick
        self.alive = True
        self.tokens = 0
        self.died: int | None = None   # the tick it went out
        self.cause: str | None = None  # wall | trail | head
        self.hit: str | None = None    # whose trail or head it ran into (itself for its own trail); not in the JS twin
        self.eaten: list[tuple[int, int]] = []   # (tick, cell) of every token taken

    def public(self) -> dict:
        return {"who": self.who, "seat": self.seat, "team": self.team, "body": list(self.body), "heading": self.heading,
                "alive": self.alive, "tokens": self.tokens, "len": len(self.body), "died": self.died, "cause": self.cause}


def sanitise(text) -> str:
    """A note as the board keeps it: printable ASCII, runs of space collapsed, at most NOTE_LEN characters."""
    return _clean(text)[:NOTE_LEN].strip()


def _clean(text) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r" +", " ", re.sub(r"[^\x20-\x7e]", " ", text)).strip()


class TrailModel:
    def __init__(self, p: dict, names: list[str], teams: list[str] | None = None):
        if len(names) != p["players"]:
            raise ValueError("one name per player")
        if teams is not None and len(teams) != len(names):
            raise ValueError("one team per player")
        self.p = p
        self.cols, self.rows, self.clock = p["cols"], p["rows"], p["clock"]
        self.rand = mulberry32(p["seed"])
        self.tick = 0
        self.players: list[Player] = []
        self.by: dict[str, Player] = {}
        for k, who in enumerate(names):
            x, y, h = seat(self.cols, self.rows, k, len(names))
            dx, dy = DELTA[h]
            body = [(y - dy * i) * self.cols + (x - dx * i) for i in range(START_LEN)]
            pl = Player(who, k, body, h, teams[k] if teams else None)
            self.players.append(pl)
            self.by[who] = pl
        self.teams: list[str] = []          # team names in seat order of their first member
        for pl in self.players:
            if pl.team not in self.teams:
                self.teams.append(pl.team)
        self.notes: dict[str, list[dict]] = {t: [] for t in self.teams}
        self.tokens: list[int] = []
        self.over = False
        self.reason: str | None = None
        self.winner: str | None = None
        self.last: dict = {"eaten": [], "out": []}
        while len(self.tokens) < p["tokens"] and self._spawn():
            pass

    # -- helpers --

    def xy(self, i: int) -> tuple[int, int]:
        return i % self.cols, i // self.cols

    def members(self, team: str) -> list[Player]:
        return [pl for pl in self.players if pl.team == team]

    def mates(self, who: str) -> list[Player]:
        me = self.by[who]
        return [pl for pl in self.players if pl.team == me.team and pl is not me]

    def team_tokens(self, team: str) -> int:
        return sum(pl.tokens for pl in self.members(team))

    def _occupied(self) -> set[int]:
        out: set[int] = set()
        for pl in self.players:
            if pl.alive:
                out.update(pl.body)
        return out

    def _spawn(self) -> bool:
        taken = self._occupied() | set(self.tokens)
        free = [i for i in range(self.cols * self.rows) if i not in taken]
        if not free:
            return False
        self.tokens.append(free[int(self.rand() * len(free))])
        return True

    # -- input --

    def turn(self, who: str, rel) -> dict:
        """An agent's turn, relative to its heading: L, R or S (straight, which cancels an earlier turn this tick).
        It is applied at the next tick, once; after that the trail keeps its heading until the next turn."""
        pl = self.by[who]
        if self.over or not pl.alive:
            return {"error": "out" if not pl.alive else "over", "done": True}
        rel = rel.strip().upper() if isinstance(rel, str) else rel
        if rel not in ("L", "R", "S"):
            return {"error": "usage: ./trail turn L|R|S"}
        pl.pending = LEFT[pl.heading] if rel == "L" else RIGHT[pl.heading] if rel == "R" else None
        pl.plan = []
        return {"ok": True, "tick": self.tick, "next_heading": pl.pending or pl.heading}

    def set_plan(self, who: str, moves) -> dict:
        """An agent's plan: up to MAX_PLAN relative moves, L, R or S, one taken per tick from the next tick on,
        each relative to the heading at that tick. It replaces any earlier plan or turn not yet taken."""
        pl = self.by[who]
        if self.over or not pl.alive:
            return {"error": "out" if not pl.alive else "over", "done": True}
        moves = moves.strip().upper() if isinstance(moves, str) else ""
        if not 1 <= len(moves) <= MAX_PLAN or any(ch not in "LRS" for ch in moves):
            return {"error": f"usage: ./trail plan <moves>, 1 to {MAX_PLAN} of L, R, S (e.g. SSLSSR)"}
        pl.pending = None
        pl.plan = list(moves)
        return {"ok": True, "tick": self.tick, "plan": "".join(pl.plan)}

    def steer(self, who: str, d) -> bool:
        """The human's steer: an absolute heading. Reversing onto yourself is ignored, as on a real stick."""
        pl = self.by[who]
        if self.over or not pl.alive or d not in DELTA or d == OPPOSITE[pl.heading]:
            return False
        pl.pending = None if d == pl.heading else d
        pl.plan = []
        return True

    # -- one tick --

    def step(self) -> dict:
        if self.over:
            return self.last
        live = [pl for pl in self.players if pl.alive]
        heads: dict[str, int | None] = {}
        for pl in live:
            if pl.pending:
                pl.heading = pl.pending
                pl.pending = None
            elif pl.plan:
                rel = pl.plan.pop(0)
                pl.heading = LEFT[pl.heading] if rel == "L" else RIGHT[pl.heading] if rel == "R" else pl.heading
            x, y = self.xy(pl.body[0])
            dx, dy = DELTA[pl.heading]
            nx, ny = x + dx, y + dy
            heads[pl.who] = ny * self.cols + nx if 0 <= nx < self.cols and 0 <= ny < self.rows else None
        tokens = set(self.tokens)
        grow = {pl.who: heads[pl.who] is not None and heads[pl.who] in tokens for pl in live}
        for pl in live:
            if not grow[pl.who]:
                pl.body.pop()
        occupied: dict[int, int] = {}
        owner: dict[int, str] = {}
        for pl in live:
            for c in pl.body:
                occupied[c] = occupied.get(c, 0) + 1
                owner[c] = pl.who
        count: dict[int, int] = {}
        for c in heads.values():
            if c is not None:
                count[c] = count.get(c, 0) + 1
        eaten, out = [], []
        for pl in live:
            c = heads[pl.who]
            cause = "wall" if c is None else "trail" if c in occupied else "head" if count[c] > 1 else None
            if cause:
                pl.alive, pl.died, pl.cause = False, self.tick + 1, cause
                pl.hit = owner[c] if cause == "trail" else ",".join(
                    q.who for q in live if q is not pl and heads[q.who] == c) if cause == "head" else None
                out.append(pl.who)
        for pl in live:
            if not pl.alive:
                pl.body = []
                continue
            c = heads[pl.who]
            pl.body.insert(0, c)
            if grow[pl.who] and c in self.tokens:
                self.tokens.remove(c)
                pl.tokens += 1
                pl.eaten.append((self.tick + 1, c))
                eaten.append([pl.who, c])
        while len(self.tokens) < self.p["tokens"] and self._spawn():
            pass
        self.tick += 1
        running = len({pl.team for pl in self.players if pl.alive})
        if running == 0 or (len(self.teams) > 1 and running <= 1):
            self.finish("out")
        elif self.tick >= self.clock:
            self.finish("clock")
        self.last = {"tick": self.tick, "eaten": eaten, "out": out}
        return self.last

    def finish(self, reason: str):
        if self.over:
            return
        self.over, self.reason = True, reason
        self.winner = self.leader()

    def standings(self) -> list[Player]:
        return sorted(self.players, key=self._rank, reverse=True)

    def _rank(self, pl: Player):
        return (pl.tokens, pl.died if pl.died is not None else self.clock + 1)

    def team_rank(self, team: str):
        """(team tokens, how long the team lasted): tokens first, so a member that crashes still counts its tokens
        for the team; then the tick its last trail went out (a team still running ranks above every team that is out).
        For a team of one this is the old rank: tokens, then still running, then out later."""
        members = self.members(team)
        lasted = self.clock + 1 if any(pl.alive for pl in members) else max(pl.died or 0 for pl in members)
        return (sum(pl.tokens for pl in members), lasted)

    def team_standings(self) -> list[str]:
        return sorted(self.teams, key=self.team_rank, reverse=True)

    def leader(self) -> str | None:
        """The leading team's name (a player's own name for a team of one), or None for a tie at the top."""
        ranked = self.team_standings()
        if len(ranked) > 1 and self.team_rank(ranked[0]) == self.team_rank(ranked[1]):
            return None
        return ranked[0]

    # -- views --

    def frame(self) -> dict:
        """Everything on the board: there is no fog in trail. Sent to every viewer each tick."""
        return {"tick": self.tick, "clock": self.clock, "cols": self.cols, "rows": self.rows, "tokens": list(self.tokens),
                "players": [pl.public() for pl in self.players], "teams": [self.team_public(t) for t in self.teams],
                "over": self.over, "reason": self.reason, "winner": self.winner}

    def team_public(self, team: str) -> dict:
        """A team's score as everyone sees it (no plans, no notes: those are the team's own)."""
        members = self.members(team)
        return {"team": team, "members": [pl.who for pl in members], "tokens": sum(pl.tokens for pl in members),
                "alive": sum(1 for pl in members if pl.alive)}

    def look(self, who: str) -> dict:
        """One agent's `./trail look`: the whole board drawn as text from its point of view, plus its rivals."""
        me = self.by[who]
        grid = ["."] * (self.cols * self.rows)
        for c in self.tokens:
            grid[c] = "*"
        for pl in self.players:
            if not pl.alive:
                continue
            mark = "@o" if pl is me else "Mm" if pl.team == me.team else "Hx"
            for k, c in enumerate(pl.body):
                grid[c] = mark[0] if k == 0 else mark[1]
        board = ["".join(grid[y * self.cols:(y + 1) * self.cols]) for y in range(self.rows)]
        mates = self.mates(who)
        out = {"who": who, "tick": self.tick, "clock": self.clock, "alive": me.alive, "heading": me.heading, "len": len(me.body),
               "tokens": me.tokens, "board": board, "food": [list(self.xy(c)) for c in self.tokens],
               "rivals": [{"who": pl.who, "alive": pl.alive, "tokens": pl.tokens, "len": len(pl.body),
                           "heading": pl.heading, **({"at": list(self.xy(pl.body[0]))} if pl.alive else {})}
                          for pl in self.players if pl.team != me.team],
               "done": self.over or not me.alive}
        if mates:
            # compact: each teammate's head, heading and the moves it still has queued (`./trail team` has the rest)
            out["team"] = [{"who": pl.who, "alive": pl.alive, "tokens": pl.tokens,
                            **({"at": list(self.xy(pl.body[0])), "heading": pl.heading, "plan": self._queued(pl)}
                               if pl.alive else {})} for pl in mates]
            out["score"] = {"team": self.team_tokens(me.team),
                            **{t: self.team_tokens(t) for t in self.teams if t != me.team}}
            lines = self.note_lines(me.team)
            out["notes"] = lines[-LOOK_NOTES:]
            if len(lines) > LOOK_NOTES:
                out["notes_total"] = len(lines)
        if me.alive:
            out["at"] = list(self.xy(me.body[0]))
            for rel, h in (("ahead", me.heading), ("left", LEFT[me.heading]), ("right", RIGHT[me.heading])):
                out[rel] = self._clear(me.body[0], h)
            out.update(self.danger(who))
        return out

    @staticmethod
    def _queued(pl: Player) -> str:
        """The moves a trail has queued, as relative moves: a pending turn first (as L or R), then the plan."""
        if pl.pending:
            return ("L" if pl.pending == LEFT[pl.heading] else "R") + "".join(pl.plan)
        return "".join(pl.plan)

    def path(self, pl: Player, n: int | None = None) -> list[int]:
        """The cells a trail's queued moves take it through, one per tick (then straight on, up to n cells in all;
        n defaults to the queued moves, at least one), stopping at the grid's edge."""
        moves = self._queued(pl)
        n = max(1, len(moves)) if n is None else n
        x, y = self.xy(pl.body[0])
        h = pl.heading
        out = []
        for k in range(n):
            if k < len(moves):
                h = LEFT[h] if moves[k] == "L" else RIGHT[h] if moves[k] == "R" else h
            dx, dy = DELTA[h]
            x, y = x + dx, y + dy
            if not (0 <= x < self.cols and 0 <= y < self.rows):
                break
            out.append(y * self.cols + x)
        return out

    def note_lines(self, team: str) -> list[str]:
        return [f"{n['who']} t{n['tick']}: {n['text']}" for n in self.notes.get(team, [])]

    def note(self, who: str, text) -> dict:
        """Pin a note to the team's board: private to the team, the newest NOTES kept."""
        me = self.by[who]
        clean = sanitise(text)
        if not clean:
            return {"error": "usage: ./trail note <text> (printable text, up to %d characters)" % NOTE_LEN}
        board = self.notes.setdefault(me.team, [])
        board.append({"who": who, "tick": self.tick, "text": clean})
        del board[:-NOTES]
        out = {"ok": True, "tick": self.tick, "notes": self.note_lines(me.team)[-LOOK_NOTES:]}
        if len(_clean(text)) > NOTE_LEN:   # the note was longer than the board keeps
            out["cut"] = True
        return out

    def team_view(self, who: str) -> dict:
        """`./trail team`: the whole team, every member's queued plan and the path it draws, recent tokens, notes."""
        me = self.by[who]
        members = []
        for pl in self.members(me.team):
            row = {"who": pl.who, "you": pl is me, "alive": pl.alive, "tokens": pl.tokens, "len": len(pl.body),
                   "recent": [[t, list(self.xy(c))] for t, c in pl.eaten[-RECENT:]]}
            if pl.alive:
                row.update(at=list(self.xy(pl.body[0])), heading=pl.heading, plan=self._queued(pl),
                           path=[list(self.xy(c)) for c in self.path(pl)])
            else:
                row.update(out_at=pl.died, cause=pl.cause)
            members.append(row)
        return {"tick": self.tick, "clock": self.clock, "team": me.team, "tokens": self.team_tokens(me.team),
                "members": members, "rivals": {t: self.team_tokens(t) for t in self.teams if t != me.team},
                "food": [list(self.xy(c)) for c in self.tokens], "notes": self.note_lines(me.team),
                "done": self.over or not me.alive}

    def danger(self, who: str) -> dict:
        """Where the trail's queued path leads: the plan left (a pending turn first), then straight on. `safe` is how
        many ticks it runs before it would leave the grid or enter a cell a trail holds now (every trail counted as
        it stands), up to DANGER_CAP; `hits` is wall, trail or null when the path is clear that far."""
        pl = self.by[who]
        moves = ([None] if pl.pending else []) + list(pl.plan)
        occupied = self._occupied()
        planned = {}   # a cell on a teammate's queued path -> that teammate
        for mate in self.mates(who):
            if mate.alive:
                for c in self.path(mate):
                    planned.setdefault(c, mate.who)
        crosses = None
        x, y = self.xy(pl.body[0])
        h = pl.heading
        for n in range(DANGER_CAP):
            if n < len(moves):
                m = moves[n]
                h = pl.pending if m is None else LEFT[h] if m == "L" else RIGHT[h] if m == "R" else h
            dx, dy = DELTA[h]
            x, y = x + dx, y + dy
            hits = "wall" if not (0 <= x < self.cols and 0 <= y < self.rows) else "trail" if y * self.cols + x in occupied else None
            if hits:
                out = {"plan": "".join(pl.plan), "safe": n, "hits": hits}
                if n < WARN_SAFE:
                    out["warning"] = f"your path hits a {hits} in {n + 1} ticks: plan a new path now"
                return self._crossing(out, crosses)
            if crosses is None and y * self.cols + x in planned and n < MAX_PLAN:
                crosses = {"who": planned[y * self.cols + x], "in": n + 1}
            occupied.add(y * self.cols + x)   # the path's own trail
        return self._crossing({"plan": "".join(pl.plan), "safe": DANGER_CAP, "hits": None}, crosses)

    @staticmethod
    def _crossing(out: dict, crosses: dict | None) -> dict:
        if crosses:
            out["crosses"] = crosses
            out.setdefault("warning", f"your path crosses {crosses['who']}'s planned path in {crosses['in']} ticks: "
                                      "plan around it")
        return out

    def _clear(self, start: int, h: str) -> int:
        """Free cells straight on from start, heading h, before a wall or a trail."""
        occupied = self._occupied()
        x, y = self.xy(start)
        dx, dy = DELTA[h]
        n = 0
        while True:
            x, y = x + dx, y + dy
            if not (0 <= x < self.cols and 0 <= y < self.rows) or y * self.cols + x in occupied:
                return n
            n += 1

    def call(self, who: str, op: str, arg=None) -> dict:
        """An agent's `./trail` call."""
        if who not in self.by:
            return {"error": "unknown agent"}
        if op == "look":
            return self.look(who)
        if op == "team":
            return self.team_view(who)
        if op == "note":
            return self.note(who, arg)
        if op == "turn":
            out = self.turn(who, arg)
        elif op == "plan":
            out = self.set_plan(who, arg)
        else:
            return {"error": USAGE}
        if out.get("ok"):
            out = {**self.look(who), **out}   # the answer is also a look: no separate look is needed to re-plan
        return out


# ---- the scripted policy (the twin of trail-core.js `choose`) ----

def view_of(model: TrailModel, who: str) -> dict:
    me = model.by[who]
    return {"cols": model.cols, "rows": model.rows, "head": me.body[0], "heading": me.heading, "len": len(me.body),
            "blocked": model._occupied(), "tokens": list(model.tokens),
            "heads": [(pl.body[0], pl.heading) for pl in model.players if pl.alive and pl is not me]}


def view_from_look(look: dict) -> dict:
    """The same view, rebuilt from an agent's `./trail look` answer (what a stand-in Ante sees)."""
    rows = look["board"]
    cols = len(rows[0])
    blocked = {y * cols + x for y, row in enumerate(rows) for x, ch in enumerate(row) if ch in "@oHxMm"}
    x, y = look["at"]
    others = list(look["rivals"]) + list(look.get("team", []))
    others.sort(key=lambda r: r["who"])
    return {"cols": cols, "rows": len(rows), "head": y * cols + x, "heading": look["heading"], "len": look["len"],
            "blocked": blocked, "tokens": [fy * cols + fx for fx, fy in look["food"]],
            "heads": [(r["at"][1] * cols + r["at"][0], r["heading"]) for r in others if r.get("alive") and "at" in r]}


def _flood(v: dict, start: int, cap: int) -> int:
    cols, rows, blocked = v["cols"], v["rows"], v["blocked"]
    seen = {start}
    queue = [start]
    k = 0
    while k < len(queue) and len(seen) < cap:
        c = queue[k]
        k += 1
        x, y = c % cols, c // cols
        for h in HEADINGS:
            dx, dy = DELTA[h]
            nx, ny = x + dx, y + dy
            if 0 <= nx < cols and 0 <= ny < rows:
                n = ny * cols + nx
                if n not in blocked and n not in seen:
                    seen.add(n)
                    queue.append(n)
                    if len(seen) >= cap:
                        break
    return min(len(seen), cap)


def choose(v: dict) -> str:
    """S, L or R. Safe first; then room to live in (a flood fill as long as the trail plus four); then away from
    cells another head could take next tick; then nearer a token; then more room; then S before L before R.
    A teammate's next cell (`avoid`, when the view has one) counts as blocked: teammates never meet head on."""
    cols, rows = v["cols"], v["rows"]
    hx, hy = v["head"] % cols, v["head"] // cols
    threat: set[int] = set()
    for c, h in v["heads"]:
        x, y = c % cols, c // cols
        for h2 in (h, LEFT[h], RIGHT[h]):
            dx, dy = DELTA[h2]
            nx, ny = x + dx, y + dy
            if 0 <= nx < cols and 0 <= ny < rows:
                threat.add(ny * cols + nx)
    need = min(FLOOD_CAP, v["len"] + 4)
    best, best_key = "S", None
    for k, rel in enumerate(("S", "L", "R")):
        h = v["heading"] if rel == "S" else LEFT[v["heading"]] if rel == "L" else RIGHT[v["heading"]]
        dx, dy = DELTA[h]
        nx, ny = hx + dx, hy + dy
        if not (0 <= nx < cols and 0 <= ny < rows):
            continue
        c = ny * cols + nx
        if c in v["blocked"] or c in v.get("avoid", ()):
            continue
        space = _flood(v, c, FLOOD_CAP)
        dist = min((abs(t % cols - nx) + abs(t // cols - ny) for t in v["tokens"]), default=0)
        key = (1 if space >= need else 0, 0 if c in threat else 1, -dist, space, -k)
        if best_key is None or key > best_key:
            best, best_key = rel, key
    return best


def _step_to(cols: int, rows: int, head: int, heading: str, rel: str) -> int | None:
    h = heading if rel == "S" else LEFT[heading] if rel == "L" else RIGHT[heading]
    dx, dy = DELTA[h]
    x, y = head % cols + dx, head // cols + dy
    return y * cols + x if 0 <= x < cols and 0 <= y < rows else None


def claims(cols: int, heads: list[tuple[str, int]], tokens: list[int]) -> dict[str, int]:
    """Split the tokens among teammates: the closest (member, token) pair first, then the next closest of the
    members and tokens left, ties to the earlier member (by name) and the earlier token. Returns member -> token."""
    pairs = sorted((abs(t % cols - h % cols) + abs(t // cols - h // cols), who, k)
                   for who, h in heads for k, t in enumerate(tokens))
    out: dict[str, int] = {}
    taken: set[int] = set()
    for _, who, k in pairs:
        if who not in out and k not in taken:
            out[who] = tokens[k]
            taken.add(k)
    return out


def team_turns(model: TrailModel, skip=("you",)) -> dict[str, str]:
    """The scripted move of every running trail not in skip, in seat order. A trail with running teammates plays
    for the team: it heads for its own token (`claims`) and treats the cells its teammates earlier in seat order
    have just chosen as blocked. A team of one plays `choose` exactly as before."""
    out: dict[str, str] = {}
    chosen: dict[str, set[int]] = {}
    split: dict[str, dict[str, int]] = {}
    for pl in model.players:
        if not pl.alive or pl.who in skip:
            continue
        v = view_of(model, pl.who)
        mates = [q for q in model.players if q.alive and q.team == pl.team and q is not pl]
        if mates:
            if pl.team not in split:
                running = [q for q in model.players if q.alive and q.team == pl.team]
                split[pl.team] = claims(model.cols, sorted((q.who, q.body[0]) for q in running), list(model.tokens))
            mine = split[pl.team].get(pl.who)
            if mine is not None:
                v["tokens"] = [mine]
            v["avoid"] = chosen.get(pl.team, set())
        rel = choose(v)
        out[pl.who] = rel
        if mates:
            nxt = _step_to(model.cols, model.rows, pl.body[0], pl.heading, rel)
            if nxt is not None:
                chosen.setdefault(pl.team, set()).add(nxt)
    return out


def simulate(p: dict, names: list[str] | None = None, max_ticks: int | None = None,
             teams: list[str] | None = None) -> dict:
    """Every seat scripted, to the end: the log the browser twin must reproduce."""
    names = names or [f"p{k}" for k in range(p["players"])]
    m = TrailModel(p, names, teams)
    log = []
    while not m.over and (max_ticks is None or m.tick < max_ticks):
        moves = []
        turns = team_turns(m, skip=())
        for pl in m.players:
            if pl.alive:
                rel = turns[pl.who]
                m.turn(pl.who, rel)
                moves.append(rel)
            else:
                moves.append("-")
        ev = m.step()
        log.append("".join(moves) + "|" + ",".join(f"{w}:{c}" for w, c in ev["eaten"]) + "|" + ",".join(ev["out"]))
    return {"tick": m.tick, "over": m.over, "reason": m.reason, "winner": m.winner, "tokens": list(m.tokens),
            "players": [{"who": pl.who, "tokens": pl.tokens, "alive": pl.alive, "died": pl.died, "cause": pl.cause,
                         "len": len(pl.body)} for pl in m.players],
            "teams": [{"team": t, "tokens": m.team_tokens(t)} for t in m.teams], "log": log}


# ---- the stand-in Ante (scripted model) ----

def view_for_team(look: dict) -> dict:
    """A stand-in's view with its team's split: its own token from `claims` over the running teammates in the look,
    and its teammates' next cells (from their heading and queued plan) blocked."""
    v = view_from_look(look)
    team = [r for r in look.get("team", []) if r.get("alive") and "at" in r]
    if not team:
        return v
    cols, rows = v["cols"], v["rows"]
    heads = sorted([(look.get("who", "~me"), v["head"])] + [(r["who"], r["at"][1] * cols + r["at"][0]) for r in team])
    mine = claims(cols, heads, v["tokens"]).get(look.get("who", "~me"))
    if mine is not None:
        v["tokens"] = [mine]
    avoid = set()
    for r in team:
        nxt = _step_to(cols, rows, r["at"][1] * cols + r["at"][0], r["heading"], (r.get("plan") or "S")[0])
        if nxt is not None:
            avoid.add(nxt)
    v["avoid"] = avoid
    return v


class TrailPolicy:
    """Look, then turn as `choose` says, then look again. Stops when its trail is out or the round is over.
    In a team it plays `view_for_team`, and pins one note to the team board on its first move."""

    def __init__(self, seed: int = 0, index: int = 0):
        self.n = 0
        self.noted = False

    def next(self, last_output):
        self.n += 1
        if last_output is None:
            return {"op": "bash", "command": "./trail look"}
        try:
            result = json.loads(last_output)
        except ValueError:
            return {"op": "final", "text": "unreadable trail output"}
        if not isinstance(result, dict) or "error" in result and result.get("done", True):
            return {"op": "final", "text": f"stopped: {result.get('error') if isinstance(result, dict) else '?'}"}
        if result.get("done"):
            return {"op": "final", "text": "my trail is out or the round is over"}
        if "board" in result:
            if result.get("team") and not self.noted:
                self.noted = True
                return {"op": "bash", "command": "./trail note scripted stand-in: I take the token closest to me"}
            return {"op": "bash", "command": f"./trail turn {choose(view_for_team(result))}"}
        return {"op": "bash", "command": "./trail look"}


# ---- the `./trail` client and the prompt ----

OPS = {"look": 0, "turn": 1, "plan": 1, "team": 0, "note": -1}   # -1: the rest of the line, one word or more
USAGE = "usage: ./trail look | turn L|R|S | plan <up to 12 of L, R, S> | team | note <text>"

PROMPT = """You are steering a trail on a shared grid, racing other players for tokens. <GOAL>

The game runs in real time: every tick (about <TICK> seconds) every trail moves one cell. Eating a token (`*`) grows
your trail by one. Leaving the grid, or running into any trail (your own too), puts you out. Each of your commands
takes you several seconds to think about, so several ticks pass between them: steer with a plan, not one turn at a
time.

You steer with one command, `./trail`, run from your working directory. Do not edit any files. Every command prints
one line of JSON.

  ./trail look           the board as text rows, top row first (`.` free, `*` token, `@` your head, `o` your body,
                         `H` another head, `x` another trail<MARKS>), your position `at` [x, y] (x to the right, y down),
                         your `heading` (N is up), free cells `ahead`, `left` and `right` of you, the tokens in `food`,
                         your rivals, your `plan` still queued, and `safe`: how many ticks your path (the plan, then
                         straight on) runs before `hits` a wall or a trail (null when it is clear that far)
  ./trail plan <moves>   queue up to 12 moves, one per tick from the next tick: L (turn left), R (turn right) or
                         S (straight), each relative to your heading at that tick, e.g. `./trail plan SSLSSR`. It
                         replaces your old plan. When the plan runs out you drive straight.
  ./trail turn L|R|S     one move at the next tick only; it clears your plan<TEAMOPS>

`plan` and `turn` answer with everything `look` shows, as it stands after your move, so you do not need a look
between moves: read the answer, then plan again. Turns are relative: `LL` is a U-turn (heading W, it ends heading
E, one row down), and after a plan runs out you drive straight on. If an answer has a `warning`, your next command
must be a new plan. Keep `safe` at 8 or more, above the ticks your next command will take: plan a path several cells
long toward a token and away from walls and other heads, check `safe` in the answer, and re-plan on every command,
because the other trails move too.<TEAM> Keep going until `done` is true, then say in one line how it went."""

SOLO_GOAL = """Win by holding the most tokens when the clock runs out or when every other trail is out."""

TEAM_GOAL = """You are one of <N> agents on ONE team (<MATES>) against a human player (`you` in
`rivals`). The team
scores the sum of its members' tokens, and that total is compared with the human's tokens. The round ends at the clock,
when the human is out, or when the whole team is out. A teammate that crashes keeps its tokens for the team, but a
trail running longer can keep eating, so stay alive."""

TEAM_MARKS = """, `M` a teammate's head, `m` a teammate's trail"""

TEAM_OPS = """
  ./trail team           your whole team: every member's position, heading, queued `plan` and the `path` of cells it
                         draws, its recent tokens, the team's total and the notes board
  ./trail note <text>    pin a short note to your team's board (up to 120 characters; the newest 20 are kept). Only
                         your teammates see it; every `look` lists the notes"""

TEAM = """

Play as a team. `look` lists your teammates (`team`: where each head is, its heading and the moves it still has
queued), the score, and the notes; a `crosses` entry and a `warning` mean your path runs over a teammate's planned
path. Coordinate:
- Avoid each other's planned paths: never plan through a cell a teammate's plan is about to take, and never head
  straight at a teammate's head.
- Split up the tokens: claim the nearest token no teammate has claimed in the notes, with a note such as
  `./trail note claim 12,5`, and leave tokens a teammate claimed to them. Re-claim when your token is eaten.
- Cooperate to cut off the human (`H`): the human is out when it runs into any trail, so teammates can lay trails
  across its way from two sides, with room left for themselves. Never trade your own trail for it.
Keep notes short and few: each command costs you seconds."""


def prompt(mode: str, count: int, tick_s: float) -> str:
    if count > 1:
        mates = ", ".join(f"a{k:02d}" for k in range(1, count + 1))
        goal = TEAM_GOAL.replace("<N>", str(count)).replace("<MATES>", f"the agents {mates} (you are one of them)")
        return (PROMPT.replace("<GOAL>", goal).replace("<MARKS>", TEAM_MARKS).replace("<TEAMOPS>", TEAM_OPS)
                .replace("<TEAM>", TEAM).replace("<TICK>", f"{tick_s:g}"))
    return (PROMPT.replace("<GOAL>", SOLO_GOAL).replace("<MARKS>", "").replace("<TEAMOPS>", "")
            .replace("<TEAM>", "").replace("<TICK>", f"{tick_s:g}"))
