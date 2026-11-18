"""Live maze: a human races real Ante processes through one maze, with an optional Ante teammate (docs/live.md).

Served by `swarm solo` under /api/play/. A round is one maze and two teams on one MazeModel: `player` (the human,
agent `you`, plus the ally Ante in assist mode) and `rival` (1..16 Antes, single or swarm). Fog of war is per team
(MazeModel.teams). Every Ante is an ordinary Swarm cell, as in the maze eval: its own sandbox (fresh HOME and workdir),
the eval's `./maze` client over the MazeService socket (token bound to the agent's process tree), the cell's cgroup
when limits are configured, and a model route on the relay (credential stripping, token budget) or, for a stand-in
round, the scripted model driving the maze Explorer. No model call is made here outside the relay.

What leaves the server during a round is what the player's team may know: the player team's moves and discoveries,
each rival's bucketed progress (distance bucket, cells mapped, calls) and the call feed (rival calls carry the op
only). The full maze, the paths and the seed come with `end`.

The passcode is never stored, logged or echoed: the server holds only its SHA-256 (from the environment), compares
in constant time and counts wrong tries per address. Round logs keep the seed hash, sizes, timings, call counts,
tokens and the winner; no player identity.
"""
from __future__ import annotations

import collections
import datetime
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

from . import harnesses, host, maze as mz
from .relay import RelayModel, RunBudget
from .sandbox import ProcessSandbox, clean_root, execute, reap_escaped, remove_tree, runner_tag, tagged_processes
from .scripted import ScriptedModel

