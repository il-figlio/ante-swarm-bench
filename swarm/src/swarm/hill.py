"""Hill: the AX-1's stacking cartridge as a rules model, its scripted policy, and the `./hill` client.

Every player has a well of its own, WIDTH x HEIGHT. Pieces come from one seeded sequence, the same for everyone
(each well draws its own next piece from it). A placement is a column and a rotation: the piece drops straight down
from above the well and locks where it lands. Full rows clear. Clearing two or three rows at once sends rows of
garbage (full but for one hole) to the next well still standing, in seat order; garbage waiting for a well rises
from its floor after that well's next placement. A piece that locks with any cell above the top puts its well out,
and so does garbage that pushes a block above it. The round ends when one well is left standing (or none), or when
the clock runs out; then the most rows cleared wins among the wells still standing, ties to the lower stack, and a
tie after that is a draw.

Teams (swarm style "pooled"): `HillModel(seed, names, teams)`. Without teams every well is a team of one and the game
is as above. With a team of several wells (a swarm), each member plays its own well alone, with its OWN piece sequence
(seed `well_seed(seed, seat)`; the human, seat 0, keeps the round's sequence, and so does a lone Ante), and the team
pools its score: its rows are the sum over its wells. Garbage goes from team to team: whatever a swarm well sends
lands in the human's well, and the human's garbage goes to the swarm's standing wells in rotation. A team is out when
all its wells are out; the round ends when one team is left standing, or at the clock, where the team with more rows
per well wins (the human's rows against the swarm's average, so a bigger swarm gets no head start), then the lower
average stack.

The piece set is the cartridge's own: one domino, two trominoes and four pentominoes, named pip, bar, elbow, cup,
flag, angle and post.

The browser runs the same rules in front-solo/hill-core.js and must agree drop for drop (tests/test_hill.py runs
both on seeded games). Change one, change the other.
"""
from __future__ import annotations

import json
import secrets
import time

from .maze import MASK32, mulberry32

WIDTH, HEIGHT = 9, 18
PIECES = (
    ("pip", ((0, 0), (1, 0))),
    ("bar", ((0, 0), (1, 0), (2, 0))),
    ("elbow", ((0, 0), (0, 1), (1, 1))),
    ("cup", ((0, 0), (2, 0), (0, 1), (1, 1), (2, 1))),
    ("flag", ((0, 0), (1, 0), (0, 1), (1, 1), (0, 2))),
    ("angle", ((0, 0), (0, 1), (0, 2), (1, 2), (2, 2))),
    ("post", ((0, 0), (1, 0), (2, 0), (1, 1), (1, 2))),
)
NAMES = tuple(n for n, _ in PIECES)
SEND = (0, 0, 1, 3)        # rows of garbage sent for 0, 1, 2, 3 rows cleared at once
MAX_PLAYERS = 5
CLOCK_S = 180.0
FUSE_S = 6.0               # a piece left hovering this long drops where it is


def _norm(cells):
    mx, my = min(x for x, _ in cells), min(y for _, y in cells)
    return tuple(sorted(((x - mx, y - my) for x, y in cells), key=lambda c: (c[1], c[0])))


def _rotate(cells):
    h = max(y for _, y in cells) + 1
    return _norm([(h - 1 - y, x) for x, y in cells])


def _shapes():
    out = []
    for _, cells in PIECES:
        rots = [_norm(cells)]
        for _ in range(3):
            rots.append(_rotate(rots[-1]))
        out.append(tuple(rots))
    return tuple(out)


SHAPES = _shapes()


def size(shape) -> tuple[int, int]:
    return max(x for x, _ in shape) + 1, max(y for _, y in shape) + 1


def spawn(piece: int) -> tuple[int, int]:
    """Where a new piece hovers: rotation 0, centred."""
    w, _ = size(SHAPES[piece][0])
    return (WIDTH - w) // 2, 0


def shape_rows(shape) -> list[str]:
    w, h = size(shape)
    rows = [["."] * w for _ in range(h)]
    for x, y in shape:
        rows[y][x] = "#"
    return ["".join(r) for r in rows]


def well_seed(seed: int, seat: int) -> int:
    """The piece seed of a swarm member's own well: the round seed plus the seat, spread over 32 bits (seat 0 is the
    round seed itself). hill-core.js `wellSeed` is the same sum."""
    return (seed + 0x9E3779B9 * seat) & MASK32


