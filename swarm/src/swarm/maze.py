"""Maze eval core: the maze, the service that holds it, the scripted explorer, and a lockstep simulation.

The maze never leaves this module's process. Agents see it only through `MazeService` (HTTP over a Unix socket
in a private directory) via the `./maze` client script, so reading the workspace reveals nothing. A token is
honoured only from the process tree it was issued to (the peer pid of the socket must descend from the agent's
root pid), so a sibling's `.maze.json` is useless to an agent that reads it. Everything an agent can
do goes through `MazeModel.call`, one function with one lock around it in the service. `simulate` drives the same
model in-process, so a simulated run and a real run of the same `Explorer` cannot disagree on the rules; the only
thing left to cross-check is `front-maze/maze-core.js`, which re-implements the model for the browser.

Cell layout: index = y * cols + x. Wall bits: N=1, E=2, S=4, W=8; a set bit means a wall. y grows downward, so
N is y-1.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler
from pathlib import Path

DIRS = (("N", 1, 0, -1), ("E", 2, 1, 0), ("S", 4, 0, 1), ("W", 8, -1, 0))
BIT = {name: bit for name, bit, _, _ in DIRS}
OPPOSITE = {"N": "S", "E": "W", "S": "N", "W": "E"}
MASK32 = 0xFFFFFFFF

PARAMS_DEFAULTS = {
    "cols": 40,
    "rows": 25,
    "seed": 2026,
    "generator": "prim",
    "loops": 0.05,
    "start": "center",
    "map": "shared",
    "step_limit": 4000,
    "call_budget": 0,
}
GENERATORS = ("prim", "backtracker", "serpentine")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_params(d: dict) -> dict:
    """Fill defaults and reject anything outside the documented ranges (docs/maze.md). Unknown keys are errors, not ignored:
    a typo in a sealed task must not silently run the default maze."""
    if not isinstance(d, dict):
        raise ValueError("maze params must be a table")
    unknown = sorted(set(d) - set(PARAMS_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown maze params: {', '.join(unknown)}")
    p = {**PARAMS_DEFAULTS, **d}
    for key, lo, hi in (("cols", 4, 128), ("rows", 4, 128), ("seed", 0, MASK32), ("step_limit", 1, 1_000_000),
                        ("call_budget", 0, 10_000_000)):
        if not _is_int(p[key]) or not lo <= p[key] <= hi:
            raise ValueError(f"maze {key} must be an integer in {lo}..{hi}, got {p[key]!r}")
    if isinstance(p["loops"], bool) or not isinstance(p["loops"], (int, float)) or not 0 <= p["loops"] <= 0.3:
        raise ValueError(f"maze loops must be a number in 0..0.3, got {p['loops']!r}")
    p["loops"] = float(p["loops"])
    for key, allowed in (("generator", GENERATORS), ("start", ("center", "corner")), ("map", ("shared", "own"))):
        if p[key] not in allowed:
            raise ValueError(f"maze {key} must be one of {', '.join(allowed)}, got {p[key]!r}")
    return p


def mulberry32(seed: int):
    """The browser's PRNG, bit for bit. Everything mod 2**32 so it matches Math.imul and >>> 0."""
    a = seed & MASK32

    def rand() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & MASK32
        t = a
        t = ((t ^ (t >> 15)) * (t | 1)) & MASK32
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & MASK32)) & MASK32
        return ((t ^ (t >> 14)) & MASK32) / 4294967296

    return rand


class Maze:
    __slots__ = ("cols", "rows", "walls", "start", "exit", "seed", "_route")

    def __init__(self, cols: int, rows: int, walls, start: int, exit: int, seed: int):
        self.cols, self.rows, self.walls = cols, rows, walls
        self.start, self.exit, self.seed = start, exit, seed
        self._route: list[int] | None = None

    def neighbours(self, i: int):
        """Open neighbours of cell i as (name, cell), in N, E, S, W order."""
        x, y = i % self.cols, i // self.cols
        for name, bit, dx, dy in DIRS:
            if not self.walls[i] & bit:
                yield name, (y + dy) * self.cols + x + dx

    def shortest_route(self) -> list[int]:
        """Cells from start to exit inclusive. Cached: the maze is immutable."""
        if self._route is None:
            prev = {self.start: self.start}
            queue = [self.start]
            for i in queue:
                for _, j in self.neighbours(i):
                    if j not in prev:
                        prev[j] = i
                        queue.append(j)
            route = [self.exit]
            while route[-1] != self.start:
                route.append(prev[route[-1]])
            self._route = route[::-1]
        return self._route

    def stats(self) -> dict:
        degree = [sum(1 for _ in self.neighbours(i)) for i in range(self.cols * self.rows)]
        return {
            "cells": self.cols * self.rows,
            "shortest": len(self.shortest_route()),
            "dead_ends": sum(1 for d in degree if d == 1),
            "branch_cells": sum(1 for d in degree if d >= 3),
        }


def _open(walls, cols: int, i: int, bit: int, j: int) -> None:
    walls[i] &= ~bit & 15
    walls[j] &= ~{1: 4, 2: 8, 4: 1, 8: 2}[bit] & 15


def _in_bounds(cols: int, rows: int, x: int, y: int) -> bool:
    return 0 <= x < cols and 0 <= y < rows


def _prim(cols: int, rows: int, start: int, rand) -> bytearray:
    """Randomized Prim over an edge list with swap-remove: many short branches. Mirrors the prototype exactly."""
    n = cols * rows
    walls = bytearray([15]) * n
    in_maze = bytearray(n)
    edges: list[tuple[int, tuple]] = []

    def add(i: int) -> None:
        in_maze[i] = 1
        x, y = i % cols, i // cols
        for d in DIRS:
            nx, ny = x + d[2], y + d[3]
            if _in_bounds(cols, rows, nx, ny) and not in_maze[ny * cols + nx]:
                edges.append((i, d))

    add(start)
    while edges:
        k = int(rand() * len(edges))
        i, d = edges[k]
        edges[k] = edges[-1]
        edges.pop()
        j = i + d[3] * cols + d[2]
        if in_maze[j]:
            continue
        _open(walls, cols, i, d[1], j)
        add(j)
    return walls


def _backtracker(cols: int, rows: int, start: int, rand) -> bytearray:
    """Seeded depth-first carve: few branches, long corridors. The control where a swarm has little to split."""
    n = cols * rows
    walls = bytearray([15]) * n
    visited = bytearray(n)
    visited[start] = 1
    stack = [start]
    while stack:
        i = stack[-1]
        x, y = i % cols, i // cols
        options = [d for d in DIRS
                   if _in_bounds(cols, rows, x + d[2], y + d[3]) and not visited[(y + d[3]) * cols + x + d[2]]]
        if not options:
            stack.pop()
            continue
        d = options[int(rand() * len(options))]
        j = (y + d[3]) * cols + x + d[2]
        _open(walls, cols, i, d[1], j)
        visited[j] = 1
        stack.append(j)
    return walls


def _serpentine(cols: int, rows: int, start: int, rand) -> bytearray:
    """One path through every cell, row by row, turning at alternate ends: nothing to split. The negative control:
    one Ante from the corner already walks the only route, so a swarm can only add calls. Uses no random draws."""
    walls = bytearray([15]) * (cols * rows)
    for y in range(rows):
        for x in range(cols - 1):
            i = y * cols + x
            _open(walls, cols, i, 2, i + 1)
        if y < rows - 1:
            x = cols - 1 if y % 2 == 0 else 0
            _open(walls, cols, y * cols + x, 4, (y + 1) * cols + x)
    return walls