SIZES = {"s": (16, 10), "m": (26, 16), "l": (40, 25)}   # the cartridge's own (front-solo/mazeplay.js)
LIVE_MODES = ("single", "swarm")
# How a live swarm plays (owner, 2026-11-17: a game's swarm plays together, sharing what it learns, wherever the work
# can be split; the maze's style is "shared"). `lead`: one Ante leads (`assign`, `team`) and the rest are workers who
# follow its assignments and report by notes; `shared`: peers on one map coordinating through next-edge claims and
# notes only. A lead needs a team of at least LEAD_MIN Antes; smaller swarms are `shared`.
SWARM_SHAPES = ("lead", "shared")
LEAD_MIN = 3
MAZE_SWARM = {"swarm_style": "shared", "sizes": [1, 4, 16], "shapes": list(SWARM_SHAPES)}
# Ante catalog `wire_style` per wire. AnthropicMessage was checked against Ante 0.3.5 on the Mac (2026-11-17): with
# base_url ending in /v1 it posts to <base>/messages; "Anthropic" is refused. Not checked on 0.3.0.
WIRE_STYLES = {"messages": "AnthropicMessage", "responses": "OpenAiResponse", "chat": "OpenAiCompatible"}
READER_GRACE_S = 30.0
PASS_TRIES, PASS_WINDOW_S, PASS_GLOBAL_TRIES = 5, 600.0, 30
MAX_BODY = 4096
MAX_READERS = 3
HEARTBEAT_S = 15.0
ROUND_ID = re.compile(r"^[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIRS = ("N", "E", "S", "W")
PROVIDERS = ("subscription", "relay", "standin")
# Ante keeps each OAuth sign-in as <ANTE_HOME>/auth/credentials/oauth/<profile>/<label>.json and, on a refresh, writes
# a new file and renames it over the old one (checked on Ante 0.3.5, 2026-11-17). Cells therefore share the one store
# by a symlink to this directory, never a copy and never a per-file symlink (a refresh replaces a file symlink with a
# private file and the shared grant is left holding a refresh token the issuer has already rotated).
SIGNIN_STORE = ("auth", "credentials", "oauth")       # + the provider's OAuth profile
# Ante's subscription providers and the OAuth profile each signs in with (Ante 0.3.5 built-in catalog). A sign-in is
# `<profile>/<label>`; a bare `<profile>` sign-in (no label) is not used: it lives in the oauth directory itself, and
# sharing that would hand every cell every sign-in Ante holds.
SUBSCRIPTION_PROFILES = {"anthropic-subscription": "anthropic", "openai-subscription": "openai"}
DEFAULT_MODEL = "claude-haiku-5-5"
HEADROOM_PCT = 95                 # a sign-in qualifies under this share of its session and of its weekly window
SIGNIN_CACHE_S = 180.0            # one `ante usage` read serves the rounds started within this long
LIMIT_COOLDOWN_S = 900.0          # a sign-in that hit its limit is skipped this long
SIGNIN_COOLDOWN_S = 300.0         # a sign-in Ante called expired or revoked is skipped this long
EXPIRY_LEAD_S = 30.0              # a grant this close to expiry: one Ante refreshes it before the others start
WEEK_S = 7 * 86400.0
# Ante's error kinds (`--output-format json` Error events, 0.3.5) and what they mean for a sign-in.
SIGNIN_KINDS = {"oauth", "auth", "authentication", "forbidden"}
LIMIT_KINDS = {"rate_limited", "quota"}

RIVAL_PROMPT = """You are inside a maze, racing to its exit. Reach the exit first.

You cannot see the maze. You explore it with one command, `./maze`, run from your working directory. Do not edit any
files; only run `./maze`. Every command prints one line of JSON.

  ./maze look           where you are, which sides are open, and which of those lead somewhere not yet explored
  ./maze move N|E|S|W   step through an open side; N is up (y - 1), E is right (x + 1)
  ./maze next-edge      a path (list of moves) to the nearest unexplored opening nobody on your team is heading for
  ./maze map            every cell your team has explored and its open sides

`look` and `move` answer with `{"at":[x,y],"open":[...],"new":[...],"exit":false,"steps":n,"done":false}`. `open`
lists the sides you can walk through, `new` the open sides into cells your team has not explored. Moving into a
wall is refused, so only move through sides listed in `open`.<TEAM>

Stop when `exit` or `done` is true, or when a command answers with an error. Then say in one line what happened."""

TEAM_COMMANDS = """
  ./maze note <text>    post one line (up to 120 characters) to your team's board
  ./maze notes          your team's board: the newest 20 notes"""

TEAM_PROMPT = """You are inside a maze with your team of <N> agents, racing a rival to its exit. The team wins when any one
of you reaches the exit, so split the maze between you instead of all walking the same corridors.

You cannot see the maze. You explore it with one command, `./maze`, run from your working directory. Do not edit any
files; only run `./maze`. Every command prints one line of JSON.

  ./maze look           where you are, which sides are open, which of those lead somewhere your team has not explored,
                        and your teammates' newest notes
  ./maze move N|E|S|W   step through an open side; N is up (y - 1), E is right (x + 1)
  ./maze next-edge      a path (list of moves) to the nearest unexplored opening no teammate holds; it is claimed
                        for you, so teammates are sent elsewhere
  ./maze map            every cell your team has explored and its open sides<COMMANDS>

`look` and `move` answer with `{"at":[x,y],"open":[...],"new":[...],"exit":false,"steps":n,"done":false}`. `open`
lists the sides you can walk through, `new` the open sides into cells nobody on your team has explored. Only move
through sides in `open`. A `move` answer also carries `notes` your teammates posted since you last saw the board.
<ROLE>
Notes are how the team shares what it learns. Post one when you hit a dead end ("dead end at 3,4: skip the west
branch") or find a promising corridor ("long corridor south from 7,2, still new"). Keep them short and do not post
every step. Read your teammates' notes and stay out of the places they have finished.

Stop when `exit` or `done` is true, then say in one line what happened. If a command answers with an error, read it
and carry on; stop only when the same command fails twice in a row."""

SHARED_ROLE = """
You are one of <N> peers. Claim different edges: when nothing near you is new, run `./maze next-edge` and follow its
path with `move`; its claim keeps you off the openings your teammates hold.
"""

LEAD_ROLE = """
You are the lead (<ME>); your workers are <WORKERS>. Keep every worker busy on a different part of the maze:

  ./maze team             each worker: where it is, its claim, its assignment, its last command, and `idle`
  ./maze assign <worker>  hand that worker the nearest unexplored opening no teammate holds; it follows the path

Work in rounds: run `./maze team`, `./maze assign` each worker whose `idle` is true, read the notes, then explore
yourself for a few moves (look, move, next-edge) and run `./maze team` again. Rebalance: a worker that reports a dead
end or shows `idle` gets a new assignment at once.
"""

WORKER_ROLE = """
You are a worker (<ME>); <LEAD> is your lead and hands out work with `./maze assign`. When a `look` or `move` answer
has `assigned`, follow its `path` with `move` (it is worked out from where you stand) and keep exploring past it
through `new` sides. Report by notes: post one at each dead end and at each promising corridor, so the lead can send
you or others elsewhere. With no assignment and nothing new beside you, post `idle at x,y` once, then use
`./maze next-edge` rather than wait.
"""

ALLY_PROMPT = """You are on a team with a human player, racing other agents to the exit of a maze. Help your player
win: explore the parts of the maze your player has not covered. Everything you discover appears on their screen.

You cannot see the maze. You explore it with one command, `./maze`, run from your working directory. Do not edit any
files; only run `./maze`. Every command prints one line of JSON.

  ./maze look           where you are, which sides are open, and which of those lead somewhere your team has not explored
  ./maze move N|E|S|W   step through an open side; N is up (y - 1), E is right (x + 1)
  ./maze next-edge      a path (list of moves) to the nearest opening your team has not explored, claimed for you
  ./maze map            every cell your team has explored and its open sides
  ./maze player         your player's position, their last moves, and the team map
  ./maze rivals         how far each rival is from the exit (far, mid, near) and how many cells each has mapped
  ./maze note <text>    post one line to your team's board; your player sees it on their screen

Check `./maze player` now and then, and head away from where your player is going. When you hit a dead end or find a
promising corridor, post a short note so your player knows ("dead end east of 5,3", "long corridor south at 9,8").
Only move through sides listed in `open`. Stop when `exit` or `done` is true, or when a command answers with an
error. Then say in one line what happened."""


def rival_prompt(mode: str, count: int, role: str = "rival", who: str | None = None, names=()) -> str:
    """A rival Ante's task. A single Ante gets the plain race; a swarm member gets the team prompt for its role
    (`rival` = a peer of the shared shape, `lead`, `worker`). `names` are the team's agents, the lead first."""
    if mode != "swarm" or count < 2:
        return RIVAL_PROMPT.replace("<TEAM>", "")
    if role == "lead":
        workers = [n for n in names if n != who]
        part = LEAD_ROLE.replace("<ME>", who or "a01").replace("<WORKERS>", ", ".join(workers) or "your teammates")
    elif role == "worker":
        part = WORKER_ROLE.replace("<ME>", who or "a worker").replace("<LEAD>", names[0] if names else "a01")
    else:
        part = SHARED_ROLE
    return TEAM_PROMPT.replace("<COMMANDS>", TEAM_COMMANDS).replace("<ROLE>", part).replace("<N>", str(count))


TEAM_OPS = {"look": 0, "move": 1, "next-edge": 0, "map": 0, "note": "text", "notes": 0}
CLIENT_OPS = {
    "rival": TEAM_OPS,
    "worker": TEAM_OPS,
    "lead": {**TEAM_OPS, "team": 0, "assign": 1},
    "ally": {"look": 0, "move": 1, "next-edge": 0, "map": 0, "player": 0, "rivals": 0, "note": "text", "notes": 0},
}


def role_client(role: str) -> str:
    """The `./maze` client of a live team member: the eval's client with its role's ops (maze.live_client)."""
    ops = CLIENT_OPS[role]
    words = {"move": "move <N|E|S|W>", "note": "note <text>", "assign": "assign <worker>"}
    return mz.live_client(ops, "usage: ./maze " + " | ".join(words.get(op, op) for op in ops))


def ally_client() -> str:
    """The ally's `./maze`: the eval's client with `player`, `rivals` and the team board. A single rival gets
    CLI_SOURCE unchanged; a swarm's members get role_client(role)."""
    return role_client("ally")


# ---- stand-in policies (scripted model; no model calls) ----

class AllyExplorer:
    """The stand-in ally: asks for its player and the rivals once, then explores like the eval's Explorer."""

    def __init__(self, seed: int, index: int):
        self.explorer = mz.Explorer(seed, index, True)
        self.n = 0

    def next(self, last_output):
        self.n += 1
        if self.n == 1:
            return {"op": "bash", "command": "./maze player"}
        if self.n == 2:
            return {"op": "bash", "command": "./maze rivals"}
        return self.explorer.next(None if self.n == 3 else last_output)


class TeamExplorer:
    """The stand-in team member (shared peer or worker): the eval's Explorer, which also follows an assignment from
    its lead when it has nothing new beside it, and posts a note at its first few dead ends."""

    NOTES = 3

    def __init__(self, seed: int, index: int):
        self.explorer = mz.Explorer(seed, index, True)
        self.notes = 0
        self.start: list | None = None

    def next(self, last_output):
        try:
            result = json.loads(last_output) if last_output else None
        except ValueError:
            result = None
        if isinstance(result, dict) and "open" in result and "error" not in result and not result.get("done"):
            self.start = self.start or result.get("at")
            assigned = result.get("assigned")
            if (not result.get("new") and not self.explorer._pending and isinstance(assigned, dict)
                    and isinstance(assigned.get("path"), list) and assigned["path"]):
                self.explorer._pending = list(assigned["path"])
            if (len(result.get("open") or ()) == 1 and not result.get("new") and result.get("at") != self.start
                    and self.notes < self.NOTES):
                self.notes += 1
                x, y = result["at"]
                return {"op": "bash", "command": f"./maze note dead end at {x},{y}"}
        return self.explorer.next(last_output)


class LeadExplorer:
    """The stand-in lead: `team`, then `assign` each idle worker, then a few steps of its own exploring, again."""

    STEPS = 4

    def __init__(self, seed: int, index: int):
        self.explorer = mz.Explorer(seed, index, True)
        self.todo: list[str] = []
        self.steps = 0
        self.resume = True

    def next(self, last_output):
        try:
            result = json.loads(last_output) if last_output else None
        except ValueError:
            result = None
        if isinstance(result, dict) and result.get("done"):
            return {"op": "final", "text": "done: the exit has been reached"}
        if isinstance(result, dict) and isinstance(result.get("team"), list):
            self.todo = [row["who"] for row in result["team"] if row.get("idle") and row.get("role") == "worker"]
        if self.todo:
            self.resume = True
            return {"op": "bash", "command": f"./maze assign {self.todo.pop(0)}"}
        if self.steps >= self.STEPS or last_output is None:
            self.steps = 0
            return {"op": "bash", "command": "./maze team"}
        self.steps += 1
        step = self.explorer.next(None if self.resume else last_output)
        self.resume = False
        if step["op"] == "final":   # nothing left for it to explore: keep leading
            self.steps = self.STEPS
            return {"op": "bash", "command": "./maze team"}
        return step


def standin_policy(role: str, seed: int, index: int, team: bool = False):
    """The scripted policy behind a stand-in Ante. `team`: a swarm member (notes, assignments)."""
    if role == "ally":
        return AllyExplorer(seed, index)
    if role == "lead":
        return LeadExplorer(seed, index)
    if role == "worker" or team:
        return TeamExplorer(seed, index)
    return mz.Explorer(seed, index, True)


# ---- budgets ----

class DailyBudget:
    """Input + output tokens across every round of one UTC day. Persisted (day, tokens) when a path is given."""

    def __init__(self, limit: int | None, path: Path | None = None):
        self.limit, self.path = limit, path
        self.lock = threading.Lock()
        self.day, self.used, self._saved = self._today(), 0, 0
        if path is not None:
            try:
                doc = json.loads(path.read_text())
                if doc.get("day") == self.day and isinstance(doc.get("tokens"), int):
                    self.used = self._saved = doc["tokens"]
            except (OSError, ValueError, AttributeError):
                pass

    @staticmethod
    def _today() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def _roll(self):
        today = self._today()
        if today != self.day:
            self.day, self.used = today, 0

    def exhausted(self) -> bool:
        with self.lock:
            self._roll()
            return self.limit is not None and self.used >= self.limit

    def add(self, tokens: int):
        with self.lock:
            self._roll()
            self.used += max(0, int(tokens or 0))

    def save(self):
        if self.path is None:
            return
        with self.lock:
            if self.used == self._saved:
                return
            doc = {"day": self.day, "tokens": self.used}
            self._saved = self.used
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(doc))
            os.replace(tmp, self.path)
        except OSError:
            pass