class Sequence:
    """Shuffled bags of the seven pieces, from one seed. Wells that share a sequence get the same k-th piece."""

    def __init__(self, seed: int):
        self.rand = mulberry32(seed)
        self.items: list[int] = []

    def __getitem__(self, k: int) -> int:
        while len(self.items) <= k:
            bag = list(range(len(PIECES)))
            for i in range(len(bag) - 1, 0, -1):
                j = int(self.rand() * (i + 1))
                bag[i], bag[j] = bag[j], bag[i]
            self.items.extend(bag)
        return self.items[k]


def land(cells: list[int], shape, col: int) -> int | None:
    """The row the shape's top lands on when dropped straight down at col, or None when col is off the well.
    The result may be negative: then part of the piece is above the top."""
    w, h = size(shape)
    if not 0 <= col <= WIDTH - w:
        return None

    def fits(top):
        for x, y in shape:
            r = top + y
            if r >= HEIGHT or (r >= 0 and cells[r * WIDTH + col + x]):
                return False
        return True

    top = -h
    while fits(top + 1):
        top += 1
    return top


def place(cells: list[int], shape, col: int) -> tuple[list[int], int, bool] | None:
    """(new cells, rows cleared, out) for a drop, or None for a column off the well."""
    top = land(cells, shape, col)
    if top is None:
        return None
    out = list(cells)
    over = False
    for x, y in shape:
        r = top + y
        if r < 0:
            over = True
        else:
            out[r * WIDTH + col + x] = 1
    kept = [out[r * WIDTH:(r + 1) * WIDTH] for r in range(HEIGHT) if not all(out[r * WIDTH:(r + 1) * WIDTH])]
    cleared = HEIGHT - len(kept)
    rows = [[0] * WIDTH for _ in range(cleared)] + kept
    return [v for row in rows for v in row], cleared, over


def heights(cells: list[int]) -> list[int]:
    out = []
    for x in range(WIDTH):
        h = 0
        for r in range(HEIGHT):
            if cells[r * WIDTH + x]:
                h = HEIGHT - r
                break
        out.append(h)
    return out


def holes(cells: list[int]) -> int:
    n = 0
    for x in range(WIDTH):
        roof = False
        for r in range(HEIGHT):
            if cells[r * WIDTH + x]:
                roof = True
            elif roof:
                n += 1
    return n


class Well:
    def __init__(self, who: str, seat: int, seed: int, team: str | None = None, seq: "Sequence | None" = None):
        self.who, self.seat = who, seat
        self.team = team or who
        self.seq = seq if seq is not None else Sequence(seed)
        self.cells = [0] * (WIDTH * HEIGHT)
        self.k = 0                      # pieces used: the next piece is sequence[k]
        self.rows = 0                   # rows cleared
        self.sent = 0                   # garbage rows sent
        self.got = 0                    # garbage rows received
        self.pending: list[int] = []    # garbage rows waiting, one entry per attack
        self.out = False
        self.out_at: int | None = None  # the drop count (model-wide) when it went out
        self.drops = 0
        self.grand = mulberry32((seed + 1013 * (seat + 1)) & MASK32)   # garbage holes for this well

    def height(self) -> int:
        return max(heights(self.cells))

    def rise(self, n: int) -> bool:
        """n garbage rows from the floor, one hole in the same column. False when it pushes a block over the top."""
        hole = int(self.grand() * WIDTH)
        spill = any(self.cells[:n * WIDTH])
        row = [2] * WIDTH
        row[hole] = 0
        self.cells = self.cells[n * WIDTH:] + row * n
        self.got += n
        return not spill