def generate(params: dict) -> Maze:
    p = validate_params(params)
    cols, rows = p["cols"], p["rows"]
    n = cols * rows
    start = (rows // 2) * cols + cols // 2 if p["start"] == "center" else 0
    rand = mulberry32(p["seed"])
    walls = {"prim": _prim, "backtracker": _backtracker, "serpentine": _serpentine}[p["generator"]](cols, rows, start, rand)
    # Extra openings so there is more than one way through. The same two draws per try as the browser.
    t = 0
    while t < n * p["loops"]:
        i = int(rand() * n)
        d = DIRS[int(rand() * 4)]
        x, y = i % cols, i // cols
        if _in_bounds(cols, rows, x + d[2], y + d[3]):
            _open(walls, cols, i, d[1], (y + d[3]) * cols + x + d[2])
        t += 1
    return Maze(cols, rows, walls, start, n - 1, p["seed"])


class _Agent:
    __slots__ = ("id", "index", "pos", "calls", "refused", "firsts", "illegal", "limit_hits", "reached", "claim", "own",
                 "team", "role", "recent", "team_firsts", "last_op", "assigned", "notes", "notes_seen")

    def __init__(self, agent_id: str, index: int, pos: int, own, team: str | None = None, role: str | None = None):
        self.id, self.index, self.pos = agent_id, index, pos
        self.calls = self.refused = self.firsts = self.illegal = self.limit_hits = 0
        self.reached = False
        self.claim: int | None = None
        self.own = own
        # Live rounds only (team set): the team whose map this agent reads and writes, its role in the team
        # (player | ally | rival), its last moves, and the cells it was first on its team to enter.
        self.team, self.role = team, role
        self.recent: list[str] = []
        self.team_firsts = 0
        # Team play (live rounds): the agent's last op ("move E", "note", ...), the target its lead assigned it
        # (a cell index, until it gets there, asks next-edge itself, or gets another), how many notes it posted,
        # and the newest team note number it has been shown.
        self.last_op: str | None = None
        self.assigned: tuple[int, str] | None = None   # (target cell, lead id)
        self.notes = 0
        self.notes_seen = 0


ROLES = ("player", "ally", "rival", "lead", "worker")
RIVAL_ROLES = ("rival", "lead", "worker")
RECENT_MOVES = 10
BUCKETS = ("near", "mid", "far")
# Team notes (live rounds): a short board per team. Never shown to another team; the player's own team board takes
# notes from its ally only (the human sends moves, never text, so no player text reaches a model).
NOTES_KEPT = 20        # the newest notes a team's board keeps
NOTE_CHARS = 120       # characters a note keeps, after cleaning
NOTES_IN_LOOK = 3      # the newest teammate notes a `look` carries


def clean_note(text) -> str:
    """A note as plain text: control and format characters (newlines, tabs, bidi and zero-width marks) become
    spaces, runs of spaces close up, and the result is cut to NOTE_CHARS."""
    if not isinstance(text, str):
        return ""
    out = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    return " ".join(out.split())[:NOTE_CHARS]


class MazeModel:
    """The rules, with no I/O and no locking. The service adds both; `simulate` adds neither.

    Live rounds (play.py) register agents with a `team`: each team then has its own map (`teams[name]`), the union
    of what its members have seen; `look`, `next-edge` (claims included) and `map` read only that map, and `done` is
    the race's (any agent at the exit). Agents without a team, which is every eval agent, behave exactly as before.
    Two read-only ops exist for a team's `ally` only: `player` (the team's player: position, last moves, team map)
    and `rivals` (each agent of another team: distance-to-exit bucket and cells it mapped, never a position).

    Team play: every team member may `note <text>` to its team's board (NOTES_KEPT newest, NOTE_CHARS each, cleaned)
    and read it with `notes`; `look` carries the newest NOTES_IN_LOOK notes by teammates, and a `move` answer carries
    teammates' notes the agent has not been shown yet. A team's `lead` also has `assign <worker>` (MazeModel.assign's
    next-edge on the worker's behalf; the worker's answers then carry `assigned`, a path from where it stands to the
    target, until it gets there) and `team` (each teammate's position, claim, assignment and last op)."""

    def __init__(self, maze: Maze, params: dict):
        self.maze = maze
        self.params = validate_params(params)
        self.shared = self.params["map"] == "shared"
        n = maze.cols * maze.rows
        self.seen = bytearray(n)
        self.seen[maze.start] = 1
        # The start cell is credited to agent index 0, as in the prototype; it never counts as a "first".
        self.by = [-1] * n
        self.by[maze.start] = 0
        self.unique = 0
        self.calls = 0
        self.exit_by: str | None = None
        self.agents: dict[str, _Agent] = {}
        self.teams: dict[str, bytearray] = {}
        self.notes: dict[str, list[dict]] = {}   # team -> its board, oldest first, at most NOTES_KEPT
        self.note_count: dict[str, int] = {}     # team -> notes ever posted (numbers the notes)
        self.last_reveal: int | None = None   # the cell the latest call added to its agent's team map, if any
        self._dist: list[int] | None = None

    def register(self, agent_id: str, index: int, team: str | None = None, role: str | None = None) -> None:
        if agent_id in self.agents:
            raise ValueError(f"agent {agent_id!r} already registered")
        if team is None and role is not None:
            raise ValueError("a role needs a team")
        if team is not None and (not isinstance(team, str) or not team or role not in ROLES):
            raise ValueError(f"a team member needs a team name and a role in {', '.join(ROLES)}")
        own = None
        if not self.shared and team is None:
            own = bytearray(len(self.seen))
            own[self.maze.start] = 1
        if team is not None and team not in self.teams:
            self.teams[team] = bytearray(len(self.seen))
            self.teams[team][self.maze.start] = 1
            self.notes[team] = []
            self.note_count[team] = 0
        self.agents[agent_id] = _Agent(agent_id, index, self.maze.start, own, team, role)

    def done(self, a: _Agent) -> bool:
        if a.team is not None:
            return self.exit_by is not None
        return self.exit_by is not None if self.shared else a.reached

    def call(self, agent_id: str, op: str, arg=None) -> dict:
        a = self.agents[agent_id]
        self.last_reveal = None
        if op == "verdict":
            return {**self.verdict(agent_id), "done": self.done(a)}
        refusal = self._refusal(a)
        a.calls += 1
        self.calls += 1
        if refusal:
            a.limit_hits += 1
            a.refused += 1
            return self._error(a, refusal)
        if a.team is not None:
            a.last_op = op if op != "move" or not isinstance(arg, str) or arg not in BIT else f"move {arg}"
        if op == "look":
            result = self._view(a)
            if a.team is not None:
                self._attach_notes(a, result, NOTES_IN_LOOK)
        elif op == "move":
            result = self._move(a, arg)
            if a.team is not None and "error" not in result:
                self._attach_notes(a, result, NOTES_IN_LOOK, unseen=True)
        elif op == "next-edge":
            a.assigned = None   # asking for itself drops what the lead handed it
            result = self._next_edge(a)
        elif op == "map":
            result = self._map(a)
        elif op == "player" and a.role == "ally":
            result = self._player(a)
        elif op == "rivals" and a.role == "ally":
            result = self._rivals(a)
        elif op == "note" and a.team is not None:
            result = self._note(a, arg)
        elif op == "notes" and a.team is not None:
            result = {"notes": [dict(n) for n in self.notes[a.team]], "done": self.done(a)}
            a.notes_seen = self.note_count[a.team]
        elif op == "assign" and a.role == "lead":
            result = self._assign(a, arg)
        elif op == "team" and a.role == "lead":
            result = self._team(a)
        else:
            result = self._error(a, "bad_op")
        if "error" in result:
            a.refused += 1
        return result

    def _refusal(self, a: _Agent) -> str | None:
        # Limits look at the counts before this call, so call number `limit` is the last one allowed.
        if a.calls >= self.params["step_limit"]:
            return "step_limit"
        if self.params["call_budget"] and self.calls >= self.params["call_budget"]:
            return "budget"
        return None

    def assign(self, lead_id: str, worker_id: str) -> dict:
        """Delegate mode: the lead runs next-edge on behalf of a worker. The call (and any refusal) is the lead's;
        the claim, the path and the starting point are the worker's."""
        lead, worker = self.agents[lead_id], self.agents[worker_id]
        refusal = self._refusal(lead)
        lead.calls += 1
        self.calls += 1
        if refusal:
            lead.limit_hits += 1
            lead.refused += 1
            return self._error(worker, refusal)
        result = self._next_edge(worker)
        if "error" in result:
            lead.refused += 1
        return result

    def verdict(self, agent_id: str) -> dict:
        a = self.agents.get(agent_id)
        if a is None:
            return {"legal": False, "within_limit": False, "exit_reached": False, "passed": False, "calls": 0}
        legal = a.illegal == 0
        within = a.limit_hits == 0
        reached = self.exit_by is not None if self.shared else a.reached
        return {"legal": legal, "within_limit": within, "exit_reached": reached,
                "passed": legal and within and reached, "calls": a.calls}

    # ---- team play: notes, assignments, the lead's view ----

    def _note(self, a: _Agent, arg) -> dict:
        text = clean_note(arg)
        if not text:
            return self._error(a, "empty_note")
        cut = isinstance(arg, str) and len(" ".join(arg.split())) > NOTE_CHARS
        self.note_count[a.team] += 1
        board = self.notes[a.team]
        board.append({"n": self.note_count[a.team], "who": a.id, "text": text})
        del board[:-NOTES_KEPT]
        a.notes += 1
        a.notes_seen = self.note_count[a.team]   # its own note is not news to it
        out = {"ok": True, "n": self.note_count[a.team], "text": text, "done": self.done(a)}
        if cut:
            out["cut"] = True
        return out

    def team_notes(self, team: str) -> list[dict]:
        return [dict(n) for n in self.notes.get(team, [])]

    def _attach_notes(self, a: _Agent, result: dict, k: int, unseen: bool = False) -> None:
        """The newest k notes by a's teammates (not a's own); with `unseen`, only those a has not been shown."""
        board = [n for n in self.notes[a.team] if n["who"] != a.id and (not unseen or n["n"] > a.notes_seen)]
        if board:
            result["notes"] = [{"who": n["who"], "text": n["text"]} for n in board[-k:]]
            a.notes_seen = max(a.notes_seen, board[-1]["n"])

    def _route_to(self, a: _Agent, target: int) -> list[str] | None:
        """Moves from a's cell to target over its team's mapped cells, or None."""
        m = self.maze
        known = self.teams[a.team]
        prev = {a.pos: a.pos}
        queue = [a.pos]
        for i in queue:
            if i == target:
                break
            for _, j in m.neighbours(i):
                if j not in prev and (known[j] or j == target):
                    prev[j] = i
                    queue.append(j)
        if target not in prev:
            return None
        cells = [target]
        while cells[-1] != a.pos:
            cells.append(prev[cells[-1]])
        cells.reverse()
        names = {-m.cols: "N", 1: "E", m.cols: "S", -1: "W"}
        return [names[j - i] for i, j in zip(cells, cells[1:])]

    def _assignment(self, a: _Agent) -> dict | None:
        if a.assigned is None:
            return None
        target, lead = a.assigned
        m = self.maze
        return {"to": [target % m.cols, target // m.cols], "path": self._route_to(a, target), "by": lead}

    def _assign(self, lead: _Agent, worker_id) -> dict:
        """MazeModel.assign's next-edge on a teammate's behalf, from the lead's own call (the call has been counted
        already). The worker keeps the target as its assignment; the lead gets the worker's path."""
        worker = self.agents.get(worker_id) if isinstance(worker_id, str) else None
        if worker is None or worker is lead or worker.team != lead.team or worker.role != "worker":
            return self._error(lead, "bad_worker")
        result = self._next_edge(worker)
        if result.get("path") is None:
            worker.assigned = None
            return {"worker": worker.id, "path": None, "done": self.done(lead)}
        worker.assigned = (worker.claim, lead.id)
        return {"worker": worker.id, "path": result["path"], "to": result["to"], "done": self.done(lead)}

    def _team(self, lead: _Agent) -> dict:
        """The lead's view of its team: where each teammate is, what it claimed or was assigned, its last op, its
        notes, and whether it is idle (nothing assigned and no claim left to reach)."""
        m = self.maze
        xy = lambda i: None if i is None else [i % m.cols, i // m.cols]   # noqa: E731
        rows = []
        for o in self.agents.values():
            if o.team != lead.team or o is lead:
                continue
            target = o.assigned[0] if o.assigned else None
            rows.append({"who": o.id, "role": o.role, "at": xy(o.pos), "claim": xy(o.claim), "assigned": xy(target),
                         "last": o.last_op, "calls": o.calls, "notes": o.notes,
                         "idle": target is None and (o.claim is None or o.claim == o.pos)})
        mapped = sum(self.teams[lead.team])
        out = {"team": rows, "mapped": mapped, "done": self.done(lead)}
        self._attach_notes(lead, out, NOTES_IN_LOOK)
        return out

    # ---- ops ----

    def _error(self, a: _Agent, error: str) -> dict:
        return {"error": error, "done": self.done(a)}

    def _known(self, a: _Agent):
        if a.team is not None:
            return self.teams[a.team]
        return self.seen if self.shared else a.own

    def _view(self, a: _Agent) -> dict:
        m = self.maze
        known = self._known(a)
        x, y = a.pos % m.cols, a.pos // m.cols
        open_sides = [name for name, bit, _, _ in DIRS if not m.walls[a.pos] & bit]
        new = [name for name, bit, dx, dy in DIRS
               if not m.walls[a.pos] & bit and not known[(y + dy) * m.cols + x + dx]]
        out = {"at": [x, y], "open": open_sides, "new": new, "exit": a.pos == m.exit, "steps": a.calls,
               "done": self.done(a)}
        assigned = self._assignment(a) if a.team is not None else None
        if assigned is not None:
            out["assigned"] = assigned
        return out

    def _move(self, a: _Agent, arg) -> dict:
        m = self.maze
        if not isinstance(arg, str) or arg not in BIT:
            a.illegal += 1
            return self._error(a, "bad_arg")
        _, bit, dx, dy = next(d for d in DIRS if d[0] == arg)
        if m.walls[a.pos] & bit:
            a.illegal += 1
            return self._error(a, "wall")
        j = (a.pos // m.cols + dy) * m.cols + a.pos % m.cols + dx
        a.pos = j
        if not self.seen[j]:
            self.seen[j] = 1
            self.by[j] = a.index
            a.firsts += 1
            self.unique += 1
        if a.own is not None:
            a.own[j] = 1
        if a.team is not None:
            team = self.teams[a.team]
            if not team[j]:
                team[j] = 1
                a.team_firsts += 1
                self.last_reveal = j
            a.recent.append(arg)
            del a.recent[:-RECENT_MOVES]
            if a.assigned is not None and a.assigned[0] == j:
                a.assigned = None   # there: the assignment is done
        if j == m.exit:
            a.reached = True
            if self.exit_by is None:
                self.exit_by = a.id
        return self._view(a)

    def _is_frontier(self, i: int, known=None) -> bool:
        known = self.seen if known is None else known
        return any(not known[j] for _, j in self.maze.neighbours(i))

    def _next_edge(self, a: _Agent) -> dict:
        """Nearest explored cell with an open side into unexplored maze, skipping cells another agent claimed.

        Claiming is what keeps two agents from walking to the same dead end. If every frontier is claimed the
        nearest one is handed out anyway (idle is worse than duplicate work), and that is recorded as a claim too."""
        if not self.shared and a.team is None:
            return self._error(a, "own_map")
        m = self.maze
        known = self.seen if a.team is None else self.teams[a.team]   # a team explores and claims within its own map
        a.claim = None
        taken = {o.claim for o in self.agents.values() if o.claim is not None and o.team == a.team}
        prev = {a.pos: a.pos}
        queue = [a.pos]
        target = fallback = None
        for i in queue:
            if i != a.pos and self._is_frontier(i, known):
                if i not in taken:
                    target = i
                    break
                if fallback is None:
                    fallback = i
            for _, j in m.neighbours(i):
                if j not in prev and known[j]:
                    prev[j] = i
                    queue.append(j)
        if target is None:
            target = fallback
        if target is None:
            return {"path": None, "done": self.done(a)}
        a.claim = target
        cells = [target]
        while cells[-1] != a.pos:
            cells.append(prev[cells[-1]])
        cells.reverse()
        names = {-m.cols: "N", 1: "E", m.cols: "S", -1: "W"}
        path = [names[j - i] for i, j in zip(cells, cells[1:])]
        return {"path": path, "to": [target % m.cols, target // m.cols], "done": self.done(a)}

    def _cells(self, known) -> list:
        m = self.maze
        cells = []
        for i in range(len(known)):
            if known[i]:
                sides = "".join(name for name, bit, _, _ in DIRS if not m.walls[i] & bit)
                cells.append([i % m.cols, i // m.cols, sides])
        return cells

    def _map(self, a: _Agent) -> dict:
        m = self.maze
        claims = []
        if self.shared or a.team is not None:
            claims = [[o.claim % m.cols, o.claim // m.cols, o.id] for o in self.agents.values()
                      if o.claim is not None and o.team == a.team]
        return {"at": [a.pos % m.cols, a.pos // m.cols], "cells": self._cells(self._known(a)), "claims": claims,
                "done": self.done(a)}

    def _player(self, a: _Agent) -> dict:
        """The ally's view of its player: where they are, their last moves, and the team map."""
        m = self.maze
        p = next((o for o in self.agents.values() if o.team == a.team and o.role == "player"), None)
        if p is None:
            return self._error(a, "no_player")
        return {"at": [p.pos % m.cols, p.pos // m.cols], "recent": list(p.recent), "moves": p.calls,
                "cells": self._cells(self.teams[a.team]), "done": self.done(a)}

    def _rivals(self, a: _Agent) -> dict:
        """Every agent of another team: distance bucket and cells mapped. Never a position or a route."""
        return {"rivals": [{"who": o.id, **self.progress(o.id, calls=False)} for o in self.agents.values()
                           if o.team is not None and o.team != a.team], "done": self.done(a)}

    def _distances(self) -> list[int]:
        """Steps from every cell to the exit (the maze is connected, so every cell has one). Computed once."""
        if self._dist is None:
            m = self.maze
            dist = [-1] * (m.cols * m.rows)
            dist[m.exit] = 0
            queue = [m.exit]
            for i in queue:
                for _, j in m.neighbours(i):
                    if dist[j] < 0:
                        dist[j] = dist[i] + 1
                        queue.append(j)
            self._dist = dist
        return self._dist

    def bucket(self, agent_id: str) -> str:
        """near | mid | far: the agent's true distance to the exit as a third of the start's. Coarse on purpose."""
        a = self.agents[agent_id]
        dist = self._distances()
        whole = max(1, dist[self.maze.start])
        share = dist[a.pos] / whole
        return "near" if share <= 1 / 3 else "mid" if share <= 2 / 3 else "far"

    def progress(self, agent_id: str, calls: bool = True) -> dict:
        """What a rival's progress shows: cells it mapped first for its team, its distance bucket, and (for the
        player's screen) how many calls it made."""
        a = self.agents[agent_id]
        out = {"bucket": self.bucket(agent_id), "mapped": a.team_firsts}
        if calls:
            out["calls"] = a.calls
            out["notes"] = a.notes
        return out

    def team_cells(self, team: str) -> list[list[int]]:
        """A team's map as [cell index, wall bits] pairs (the play API's form)."""
        known = self.teams[team]
        return [[i, self.maze.walls[i]] for i in range(len(known)) if known[i]]

    def repeat_pct(self) -> float:
        return 100 * max(0, self.calls - self.unique) / self.calls if self.calls else 0.0

    def credit(self) -> dict[str, int]:
        """Route cells credited to whoever entered them first. Empty until somebody reaches the exit."""
        if self.exit_by is None:
            return {}
        ids = {a.index: a.id for a in self.agents.values()}
        out: dict[str, int] = {}
        for cell in self.maze.shortest_route():
            agent = ids.get(self.by[cell])
            if agent is not None:
                out[agent] = out.get(agent, 0) + 1
        return out


# ---- the `./maze` client ----

CLI_SOURCE = '''#!/usr/bin/env python3
"""Maze client. One command is one call: it prints the service's JSON answer on one line."""
import http.client
import json
import socket
import sys

USAGE = "usage: ./maze look | move <N|E|S|W> | next-edge | map | verdict"
OPS = {"look": 0, "move": 1, "next-edge": 0, "map": 0, "verdict": 0}


class UnixConnection(http.client.HTTPConnection):
    """HTTP over the service's Unix socket: the service is on this machine and is never reached through the network."""

    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def main(argv):
    if not argv or argv[0] not in OPS or len(argv) - 1 != OPS[argv[0]]:
        print(json.dumps({"error": USAGE}))
        return 2
    try:
        with open(".maze.json") as f:
            conn = json.load(f)
        body = json.dumps({"token": conn["token"], "op": argv[0], "arg": argv[1] if len(argv) > 1 else None})
        client = UnixConnection(conn["socket"])
        try:
            client.request("POST", "/call", body=body.encode(), headers={"Content-Type": "application/json"})
            response = client.getresponse()
            print(response.read().decode().strip())
            return 0 if response.status == 200 else 1
        finally:
            client.close()
    except Exception as err:
        print(json.dumps({"error": "transport: " + str(err)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def live_client(ops: dict, usage: str) -> str:
    """The `./maze` client for a live round's Ante: CLI_SOURCE (the eval's, which stays byte for byte as it is) with
    its own ops. An op whose count is "text" takes the rest of the line as one argument (`./maze note dead end at
    3,4`); every other op keeps the eval's exact argument count."""
    src = CLI_SOURCE
    swaps = (
        ('USAGE = "usage: ./maze look | move <N|E|S|W> | next-edge | map | verdict"', f"USAGE = {json.dumps(usage)}"),
        ('OPS = {"look": 0, "move": 1, "next-edge": 0, "map": 0, "verdict": 0}', f"OPS = {json.dumps(ops)}"),
        ("if not argv or argv[0] not in OPS or len(argv) - 1 != OPS[argv[0]]:",
         'if not argv or argv[0] not in OPS or (len(argv) < 2 if OPS[argv[0]] == "text" '
         "else len(argv) - 1 != OPS[argv[0]]):"),
        ('"arg": argv[1] if len(argv) > 1 else None', '"arg": " ".join(argv[1:]) if len(argv) > 1 else None'),
    )
    for old, new in swaps:
        if src.count(old) != 1:
            raise RuntimeError("maze client source changed: the live client cannot be built from it")
        src = src.replace(old, new)
    return src


# ---- the service ----

ARG_LOG_CAP = 64      # characters of an op or arg kept in the log; the rest is cut and the cut is marked
MAX_BODY = 4096       # bytes; a `./maze` request is under 200
BIND_WAIT_S = 5.0     # a call that arrives before the runner has bound the agent's pid waits this long, then is refused
MAX_ANCESTRY = 64     # parent hops walked from the peer before giving up


class PeerUnavailable(RuntimeError):
    """The peer pid of a Unix socket, or a process's parent, cannot be read on this platform."""


def peer_pid(sock: socket.socket) -> int:
    """The pid of the process on the other end of a connected Unix socket. Raises PeerUnavailable where unknown."""
    if sys.platform.startswith("linux"):
        pid, _, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return pid
    if sys.platform == "darwin":
        return struct.unpack("i", sock.getsockopt(0, 2, 4))[0]   # SOL_LOCAL = 0, LOCAL_PEERPID = 2
    raise PeerUnavailable(f"no peer pid lookup for Unix sockets on {sys.platform}")


_libproc = None


def _darwin_ppid(pid: int) -> int | None:
    """proc_pidinfo(PROC_PIDTBSDINFO): struct proc_bsdinfo has pbi_ppid at byte 16 of 136. Falls back to ps."""
    global _libproc
    try:
        if _libproc is None:
            import ctypes
            import ctypes.util
            _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
        import ctypes
        buf = ctypes.create_string_buffer(136)
        if _libproc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0), buf, ctypes.c_int(136)) == 136:
            return struct.unpack_from("I", buf.raw, 16)[0]
        return None   # no such process
    except (OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip()) if out.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def parent_pid(pid: int) -> int | None:
    """The parent of pid, or None when the process is gone. Raises PeerUnavailable on other platforms."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        return int(stat.rsplit(")", 1)[1].split()[1])   # the name in parentheses may hold spaces
    if sys.platform == "darwin":
        return _darwin_ppid(pid)
    raise PeerUnavailable(f"no parent pid lookup on {sys.platform}")


def descends_from(pid: int, root: int) -> bool:
    """True when pid is root or one of its descendants. A broken chain (an exited parent, an orphan reparented to
    init) is False: the check fails closed."""
    for _ in range(MAX_ANCESTRY):
        if pid == root:
            return True
        if pid <= 1:
            return False
        parent = parent_pid(pid)
        if parent is None or parent == pid:
            return False
        pid = parent
    return False


def peer_check_problem() -> str | None:
    """Why the service cannot tie tokens to processes here, or None. Checked on our own pid at start."""
    try:
        if parent_pid(os.getpid()) != os.getppid():
            return "parent pid lookup gives a wrong answer for this process"
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            if peer_pid(left) != os.getpid():
                return "peer pid lookup gives a wrong answer for this process"
    except (PeerUnavailable, OSError, ValueError, struct.error) as error:
        return f"peer pid check unavailable: {error}"
    return None


def _cap(value):
    """(value as logged, original length or None). Strings and other values longer than ARG_LOG_CAP are cut."""
    if value is None or isinstance(value, (bool, int, float)):
        return value, None
    text = value if isinstance(value, str) else json.dumps(value)
    if len(text) <= ARG_LOG_CAP:
        return value, None
    return text[:ARG_LOG_CAP], len(text)


class MazeService:
    """One maze for one cell. Holds the maze in memory, answers `./maze` calls over a Unix socket, logs every one.

    The log stays in memory while the cell runs and is written at stop(): a file flushed per call would show every
    agent's live position to any agent that reads it. Each token is bound to an agent's root pid (bind()); a
    request is honoured only when the socket's peer descends from that pid."""

    def __init__(self, maze: Maze, params: dict, log_path: Path, seed_info: dict | None = None,
                 socket_dir: Path | None = None, on_call=None):
        self.maze = maze
        # Live rounds: called as on_call(agent_id, op, arg, result, model) after every honoured call, with the lock
        # held (so events come out in call order). It must be quick and must not call back into the service.
        self.on_call = on_call
        self.params = validate_params(params)
        self.log_path = Path(log_path)
        self.seed_info = seed_info
        self._socket_dir = socket_dir
        self._model = MazeModel(maze, self.params)
        self._lock = threading.Lock()
        self._bound = threading.Condition(self._lock)
        self._tokens: dict[str, str] = {}
        self._roots: dict[str, int] = {}
        self._indexes: set[int] = set()
        self._server = None
        self._thread: threading.Thread | None = None
        self._dir: Path | None = None
        self.socket_path: Path | None = None
        self._rows: list[dict] = []
        self._t0 = 0.0
        self._exit_t: float | None = None
        self._forbidden = 0
        self._stolen = 0
        self._stopped = False
        self._written = False

    def start(self) -> str:
        """Bind the socket and serve. Returns the socket path. Nothing is opened or created before the checks pass,
        and a failed bind leaves nothing behind."""
        problem = peer_check_problem()
        if problem:
            raise PeerUnavailable(f"cannot tie tokens to agent processes here: {problem}")
        base = self._socket_dir or tempfile.gettempdir()
        if len(str(base)) > 60:
            base = "/tmp"   # sun_path is 104 bytes on macOS, 108 on Linux
        self._dir = Path(tempfile.mkdtemp(prefix="swarm-maze-", dir=base))   # mode 0700
        self.socket_path = self._dir / "maze.sock"
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    # Never read: a negative length would block the read, a huge one would hold memory.
                    self.close_connection = True
                    return self._send(400, {"error": "bad_length"})
                data = self.rfile.read(length)   # read before any answer, so a well-formed client is never cut off
                if self.path != "/call":
                    return self._send(404, {"error": "not_found"})
                try:
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                except ValueError:
                    return self._send(400, {"error": "bad_request"})
                try:
                    peer = peer_pid(self.request)
                except (PeerUnavailable, OSError, struct.error):
                    peer = None
                self._send(*service._handle(body, peer))

            def _send(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            request_queue_size = 256   # a 256-agent cell connects in bursts

            def handle_error(self, request, client_address):
                if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                    super().handle_error(request, client_address)   # a client that hung up early is not news

        try:
            self._server = Server(str(self.socket_path), Handler)
        except BaseException:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._server.serve_forever, name="maze-service", daemon=True)
        self._thread.start()
        return str(self.socket_path)

    def register(self, agent_id: str, index: int, pid: int | None = None, team: str | None = None,
                 role: str | None = None) -> str:
        """A token for a new agent. Its calls are honoured once bind() names the agent's root pid (or `pid` here).
        `team` and `role` are for live rounds (see MazeModel); evals leave them out."""
        with self._lock:
            if index in self._indexes:
                raise ValueError(f"agent index {index} already registered")
            if pid is not None and (not isinstance(pid, int) or pid <= 1):
                raise ValueError(f"cannot bind a token to pid {pid!r}: every process descends from it")
            self._model.register(agent_id, index, team, role)
            self._indexes.add(index)
            token = secrets.token_hex(16)
            self._tokens[token] = agent_id
            if pid is not None:
                self._roots[agent_id] = pid
                self._bound.notify_all()
            return token

    def bind(self, agent_id: str, pid: int) -> None:
        """Honour agent_id's token only from pid and its descendants. Binding again moves the token: the runner binds
        the agent's process at launch and the checker's process for the verdict."""
        with self._lock:
            if agent_id not in self._model.agents:
                raise KeyError(agent_id)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                raise ValueError(f"cannot bind a token to pid {pid!r}: every process descends from it")
            self._roots[agent_id] = pid
            self._bound.notify_all()

    def _root_for(self, agent_id: str) -> int | None:
        with self._lock:
            self._bound.wait_for(lambda: agent_id in self._roots or self._stopped, timeout=BIND_WAIT_S)
            return self._roots.get(agent_id)

    def _handle(self, body: dict, peer: int | None = None) -> tuple[int, dict]:
        token, op, arg = body.get("token"), body.get("op"), body.get("arg")
        agent_id = self._tokens.get(token) if isinstance(token, str) else None
        refusal = None
        if agent_id is not None:
            # The ancestry walk reads /proc or libproc: outside the lock, so one slow lookup does not stall the cell.
            root = self._root_for(agent_id)
            if root is None:
                refusal = "unbound"
            elif peer is None:
                refusal = "peer_unknown"
            elif not descends_from(peer, root):
                refusal = "stolen_token"
        with self._lock:
            now = round(time.monotonic() - self._t0, 4)
            if agent_id is None or refusal:
                logged_op, op_len = _cap(op if isinstance(op, str) else None)
                row = {"t": now, "agent": None, "op": logged_op, "arg": None, "at": None, "ok": False,
                       "error": refusal or "forbidden"}
                if op_len:
                    row["op_len"] = op_len
                if refusal:
                    # The owner's counters are untouched: a stolen token costs its owner nothing.
                    self._stolen += 1
                    row.update(owner=agent_id, peer=peer)
                else:
                    self._forbidden += 1
                self._write(row)
                return 403, {"error": "forbidden", "done": False}
            return 200, self._call_locked(agent_id, op, arg, now)

    def _call_locked(self, agent_id: str, op, arg, now: float) -> dict:
        """One honoured call, with the lock held: the model, the log row, then on_call."""
        result = self._model.call(agent_id, op if isinstance(op, str) else "", arg)
        if self._exit_t is None and self._model.exit_by is not None:
            self._exit_t = now
        a = self._model.agents[agent_id]
        logged_op, op_len = _cap(op)
        logged_arg, arg_len = _cap(arg)
        row = {"t": now, "agent": agent_id, "op": logged_op, "arg": logged_arg,
               "at": [a.pos % self.maze.cols, a.pos // self.maze.cols], "ok": "error" not in result}
        if op_len:
            row["op_len"] = op_len   # the op was cut to ARG_LOG_CAP characters; this is its full length
        if arg_len:
            row["arg_len"] = arg_len
        if "error" in result:
            row["error"] = result["error"]
        self._write(row)
        if self.on_call is not None:
            self.on_call(agent_id, op, arg, result, self._model)
        return result

    def call_as(self, agent_id: str, op: str, arg=None) -> dict:
        """A call made for an agent by the server itself (the human player of a live round, whose requests the play
        API authenticates). No token and no peer check: never reachable through the socket."""
        with self._lock:
            if self._stopped:
                return {"error": "stopped", "done": True}
            return self._call_locked(agent_id, op, arg, round(time.monotonic() - self._t0, 4))

    def read(self, fn):
        """fn(model) with the lock held: a consistent snapshot for the play API. fn must not call the service."""
        with self._lock:
            return fn(self._model)

    def _write(self, row: dict) -> None:
        """Called with the lock held. Memory only: the file is written once, at stop()."""
        self._rows.append(row)

    def write_log(self) -> None:
        """Write the call log and the summary. Once; stop() calls it, and so may a runner cleaning up after a failure."""
        with self._lock:
            if self._written:
                return
            self._written = True
            rows = list(self._rows)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.log_path.with_suffix(".json").write_text(json.dumps(self.summary(), indent=1) + "\n")

    def verdict(self, agent_id: str) -> dict:
        with self._lock:
            return self._model.verdict(agent_id)

    def summary(self) -> dict:
        with self._lock:
            m = self._model
            return {
                "params": self.params,
                "cells": self.maze.cols * self.maze.rows,
                "shortest": len(self.maze.shortest_route()),
                "exit": None if m.exit_by is None else {"t": self._exit_t, "agent": m.exit_by},
                "calls": m.calls,
                "unique_cells": m.unique,
                "repeat_pct": m.repeat_pct(),
                "route": list(self.maze.shortest_route()),
                "credit": m.credit(),
                "per_agent": {a.id: {"calls": a.calls, "firsts": a.firsts, "refused": a.refused}
                              for a in m.agents.values()},
                "forbidden": self._forbidden,
                "stolen": self._stolen,
                **({"seed": self.seed_info} if self.seed_info else {}),
            }

    @property
    def running(self) -> bool:
        return self._server is not None and not self._stopped

    def stop(self) -> None:
        """Stop serving, remove the socket and its directory, then write the log and the summary. Idempotent."""
        if self._stopped or self._server is None:
            return
        with self._lock:
            self._stopped = True
            self._bound.notify_all()
        try:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
        finally:
            if self._dir is not None:
                shutil.rmtree(self._dir, ignore_errors=True)
            self.write_log()


# ---- the scripted explorer ----

class Explorer:
    """The prototype's rule as a pure policy: result in, next command out. No clocks, no I/O.

    Look first. Done: stop. Somewhere new beside me: take one of them, chosen by a seeded PRNG. Nothing new:
    alone, walk back along my own steps; with a shared map, follow a path from next-edge, and ask for one when
    I have none. A refused call ends the run: a scripted agent that bumps a wall is a bug, not a strategy."""

    def __init__(self, seed: int, index: int, shared: bool):
        self._rand = mulberry32((seed * 31 + index * 7919 + 1) & MASK32)
        self.shared = shared
        self._stack: list[str] = []     # my forward moves, for backtracking on my own map
        self._pending: list[str] = []   # remaining moves of the path next-edge gave me

    @staticmethod
    def _bash(command: str) -> dict:
        return {"op": "bash", "command": "./maze " + command}

    @staticmethod
    def _final(text: str) -> dict:
        return {"op": "final", "text": text}

    def next(self, last_output: str | None) -> dict:
        if last_output is None:
            return self._bash("look")
        try:
            result = json.loads(last_output)
        except ValueError:
            return self._final("unreadable maze output")
        if not isinstance(result, dict):
            return self._final("unreadable maze output")
        if "error" in result:
            return self._final(f"error: {result['error']}")
        if result.get("done"):
            return self._final("done: the exit has been reached")
        if "path" in result:
            if result["path"] is None:
                return self._final("no open edge left")
            self._pending = list(result["path"])
            return self._bash(f"move {self._pending.pop(0)}") if self._pending else self._bash("look")
        new = result.get("new") or []
        if new:
            self._pending = []
            direction = new[int(self._rand() * len(new))]
            self._stack.append(direction)
            return self._bash(f"move {direction}")
        if not self.shared:
            if not self._stack:
                return self._final("nothing left to explore")
            return self._bash(f"move {OPPOSITE[self._stack.pop()]}")
        if self._pending:
            return self._bash(f"move {self._pending.pop(0)}")
        return self._bash("next-edge")


# ---- lockstep simulation ----

def parse_command(command: str) -> tuple[str, str | None]:
    parts = command.split()
    if len(parts) < 2 or parts[0] != "./maze":
        raise ValueError(f"not a maze command: {command!r}")
    return parts[1], parts[2] if len(parts) > 2 else None


def simulate(params: dict, agents: int) -> dict:
    """Every tick, each started agent makes exactly one call, in index order; agent k starts at tick k // 4.

    Stops at the call that reaches the exit, mid-tick if need be, which is what a wall-clock race would show
    to the first agent there. Returns plain numbers only, so the browser's `simulate` can be compared to it."""
    p = validate_params(params)
    maze = generate(p)
    model = MazeModel(maze, p)
    explorers = []
    for k in range(agents):
        model.register(str(k), k)
        explorers.append(Explorer(p["seed"], k, model.shared))
    last: list[str | None] = [None] * agents
    finished = [False] * agents
    tick = 0
    exit_tick = exit_agent = None
    while exit_tick is None and not all(finished):
        for k in range(agents):
            if finished[k] or tick < k // 4:
                continue
            step = explorers[k].next(last[k])
            if step["op"] == "final":
                finished[k] = True
                continue
            op, arg = parse_command(step["command"])
            last[k] = json.dumps(model.call(str(k), op, arg))
            if model.exit_by is not None:
                exit_tick, exit_agent = tick + 1, k
                break
        tick += 1
    return {
        "exit_tick": exit_tick,
        "exit_agent": exit_agent,
        "calls": model.calls,
        "unique_cells": model.unique,
        "repeat_pct": model.repeat_pct(),
        "credit": {int(k): v for k, v in model.credit().items()},
        "per_agent_calls": [model.agents[str(k)].calls for k in range(agents)],
    }


# ---- fleet modes: a desktop agent client's orchestration shapes on the same maze ----
#
# The fleet modes of a desktop multi-agent client, reduced to the
# maze so they can be compared tick for tick with the peer swarm:
#
#   single    one Ante: `simulate(params, 1)`, with the task's map setting as given.
#   swarm     N peers on one shared map, coordinating through next-edge claims: `simulate` on a shared map.
#   parallel  N independent Antes, each on its own map, at most PARALLEL_CONCURRENCY running at once; keep the
#             first to reach the exit and cancel the rest.
#   delegate  a lead plus W workers on one shared map. Workers never call next-edge: they wait, and the lead hands
#             out at most one assignment per tick (its own call, logged as op "assign") to the lowest waiting index.
#   council   N members, each on its own map with a per-member call cap; all of them run until final or out of
#             calls, then one synthesizer call joins their maps and routes start -> exit over known cells only.
#
# Every mode returns the same keys, None where a key does not apply. `log`, when given, collects one row per event:
# [tick, who, op, arg] with tick counted from 0, who an agent index, "lead" or "synth", and op a maze op, "assign",
# "synthesize", or one of the lifecycle events "start", "final" (arg "budget" when a council member ran out of
# calls) and "cancel" (parallel runs still going when the winner exits).

FLEET_MODES = ("single", "swarm", "parallel", "delegate", "council")
FLEET_COUNTS = {"single": (1, 1), "swarm": (1, 256), "parallel": (2, 6), "delegate": (1, 6), "council": (2, 6)}
PARALLEL_CONCURRENCY = 3


def _turn(model: MazeModel, explorer: Explorer, k: int, last: list, tick: int, log: list,
          hold_next_edge: bool = False) -> str:
    """One turn of agent k: ask its explorer, make the call. Returns "final", "call", or "wait" when the explorer
    asked for next-edge and `hold_next_edge` keeps that call back (delegate workers)."""
    step = explorer.next(last[k])
    if step["op"] == "final":
        log.append([tick, k, "final", None])
        return "final"
    op, arg = parse_command(step["command"])
    if hold_next_edge and op == "next-edge":
        return "wait"
    log.append([tick, k, op, arg])
    last[k] = json.dumps(model.call(str(k), op, arg))
    return "call"


def _known_route(maze: Maze, known) -> list[int] | None:
    """Shortest start -> exit cells moving only through open sides between known cells, or None."""
    if not known[maze.start] or not known[maze.exit]:
        return None
    prev = {maze.start: maze.start}
    queue = [maze.start]
    for i in queue:
        if i == maze.exit:
            break
        for _, j in maze.neighbours(i):
            if known[j] and j not in prev:
                prev[j] = i
                queue.append(j)
    if maze.exit not in prev:
        return None
    route = [maze.exit]
    while route[-1] != maze.start:
        route.append(prev[route[-1]])
    return route[::-1]


def _route_is_legal(maze: Maze, route) -> bool:
    """Checked against the walls, not against how the route was found: start to exit, one open side per step."""
    if not route or route[0] != maze.start or route[-1] != maze.exit:
        return False
    return all(any(j == b for _, j in maze.neighbours(a)) for a, b in zip(route, route[1:]))


def _fleet_lockstep(model: MazeModel, p: dict, count: int, log: list, out: dict) -> None:
    """single and swarm: exactly `simulate`'s loop, with the log added."""
    explorers = []
    for k in range(count):
        model.register(str(k), k)
        explorers.append(Explorer(p["seed"], k, model.shared))
    last: list[str | None] = [None] * count
    finished = [False] * count
    started = [False] * count
    tick = 0
    while out["exit_tick"] is None and not all(finished):
        for k in range(count):
            if finished[k] or tick < k // 4:
                continue
            if not started[k]:
                started[k] = True
                log.append([tick, k, "start", None])
            if _turn(model, explorers[k], k, last, tick, log) == "final":
                finished[k] = True
                continue
            if model.exit_by is not None:
                out["exit_tick"], out["exit_agent"] = tick + 1, k
                break
        tick += 1
    out["ticks"] = tick


def _fleet_parallel(model: MazeModel, p: dict, count: int, log: list, out: dict) -> None:
    """Independent runs, own maps, PARALLEL_CONCURRENCY at a time, first exit wins.

    A slot frees when a run returns final, and the lowest-index run not started yet takes it at the next tick.
    In a race an own-map explorer only returns final at the exit (or on a limit), so with concurrency 3 runs
    3..5 can only start after one has finished, which is after the winner: they never start. That is the expected
    finding (extra runs past the concurrency cap buy nothing in a race), not a bug."""
    explorers = []
    for k in range(count):
        model.register(str(k), k)
        explorers.append(Explorer(p["seed"], k, False))
    last: list[str | None] = [None] * count
    started = [False] * count
    running = [False] * count
    tick = 0
    while out["exit_tick"] is None:
        for k in range(count):
            if sum(running) >= PARALLEL_CONCURRENCY:
                break
            if not started[k]:
                started[k] = running[k] = True
                log.append([tick, k, "start", None])
        if not any(running):
            break
        for k in range(count):
            if not running[k]:
                continue
            if _turn(model, explorers[k], k, last, tick, log) == "final":
                running[k] = False
                continue
            if model.exit_by is not None:
                out["exit_tick"], out["exit_agent"] = tick + 1, k
                running[k] = False
                out["cancelled"] = [j for j in range(count) if running[j]]
                for j in out["cancelled"]:
                    log.append([tick, j, "cancel", None])
                break
        tick += 1
    out["ticks"] = tick
    if out["cancelled"] is None:
        out["cancelled"] = []
    out["concurrency"] = PARALLEL_CONCURRENCY


def _fleet_delegate(model: MazeModel, p: dict, count: int, log: list, out: dict) -> None:
    """A lead and `count` workers on one shared map; the lead is the only one who runs next-edge.

    Each tick the lead goes first and serves at most one waiting worker, the lowest index; then the workers take
    their turns in index order, the one just served included. A worker that would call next-edge makes no call
    and waits instead; every tick it spends waiting (the asking tick included) counts in wait_ticks. Uncontended,
    that is one tick per assignment, the same as the swarm's own next-edge call, so any extra is the bottleneck."""
    explorers = []
    for k in range(count):
        model.register(str(k), k)
        explorers.append(Explorer(p["seed"], k, True))
    model.register("lead", count)
    last: list[str | None] = [None] * count
    finished = [False] * count
    started = [False] * count
    waiting = [False] * count
    wait_ticks = 0
    log.append([0, "lead", "start", None])
    tick = 0
    while out["exit_tick"] is None and not all(finished):
        k = next((k for k in range(count) if waiting[k]), None)
        if k is not None:
            log.append([tick, "lead", "assign", k])
            last[k] = json.dumps(model.assign("lead", str(k)))
            waiting[k] = False
        for k in range(count):
            if finished[k] or tick < k // 4:
                continue
            if not started[k]:
                started[k] = True
                log.append([tick, k, "start", None])
            if waiting[k]:
                wait_ticks += 1
                continue
            done = _turn(model, explorers[k], k, last, tick, log, hold_next_edge=True)
            if done == "final":
                finished[k] = True
                continue
            if done == "wait":
                waiting[k] = True
                wait_ticks += 1
                continue
            if model.exit_by is not None:
                out["exit_tick"], out["exit_agent"] = tick + 1, k
                break
        tick += 1
    out["ticks"] = tick
    out["lead_calls"] = model.agents["lead"].calls
    out["wait_ticks"] = wait_ticks


def _fleet_council(model: MazeModel, p: dict, count: int, member_budget: int | None, log: list, out: dict) -> None:
    """Members on their own maps, all from tick 0, until each is final or has made `member_budget` calls (reaching
    the exit does not stop the others). Then one synthesizer call at the next tick: the union of what they know,
    routed start -> exit. Credit goes to the member whose move first entered each route cell; within a tick
    members move in index order, so a tie goes to the lowest index, and the start cell is member 0's."""
    maze = model.maze
    explorers = []
    for k in range(count):
        model.register(str(k), k)
        explorers.append(Explorer(p["seed"], k, False))
    last: list[str | None] = [None] * count
    finished = [False] * count
    for k in range(count):
        log.append([0, k, "start", None])
    tick = 0
    while not all(finished):
        for k in range(count):
            if finished[k]:
                continue
            if member_budget is not None and model.agents[str(k)].calls >= member_budget:
                finished[k] = True
                log.append([tick, k, "final", "budget"])
                continue
            if _turn(model, explorers[k], k, last, tick, log) == "final":
                finished[k] = True
        tick += 1
    # The loop's last tick is the one in which the final member returned final: it makes no call, so it is the
    # synthesizer's tick, not an extra one. Members' calls fill ticks 0..tick-2; synth runs at tick-1.
    tick -= 1
    # The synthesizer: one call, not subject to the maze's limits (it never touches the maze service).
    log.append([tick, "synth", "synthesize", None])
    union = bytearray(len(model.seen))
    member_lens = []
    for k in range(count):
        own = model.agents[str(k)].own
        for i in range(len(union)):
            union[i] |= own[i]
        r = _known_route(maze, own)
        if r is not None:
            member_lens.append(len(r))
    route = _known_route(maze, union)
    best = min(member_lens) if member_lens else None
    out["ticks"] = tick + 1
    out["extra_calls"] = 1
    out["route"] = route
    out["route_len"] = None if route is None else len(route)
    out["best_member_route_len"] = best
    out["union_shorter"] = route is not None and (best is None or len(route) < best)
    out["verified"] = _route_is_legal(maze, route)
    out["member_budget"] = member_budget
    if route is not None:
        out["exit_tick"] = tick + 1
        # Credit rule: the member whose move first entered the cell (model.by, set in tick order, index order
        # within a tick). Members have their own maps and only their own moves set `by`; a member's knowledge is
        # exactly the cells it entered, so every route cell was entered by a member who knows it. Nothing else
        # (no shared sighting, not the synthesizer) is credited.
        credit: dict[int, int] = {}
        for cell in route:
            if model.by[cell] >= 0:
                credit[model.by[cell]] = credit.get(model.by[cell], 0) + 1
        out["credit"] = credit


def simulate_fleet(params: dict, mode: str, count: int, member_budget: int | None = None, log: list | None = None) -> dict:
    """One fleet mode on the maze from `params`. The mode decides the map where it has to: swarm and delegate
    share one, parallel and council each have their own; single keeps the task's setting. Mirrors
    `simulateFleet` in front-maze/maze-core.js key for key."""
    if mode not in FLEET_MODES:
        raise ValueError(f"fleet mode must be one of {', '.join(FLEET_MODES)}, got {mode!r}")
    lo, hi = FLEET_COUNTS[mode]
    if not _is_int(count) or not lo <= count <= hi:
        raise ValueError(f"fleet {mode} count must be an integer in {lo}..{hi}, got {count!r}")
    if mode == "council":
        if member_budget is not None and (not _is_int(member_budget) or member_budget < 1):
            raise ValueError(f"council member_budget must be a positive integer or None, got {member_budget!r}")
    elif member_budget is not None:
        raise ValueError("member_budget applies to council only")
    p = validate_params(params)
    if mode in ("swarm", "delegate"):
        p["map"] = "shared"
    elif mode in ("parallel", "council"):
        p["map"] = "own"
    model = MazeModel(generate(p), p)
    log = [] if log is None else log
    out = {"mode": mode, "count": count, "exit_tick": None, "exit_agent": None, "ticks": 0, "credit": None,
           "lead_calls": None, "wait_ticks": None, "concurrency": None, "cancelled": None, "member_budget": None,
           "route": None, "route_len": None, "best_member_route_len": None, "union_shorter": None, "verified": None,
           "extra_calls": 0}
    if mode in ("single", "swarm"):
        _fleet_lockstep(model, p, count, log, out)
    elif mode == "parallel":
        _fleet_parallel(model, p, count, log, out)
    elif mode == "delegate":
        _fleet_delegate(model, p, count, log, out)
    else:
        _fleet_council(model, p, count, member_budget, log, out)
    # Totals cover every call: the lead's assignments are model calls already; the synthesizer's is added here.
    calls = model.calls + out.pop("extra_calls")
    out["calls"] = calls
    out["per_agent_calls"] = [model.agents[str(k)].calls for k in range(count)]
    out["unique_cells"] = model.unique
    out["repeat_pct"] = 100 * max(0, calls - model.unique) / calls if calls else 0.0
    if out["credit"] is None and mode == "parallel":
        # Independent attempts, best one kept: the route belongs to the winning run alone. `by` is global (first
        # entry by any run), so it would credit cancelled runs for cells they merely reached first.
        w = out["exit_agent"]
        out["credit"] = {} if w is None else {w: len(model.maze.shortest_route())}
    if out["credit"] is None:
        out["credit"] = {} if mode == "council" else {int(k): v for k, v in model.credit().items() if k != "lead"}
    return out