class RoundBudget(RunBudget):
    """The relay's run budget for one round, which also spends (and stops at) the day's."""

    def __init__(self, max_tokens: int | None, daily: DailyBudget):
        super().__init__(max_tokens)
        self.daily = daily

    def exhausted(self) -> bool:
        return super().exhausted() or self.daily.exhausted()

    def add(self, tokens: int):
        super().add(tokens)
        self.daily.add(tokens)


# ---- the Claude sign-in (subscription mode) ----

def _utc(text) -> datetime.datetime | None:
    """An ISO time with its zone (Ante's `usage --json` gives UTC with Z); None when absent or unreadable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        t = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def parse_resets(items) -> dict:
    """["work=2026-11-18T21:00:00Z", ...] -> {label: aware datetime}. A time without a zone is refused."""
    out = {}
    for item in items or ():
        label, _, when = str(item).partition("=")
        t = _utc(when)
        if not label.strip() or t is None:
            raise ValueError(f"sign-in reset must be LABEL=ISO-time-with-zone, got {item!r}")
        out[label.strip()] = t
    return out


def next_weekly(t: datetime.datetime, now: datetime.datetime) -> datetime.datetime:
    """A weekly reset given once, rolled forward to the next one at or after now."""
    while t < now:
        t += datetime.timedelta(seconds=WEEK_S)
    return t


class SigninPicker:
    """Which stored sign-in (`<profile>/<label>`, e.g. anthropic/work) a round runs on. Exactly one, never a list.

    `auto` reads `ante usage --json` and takes the sign-in whose weekly window resets soonest among those still under
    HEADROOM_PCT of both their session and weekly windows. When
    usage cannot be read (an older Ante, a network error) it falls back to the configured order and weekly reset
    times; it never fails open to a list. A fixed label is used alone (still checked against usage when readable).
    The read is cached for SIGNIN_CACHE_S and never made while a round runs: `ante usage` may refresh a grant, and a
    refresh racing the round's Antes could cost one of them its sign-in.
    """

    def __init__(self, ante: Path, ante_home: Path, signin: str = "auto", order=("work", "personal"),
                 resets: dict | None = None, cache_s: float = SIGNIN_CACHE_S, usage_runner=None,
                 profile: str = "anthropic"):
        self.ante, self.ante_home, self.profile = Path(ante), Path(ante_home), profile
        self.signin, self.order, self.resets = signin, list(order), dict(resets or {})
        self.cache_s = cache_s
        self.usage_runner = usage_runner or self._run_usage
        self.lock = threading.Lock()
        self.cached: tuple | None = None      # (monotonic time, label, reason, source)
        self.blocked: dict[str, tuple[float, str]] = {}   # label -> (monotonic until, "limit"|"signin")
        self._scratch: Path | None = None

    @property
    def rel(self) -> tuple:
        return (*SIGNIN_STORE, self.profile)

    @property
    def store(self) -> Path:
        return self.ante_home.joinpath(*self.rel)

    def stored(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.store.glob("*.json") if p.is_file())
        except OSError:
            return []

    def link_store(self, ante_home: Path) -> Path:
        """Point <ante_home>/auth/credentials/oauth/<profile> at the one real store. Returns the link."""
        link = Path(ante_home).joinpath(*self.rel)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.store, target_is_directory=True)
        return link

    def expires_in(self, label: str) -> float | None:
        """Seconds until the grant's access token expires. Only `expires_at` is read; no token leaves the file."""
        try:
            doc = json.loads((self.store / f"{label}.json").read_text())
        except (OSError, ValueError):
            return None
        t = _utc(doc.get("expires_at")) if isinstance(doc, dict) else None
        if t is None:
            return None
        return (t - datetime.datetime.now(datetime.timezone.utc)).total_seconds()

    def _run_usage(self):
        """`ante usage --json` in a scratch Ante home holding only a link to the sign-in store (the service's own
        home may be read-only). Returns the parsed document, or None. Its output (it names account emails) is
        neither logged nor kept beyond the labels, statuses and windows."""
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="swarm-play-usage-"))
            (self._scratch / ".ante").mkdir()
            (self._scratch / "tmp").mkdir()
            self.link_store(self._scratch / ".ante")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self._scratch), "TMPDIR": str(self._scratch / "tmp"),
               "ANTE_HOME": str(self._scratch / ".ante"), "ANTE_TELEMETRY": "off", "LANG": "C.UTF-8", "NO_COLOR": "1"}
        try:
            out = subprocess.run([str(self.ante), "usage", "--json"], env=env, cwd=self._scratch, timeout=30,
                                 stdin=subprocess.DEVNULL, capture_output=True)
            return json.loads(out.stdout) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def usage(self) -> dict | None:
        """{label: {"status", "session", "weekly", "weekly_reset"}} for this profile's labelled sign-ins, or None."""
        doc = self.usage_runner()
        subs = doc.get("subscriptions") if isinstance(doc, dict) else None
        if not isinstance(subs, list):
            return None
        out = {}
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            cred = sub.get("credential")
            if not isinstance(cred, str) or not cred.startswith(self.profile + "/"):
                continue
            row = {"status": sub.get("status"), "session": None, "weekly": None, "weekly_reset": None}
            for w in sub.get("windows") or []:
                name = str(w.get("label", "")).lower() if isinstance(w, dict) else ""
                pct = w.get("used_percent") if isinstance(w, dict) else None
                if "session" in name:
                    row["session"] = pct
                elif name.startswith("weekly"):
                    row["weekly"], row["weekly_reset"] = pct, _utc(w.get("resets_at"))
            out[cred.split("/", 1)[1]] = row
        return out

    def block(self, label: str, why: str, seconds: float):
        with self.lock:
            self.blocked[label] = (time.monotonic() + seconds, why)
            self.cached = None

    def invalidate(self):
        with self.lock:
            self.cached = None

    def pick(self, evaluate: bool = True) -> tuple[str | None, str | None, str]:
        """(label, reason, source). reason is `signin` or `limit` when no sign-in may be used; source is `usage`,
        `order` or `fixed`. With evaluate False only a cached answer is given (a round is running)."""
        with self.lock:
            now = time.monotonic()
            if self.cached is not None and (now - self.cached[0] < self.cache_s or not evaluate):
                return self.cached[1:]
            if not evaluate:
                return None, None, "pending"
            for k in [k for k, (until, _) in self.blocked.items() if until <= now]:
                del self.blocked[k]
            answer = self._evaluate()
            self.cached = (now, *answer)
            return answer

    def _evaluate(self):
        stored = set(self.stored())
        blocked = {k: why for k, (_, why) in self.blocked.items()}
        wanted = [self.signin] if self.signin != "auto" else None
        if not stored or (wanted and wanted[0] not in stored):
            return None, "signin", "fixed" if wanted else "order"
        report = self.usage()
        rank = {label: k for k, label in enumerate(self.order)}
        if report is not None:
            over = limited = False
            fits = []
            for label, row in report.items():
                if label not in stored or (wanted and label not in wanted):
                    continue
                if label in blocked:
                    limited = limited or blocked[label] == "limit"
                    continue
                if row["status"] != "ready":
                    continue
                if any(isinstance(row[k], (int, float)) and row[k] >= HEADROOM_PCT for k in ("session", "weekly")):
                    over = True
                    continue
                reset = row["weekly_reset"].timestamp() if row["weekly_reset"] else float("inf")
                fits.append((reset, rank.get(label, len(rank)), label))
            if fits:
                return min(fits)[2], None, "fixed" if wanted else "usage"
            if wanted and wanted[0] not in report and wanted[0] not in blocked:
                return wanted[0], None, "fixed"       # usage does not list it: the label alone decides
            return None, "limit" if (over or limited) else "signin", "fixed" if wanted else "usage"
        # Usage unreadable: the configured order, soonest configured weekly reset first. Never a list.
        now = datetime.datetime.now(datetime.timezone.utc)
        names = wanted or [label for label in self.order if label in stored]
        fits = []
        for label in names:
            if label in blocked:
                continue
            reset = self.resets.get(label)
            fits.append((next_weekly(reset, now).timestamp() if reset else float("inf"), rank.get(label, len(rank)), label))
        if fits:
            return min(fits)[2], None, "fixed" if wanted else "order"
        limited = any(blocked.get(label) == "limit" for label in names)
        return None, "limit" if limited else "signin", "fixed" if wanted else "order"

    def close(self):
        if self._scratch is not None:
            link = self._scratch.joinpath(".ante", *self.rel)
            try:
                if link.is_symlink():
                    link.unlink()        # the link first: removing the scratch home never walks into the store
            except OSError:
                pass
            remove_tree(self._scratch)
            self._scratch = None