class HillModel:
    def __init__(self, seed: int, names: list[str], teams: list[str] | None = None):
        if not 1 <= len(names) <= MAX_PLAYERS:
            raise ValueError(f"hill players must be 1..{MAX_PLAYERS}")
        if teams is not None and len(teams) != len(names):
            raise ValueError("one team per player")
        self.seed = seed & MASK32
        self.seq = Sequence(self.seed)
        teams = list(teams) if teams else list(names)
        size = {t: teams.count(t) for t in teams}
        # A member of a team of several wells draws its own sequence; everyone else shares the round's.
        self.wells = [Well(who, k, self.seed, teams[k],
                           Sequence(well_seed(self.seed, k)) if size[teams[k]] > 1 and k > 0 else self.seq)
                      for k, who in enumerate(names)]
        self.by = {w.who: w for w in self.wells}
        self.teams: list[str] = []
        for w in self.wells:
            if w.team not in self.teams:
                self.teams.append(w.team)
        self.turn = {t: -1 for t in self.teams}   # the seat each team's last garbage went to: the next goes on from it
        self.drops = 0
        self.over = False
        self.reason: str | None = None
        self.winner: str | None = None

    def piece(self, who: str) -> int:
        w = self.by[who]
        return w.seq[w.k]

    def next_piece(self, who: str) -> int:
        w = self.by[who]
        return w.seq[w.k + 1]

    def members(self, team: str) -> list[Well]:
        return [w for w in self.wells if w.team == team]

    def target(self, w: Well) -> Well | None:
        """Where w's garbage goes: the next team round the ring (in seat order of first members) with a well
        standing, and within that team its standing wells in rotation (the first standing well after the one that got the last). With teams of one this is
        the plain ring."""
        n = len(self.teams)
        at = self.teams.index(w.team)
        for i in range(1, n):
            team = self.teams[(at + i) % n]
            standing = [t for t in self.members(team) if not t.out]
            if standing:
                pick = next((t for t in standing if t.seat > self.turn[team]), standing[0])
                self.turn[team] = pick.seat
                return pick
        return None

    def drop(self, who: str, col, rot) -> dict:
        w = self.by[who]
        if self.over:
            return {"error": "over", "done": True}
        if w.out:
            return {"error": "out", "done": True}
        if isinstance(col, bool) or isinstance(rot, bool) or not isinstance(col, int) or not isinstance(rot, int):
            return {"error": "usage: ./hill drop <col> <rot>"}
        if not 0 <= rot <= 3:
            return {"error": "rot must be 0, 1, 2 or 3"}
        p = self.piece(who)
        shape = SHAPES[p][rot]
        got = place(w.cells, shape, col)
        if got is None:
            return {"error": f"col must be 0..{WIDTH - size(shape)[0]} for {NAMES[p]} at rot {rot}"}
        w.cells, cleared, topped = got
        self.drops += 1
        w.drops += 1
        w.k += 1
        w.rows += cleared
        sent = SEND[cleared] if cleared < len(SEND) else cleared
        target = None
        if topped:
            self._out(w)
        else:
            if sent:
                target = self.target(w)
                if target is not None:
                    target.pending.append(sent)
                    w.sent += sent
            while w.pending and not w.out:
                if not w.rise(w.pending.pop(0)):
                    self._out(w)
        self._check()
        return {"ok": True, "piece": NAMES[p], "col": col, "rot": rot, "cleared": cleared,
                "sent": sent if target else 0, "to": target.who if target else None, "out": w.out, "done": self.over or w.out}

    def _out(self, w: Well):
        w.out, w.out_at = True, self.drops
        w.pending = []

    def _check(self):
        standing = len({w.team for w in self.wells if not w.out})
        if standing == 0 or (len(self.teams) > 1 and standing <= 1):
            self.finish("out")

    def finish(self, reason: str):
        if self.over:
            return
        self.over, self.reason = True, reason
        self.winner = self.leader()

    def _rank(self, w: Well):
        return (0 if w.out else 1, w.out_at or 0, w.rows, -w.height())

    def standings(self) -> list[Well]:
        return sorted(self.wells, key=self._rank, reverse=True)

    def team_rows(self, team: str) -> int:
        return sum(w.rows for w in self.members(team))

    def per_well(self, team: str) -> float:
        ms = self.members(team)
        return sum(w.rows for w in ms) / len(ms)

    def team_rank(self, team: str):
        """(standing, when its last well went out, rows per well, minus the average stack). A team of one ranks as
        a well always did: standing, out later, rows cleared, lower stack."""
        ms = self.members(team)
        standing = any(not w.out for w in ms)
        out_at = 0 if standing else max(w.out_at or 0 for w in ms)
        return (1 if standing else 0, out_at, sum(w.rows for w in ms) / len(ms),
                -sum(w.height() for w in ms) / len(ms))

    def team_standings(self) -> list[str]:
        return sorted(self.teams, key=self.team_rank, reverse=True)

    def leader(self) -> str | None:
        """The leading team's name (a well's own name for a team of one), or None for a tie at the top."""
        ranked = self.team_standings()
        if len(ranked) > 1 and self.team_rank(ranked[0]) == self.team_rank(ranked[1]):
            return None
        return ranked[0]

    # -- views --

    def public(self, w: Well) -> dict:
        return {"who": w.who, "seat": w.seat, "team": w.team, "cells": list(w.cells), "piece": NAMES[self.piece(w.who)],
                "next": NAMES[self.next_piece(w.who)], "k": w.k, "rows": w.rows, "sent": w.sent, "got": w.got,
                "pending": sum(w.pending), "out": w.out, "height": w.height(), "drops": w.drops}

    def frame(self) -> dict:
        return {"cols": WIDTH, "rows": HEIGHT, "wells": [self.public(w) for w in self.wells],
                "teams": [self.team_public(t) for t in self.teams], "over": self.over, "reason": self.reason,
                "winner": self.winner}

    def team_public(self, team: str) -> dict:
        ms = self.members(team)
        return {"team": team, "members": [w.who for w in ms], "rows": sum(w.rows for w in ms),
                "per_well": sum(w.rows for w in ms) / len(ms), "standing": sum(1 for w in ms if not w.out),
                "sent": sum(w.sent for w in ms)}

    def look(self, who: str) -> dict:
        w = self.by[who]
        p = self.piece(who)
        return {"well": ["".join("#" if v else "." for v in w.cells[r * WIDTH:(r + 1) * WIDTH]) for r in range(HEIGHT)],
                "piece": {"name": NAMES[p], "rotations": [shape_rows(s) for s in SHAPES[p]]},
                "next": NAMES[self.next_piece(who)], "rows": w.rows, "pending": sum(w.pending), "height": w.height(),
                "out": w.out, "rivals": [{"who": o.who, "rows": o.rows, "height": o.height(), "out": o.out}
                                         for o in self.wells if o.team != w.team],
                **({"team": {"rows": self.team_rows(w.team), "per_well": round(self.per_well(w.team), 2),
                             "members": [{"who": o.who, "rows": o.rows, "height": o.height(), "out": o.out}
                                         for o in self.members(w.team) if o is not w]}}
                   if len(self.members(w.team)) > 1 else {}),
                "done": self.over or w.out}

    def call(self, who: str, op: str, arg=None) -> dict:
        if who not in self.by:
            return {"error": "unknown agent"}
        if op == "look":
            return self.look(who)
        if op == "drop":
            parts = arg.split() if isinstance(arg, str) else arg if isinstance(arg, list) else []
            try:
                col, rot = (int(v) for v in parts[:2])
                if len(parts) not in (2, 3):
                    raise ValueError
            except (TypeError, ValueError):
                return {"error": USAGE}
            return self.drop(who, col, rot)
        return {"error": USAGE}