class UsageMeter:
    """Reads one Ante's `--output-format json` stream as it arrives: each UsageUpdate is one model response
    (input_tokens already include cache reads and writes, checked on 0.3.5), and an Error says why it stopped."""

    def __init__(self, on_tokens=None):
        self.on_tokens = on_tokens
        self.buf = b""
        self.tokens = self.requests = 0
        self.error_kind: str | None = None

    def feed(self, chunk: bytes):
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > 1 << 20:     # one line this long is not an event we count
            self.buf = b""
        for line in lines:
            self._line(line)

    def _line(self, line: bytes):
        if b"UsageUpdate" not in line and b"Error" not in line:
            return
        try:
            event = json.loads(line).get("event")
        except (ValueError, AttributeError):
            return
        if not isinstance(event, dict):
            return
        usage = (event.get("UsageUpdate") or {}).get("usage") if isinstance(event.get("UsageUpdate"), dict) else None
        if isinstance(usage, dict):
            n = sum(v for k, v in usage.items() if k in ("input_tokens", "output_tokens") and isinstance(v, int))
            self.tokens += n
            self.requests += 1
            if self.on_tokens is not None:
                self.on_tokens(n)
        error = event.get("Error")
        if error is None and isinstance(event.get("TurnEnd"), dict):
            status = event["TurnEnd"].get("status")
            error = status.get("Error") if isinstance(status, dict) else None
        if isinstance(error, dict) and isinstance(error.get("kind"), str):
            self.error_kind = error["kind"]

    def verdict(self) -> str | None:
        """`limit`, `signin` or None, from the last error Ante reported."""
        if self.error_kind in LIMIT_KINDS:
            return "limit"
        if self.error_kind in SIGNIN_KINDS:
            return "signin"
        return None


class PlayRelay(RelayModel):
    """The relay, telling the manager when the upstream says the credits are gone (HTTP 402)."""

    on_status = None

    def _finish(self, attempt, row, status, usage, kind):
        super()._finish(attempt, row, status, usage, kind)
        if status >= 400:   # operator-visible reason for a failed model call; the row holds no prompt or credential
            print(f"play relay · {kind} · status {status} · {row.get('error') or ''} · {(row.get('detail') or '')[:200]}", file=sys.stderr, flush=True)
        if self.on_status is not None:
            self.on_status(status)


# ---- configuration ----

@dataclass
class PlayConfig:
    ante: Path                                  # the Ante binary
    passcode_sha256: str | None                 # hex digest; None = live rounds off
    upstream: str = ""                          # relay only: the model gateway (no default; set --play-upstream)
    model: str | None = None                    # None: subscription = the Ante home's settings.json model; else Haiku
    wire: str = "messages"
    wire_style: str | None = None               # Ante catalog spelling; None = WIRE_STYLES[wire]
    effort: str = "low"
    max_output_tokens: int = 4096
    standin: bool = False                       # scripted model (Explorer policies); same as provider="standin"
    provider: str = "relay"                     # subscription (Ante's own sign-in) | relay | standin
    ante_provider: str | None = None            # subscription: Ante catalog provider; None = settings.json's
    signin: str = "auto"                        # subscription: "auto" or one stored <profile>/<label>
    signin_order: tuple = ("work", "personal")  # subscription fallback order when `ante usage` cannot be read
    signin_resets: dict = field(default_factory=dict)   # label -> a weekly reset time (aware), for the fallback
    ante_home: Path | None = None               # the Ante home holding the sign-ins; None = ~/.ante
    usage_runner: object = None                 # tests: () -> the `ante usage --json` document
    round_timeout_s: float = 300.0
    round_tokens: int = 400_000
    daily_tokens: int = 5_000_000
    max_antes: int = 16
    queue: int = 3
    swarm_shape: str = "lead"                   # a swarm round's shape when the request names none (SWARM_SHAPES)
    max_requests: int = 400                     # model requests per Ante per round
    data: Path | None = None                    # rounds.jsonl and usage.json; None = memory only
    limits: dict = field(default_factory=dict)  # cgroup limits for a round's Antes: cpu, memory_mb, pids
    trust_forwarded: bool = False               # rate-limit on X-Forwarded-For's last hop (behind a proxy)
    reserve_mb: int = 512                       # free memory a round must leave, beyond ~100 MB per Ante
    reader_grace_s: float = READER_GRACE_S
    model_factory: object = None                # tests: () -> model with register/release/close

    def __post_init__(self):
        if self.wire not in WIRE_STYLES:
            raise ValueError(f"play wire must be one of {', '.join(WIRE_STYLES)}")
        if self.swarm_shape not in SWARM_SHAPES:
            raise ValueError(f"play swarm shape must be one of {', '.join(SWARM_SHAPES)}")
        if self.standin:
            self.provider = "standin"
        if self.provider not in PROVIDERS:
            raise ValueError(f"play provider must be one of {', '.join(PROVIDERS)}")
        self.standin = self.provider == "standin"
        if self.provider != "subscription" and not self.model:
            self.model = DEFAULT_MODEL
        if self.signin != "auto" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.signin or ""):
            raise ValueError("play sign-in must be 'auto' or a sign-in label such as 'personal'")
        digest = (self.passcode_sha256 or "").strip().lower()
        self.passcode_sha256 = digest if HEX64.match(digest) else None


def subscription_defaults(ante_home: Path, provider: str | None, model: str | None) -> tuple[str, str]:
    """The Ante provider and model live rounds run: the flags, else the Ante home's settings.json `provider` and
    `model` (only those two keys are read). The provider must be one of Ante's subscription providers."""
    settings = {}
    try:
        doc = json.loads((Path(ante_home) / "settings.json").read_text())
        settings = doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        pass
    provider = provider or settings.get("provider") or "anthropic-subscription"
    if provider not in SUBSCRIPTION_PROFILES:
        raise ValueError(f"play subscription provider must be one of {', '.join(SUBSCRIPTION_PROFILES)}, not {provider!r}"
                         " (set --play-ante-provider)")
    if not model:
        model = settings.get("model") if provider == settings.get("provider") else None
        model = model if isinstance(model, str) and model else None
    if not model:
        raise ValueError("play subscription model unknown: set --play-model or `model` in the Ante home's settings.json")
    return provider, model


def passcode_hash_from_env(name: str) -> tuple[str | None, str | None]:
    """(digest, problem). The value is never printed; a malformed one is reported by its shape only."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None, None
    if not HEX64.match(raw.lower()):
        return None, f"{name} is set but is not a 64-character SHA-256 hex digest; live rounds stay off"
    return raw.lower(), None


# ---- rounds ----

class Cell:
    def __init__(self, who: str, role: str, team: str, index: int):
        self.who, self.role, self.team, self.index = who, role, team, index
        self.key = ""
        self.token = None
        self.result: dict = {}
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()   # this Ante alone (its request cap); the round's end sets every cell's
        self.meter: UsageMeter | None = None


class Round:
    def __init__(self, size: str, mode: str, count: int, assist: bool, timeout_s: float, shape: str | None = None):
        self.id = secrets.token_hex(6)
        self.token = secrets.token_hex(16)
        self.size, self.mode, self.count, self.assist = size, mode, count, assist
        # single | shared | lead: how the rival team plays (see SWARM_SHAPES).
        self.shape = "single" if mode == "single" else shape or "shared"
        cols, rows = SIZES[size]
        self.seed = secrets.randbits(32)
        self.nonce = secrets.token_hex(16)
        # Committed now, revealed at the end: the seed alone is 32 bits, so the nonce keeps it from being searched.
        self.seed_hash = hashlib.sha256(f"{self.seed}:{self.nonce}".encode()).hexdigest()
        self.params = mz.validate_params({"cols": cols, "rows": rows, "seed": self.seed, "generator": "prim",
                                          "loops": 0.05, "start": "center", "map": "shared", "step_limit": 20000})
        self.maze = mz.generate(self.params)
        self.timeout_s = timeout_s
        self.phase = "queued"   # queued | running | over
        self.created = time.monotonic()
        self.started: float | None = None
        self.ended: float | None = None
        self.lock = threading.Lock()
        self.readers: list[queue.Queue] = []
        self.no_reader_since: float | None = self.created
        self.cancel = threading.Event()   # kills every cell (execute() watches it)
        self.over = threading.Event()     # set once the end is decided
        self.reason: str | None = None
        self.winner: str | None = None
        self.end_event: dict | None = None
        self.service: mz.MazeService | None = None
        self.model = None
        self.budget: RoundBudget | None = None
        self.cells: list[Cell] = []
        self.trails: dict[str, list[int]] = {}
        self.calls: dict[str, int] = {}
        self.ops: dict[str, dict[str, int]] = {}   # who -> op -> honoured calls (the round's log row)
        self.player_moves = 0
        self.queue_pos = 0
        self.cleanup: dict = {}
        self.signin: str | None = None      # subscription: the sign-in label this round runs on (never an email)
        self.signin_source: str | None = None
        rival_names = ["ante"] if mode == "single" else [f"a{k + 1:02d}" for k in range(count)]
        index = 1
        if assist:
            self.cells.append(Cell("ally", "ally", "player", index))
            index += 1
        self.rival_names = rival_names
        for k, name in enumerate(rival_names):
            role = ("lead" if k == 0 else "worker") if self.shape == "lead" else "rival"
            self.cells.append(Cell(name, role, "rival", index))
            index += 1
        for who in ["you"] + [c.who for c in self.cells]:
            self.trails[who] = [self.maze.start]
            self.calls[who] = 0

    # -- events --

    def emit(self, kind: str, data: dict):
        with self.lock:
            for q in self.readers:
                q.put((kind, data))

    def subscribe(self) -> queue.Queue | None:
        q: queue.Queue = queue.Queue()

        def add(model=None):
            snapshot = self.snapshot(model)
            with self.lock:
                if len(self.readers) >= MAX_READERS:
                    return None
                q.put(("state", snapshot))
                if self.end_event is not None:
                    q.put(("end", self.end_event))
                self.readers.append(q)
                self.no_reader_since = None
            return q

        service = self.service
        if service is not None and service.running:
            return service.read(add)   # under the service lock: no call lands between the snapshot and the queue
        return add()

    def unsubscribe(self, q):
        with self.lock:
            if q in self.readers:
                self.readers.remove(q)
            if not self.readers:
                self.no_reader_since = time.monotonic()

    def snapshot(self, model=None) -> dict:
        m = self.maze
        out = {"round": self.id, "phase": self.phase, "cols": m.cols, "rows": m.rows, "start": m.start,
               "exit": m.exit, "size": self.size, "mode": self.mode, "count": self.count, "assist": self.assist,
               "timeout_s": self.timeout_s, "seed_hash": self.seed_hash, "shape": self.shape,
               "t": round(time.monotonic() - self.started, 3) if self.started else 0.0}
        if self.phase == "queued":
            out["queue"] = self.queue_pos
        if model is not None:
            you = model.agents["you"]
            out["seen"] = model.team_cells("player")
            out["you"] = {"pos": you.pos, "moves": self.player_moves}
            out["ally"] = {"pos": model.agents["ally"].pos} if "ally" in model.agents else None
            out["rivals"] = [{"who": c.who, "role": c.role, "progress": model.progress(c.who)}
                             for c in self.cells if c.team == "rival"]
        else:
            out["seen"] = [[m.start, m.walls[m.start]]]
            out["you"] = {"pos": m.start, "moves": 0}
            out["ally"] = {"pos": m.start} if self.assist else None
            out["rivals"] = [{"who": c.who, "role": c.role, "progress": {"calls": 0, "mapped": 0, "bucket": "far",
                                                                          "notes": 0}}
                             for c in self.cells if c.team == "rival"]
        return out

    def on_call(self, agent_id, op, arg, result, model):
        """MazeService hook, lock held: turn one honoured call into the player's events."""
        a = model.agents[agent_id]
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        name = op if isinstance(op, str) and len(op) <= 16 else "?"
        per = self.ops.setdefault(agent_id, {})
        per[name] = per.get(name, 0) + 1
        noted = op == "note" and "error" not in result
        if op == "move" and "error" not in result and self.trails.get(agent_id, [None])[-1] != a.pos:
            self.trails.setdefault(agent_id, []).append(a.pos)
        if a.team == "player":
            if agent_id != "you":
                ev = {"who": agent_id, "team": "player", "op": op if isinstance(op, str) else "?"}
                if isinstance(arg, str) and len(arg) <= 8:
                    ev["arg"] = arg
                if op == "next-edge" and isinstance(result.get("path"), list) and result["path"]:
                    ev["result"] = result["path"][0]
                if noted:
                    ev.pop("arg", None)
                    ev["text"] = result["text"]   # the ally's note: cleaned, at most NOTE_CHARS, for the player
                self.emit("call", ev)
            if op == "move" and "error" not in result:
                self.emit("move", {"who": agent_id, "team": "player", "pos": a.pos})
            if model.last_reveal is not None:
                j = model.last_reveal
                self.emit("reveal", {"team": "player", "cells": [[j, model.maze.walls[j]]]})
        else:
            # A rival's call is its op and its bucketed progress. No argument, result, position or note text; a note
            # carries only how many the rival team has posted.
            ev = {"who": agent_id, "team": "rival", "op": op if isinstance(op, str) else "?"}
            if noted:
                ev["notes"] = model.note_count.get(a.team, 0)
            self.emit("call", ev)
            self.emit("rival", {"who": agent_id, "progress": model.progress(agent_id)})
        if model.exit_by is not None and not self.over.is_set():
            winner = model.agents[model.exit_by].team
            self.decide("exit", winner)

    def decide(self, reason: str, winner: str | None = None) -> bool:
        """The round's end, once: the first reason wins. The round's own thread does the rest."""
        with self.lock:
            if self.over.is_set():
                return False
            self.reason, self.winner = reason, winner
            self.over.set()
        self.kill_all()
        return True

    def kill_all(self):
        self.cancel.set()
        for c in self.cells:
            c.cancel.set()