class LiveHill(HillModel):
    """The rules model for a live round, with two rules for agents (the human's moves go to `drop` and are not
    touched by either):

    - every piece has a fresh random id, shown only by `look` (and by the look that follows a drop), and a drop must
      name the current piece's id: a drop for a piece the agent has not looked at is refused, so every drop is made
      on a well that was read after the last piece landed;
    - a well's drops are at least `min_drop_s` apart; one sooner is refused with how long to wait.

    Refusals change nothing. Ids come from `secrets`, so they cannot be worked out from the seed."""

    def __init__(self, seed: int, names: list[str], min_drop_s: float = 1.0, clock=time.monotonic,
                 teams: list[str] | None = None):
        super().__init__(seed, names, teams)
        self.min_drop_s, self.clock = min_drop_s, clock
        self.ids: dict[str, tuple[int, str]] = {}      # who -> (piece number k, id)
        self.last_drop: dict[str, float] = {}           # who -> when its last agent drop landed
        self.refused: dict[str, int] = {w.who: 0 for w in self.wells}

    def piece_id(self, who: str) -> str:
        w = self.by[who]
        got = self.ids.get(who)
        if got is None or got[0] != w.k:
            got = (w.k, secrets.token_hex(3))
            self.ids[who] = got
        return got[1]

    def wait_s(self, who: str) -> float:
        last = self.last_drop.get(who)
        return 0.0 if last is None else max(0.0, round(self.min_drop_s - (self.clock() - last), 2))

    def look(self, who: str) -> dict:
        out = super().look(who)
        if not out["done"]:
            out["piece"]["id"] = self.piece_id(who)
            out["wait_s"] = self.wait_s(who)
        return out

    def call(self, who: str, op: str, arg=None) -> dict:
        if who not in self.by or op != "drop":
            return super().call(who, op, arg)
        parts = arg.split() if isinstance(arg, str) else []
        try:
            if len(parts) != 3:
                raise ValueError
            col, rot = int(parts[0]), int(parts[1])
        except ValueError:
            return {"error": USAGE}
        w = self.by[who]
        if self.over or w.out:
            return self.drop(who, col, rot)
        got = self.ids.get(who)
        if got is None or got != (w.k, parts[2]):
            self.refused[who] += 1
            return {"error": "stale piece id: that piece has landed or was never looked at; run ./hill look and "
                             "decide on the well and piece it shows", "done": False}
        wait = self.wait_s(who)
        if wait > 0:
            self.refused[who] += 1
            return {"error": f"too soon: drops are at least {self.min_drop_s:g} s apart", "wait_s": wait, "done": False}
        out = self.drop(who, col, rot)
        if "error" not in out:
            self.last_drop[who] = self.clock()
            if not out["done"]:
                out["now"] = self.look(who)
        return out