class PlayManager:
    """One live round at a time, a short queue behind it, and the limits around both."""

    def __init__(self, config: PlayConfig):
        self.config = config
        self.lock = threading.Lock()
        self.rounds: dict[str, Round] = {}
        self.active: Round | None = None
        self.waiting: collections.deque[Round] = collections.deque()
        self.tries: dict[str, collections.deque] = {}
        self.all_tries: collections.deque = collections.deque()
        self.credits_out_until = 0.0
        self.picker: SigninPicker | None = None
        if config.provider == "subscription":
            home = config.ante_home or Path.home() / ".ante"
            config.ante_provider, config.model = subscription_defaults(home, config.ante_provider, config.model)
            self.picker = SigninPicker(config.ante, home, config.signin, config.signin_order, config.signin_resets,
                                       usage_runner=config.usage_runner,
                                       profile=SUBSCRIPTION_PROFILES[config.ante_provider])
        data = config.data
        if data is not None:
            data.mkdir(parents=True, exist_ok=True)
        self.daily = DailyBudget(config.daily_tokens, data / "usage.json" if data else None)
        self.harness = harnesses.get("ante", {"telemetry": "off"})
        # The adapter writes 0.2's or 0.3's catalog spelling from the version (harnesses/ante.py).
        self.harness.frozen = {"path": str(config.ante), "version": self.harness.version(Path(config.ante))}
        self.limits = None
        if config.limits:
            from . import limits as cgroup_limits
            wanted = cgroup_limits.Limits.from_spec(config.limits)
            reason = cgroup_limits.probe(wanted)
            if reason:
                raise RuntimeError(f"play limits cannot be applied: {reason}")
            cgroup_limits.prepare(wanted)
            self.limits = wanted
        self.ticker = threading.Thread(target=self._tick, name="play-ticker", daemon=True)
        self.ticker.start()
        # Live trail and hill (docs/live.md, "Games: trail and hill") share this manager's passcode check, budgets,
        # model route, limits and one-round slot.
        from .arcade import Arcade
        self.arcade = Arcade(self)

    # -- status and admission --

    def status(self) -> dict:
        with self.lock:
            waiting = len(self.waiting)
            full = self.active is not None and waiting >= self.config.queue
        out = {"live": False, "queue": waiting, "max_antes": self.config.max_antes, "model": self.config.model,
               "provider": self.config.provider}
        if self.config.ante_provider:
            out["ante_provider"] = self.config.ante_provider
        reason = self.unavailable()
        if reason is None and full:
            reason = "busy"
        if reason:
            out["reason"] = reason
        else:
            out["live"] = True
        return out

    def maze_status(self) -> dict:
        """GET /api/play/status: the shared status plus how the maze's swarm plays and the sizes the page offers."""
        return {**self.status(), **MAZE_SWARM, "swarm_shape": self.config.swarm_shape}

    def unavailable(self) -> str | None:
        if not self.config.passcode_sha256:
            return "no_passcode_configured"
        if self.daily.exhausted() or time.monotonic() < self.credits_out_until:
            return "credits"
        if self.picker is not None:
            # A running round keeps the store to itself: only a cached answer then (see SigninPicker).
            _, reason, _ = self.picker.pick(evaluate=self.active is None)
            return reason
        return None

    def _client(self, handler) -> str:
        if self.config.trust_forwarded:
            hops = [h.strip() for h in (handler.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
            if hops:
                return hops[-1]
        return handler.client_address[0]

    def _limited(self, ip: str, now: float) -> bool:
        for q in (self.tries.get(ip), self.all_tries):
            while q and now - q[0] > PASS_WINDOW_S:
                q.popleft()
        if ip in self.tries and not self.tries[ip]:
            del self.tries[ip]
        return len(self.tries.get(ip, ())) >= PASS_TRIES or len(self.all_tries) >= PASS_GLOBAL_TRIES

    def check_passcode(self, ip: str, passcode) -> int | None:
        """None when it is right; 429 when this address (or everyone) is out of tries; 403 when it is wrong."""
        now = time.monotonic()
        with self.lock:
            if self._limited(ip, now):
                return 429
        given = passcode.encode("utf-8", "surrogatepass") if isinstance(passcode, str) else b""
        digest = hashlib.sha256(given).hexdigest()
        ok = isinstance(passcode, str) and hmac.compare_digest(digest, self.config.passcode_sha256 or "x" * 64)
        if ok:
            return None
        with self.lock:
            self.tries.setdefault(ip, collections.deque()).append(now)
            self.all_tries.append(now)
        return 403

    @staticmethod
    def parse_request(body) -> tuple[dict | None, str | None]:
        if not isinstance(body, dict):
            return None, "bad_request"
        size, rival, assist = body.get("size"), body.get("rival"), body.get("assist", False)
        if size not in SIZES:
            return None, "bad_size"
        if not isinstance(rival, dict):
            return None, "bad_rival"
        mode, count, shape = rival.get("mode"), rival.get("count"), rival.get("shape")
        if mode not in mz.FLEET_MODES:
            return None, "bad_mode"
        if mode not in LIVE_MODES:
            return None, "mode_not_live"
        if not isinstance(count, int) or isinstance(count, bool):
            return None, "bad_count"
        if not isinstance(assist, bool):
            return None, "bad_assist"
        if shape is not None and (shape not in SWARM_SHAPES or mode != "swarm"):
            return None, "bad_shape"
        return {"size": size, "mode": mode, "count": count, "assist": assist, "shape": shape}, None

    def create(self, req: dict) -> tuple[int, dict]:
        mode, count = req["mode"], req["count"]
        if (mode == "single" and count != 1) or not 1 <= count <= self.config.max_antes:
            return 422, {"error": "bad_count", "max_antes": self.config.max_antes}
        shape = req.get("shape")
        if mode == "swarm" and shape == "lead" and count < LEAD_MIN:
            return 422, {"error": "bad_shape", "lead_min": LEAD_MIN}
        if mode == "swarm" and shape is None:
            shape = self.config.swarm_shape if count >= LEAD_MIN else "shared"
        reason = self.unavailable()
        if reason:
            return 503, {"error": "unavailable", "reason": reason}
        with self.lock:
            if self.active is not None and len(self.waiting) >= self.config.queue:
                return 409, {"error": "busy", "queue": len(self.waiting)}
            if self.active is None and not self._memory_ok(count + req["assist"]):
                return 503, {"error": "unavailable", "reason": "memory"}
            r = Round(req["size"], mode, count, req["assist"], self.config.round_timeout_s, shape)
            self.rounds[r.id] = r
            if self.active is None:
                self.active = r
                start = True
            else:
                self.waiting.append(r)
                start = False
            self._renumber()
        if start:
            self._launch(r)
        return 201, {"round": r.id, "token": r.token, "seed_hash": r.seed_hash, "queue": r.queue_pos, "shape": r.shape}

    def _memory_ok(self, antes: int) -> bool:
        available = host.memory_state().get("available_mb")
        return available is None or available >= antes * 100 + self.config.reserve_mb

    def _renumber(self):
        """Lock held. Queue positions from 1; a change is sent to each waiting round's reader."""
        for k, r in enumerate(self.waiting, 1):
            if r.queue_pos != k:
                r.queue_pos = k
                r.emit("state", r.snapshot())

    def get(self, rid: str, token) -> tuple[Round | None, int]:
        r = self.rounds.get(rid) if isinstance(rid, str) and ROUND_ID.match(rid) else None
        if r is None:
            return None, 404
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), r.token.encode()):
            return None, 403
        return r, 200

    def give_up(self, r: Round):
        if r.decide("gave_up", None):
            with self.lock:
                if r in self.waiting:
                    self.waiting.remove(r)
                    self._renumber()
                    queued = True
                else:
                    queued = False
            if queued:
                self._finish_unstarted(r)

    # -- the player's move --

    def move(self, r: Round, d) -> tuple[int, dict]:
        if d not in DIRS:
            return 422, {"error": "bad_dir"}
        if r.phase == "queued":
            return 409, {"error": "not_started", "queue": r.queue_pos}
        service = r.service
        if r.over.is_set() or service is None or not service.running:
            return 410, {"error": "over", "reason": r.reason}

        def step(model):
            you = model.agents["you"]
            m = model.maze
            open_now = [n for n, bit, _, _ in mz.DIRS if not m.walls[you.pos] & bit]
            if d not in open_now:
                return None, you.pos, open_now   # refused here: a wall is not a move and is not counted
            return True, None, None

        legal, pos, open_now = service.read(step)
        if legal is None:
            return 200, {"pos": pos, "ok": False, "open": open_now}
        result = service.call_as("you", "move", d)
        if "error" in result:
            if result["error"] == "stopped":
                return 410, {"error": "over", "reason": r.reason}
            pos = service.read(lambda model: model.agents["you"].pos)
            return 200, {"pos": pos, "ok": False, "open": [], "error": result["error"]}
        r.player_moves += 1
        m = r.maze
        pos = result["at"][1] * m.cols + result["at"][0]
        out = {"pos": pos, "ok": True, "open": result["open"]}
        if result.get("exit") and r.winner == "player" and r.reason == "exit":
            out["won"] = True
        return 200, out

    # -- the round's life --

    def _launch(self, r: Round):
        threading.Thread(target=self._run, args=(r,), name=f"play-{r.id}", daemon=True).start()

    def _next(self):
        with self.lock:
            self.active = None
            while self.waiting:
                r = self.waiting.popleft()
                if not r.over.is_set():
                    self.active = r
                    break
            self._renumber()
            nxt = self.active
        if nxt is not None:
            self._launch(nxt)

    def _tick(self):
        """Queued rounds whose reader left are dropped; the running round watches its own readers."""
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            with self.lock:
                gone = [r for r in self.waiting if r.no_reader_since is not None
                        and now - r.no_reader_since > self.config.reader_grace_s]
                for r in gone:
                    self.waiting.remove(r)
                if gone:
                    self._renumber()
            for r in gone:
                if r.decide("left", None):
                    self._finish_unstarted(r)
            self.daily.save()

    def _finish_unstarted(self, r: Round):
        r.phase = "over"
        r.ended = time.monotonic()
        self._end(r, {"antes": [], "killed": True, "sandboxes_removed": True})

    def _make_model(self, r: Round):
        if self.config.model_factory is not None:
            return self.config.model_factory()
        if self.config.standin:
            return ScriptedModel()
        if self.config.provider == "subscription":
            # No relay: each Ante calls Anthropic itself; its usage events spend this budget (UsageMeter).
            r.budget = RoundBudget(self.config.round_tokens, self.daily)
            return None
        model = PlayRelay(self.config.upstream, self.config.round_tokens)
        r.budget = model.budget = RoundBudget(self.config.round_tokens, self.daily)

        def on_status(status):
            if status == 402:
                self.credits_out_until = time.monotonic() + 600
        model.on_status = on_status
        return model

    def _run(self, r: Round):
        root = group = None
        cleanup: dict = {}
        try:
            if r.over.is_set():   # given up or left between the queue and here
                return
            root = clean_root()
            r.prefix = f"p{r.id}-"
            workspace = root / "_workspace"
            workspace.mkdir()
            client = workspace / "maze"
            client.write_text(mz.CLI_SOURCE)
            client.chmod(0o755)
            sandbox = ProcessSandbox(root)
            service = mz.MazeService(r.maze, r.params, root / "_maze" / "calls.jsonl",
                                     seed_info={"hash": r.seed_hash}, on_call=r.on_call)
            service.start()
            r.service = service
            service.register("you", 0, team="player", role="player")
            for c in r.cells:
                c.key = f"{r.prefix}{c.who}"
                c.token = service.register(c.who, c.index, team=c.team, role=c.role)
            if self.picker is not None:
                r.signin, reason, r.signin_source = self.picker.pick(evaluate=True)
                if reason:
                    r.decide(reason)
                    return
            r.model = self._make_model(r)
            if self.limits is not None:
                from . import limits as cgroup_limits
                group = cgroup_limits.CellGroup.create(f"play-{r.id}", self.limits)
                if group is None:
                    raise RuntimeError(f"play limits not applied: {cgroup_limits.CellGroup.last_reason}")
            r.phase = "running"
            r.started = time.monotonic()
            r.emit("state", service.read(r.snapshot))
            ws = types.SimpleNamespace(workspace=workspace)
            lead = None
            if self.picker is not None and r.signin:
                left = self.picker.expires_in(r.signin)
                if left is not None and left < EXPIRY_LEAD_S:
                    lead = r.cells[0]   # Ante 0.3.5 has no cross-process refresh lock: one refreshes, then the rest
            for c in r.cells:
                c.meter = UsageMeter(r.budget.add if r.budget is not None and self.picker is not None else None)
                c.thread = threading.Thread(target=self._cell, args=(r, c, sandbox, ws, group), name=f"play-{c.key}",
                                            daemon=True)
                c.thread.start()
                if c is lead:
                    until = time.monotonic() + 45
                    while (time.monotonic() < until and not r.over.is_set() and c.thread.is_alive()
                           and c.meter.requests == 0):
                        time.sleep(0.1)
            while not r.over.wait(0.25):
                now = time.monotonic()
                if now - r.started > r.timeout_s:
                    r.decide("timeout")
                elif r.budget is not None and r.budget.exhausted():
                    r.decide("tokens")
                elif r.no_reader_since is not None and now - r.no_reader_since > self.config.reader_grace_s:
                    r.decide("left")
        except Exception as error:  # noqa: BLE001 - a round that cannot run ends, says why, and is cleaned up
            r.decide("error")
            cleanup["error"] = f"{type(error).__name__}"
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
                    self._next()

    def _cell(self, r: Round, c: Cell, sandbox: ProcessSandbox, ws, group):
        """One Ante: sandbox, `./maze`, model route, run until it exits or the round cancels it."""
        box = None
        cfg = self.config
        try:
            if r.cancel.is_set():
                return
            box = sandbox.create(c.key, ws)
            if c.role == "ally" or r.shape != "single":
                (box["workdir"] / "maze").write_text(role_client(c.role))
                (box["workdir"] / "maze").chmod(0o755)
            (box["workdir"] / ".maze.json").write_text(json.dumps({"socket": str(r.service.socket_path), "token": c.token}))
            harness = harnesses.get("ante", {"telemetry": "off"})
            harness.frozen = self.harness.frozen
            harness.wire = cfg.wire
            env = harness.environment(cfg.ante, box["home"])
            env["SWARM_ATTEMPT"] = c.key
            env["SWARM_RUNNER"] = runner_tag()
            prompt = ALLY_PROMPT if c.role == "ally" else rival_prompt(r.mode, r.count, c.role, c.who, r.rival_names)
            argv = harness.argv(cfg.ante, prompt, box["workdir"], cfg.model, cfg.effort)
            if self.picker is not None:
                env.update(self.subscription_home(box["home"], r.signin))
                argv[argv.index("--provider") + 1] = cfg.ante_provider
            else:
                script = (standin_policy(c.role, r.seed, c.index, team=r.shape != "single")
                          if cfg.standin or cfg.model_factory else [])
                url = r.model.register(c.key, script, box["workdir"], cfg.model, cfg.effort, cfg.max_output_tokens,
                                       cfg.max_requests)
                env.update(harness.prepare(box["home"], url, cfg.model, cfg.effort, cfg.max_output_tokens))
                catalog_path = Path(env["ANTE_HOME"]) / "catalog.json"
                catalog = json.loads(catalog_path.read_text())
                catalog["providers"]["swarm"]["wire_style"] = cfg.wire_style or WIRE_STYLES[cfg.wire]
                catalog_path.write_text(json.dumps(catalog))
            if group is not None:
                argv = group.wrap(argv)

            def started(pid):
                r.service.bind(c.who, pid)

            meter = c.meter or UsageMeter()

            def output(chunk):
                meter.feed(chunk)
                if self.picker is not None and meter.requests >= cfg.max_requests:
                    c.cancel.set()      # the relay's per-Ante request cap, kept without the relay

            if r.cancel.is_set():
                c.cancel.set()
            result = execute(argv, box["workdir"], env, r.timeout_s + 30, box["base"] / "agent.log",
                             on_start=started, cancel=c.cancel, on_output=output)
            c.result = {k: result.get(k) for k in ("exit_code", "termination", "wall_s", "cpu_s")}
            rss = result.get("max_process_rss_kb")
            c.result["rss_mb"] = round(rss / 1024, 1) if isinstance(rss, int) else None
            if self.picker is not None:
                c.result.update({"requests": meter.requests, "tokens": meter.tokens, "error_kind": meter.error_kind})
                self._signin_verdict(r, c, meter)
        except Exception as error:  # noqa: BLE001 - one Ante that cannot start is recorded; the round goes on
            c.result = {"error": type(error).__name__}
        finally:
            if box is not None:
                link = Path(box["home"]).joinpath(".ante", *self.picker.rel) if self.picker else None
                try:
                    if link is not None and link.is_symlink():
                        link.unlink()   # first: removing the sandbox must never walk into the shared store
                except OSError:
                    c.result["store_link_left"] = True
            try:
                if r.model is None:
                    raise LookupError   # subscription: no relay rows; the meter counted this Ante
                rows, _ = r.model.release(c.key, settle_s=0) if c.key else ([], False)
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

    def subscription_home(self, home: Path, label: str) -> dict:
        """A cell's Ante home for subscription mode: a catalog naming exactly one credential, the chosen sign-in,
        and a link to the one real sign-in store (shared refreshes; see SIGNIN_STORE). Returns the env to add."""
        config = Path(home) / ".ante"
        config.mkdir(parents=True, exist_ok=True)
        if not label or "/" in label:
            raise ValueError("no sign-in chosen")
        catalog = {"providers": {self.config.ante_provider: {"credential": f"oauth:{self.picker.profile}/{label}"}}}
        (config / "catalog.json").write_text(json.dumps(catalog))
        self.picker.link_store(config)
        return {"ANTE_HOME": str(config), "ANTE_TELEMETRY": "off"}

    def _signin_verdict(self, r: Round, c: Cell, meter: UsageMeter):
        """A limit ends the round and rests that sign-in for LIMIT_COOLDOWN_S. An expired or revoked
        sign-in is called only when no Ante in the round got a single answer: one Ante losing a refresh race to
        another (Ante 0.3.5 does not serialise refreshes across processes) leaves the shared grant valid."""
        verdict = meter.verdict()
        if verdict is None or r.signin is None:
            return
        if verdict == "limit":
            self.picker.block(r.signin, "limit", LIMIT_COOLDOWN_S)
            r.decide("limit")
        elif not any(x.meter is not None and x.meter.requests for x in r.cells):
            self.picker.block(r.signin, "signin", SIGNIN_COOLDOWN_S)
            r.decide("signin")
        else:
            self.picker.invalidate()

    def _teardown(self, r: Round, root, group) -> dict:
        """Every cell killed, the service closed, the sandboxes gone. Each step runs whatever the others did."""
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
            except Exception as error:  # noqa: BLE001
                out["service_error"] = type(error).__name__
        if r.model is not None:
            try:
                r.model.close()
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
        self.daily.save()
        return out

    def _end(self, r: Round, cleanup: dict):
        """The `end` event (the maze, the paths and the seed revealed) and the round's log row."""
        m = r.maze
        model = None
        tokens = None
        if r.budget is not None:
            tokens = r.budget.used
        else:
            got = [c.result.get(k) for c in r.cells for k in ("input_tokens", "output_tokens")]
            tokens = sum(t for t in got if isinstance(t, int)) if any(isinstance(t, int) for t in got) else None
        if r.service is not None:
            model = r.service._model   # the service is stopped: nothing changes the model any more
        cells = {"player": None, "rival": None}
        notes: dict = {"player": [], "rival": []}
        if model is not None:
            cells = {team: sum(model.teams[team]) for team in model.teams}
            notes = {team: model.team_notes(team) for team in model.teams}
            note_counts = dict(model.note_count)
        else:
            note_counts = {"player": 0, "rival": 0}
        stats = {"seconds": round((r.ended or time.monotonic()) - (r.started or r.created), 3) if r.started else 0.0,
                 "calls": dict(r.calls), "moves": r.player_moves, "mapped": cells, "tokens": tokens,
                 "antes": len(r.cells), "shortest": len(m.shortest_route()), "shape": r.shape, "notes": note_counts}
        # The teams' boards come out with the end, like the maze: the player reads the rivals' chatter afterwards.
        event = {"winner": r.winner, "reason": r.reason, "stats": stats, "shape": r.shape, "notes": notes,
                 "roles": {c.who: c.role for c in r.cells},
                 "maze": {"cols": m.cols, "rows": m.rows, "walls": list(m.walls), "start": m.start, "exit": m.exit,
                          "route": m.shortest_route(), "paths": {k: v for k, v in r.trails.items()},
                          "seed": r.seed, "nonce": r.nonce, "seed_hash": r.seed_hash}}
        r.cleanup = cleanup
        with r.lock:
            r.end_event = event
        r.emit("end", event)
        row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "round": r.id,
               "seed_hash": r.seed_hash, "size": r.size, "cols": m.cols, "rows": m.rows, "mode": r.mode,
               "count": r.count, "shape": r.shape, "assist": r.assist, "notes": note_counts,
               "ops": {k: dict(v) for k, v in r.ops.items()}, "queued_s": round((r.started or r.ended or r.created) - r.created, 3),
               "wall_s": stats["seconds"], "winner": r.winner, "reason": r.reason, "calls": dict(r.calls),
               "moves": r.player_moves, "tokens": tokens, "provider": self.config.provider,
               "model": self.config.model, "signin": r.signin,
               "ante_provider": self.config.ante_provider,
               "antes": [{"who": c.who, "role": c.role, **{k: v for k, v in c.result.items()}} for c in r.cells],
               "cleanup": cleanup}
        if self.config.data is not None:
            try:
                with (self.config.data / "rounds.jsonl").open("a") as f:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
            except OSError:
                pass
        via = (f"{self.config.ante_provider} signin {self.picker.profile}/{r.signin} ({r.signin_source})" if r.signin
               else self.config.provider)
        print(f"play round {r.id} · {r.size} {r.mode}x{r.count} {r.shape}{' +ally' if r.assist else ''} · {r.reason} · "
              f"winner {r.winner} · {stats['seconds']} s · tokens {tokens} · {self.config.model} via {via}", flush=True)
        with self.lock:
            # Finished rounds are kept a while for a reconnecting reader, then dropped.
            cutoff = time.monotonic() - 600
            for rid in [k for k, v in self.rounds.items() if v.ended is not None and v.ended < cutoff]:
                del self.rounds[rid]