# ---- the scripted policy (the twin of hill-core.js `choose`) ----

def score(cells: list[int], cleared: int) -> int:
    hs = heights(cells)
    bump = sum(abs(hs[i] - hs[i + 1]) for i in range(WIDTH - 1))
    return 76 * cleared - 51 * sum(hs) - 36 * holes(cells) - 18 * bump


def choose(cells: list[int], piece: int) -> tuple[int, int]:
    """(col, rot): every column of every rotation, scored on the well after the drop; the first best wins.
    A drop that tops out is taken only when nothing else is left."""
    best, best_key = None, None
    for rot in range(4):
        shape = SHAPES[piece][rot]
        for col in range(WIDTH - size(shape)[0] + 1):
            cells2, cleared, out = place(cells, shape, col)
            key = (0 if out else 1, score(cells2, cleared))
            if best_key is None or key > best_key:
                best, best_key = (col, rot), key
    return best


def cells_from_look(look: dict) -> list[int]:
    return [1 if ch != "." else 0 for row in look["well"] for ch in row]


def simulate(seed: int, players: int, max_beats: int = 120, teams: list[str] | None = None) -> dict:
    """Every well scripted, one drop each per beat in seat order, to the end or max_beats."""
    names = [f"p{k}" for k in range(players)]
    m = HillModel(seed, names, teams)
    log = []
    beat = 0
    while not m.over and beat < max_beats:
        beat += 1
        moves = []
        for w in m.wells:
            if w.out or m.over:
                moves.append("-")
                continue
            col, rot = choose(w.cells, m.piece(w.who))
            r = m.drop(w.who, col, rot)
            moves.append(f"{NAMES[w.seq[w.k - 1]][0]}{col}{rot}{r['cleared']}{r['sent']}{r['to'] or ''}")
        log.append(" ".join(moves))
    if not m.over:
        m.finish("clock")
    return {"beats": beat, "over": m.over, "reason": m.reason, "winner": m.winner,
            "wells": [{"who": w.who, "rows": w.rows, "sent": w.sent, "got": w.got, "out": w.out, "k": w.k,
                       "cells": "".join(str(v) for v in w.cells)} for w in m.wells],
            "teams": [m.team_public(t) for t in m.teams], "log": log}


# ---- the stand-in Ante (scripted model) ----

class HillPolicy:
    def __init__(self, seed: int = 0, index: int = 0):
        self.n = 0

    def next(self, last_output):
        self.n += 1
        if last_output is None:
            return {"op": "bash", "command": "./hill look"}
        try:
            result = json.loads(last_output)
        except ValueError:
            return {"op": "final", "text": "unreadable hill output"}
        if not isinstance(result, dict):
            return {"op": "final", "text": "stopped: ?"}
        if result.get("done"):
            return {"op": "final", "text": "my well is out or the round is over"}
        if "error" in result:
            wait = result.get("wait_s")
            if isinstance(wait, (int, float)):
                return {"op": "bash", "command": f"sleep {min(max(float(wait), 0.05), 5.0):.2f}; ./hill look"}
            if "stale" in str(result["error"]):
                return {"op": "bash", "command": "./hill look"}
            return {"op": "final", "text": f"stopped: {result['error']}"}
        look = result.get("now") if isinstance(result.get("now"), dict) else result
        if "well" in look:
            col, rot = choose(cells_from_look(look), NAMES.index(look["piece"]["name"]))
            pid = look["piece"].get("id")
            return {"op": "bash", "command": f"./hill drop {col} {rot}" + (f" {pid}" if pid else "")}
        return {"op": "bash", "command": "./hill look"}