# ---- HTTP: /api/play/* on the solo server's handler ----

class PlayAPI:
    def __init__(self, manager: PlayManager):
        self.manager = manager

    def route(self, handler, method: str) -> bool:
        """Answer an /api/play/ request on a solo Handler. False when the path is not ours."""
        path, _, query = handler.path.partition("?")
        if not path.startswith("/api/play/"):
            return False
        arcade = getattr(self.manager, "arcade", None)
        if arcade is not None and arcade.route(handler, method):   # /api/play/trail/..., /api/play/hill/...
            return True
        parts = path[len("/api/play/"):].strip("/").split("/")
        try:
            if method == "GET" and parts == ["status"]:
                handler._send(200, self.manager.maze_status())
            elif method == "POST" and parts == ["rounds"]:
                self._create(handler)
            elif len(parts) >= 2 and parts[0] == "rounds":
                self._round(handler, method, parts[1], parts[2:], query)
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

    def _create(self, handler):
        body = self._body(handler)
        if body is None:
            return
        m = self.manager
        if not m.config.passcode_sha256:
            return handler._send(503, {"error": "unavailable", "reason": "no_passcode_configured"})
        # The passcode is checked before anything else is looked at, and never kept, logged or sent back.
        verdict = m.check_passcode(m._client(handler), body.pop("passcode", None))
        if verdict == 429:
            return handler._send(429, {"error": "too_many_tries"})
        if verdict == 403:
            return handler._send(403, {"error": "wrong_passcode"})
        req, problem = m.parse_request(body)
        if problem:
            return handler._send(422, {"error": problem})
        status, out = m.create(req)
        handler._send(status, out)

    @staticmethod
    def _token(handler, query: str):
        token = handler.headers.get("X-Round-Token")
        if token:
            return token
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "t":
                return v
        return None

    def _round(self, handler, method, rid, rest, query):
        r, status = self.manager.get(rid, self._token(handler, query))
        if r is None:
            return handler._send(status, {"error": "not found" if status == 404 else "forbidden"})
        if method == "DELETE" and not rest:
            self.manager.give_up(r)
            return handler._send(200, {"ok": True, "reason": r.reason})
        if method == "POST" and rest == ["move"]:
            body = self._body(handler)
            if body is None:
                return
            status, out = self.manager.move(r, body.get("dir"))
            return handler._send(status, out)
        if method == "GET" and rest == ["events"]:
            return self._events(handler, r)
        handler._send(404, {"error": "not found"})

    @staticmethod
    def _events(handler, r: Round):
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