# ---- the `./hill` client and the prompt ----

OPS = {"look": 0, "drop": 3}
USAGE = "usage: ./hill look | drop <col> <rot> <piece id from look>"

PROMPT = f"""You are playing a stacking game in a well of your own, {WIDTH} columns wide and {HEIGHT} rows deep.
<GOAL>

<PIECES> You place your current piece by choosing a column and a rotation; it drops straight down from above the well
and locks where it lands. A full row clears. Clearing two rows at once sends one row of garbage <TO>, three rows sends
three. Garbage sent to you rises from your floor after your next placement. If a piece locks above the top of your
well, you are out. A piece you leave hovering for about <FUSE> seconds drops where it is (in the middle, unrotated),
so do not wait long.

You play with one command, `./hill`, run from your working directory. Do not edit any files. Every command prints
one line of JSON.

  ./hill look                   your well as text rows, top first (`#` filled, `.` empty), your current piece with
                                its `id` and its four rotations drawn as rows, the next piece, rows cleared, garbage
                                waiting and your rivals<TEAMLOOK>
  ./hill drop <col> <rot> <id>  drop the current piece at rotation <rot> (0..3, as drawn in `look`) with its leftmost
                                cell in column <col> (0 is the left wall); <id> is the piece id `look` showed

Before every drop, read the well, the current piece and the next piece, and choose the placement that keeps the
stack low and flat with no covered holes. Every piece gets a new id, and a drop that does not name the current
piece's id is refused, so each drop must follow a fresh look. A successful drop answers with `now`: the same view
as `look` for your next piece, with its id, so you can decide your next drop from it without another look. Drops
are at least <GAP> second apart. Dropping without reading the well loses fast: a stack in one column tops out in a
few pieces. Keep going until `done` is true, then say in one line how it went."""

SOLO_GOAL = """Your rival plays a well of its own beside you. Win by being the last well standing, or by clearing the
most rows before the clock runs out (about <CLOCK> seconds)."""
SOLO_PIECES = "Everyone gets the same pieces in the same order."

TEAM_GOAL = """You are one of <N> agents on ONE team (<MATES>) against a human player (`you` in `rivals`). Each of
you plays your own well, alone: there is nothing to coordinate, just play your well well. The team's score is the sum
of the rows its members clear. At the clock (about <CLOCK> seconds) the team's rows per well are compared with the
human's rows, so every row you clear counts. The team loses only when ALL its wells are out, and wins at once if
the human's well tops out first, so staying alive matters more than a big clear."""
TEAM_PIECES = "Your well has its own piece sequence."

TEAM_LOOK = """, and `team`: the team's
                                pooled rows, its rows per well, and your teammates' rows and stacks"""


def prompt(mode: str, count: int, clock_s: float, fuse_s: float, min_drop_s: float = 1.0) -> str:
    if count > 1:
        mates = ", ".join(f"a{k:02d}" for k in range(1, count + 1))
        text = (PROMPT.replace("<GOAL>", TEAM_GOAL.replace("<N>", str(count)).replace("<MATES>", f"the agents {mates}; you are one of them"))
                .replace("<PIECES>", TEAM_PIECES).replace("<TO>", "to the human's well")
                .replace("<TEAMLOOK>", TEAM_LOOK))
    else:
        text = (PROMPT.replace("<GOAL>", SOLO_GOAL).replace("<PIECES>", SOLO_PIECES).replace("<TO>", "to your rival")
                .replace("<TEAMLOOK>", ""))
    return (text.replace("<CLOCK>", f"{clock_s:g}").replace("<FUSE>", f"{fuse_s:g}")
            .replace("<GAP>", f"{min_drop_s:g}"))
