"""Live maze (docs/live.md): per-team fog and the ally's ops in MazeModel, the passcode, the queue, cleanup, and
whole rounds over HTTP.

No model is ever called. Rounds that only need Antes to exist run a fake `ante` that sleeps until it is killed. The
end-to-end rounds run the real Ante binary (when ~/.ante/bin/ante is here) against the scripted model, which drives
every Ante with the maze Explorer: the stand-in upstream (docs/live.md, "Checks")."""
import contextlib
import datetime
import hashlib
import http.client
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarm import maze as mz, play, solo  # noqa: E402
from swarm.sandbox import tagged_processes  # noqa: E402

PASS = "correct horse battery staple 7f3a"
PASS_HASH = hashlib.sha256(PASS.encode()).hexdigest()
REAL_ANTE = Path.home() / ".ante" / "bin" / "ante"
FRONT = Path(__file__).resolve().parents[1] / "front-solo"
MAZE_STATUS = {"swarm_style": "shared", "sizes": [1, 4, 16], "shapes": ["lead", "shared"], "swarm_shape": "lead"}


def corridor_model(team_mode=True):
    """A 4x4 serpentine maze (one path through every cell) with two teams."""
    p = mz.validate_params({"cols": 4, "rows": 4, "seed": 1, "generator": "serpentine", "loops": 0, "start": "corner"})
    model = mz.MazeModel(mz.generate(p), p)
    if team_mode:
        model.register("you", 0, "player", "player")
        model.register("ally", 1, "player", "ally")
        model.register("r1", 2, "rival", "rival")
        model.register("r2", 3, "rival", "rival")
    return model


class TeamFogTests(unittest.TestCase):
    def test_each_team_sees_only_what_its_members_saw(self):
        m = corridor_model()
        self.assertEqual(m.call("you", "move", "E")["new"], ["E"])
        # The rival team has not been to (1,0): for it, that cell is still new.
        self.assertEqual(m.call("r1", "look")["new"], ["E"])
        self.assertEqual(m.team_cells("player"), [[0, m.maze.walls[0]], [1, m.maze.walls[1]]])
        self.assertEqual([c[0] for c in m.team_cells("rival")], [0])
        # The ally shares its player's map: (1,0) is not new for it.
        self.assertEqual(m.call("ally", "look")["new"], [])
        self.assertEqual(len(m.call("ally", "map")["cells"]), 2)
        self.assertEqual(len(m.call("r2", "map")["cells"]), 1)

    def test_last_reveal_names_a_cell_new_to_the_team_only(self):
        m = corridor_model()
        m.call("you", "move", "E")
        self.assertEqual(m.last_reveal, 1)
        m.call("ally", "move", "E")
        self.assertIsNone(m.last_reveal)   # the player already mapped it
        m.call("r1", "move", "E")
        self.assertEqual(m.last_reveal, 1)   # new for the rival team
        self.assertEqual((m.agents["you"].team_firsts, m.agents["ally"].team_firsts, m.agents["r1"].team_firsts), (1, 0, 1))

    def test_next_edge_and_claims_stay_inside_a_team(self):
        p = mz.validate_params({"cols": 8, "rows": 8, "seed": 3, "map": "shared"})
        m = mz.MazeModel(mz.generate(p), p)
        for k, (who, team) in enumerate((("r1", "rival"), ("r2", "rival"), ("you", "player"), ("ally", "player"))):
            m.register(who, k, team, "player" if who == "you" else "ally" if who == "ally" else "rival")
        for who in ("r1", "r2"):
            m.call(who, "move", m.call(who, "look")["new"][0])
        a = m.call("r1", "next-edge")
        self.assertIsNotNone(a["path"])
        first = m.agents["r1"].claim
        # A teammate skips the claimed opening; the other team does not see the claim at all.
        m.call("r2", "next-edge")
        self.assertNotEqual(m.agents["r2"].claim, first)
        self.assertEqual(m.call("ally", "map")["claims"], [])
        self.assertEqual({c[2] for c in m.call("r1", "map")["claims"]}, {"r1", "r2"})

    def test_ally_ops_show_the_player_and_rival_buckets_never_rival_positions(self):
        m = corridor_model()
        for d in "EEE":
            m.call("you", "move", d)
        for _ in range(12):
            m.call("you", "look")
        m.call("r1", "move", "E")
        p = m.call("ally", "player")
        self.assertEqual(p["at"], [3, 0])
        self.assertEqual(p["recent"], ["E", "E", "E"])
        self.assertEqual(len(p["cells"]), 4)
        r = m.call("ally", "rivals")
        self.assertEqual([x["who"] for x in r["rivals"]], ["r1", "r2"])
        for row in r["rivals"]:
            self.assertEqual(set(row), {"who", "bucket", "mapped"})
            self.assertIn(row["bucket"], ("far", "mid", "near"))
        self.assertEqual(r["rivals"][0]["mapped"], 1)
        self.assertNotIn("at", json.dumps(r))

    def test_recent_keeps_ten_moves(self):
        m = corridor_model()
        for _ in range(6):
            m.call("you", "move", "E")
            m.call("you", "move", "W")
        self.assertEqual(len(m.call("ally", "player")["recent"]), 10)

    def test_only_an_ally_has_player_and_rivals(self):
        m = corridor_model()
        for who in ("you", "r1"):
            self.assertEqual(m.call(who, "player")["error"], "bad_op")
            self.assertEqual(m.call(who, "rivals")["error"], "bad_op")
        e = corridor_model(False)
        e.register("0", 0)
        self.assertEqual(e.call("0", "rivals")["error"], "bad_op")

    def test_the_race_is_done_for_both_teams_when_anyone_exits(self):
        m = corridor_model()
        route = "EEESWWWSEEES"
        for d in route[:-1]:
            self.assertFalse(m.call("r1", "move", d)["done"])
        self.assertTrue(m.call("r1", "move", route[-1])["exit"])
        self.assertEqual(m.exit_by, "r1")
        self.assertTrue(m.call("you", "look")["done"])
        self.assertTrue(m.call("ally", "look")["done"])

    def test_buckets_run_far_to_near(self):
        m = corridor_model()
        self.assertEqual(m.bucket("r1"), "far")
        route = "EEESWWWSEEES"
        buckets = []
        for d in route:
            m.call("r1", "move", d)
            buckets.append(m.bucket("r1"))
        self.assertEqual(buckets[-1], "near")
        self.assertIn("mid", buckets)
        self.assertEqual(buckets, sorted(buckets, key=("far", "mid", "near").index))

    def test_registering_a_role_needs_a_team_and_a_known_role(self):
        m = corridor_model(False)
        with self.assertRaises(ValueError):
            m.register("x", 0, None, "ally")
        with self.assertRaises(ValueError):
            m.register("x", 0, "t", "boss")

    def test_eval_agents_without_teams_are_unchanged(self):
        p = mz.validate_params({"cols": 10, "rows": 8, "seed": 9})
        self.assertEqual(mz.simulate(p, 3), mz.simulate(dict(p), 3))
        m = mz.MazeModel(mz.generate(p), p)
        m.register("0", 0)
        self.assertEqual(m.teams, {})
        self.assertEqual(m.call("0", "player")["error"], "bad_op")


def team_model():
    """An 8x8 maze, the player's team (you + ally) and a lead-shaped rival team (a01 lead, a02..a04 workers)."""
    p = mz.validate_params({"cols": 8, "rows": 8, "seed": 3, "map": "shared"})
    m = mz.MazeModel(mz.generate(p), p)
    m.register("you", 0, "player", "player")
    m.register("ally", 1, "player", "ally")
    for k, who in enumerate(("a01", "a02", "a03", "a04")):
        m.register(who, 2 + k, "rival", "lead" if k == 0 else "worker")
    return m


class TeamNotesTests(unittest.TestCase):
    def test_a_board_keeps_the_newest_twenty_notes_of_120_characters(self):
        m = team_model()
        for k in range(25):
            self.assertTrue(m.call("a02", "note", f"note {k}")["ok"])
        board = m.call("a03", "notes")["notes"]
        self.assertEqual(len(board), mz.NOTES_KEPT)
        self.assertEqual([n["n"] for n in board], list(range(6, 26)))
        self.assertEqual(board[-1], {"n": 25, "who": "a02", "text": "note 24"})
        out = m.call("a02", "note", "x" * 300)
        self.assertEqual((len(out["text"]), out["cut"]), (120, True))
        self.assertNotIn("cut", m.call("a02", "note", "y" * 120))

    def test_notes_are_plain_text_and_empty_ones_are_refused(self):
        m = team_model()
        out = m.call("a02", "note", "dead\nend\tat 3,4\x1b[31m ‮gone​!\x00")
        self.assertEqual(out["text"], "dead end at 3,4 [31m gone !")
        for bad in ("", "   ", "\n\t\x00​", None, 7, ["a"]):
            self.assertEqual(m.call("a02", "note", bad)["error"], "empty_note")
        self.assertEqual(mz.clean_note("a  \r\n  b"), "a b")

    def test_notes_never_cross_teams(self):
        m = team_model()
        m.call("a02", "note", "corridor south at 7,2")
        m.call("ally", "note", "dead end east")
        self.assertEqual([n["text"] for n in m.call("ally", "notes")["notes"]], ["dead end east"])
        self.assertEqual([n["text"] for n in m.call("a03", "notes")["notes"]], ["corridor south at 7,2"])
        self.assertNotIn("notes", m.call("ally", "look"))   # its own note is not shown back to it
        self.assertEqual(m.call("you", "look").get("notes"), [{"who": "ally", "text": "dead end east"}])
        self.assertEqual(m.team_notes("player"), [{"n": 1, "who": "ally", "text": "dead end east"}])
        self.assertEqual(m.note_count, {"player": 1, "rival": 1})
        self.assertEqual(set(m.call("ally", "rivals")["rivals"][0]), {"who", "bucket", "mapped"})

    def test_look_carries_the_newest_three_teammate_notes_and_a_move_only_unseen_ones(self):
        m = team_model()
        for k in range(5):
            m.call("a02", "note", f"n{k}")
        m.call("a03", "note", "mine")
        look = m.call("a03", "look")
        self.assertEqual(look["notes"], [{"who": "a02", "text": f"n{k}"} for k in (2, 3, 4)])
        move = m.call("a03", "move", look["open"][0])
        self.assertNotIn("notes", move)   # nothing new since the look
        m.call("a04", "note", "fresh")
        back = mz.OPPOSITE[look["open"][0]]
        self.assertEqual(m.call("a03", "move", back)["notes"], [{"who": "a04", "text": "fresh"}])
        self.assertNotIn("notes", m.call("a03", "move", look["open"][0]))

    def test_agents_without_a_team_have_no_board(self):
        p = mz.validate_params({"cols": 6, "rows": 6, "seed": 2})
        m = mz.MazeModel(mz.generate(p), p)
        m.register("0", 0)
        self.assertEqual(m.call("0", "note", "hi")["error"], "bad_op")
        self.assertEqual(m.call("0", "notes")["error"], "bad_op")
        self.assertNotIn("notes", m.call("0", "look"))


class LeadTests(unittest.TestCase):
    def test_the_lead_assigns_a_worker_who_then_sees_its_path(self):
        m = team_model()
        # Nothing mapped past the start yet: there is nothing to hand out.
        self.assertEqual(m.call("a01", "assign", "a02"), {"worker": "a02", "path": None, "done": False})
        for _ in range(3):
            new = m.call("a01", "look")["new"]
            if new:
                m.call("a01", "move", new[0])
        out = m.call("a01", "assign", "a02")
        self.assertEqual((out["worker"], bool(out["path"])), ("a02", True))
        a02 = m.agents["a02"]
        self.assertEqual(a02.claim, out["to"][1] * 8 + out["to"][0])
        self.assertEqual(a02.calls, 0)   # the lead's call, the worker's claim
        look = m.call("a02", "look")
        self.assertEqual(look["assigned"], {"to": out["to"], "path": out["path"], "by": "a01"})
        # Another worker is handed a different opening: claims keep them apart.
        known = m.teams["rival"]
        frontiers = [i for i in range(64) if known[i] and m._is_frontier(i, known) and i != m.agents["a03"].pos]
        other = m.call("a01", "assign", "a03")
        if len(frontiers) > 1:
            self.assertNotEqual(other["to"], out["to"])
        # The path is worked out again from where the worker stands, and the assignment ends at the target.
        ans = None
        for d in out["path"]:
            ans = m.call("a02", "move", d)
        self.assertNotIn("assigned", ans)
        self.assertIsNone(a02.assigned)

    def test_asking_next_edge_drops_an_assignment(self):
        m = team_model()
        m.call("a01", "move", m.call("a01", "look")["new"][0])
        m.call("a01", "assign", "a02")
        m.call("a02", "next-edge")
        self.assertNotIn("assigned", m.call("a02", "look"))

    def test_only_a_lead_assigns_and_only_its_own_workers(self):
        m = team_model()
        self.assertEqual(m.call("a01", "assign", "a01")["error"], "bad_worker")
        self.assertEqual(m.call("a01", "assign", "ally")["error"], "bad_worker")
        self.assertEqual(m.call("a01", "assign", "nobody")["error"], "bad_worker")
        self.assertEqual(m.call("a01", "assign", None)["error"], "bad_worker")
        self.assertEqual(m.call("a02", "assign", "a03")["error"], "bad_op")
        self.assertEqual(m.call("ally", "assign", "you")["error"], "bad_op")
        self.assertEqual(m.call("a02", "team")["error"], "bad_op")

    def test_team_shows_each_teammate_and_who_is_idle(self):
        m = team_model()
        m.call("a02", "move", m.call("a02", "look")["open"][0])
        m.call("a01", "move", m.call("a01", "look")["new"][0])
        m.call("a01", "assign", "a03")
        m.call("a04", "note", "idle at 4,4")
        out = m.call("a01", "team")
        rows = {r["who"]: r for r in out["team"]}
        self.assertEqual(set(rows), {"a02", "a03", "a04"})   # teammates only: never the lead itself or the player's team
        self.assertTrue(rows["a02"]["last"].startswith("move "))
        self.assertEqual(rows["a04"]["last"], "note")
        self.assertTrue(rows["a02"]["idle"] and rows["a04"]["idle"])
        self.assertFalse(rows["a03"]["idle"])
        self.assertIsNotNone(rows["a03"]["assigned"])
        self.assertEqual(rows["a04"]["notes"], 1)
        self.assertEqual(set(rows["a02"]), {"who", "role", "at", "claim", "assigned", "last", "calls", "notes", "idle"})
        self.assertEqual(out["notes"], [{"who": "a04", "text": "idle at 4,4"}])

    def test_the_eval_delegate_shape_is_unchanged(self):
        a = mz.simulate_fleet({"cols": 12, "rows": 8, "seed": 5}, "delegate", 3)
        b = mz.simulate_fleet({"cols": 12, "rows": 8, "seed": 5}, "delegate", 3)
        self.assertIsNotNone(a["exit_tick"])
        self.assertEqual(a, b)


class LiveClientTests(unittest.TestCase):
    def test_the_eval_client_is_untouched_and_note_takes_the_rest_of_the_line(self):
        self.assertIn('OPS = {"look": 0, "move": 1, "next-edge": 0, "map": 0, "verdict": 0}', mz.CLI_SOURCE)
        p = mz.validate_params({"cols": 6, "rows": 6, "seed": 2, "map": "shared"})
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            service = mz.MazeService(mz.generate(p), p, Path(tmp) / "log.jsonl")
            service.start()
            try:
                lead = service.register("a01", 1, pid=os.getpid(), team="rival", role="lead")
                service.register("a02", 2, pid=os.getpid(), team="rival", role="worker")
                work = Path(tmp) / "w"
                work.mkdir()
                (work / "maze").write_text(play.role_client("lead"))
                (work / ".maze.json").write_text(json.dumps({"socket": str(service.socket_path), "token": lead}))

                def run(*args):
                    out = subprocess.run([sys.executable, "maze", *args], cwd=work, capture_output=True, text=True,
                                         timeout=30)
                    return json.loads(out.stdout)

                self.assertEqual(run("note", "dead", "end", "at", "3,4")["text"], "dead end at 3,4")
                self.assertIn("usage", run("note")["error"])
                self.assertIn("usage", run("move")["error"])
                self.assertEqual(run("assign", "a02")["worker"], "a02")
                self.assertIn("team", run("team"))
                self.assertIn("usage", run("verdict")["error"])   # a live client has no verdict
            finally:
                service.stop()
        self.assertNotIn('"assign"', play.role_client("worker"))
        self.assertIn('"player"', play.ally_client())
        self.assertIn('"note": "text"', play.ally_client())


class ServiceHookTests(unittest.TestCase):
    def test_call_as_and_on_call(self):
        p = mz.validate_params({"cols": 4, "rows": 4, "seed": 1, "generator": "serpentine", "loops": 0, "start": "corner"})
        seen = []
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            service = mz.MazeService(mz.generate(p), p, Path(tmp) / "log.jsonl",
                                     on_call=lambda *a: seen.append((a[0], a[1], a[4].last_reveal)))
            service.start()
            try:
                service.register("you", 0, team="player", role="player")
                self.assertTrue(service.call_as("you", "move", "E")["new"])
                self.assertEqual(service.read(lambda model: model.agents["you"].pos), 1)
            finally:
                service.stop()
            self.assertEqual(seen, [("you", "move", 1)])
            self.assertEqual(service.call_as("you", "move", "E")["error"], "stopped")


class BudgetTests(unittest.TestCase):
    def test_round_budget_spends_the_day_and_the_day_persists(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            day = play.DailyBudget(100, Path(tmp) / "usage.json")
            rb = play.RoundBudget(60, day)
            rb.add(50)
            self.assertFalse(rb.exhausted())
            rb.add(20)
            self.assertTrue(rb.exhausted())
            self.assertFalse(day.exhausted())
            rb2 = play.RoundBudget(1000, day)
            rb2.add(40)
            self.assertTrue(rb2.exhausted())   # the day's cap ends the next round too
            day.save()
            self.assertEqual(play.DailyBudget(100, Path(tmp) / "usage.json").used, 110)


# ---- manager and HTTP ----

def fake_ante(dirpath: Path) -> Path:
    path = dirpath / "ante"
    # Not /bin/sleep: macOS hides a protected binary's environment, and the sweep finds Antes by SWARM_ATTEMPT.
    path.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "ante 0.3.0"; exit 0; fi\n'
                    f'exec {sys.executable} -c "import time; time.sleep(120)"\n')
    path.chmod(0o755)
    return path


class Server:
    def __init__(self, config, tmp: Path):
        (tmp / "runs").mkdir(exist_ok=True)
        self.manager = play.PlayManager(config)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), solo.handler_for(solo.Library(tmp), FRONT, None,
                                                                             play.PlayAPI(self.manager)))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.bodies: list[str] = []

    def close(self):
        for r in list(self.manager.rounds.values()):
            self.manager.give_up(r)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(r.end_event is None for r in self.manager.rounds.values()):
            time.sleep(0.1)
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                text = res.read().decode()
                status = res.status
        except urllib.error.HTTPError as error:
            text = error.read().decode()
            status = error.code
        self.bodies.append(text)
        return status, json.loads(text) if text else None

    def start(self, size="s", mode="single", count=1, assist=False, passcode=PASS, rival_extra=None):
        return self.call("POST", "/api/play/rounds", {"passcode": passcode, "size": size,
                                                      "rival": {"mode": mode, "count": count, **(rival_extra or {})},
                                                      "assist": assist})


class Events:
    """An SSE reader on its own thread; `events` fills as they arrive."""

    def __init__(self, base, rid, token):
        self.events: list[tuple[str, dict]] = []
        self.raw = io.StringIO()
        self.done = threading.Event()
        host, port = base.split("//")[1].split(":")
        self.conn = http.client.HTTPConnection(host, int(port), timeout=120)
        self.conn.request("GET", f"/api/play/rounds/{rid}/events?t={token}")
        self.res = self.conn.getresponse()
        self.status = self.res.status
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        kind = None
        try:
            for line in self.res:
                line = line.decode()
                self.raw.write(line)
                if line.startswith("event: "):
                    kind = line[7:].strip()
                elif line.startswith("data: "):
                    self.events.append((kind, json.loads(line[6:])))
                    if kind == "end":
                        break
        except (OSError, ValueError):
            pass
        finally:
            self.done.set()

    def wait(self, pred, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(pred(k, d) for k, d in list(self.events)):
                return True
            time.sleep(0.02)
        return False

    def end(self, timeout=60):
        self.done.wait(timeout)
        return next((d for k, d in self.events if k == "end"), None)

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass


class PlayTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="live-maze-", dir="/tmp")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(contextlib.redirect_stderr(self.out))
        self.addCleanup(stack.close)

    def server(self, **over):
        cfg = dict(ante=fake_ante(self.tmp), passcode_sha256=PASS_HASH, standin=True, data=self.tmp / "play",
                   round_timeout_s=60)
        cfg.update(over)
        s = Server(play.PlayConfig(**cfg), self.tmp)
        self.addCleanup(s.close)
        return s

    def assert_no_leak(self, s):
        everything = "".join(s.bodies) + self.out.getvalue()
        for f in (self.tmp / "play").glob("*"):
            everything += f.read_text()
        self.assertNotIn(PASS, everything)
        self.assertNotIn(PASS_HASH, everything)

    def wait_end(self, r, timeout=40):
        deadline = time.monotonic() + timeout
        while r.end_event is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(r.end_event, "the round never ended")
        return r.end_event


class PasscodeTests(PlayTestCase):
    def test_no_passcode_configured_means_live_off(self):
        s = self.server(passcode_sha256=None)
        self.assertEqual(s.call("GET", "/api/play/status"),
                         (200, {"live": False, "reason": "no_passcode_configured", "queue": 0, "max_antes": 16,
                                "model": "claude-haiku-5-5", "provider": "standin", **MAZE_STATUS}))
        status, body = s.start()
        self.assertEqual((status, body["reason"]), (503, "no_passcode_configured"))
        self.assertEqual(s.manager.rounds, {})

    def test_a_malformed_hash_in_the_environment_is_refused_without_echoing_it(self):
        os.environ["SWARM_PLAY_TEST_HASH"] = "not-a-hash-" + PASS
        self.addCleanup(os.environ.pop, "SWARM_PLAY_TEST_HASH")
        digest, problem = play.passcode_hash_from_env("SWARM_PLAY_TEST_HASH")
        self.assertIsNone(digest)
        self.assertNotIn(PASS, problem)
        os.environ["SWARM_PLAY_TEST_HASH"] = PASS_HASH.upper()
        self.assertEqual(play.passcode_hash_from_env("SWARM_PLAY_TEST_HASH"), (PASS_HASH, None))

    def test_wrong_passcodes_are_refused_before_any_cell_and_limited_per_address(self):
        s = self.server()
        self.assertEqual(s.call("GET", "/api/play/status")[1]["live"], True)
        for k in range(play.PASS_TRIES):
            self.assertEqual(s.start(passcode=f"wrong {k}")[0], 403)
        self.assertEqual(s.start(passcode=None)[0], 429)
        self.assertEqual(s.start()[0], 429)   # the right one too, until the window passes
        self.assertEqual(s.manager.rounds, {})
        self.assertEqual(tagged_processes("p", prefix=True), {})
        # Another address still has its tries; the limit is per address (and a global cap).
        s.manager.tries.clear()
        status, body = s.start()
        self.assertEqual(status, 201)
        self.assertEqual(set(body), {"round", "token", "seed_hash", "queue", "shape"})
        self.assert_no_leak(s)

    def test_bodies_that_are_not_live_v1_are_422(self):
        s = self.server()
        self.assertEqual(s.start(mode="parallel", count=3), (422, {"error": "mode_not_live"}))
        self.assertEqual(s.start(mode="council", count=3), (422, {"error": "mode_not_live"}))
        self.assertEqual(s.start(mode="hive", count=3), (422, {"error": "bad_mode"}))
        self.assertEqual(s.start(mode="single", count=2)[0], 422)
        self.assertEqual(s.start(mode="swarm", count=17)[0], 422)
        self.assertEqual(s.start(size="xl")[0], 422)
        self.assertEqual(s.manager.rounds, {})
        self.assert_no_leak(s)

    def test_play_is_absent_without_a_play_api(self):
        (self.tmp / "runs").mkdir()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), solo.handler_for(solo.Library(self.tmp), FRONT))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        for method in ("GET", "POST"):
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/play/status", method=method,
                                         data=b"{}" if method == "POST" else None)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(caught.exception.code, 404)


class QueueAndCleanupTests(PlayTestCase):
    def test_one_round_runs_three_wait_and_the_fifth_is_busy(self):
        s = self.server()
        made = [s.start() for _ in range(4)]
        self.assertEqual([m[0] for m in made], [201] * 4)
        self.assertEqual([m[1]["queue"] for m in made], [0, 1, 2, 3])
        readers = [Events(s.base, b["round"], b["token"]) for _, b in made]
        for e in readers:
            self.addCleanup(e.close)
        self.assertEqual(s.start(), (409, {"error": "busy", "queue": 3}))
        self.assertEqual(s.call("GET", "/api/play/status")[1], {"live": False, "reason": "busy", "queue": 3, "max_antes": 16,
                                                                  "model": "claude-haiku-5-5", "provider": "standin",
                                                                  **MAZE_STATUS})
        # A queued round takes no moves yet.
        self.assertEqual(s.call("POST", f"/api/play/rounds/{made[1][1]['round']}/move", {"dir": "N"},
                                {"X-Round-Token": made[1][1]["token"]})[0], 409)
        first = s.manager.rounds[made[0][1]["round"]]
        self.assertTrue(readers[0].wait(lambda k, d: k == "state" and d["phase"] == "running"))
        # Giving up ends the first round: its Ante is killed and the next round starts; the others move up.
        self.assertEqual(s.call("DELETE", f"/api/play/rounds/{first.id}", None, {"X-Round-Token": first.token})[0], 200)
        end = self.wait_end(first)
        self.assertEqual((end["reason"], end["winner"]), ("gave_up", None))
        self.assertTrue(first.cleanup["killed"])
        self.assertTrue(first.cleanup["sandboxes_removed"])
        self.assertEqual(tagged_processes(f"p{first.id}-", prefix=True), {})
        self.assertTrue(readers[1].wait(lambda k, d: k == "state" and d["phase"] == "running"))
        self.assertTrue(readers[3].wait(lambda k, d: k == "state" and d.get("queue") == 2))
        self.assertEqual(s.call("GET", "/api/play/status")[1]["queue"], 2)
        self.assert_no_leak(s)

    def test_a_wrong_token_is_forbidden_and_an_unknown_round_is_404(self):
        s = self.server()
        _, b = s.start()
        self.assertEqual(s.call("DELETE", f"/api/play/rounds/{b['round']}", None, {"X-Round-Token": "x" * 32})[0], 403)
        self.assertEqual(s.call("POST", f"/api/play/rounds/{b['round']}/move", {"dir": "N"})[0], 403)
        self.assertEqual(s.call("DELETE", "/api/play/rounds/000000000000", None, {"X-Round-Token": b["token"]})[0], 404)

    def test_the_round_timeout_kills_every_cell(self):
        s = self.server(round_timeout_s=1.5)
        _, b = s.start(mode="swarm", count=3, assist=True)
        reader = Events(s.base, b["round"], b["token"])
        self.addCleanup(reader.close)
        r = s.manager.rounds[b["round"]]
        self.assertTrue(reader.wait(lambda k, d: k == "state" and d["phase"] == "running"))
        deadline = time.monotonic() + 10
        while len(tagged_processes(f"p{r.id}-", prefix=True)) < 4 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(tagged_processes(f"p{r.id}-", prefix=True)), 4)   # three rivals and the ally
        end = reader.end(30)
        self.assertEqual(end["reason"], "timeout")
        self.assertEqual(len(end["maze"]["walls"]), 16 * 10)
        self.assertEqual(tagged_processes(f"p{r.id}-", prefix=True), {})
        self.assertTrue(r.cleanup["killed"] and r.cleanup["sandboxes_removed"])
        self.assertEqual({a["termination"] for a in [c.result for c in r.cells]}, {"cancelled"})
        self.assertFalse(r.service.running)
        self.assertFalse(r.service.socket_path.exists())
        rows = [json.loads(line) for line in (self.tmp / "play" / "rounds.jsonl").read_text().splitlines()]
        self.assertEqual((rows[-1]["round"], rows[-1]["reason"], rows[-1]["seed_hash"]), (r.id, "timeout", r.seed_hash))
        self.assertNotIn("seed", rows[-1])
        self.assertNotIn("ip", json.dumps(rows[-1]))

    def test_a_round_with_no_reader_ends_as_left(self):
        s = self.server(reader_grace_s=1.0)
        _, b = s.start()
        r = s.manager.rounds[b["round"]]
        end = self.wait_end(r, 15)
        self.assertEqual(end["reason"], "left")
        self.assertEqual(tagged_processes(f"p{r.id}-", prefix=True), {})

    def test_the_daily_cap_turns_live_off_as_credits(self):
        s = self.server()
        s.manager.daily.add(s.manager.config.daily_tokens)
        self.assertEqual(s.call("GET", "/api/play/status")[1]["reason"], "credits")
        self.assertEqual(s.start(), (503, {"error": "unavailable", "reason": "credits"}))

    def test_moves_wall_bumps_are_refused_and_not_counted(self):
        s = self.server()
        _, b = s.start()
        reader = Events(s.base, b["round"], b["token"])
        self.addCleanup(reader.close)
        self.assertTrue(reader.wait(lambda k, d: k == "state" and d["phase"] == "running"))
        r = s.manager.rounds[b["round"]]
        m = r.maze
        hdr = {"X-Round-Token": b["token"]}
        here = m.start
        while not m.walls[here] & 15:   # a start open on every side: step until a wall is beside us
            s.call("POST", f"/api/play/rounds/{r.id}/move", {"dir": "N"}, hdr)
            here -= m.cols
        moved = r.player_moves
        wall = next(d for d, bit, _, _ in mz.DIRS if m.walls[here] & bit)
        status, out = s.call("POST", f"/api/play/rounds/{r.id}/move", {"dir": wall}, hdr)
        self.assertEqual((status, out["ok"], out["pos"]), (200, False, here))
        self.assertEqual(r.player_moves, moved)
        self.assertEqual(s.call("POST", f"/api/play/rounds/{r.id}/move", {"dir": "up"}, hdr)[0], 422)
        # Into a cell the team has not mapped (the steps north above mapped some), so the move is also a reveal.
        known = r.service.read(lambda model: bytes(model.teams["player"]))
        go = next(d for d, bit, dx, dy in mz.DIRS if not m.walls[here] & bit and not known[here + dy * m.cols + dx])
        status, out = s.call("POST", f"/api/play/rounds/{r.id}/move", {"dir": go}, hdr)
        self.assertEqual((status, out["ok"]), (200, True))
        self.assertEqual(r.player_moves, moved + 1)
        self.assertTrue(reader.wait(lambda k, d: k == "move" and d["who"] == "you" and d["pos"] == out["pos"]))
        self.assertTrue(reader.wait(lambda k, d: k == "reveal" and d["cells"][0][0] == out["pos"]))


class RolesAndShapesTests(PlayTestCase):
    def test_role_prompts(self):
        single = play.rival_prompt("single", 1)
        self.assertNotIn("note", single)
        self.assertEqual(single, play.RIVAL_PROMPT.replace("<TEAM>", ""))
        names = ["a01", "a02", "a03", "a04"]
        lead = play.rival_prompt("swarm", 4, "lead", "a01", names)
        for part in ("./maze assign <worker>", "./maze team", "a02, a03, a04", "idle", "Rebalance", "./maze note"):
            self.assertIn(part, lead)
        worker = play.rival_prompt("swarm", 4, "worker", "a03", names)
        for part in ("a01 is your lead", "`assigned`", "dead end", "./maze note", "idle at x,y"):
            self.assertIn(part, worker)
        self.assertNotIn("./maze assign <worker>", worker)
        shared = play.rival_prompt("swarm", 4, "rival", "a02", names)
        for part in ("Claim different edges", "./maze next-edge", "dead end", "promising corridor", "team of 4"):
            self.assertIn(part, shared)
        self.assertNotIn("./maze team", shared)
        for text in (lead, worker, shared):
            for mark in ("<N>", "<ME>", "<LEAD>", "<WORKERS>", "<ROLE>", "<COMMANDS>"):
                self.assertNotIn(mark, text)
        self.assertIn("./maze note", play.ALLY_PROMPT)

    def test_rounds_get_roles_by_shape(self):
        lead = play.Round("s", "swarm", 4, True, 60, "lead")
        self.assertEqual([(c.who, c.role) for c in lead.cells],
                         [("ally", "ally"), ("a01", "lead"), ("a02", "worker"), ("a03", "worker"), ("a04", "worker")])
        self.assertEqual([r["role"] for r in lead.snapshot()["rivals"]], ["lead", "worker", "worker", "worker"])
        shared = play.Round("s", "swarm", 4, False, 60, "shared")
        self.assertEqual({c.role for c in shared.cells}, {"rival"})
        self.assertEqual(play.Round("s", "single", 1, False, 60).shape, "single")

    def test_shape_requests(self):
        s = self.server()
        self.assertEqual(s.start(mode="single", count=1, rival_extra={"shape": "lead"}), (422, {"error": "bad_shape"}))
        self.assertEqual(s.start(mode="swarm", count=4, rival_extra={"shape": "hive"}), (422, {"error": "bad_shape"}))
        self.assertEqual(s.start(mode="swarm", count=2, rival_extra={"shape": "lead"}),
                         (422, {"error": "bad_shape", "lead_min": 3}))
        self.assertEqual(s.manager.rounds, {})
        for count, extra, want in ((4, {}, "lead"), (2, {}, "shared"), (4, {"shape": "shared"}, "shared"),
                                   (16, {}, "lead")):
            status, body = s.start(mode="swarm", count=count, rival_extra=extra)
            self.assertEqual((status, body["shape"]), (201, want))
            r = s.manager.rounds[body["round"]]
            s.manager.give_up(r)
            self.wait_end(r)
        self.assertEqual(play.PlayConfig(ante=Path("x"), passcode_sha256=None, swarm_shape="shared").swarm_shape, "shared")
        with self.assertRaises(ValueError):
            play.PlayConfig(ante=Path("x"), passcode_sha256=None, swarm_shape="hive")

    def test_status_names_the_swarm_style_and_sizes_for_the_maze_only(self):
        s = self.server(swarm_shape="shared")
        status = s.call("GET", "/api/play/status")[1]
        self.assertEqual((status["swarm_style"], status["sizes"], status["swarm_shape"]), ("shared", [1, 4, 16], "shared"))
        self.assertNotIn("swarm_style", s.manager.status())   # trail and hill build on status(): they set their own

    def test_note_events_show_the_allys_text_and_only_a_count_for_rivals(self):
        r = play.Round("s", "swarm", 4, True, 60, "lead")
        q = queue.Queue()
        r.readers.append(q)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            service = mz.MazeService(r.maze, r.params, Path(tmp) / "log.jsonl", on_call=r.on_call)
            service.register("you", 0, team="player", role="player")
            for c in r.cells:
                service.register(c.who, c.index, team=c.team, role=c.role)
            service.call_as("ally", "note", "dead end\neast of 5,3")
            service.call_as("a02", "note", "SECRET corridor at 9,9")
            service.call_as("a03", "note", "   ")
            service.call_as("a01", "assign", "a02")
        events = []
        while not q.empty():
            events.append(q.get())
        calls = [d for k, d in events if k == "call"]
        self.assertEqual(calls[0], {"who": "ally", "team": "player", "op": "note", "text": "dead end east of 5,3"})
        self.assertEqual(calls[1], {"who": "a02", "team": "rival", "op": "note", "notes": 1})
        self.assertEqual(calls[2], {"who": "a03", "team": "rival", "op": "note"})   # refused: no count
        self.assertEqual(calls[3], {"who": "a01", "team": "rival", "op": "assign"})
        self.assertNotIn("SECRET", json.dumps(events))
        progress = [d["progress"] for k, d in events if k == "rival" and d["who"] == "a02"]
        self.assertEqual(progress[0]["notes"], 1)
        self.assertEqual(r.ops["a01"], {"assign": 1})


@unittest.skipUnless(REAL_ANTE.exists(), "needs the Ante binary at ~/.ante/bin/ante")
class EndToEndTests(PlayTestCase):
    """Real Ante processes, stand-in model (scripted Explorer on the messages wire), real sandbox and maze service."""

    def check_stream(self, reader, r):
        for kind, d in reader.events:
            if kind in ("move", "reveal"):
                self.assertEqual(d["team"], "player", "a rival move or reveal reached the player")
            if kind == "rival":
                self.assertEqual(set(d), {"who", "progress"})
                self.assertEqual(set(d["progress"]), {"calls", "mapped", "bucket", "notes"})
            if kind == "call" and d["team"] == "rival":
                self.assertEqual(set(d), {"who", "team", "op", "notes"} if "notes" in d else {"who", "team", "op"})
                if "notes" in d:
                    self.assertEqual(d["op"], "note")
        # Nothing in the stream before `end` names a rival's position or carries a rival's note.
        before_end = reader.raw.getvalue().split("event: end")[0]
        self.assertNotIn('"paths"', before_end)
        self.assertNotIn('"walls"', before_end)
        self.assertNotIn('"text"', "".join(line for line in before_end.splitlines() if '"team":"rival"' in line))
        end = next((d for k, d in reader.events if k == "end"), None)
        for note in (end or {}).get("notes", {}).get("rival", []):
            self.assertNotIn(note["text"], before_end)

    def run_round(self, count, assist=True, extra=None):
        s = self.server(ante=REAL_ANTE)
        mode = "single" if count == 1 else "swarm"
        status, b = s.start(size="m", mode=mode, count=count, assist=assist, rival_extra=extra)
        self.assertEqual(status, 201)
        reader = Events(s.base, b["round"], b["token"])
        self.addCleanup(reader.close)
        r = s.manager.rounds[b["round"]]
        self.assertTrue(reader.wait(lambda k, d: k == "rival", 60), "no rival progress")
        if assist:
            self.assertTrue(reader.wait(lambda k, d: k == "call" and d["who"] == "ally" and d["op"] == "player", 60))
        return s, r, reader, b

    def round_row(self, s, r):
        rows = [json.loads(line) for line in (self.tmp / "play" / "rounds.jsonl").read_text().splitlines()]
        return next(row for row in rows if row["round"] == r.id)

    def walk(self, s, r, token):
        m = r.maze
        route = m.shortest_route()
        names = {-m.cols: "N", 1: "E", m.cols: "S", -1: "W"}
        out = None
        for a, c in zip(route, route[1:]):
            status, out = s.call("POST", f"/api/play/rounds/{r.id}/move", {"dir": names[c - a]}, {"X-Round-Token": token})
            if status != 200:
                break
        return out

    def test_one_rival_and_an_ally_against_a_player_who_wins(self):
        s, r, reader, b = self.run_round(1)
        out = self.walk(s, r, b["token"])
        end = reader.end(30)
        self.assertIsNotNone(end)
        if end["winner"] == "player" and end["stats"]["moves"] == len(r.maze.shortest_route()) - 1:
            self.assertTrue(out["won"])
        self.assertEqual(end["reason"], "exit")
        self.assertEqual(end["maze"]["route"], r.maze.shortest_route())
        self.assertIn("ante", end["maze"]["paths"])
        self.assertEqual(hashlib.sha256(f"{end['maze']['seed']}:{end['maze']['nonce']}".encode()).hexdigest(), b["seed_hash"])
        self.assertTrue(reader.wait(lambda k, d: k == "reveal", 1))
        self.check_stream(reader, r)
        self.wait_end(r)
        self.assertTrue(r.cleanup["killed"] and r.cleanup["sandboxes_removed"])
        self.assertEqual(tagged_processes(f"p{r.id}-", prefix=True), {})
        self.assertGreater(r.cells[1].result["requests"], 0)
        self.assert_no_leak(s)

    def test_four_rivals_and_an_ally_race_to_the_exit(self):
        s, r, reader, b = self.run_round(4)
        end = reader.end(90)
        self.assertIsNotNone(end)
        self.assertEqual(end["reason"], "exit")
        self.assertIn(end["winner"], ("player", "rival"))
        self.assertEqual(set(end["maze"]["paths"]), {"you", "ally", "a01", "a02", "a03", "a04"})
        # The default swarm shape: a01 leads (team, assign), a02..a04 work and report by notes.
        self.assertEqual((b["shape"], end["shape"], end["stats"]["shape"]), ("lead", "lead", "lead"))
        self.assertEqual(end["roles"], {"ally": "ally", "a01": "lead", "a02": "worker", "a03": "worker", "a04": "worker"})
        self.assertEqual(set(end["notes"]), {"player", "rival"})
        self.check_stream(reader, r)
        self.wait_end(r)
        self.assertTrue(r.cleanup["killed"] and r.cleanup["sandboxes_removed"])
        self.assertEqual(tagged_processes(f"p{r.id}-", prefix=True), {})
        self.assertTrue(all(c.result.get("requests", 0) > 0 for c in r.cells))
        row = self.round_row(s, r)
        self.assertEqual(row["shape"], "lead")
        self.assertGreater(row["ops"]["a01"].get("team", 0), 0)
        self.assertGreater(row["ops"]["a01"].get("assign", 0), 0)
        self.assertEqual(row["notes"]["rival"], end["stats"]["notes"]["rival"])
        self.assert_no_leak(s)

    def test_a_shared_swarm_of_four_is_still_a_choice(self):
        s, r, reader, b = self.run_round(4, assist=False, extra={"shape": "shared"})
        end = reader.end(90)
        self.assertIsNotNone(end)
        self.assertEqual(end["reason"], "exit")
        self.assertEqual(end["shape"], "shared")
        self.assertEqual(set(end["roles"].values()), {"rival"})
        self.check_stream(reader, r)
        self.wait_end(r)
        row = self.round_row(s, r)
        self.assertEqual(row["shape"], "shared")
        self.assertFalse(any("assign" in ops for ops in row["ops"].values()))
        self.assertEqual(tagged_processes(f"p{r.id}-", prefix=True), {})
        self.assert_no_leak(s)


if __name__ == "__main__":
    unittest.main()


# ---- subscription mode: Ante's own Claude sign-in (no relay) ----

FAKE_SUB = r'''
import json, os, sys, time
from pathlib import Path
rec, mode_file = Path(sys.argv[1]), Path(sys.argv[2])
args = sys.argv[3:]
home = Path(os.environ["ANTE_HOME"])
oauth = home / "auth" / "credentials" / "oauth"
row = {"provider": args[args.index("--provider") + 1], "model": args[args.index("--model") + 1],
       "catalog": json.loads((home / "catalog.json").read_text()),
       "links": {p.name: os.readlink(p) for p in oauth.iterdir() if p.is_symlink()},
       "entries": sorted(p.name for p in oauth.iterdir()), "telemetry": os.environ.get("ANTE_TELEMETRY"),
       "attempt": os.environ.get("SWARM_ATTEMPT")}
with rec.open("a") as f:
    f.write(json.dumps(row) + "\n")
mode = mode_file.read_text().split()
def emit(event):
    print(json.dumps({"timestamp": "t", "id": "e", "event": event}), flush=True)
if mode[0] == "usage":
    for _ in range(int(mode[2])):
        emit({"UsageUpdate": {"usage": {"input_tokens": int(mode[1]) - 10, "output_tokens": 10,
                                        "cache_read_tokens": 5}, "context": {"used_tokens": 1, "limit_tokens": 2}}})
        time.sleep(0.05)
    time.sleep(120)
kind = {"limit": "rate_limited", "oauth": "oauth"}[mode[0]]
emit({"Error": {"kind": kind, "headline": "x", "details": []}})
emit({"TurnEnd": {"turn_id": "t", "status": {"Error": {"kind": kind, "headline": "x", "details": []}}}})
sys.exit(1)
'''


def usage_doc(**subs):
    """`ante usage --json` shape: subs = {label: (status, session %, weekly %, weekly reset ISO)}."""
    out = []
    for label, (status, session, weekly, reset) in subs.items():
        out.append({"credential": f"anthropic/{label}", "provider": "anthropic", "email": f"{label}@example.invalid",
                    "plan": "Max", "status": status,
                    "windows": [{"label": "Current session", "used_percent": session, "resets_at": "2026-11-17T19:00:00Z"},
                                {"label": "Weekly limit", "used_percent": weekly, "resets_at": reset},
                                {"label": "Fable", "used_percent": 99, "resets_at": "2026-11-17T20:00:00Z"}]})
    out.append({"credential": "openai", "provider": "openai", "status": "ready",
                "windows": [{"label": "Weekly limit", "used_percent": 1, "resets_at": "2026-11-17T20:00:00Z"}]})
    return {"subscriptions": out}


SOON, LATER = "2026-11-18T20:59:59.515563Z", "2026-11-24T14:00:00.483448Z"


def make_store(root: Path, labels=("work", "personal"), profile="anthropic",
               settings=("anthropic-subscription", "claude-haiku-5-5")) -> Path:
    oauth = root / "antehome" / "auth" / "credentials" / "oauth"
    store = oauth / profile
    store.mkdir(parents=True)
    for label in labels:
        (store / f"{label}.json").write_text(json.dumps({"expires_at": "2099-01-01T00:00:00Z", "refresh_token": "fake"}))
    (oauth / "openai.json").write_text("{}")   # a bare sign-in: never shared, never picked
    (root / "antehome" / "settings.json").write_text(json.dumps({"provider": settings[0], "model": settings[1],
                                                                 "theme": "x"}))
    return root / "antehome"


class SigninPickerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.home = make_store(Path(self._tmp.name))
        self.calls = 0

    def picker(self, doc, labels=None, **kw):
        if labels is not None:
            for p in self.home.glob("auth/credentials/oauth/anthropic/*.json"):
                if p.stem not in labels:
                    p.unlink()

        def runner():
            self.calls += 1
            return doc
        return play.SigninPicker(Path("/nonexistent/ante"), self.home, usage_runner=runner, **kw)

    def test_auto_takes_the_soonest_weekly_reset_with_headroom(self):
        p = self.picker(usage_doc(work=("ready", 38, 74, SOON), personal=("ready", 2, 0, LATER)))
        self.assertEqual(p.pick(), ("work", None, "usage"))

    def test_auto_skips_a_sign_in_at_95_percent_of_either_window_or_not_ready(self):
        for work in (("ready", 38, 95, SOON), ("ready", 95, 10, SOON), ("expired", 0, 0, SOON)):
            p = self.picker(usage_doc(work=work, personal=("ready", 2, 0, LATER)))
            self.assertEqual(p.pick(), ("personal", None, "usage"), work)

    def test_a_sign_in_usage_lists_but_the_store_lacks_is_never_picked(self):
        p = self.picker(usage_doc(work=("ready", 1, 1, SOON), personal=("ready", 2, 0, LATER)), labels={"personal"})
        self.assertEqual(p.pick()[0], "personal")

    def test_nothing_with_headroom_is_limit_and_nothing_stored_is_signin(self):
        p = self.picker(usage_doc(work=("ready", 99, 74, SOON), personal=("ready", 2, 97, LATER)))
        self.assertEqual(p.pick()[:2], (None, "limit"))
        p = self.picker(usage_doc(work=("expired", 0, 0, SOON), personal=("expired", 0, 0, LATER)))
        self.assertEqual(p.pick()[:2], (None, "signin"))
        p = self.picker(usage_doc(), labels=set())
        self.assertEqual(p.pick()[:2], (None, "signin"))

    def test_a_limit_hit_rests_that_sign_in_and_the_next_pick_falls_to_the_other(self):
        p = self.picker(usage_doc(work=("ready", 38, 74, SOON), personal=("ready", 2, 0, LATER)))
        self.assertEqual(p.pick()[0], "work")
        p.block("work", "limit", 900)
        self.assertEqual(p.pick()[0], "personal")
        p.block("personal", "limit", 900)
        self.assertEqual(p.pick()[:2], (None, "limit"))

    def test_usage_is_read_once_per_cache_window_and_never_while_a_round_runs(self):
        p = self.picker(usage_doc(work=("ready", 1, 1, SOON)))
        self.assertEqual(p.pick(evaluate=False), (None, None, "pending"))
        self.assertEqual(self.calls, 0)
        p.pick()
        p.pick()
        p.pick(evaluate=False)
        self.assertEqual(self.calls, 1)
        p.invalidate()
        p.pick()
        self.assertEqual(self.calls, 2)

    def test_without_usage_the_order_and_weekly_resets_decide_never_a_list(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        resets = {"work": now - datetime.timedelta(days=1),          # rolls forward to six days from now
                  "personal": now + datetime.timedelta(days=2)}
        p = self.picker(None, order=("work", "personal"), resets=resets)
        self.assertEqual(p.pick(), ("personal", None, "order"))
        p = self.picker(None, order=("work", "personal"), resets={"work": now + datetime.timedelta(hours=5),
                                                                  "personal": now + datetime.timedelta(days=6)})
        self.assertEqual(p.pick(), ("work", None, "order"))
        p = self.picker(None, order=("personal", "work"), resets={})
        self.assertEqual(p.pick(), ("personal", None, "order"))
        p.block("personal", "limit", 900)
        self.assertEqual(p.pick(), ("work", None, "order"))
        p.block("work", "limit", 900)
        self.assertEqual(p.pick()[:2], (None, "limit"))

    def test_a_fixed_label_is_used_alone(self):
        p = self.picker(usage_doc(work=("ready", 1, 1, SOON), personal=("ready", 2, 0, LATER)), signin="personal")
        self.assertEqual(p.pick(), ("personal", None, "fixed"))
        p = self.picker(usage_doc(work=("ready", 1, 1, SOON), personal=("ready", 2, 99, LATER)), signin="personal")
        self.assertEqual(p.pick()[:2], (None, "limit"))
        p = self.picker(None, signin="personal")
        self.assertEqual(p.pick(), ("personal", None, "fixed"))
        p = self.picker(None, signin="other")
        self.assertEqual(p.pick()[:2], (None, "signin"))

    def test_reset_times_need_a_zone(self):
        got = play.parse_resets(["work=2026-11-18T21:00:00Z", "personal=2026-11-24T09:00:00-05:00"])
        self.assertEqual(got["personal"], datetime.datetime(2026, 11, 24, 14, 0, tzinfo=datetime.timezone.utc))
        with self.assertRaises(ValueError):
            play.parse_resets(["work=2026-11-18T21:00:00"])

    def test_expiry_is_read_without_the_tokens(self):
        p = self.picker(None)
        self.assertGreater(p.expires_in("work"), 3600)
        self.assertIsNone(p.expires_in("nobody"))


class UsageMeterTests(unittest.TestCase):
    def test_usage_events_count_across_chunks_and_errors_are_classified(self):
        seen = []
        m = play.UsageMeter(seen.append)
        line = json.dumps({"event": {"UsageUpdate": {"usage": {"input_tokens": 1057, "output_tokens": 10,
                                                                "cache_read_tokens": 50}}}}).encode() + b"\n"
        m.feed(line[:20])
        m.feed(line[20:] + b"not json\n" + line)
        self.assertEqual((m.tokens, m.requests, seen), (2134, 2, [1067, 1067]))
        self.assertIsNone(m.verdict())
        m.feed(json.dumps({"event": {"TurnEnd": {"status": {"Error": {"kind": "rate_limited"}}}}}).encode() + b"\n")
        self.assertEqual(m.verdict(), "limit")
        m.feed(json.dumps({"event": {"Error": {"kind": "oauth", "headline": "OAuth sign-in required"}}}).encode() + b"\n")
        self.assertEqual(m.verdict(), "signin")
        m.feed(json.dumps({"event": {"Error": {"kind": "overloaded"}}}).encode() + b"\n")
        self.assertIsNone(m.verdict())


class SubscriptionTests(PlayTestCase):
    def sub_server(self, doc=None, labels=("work", "personal"), mode="usage 100 1", profile="anthropic",
                   settings=("anthropic-subscription", "claude-haiku-5-5"), **over):
        self.ante_home = make_store(self.tmp, labels, profile, settings)
        self.store = self.ante_home / "auth" / "credentials" / "oauth" / profile
        self.before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        self.rec, self.mode = self.tmp / "fake-record.jsonl", self.tmp / "fake-mode"
        self.mode.write_text(mode)
        (self.tmp / "fake_sub.py").write_text(FAKE_SUB)
        ante = self.tmp / "ante-sub"   # not "ante": server() writes its sleeping fake there
        ante.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "ante 0.3.5"; exit 0; fi\n'
                        f'exec {sys.executable} {self.tmp / "fake_sub.py"} {self.rec} {self.mode} "$@"\n')
        ante.chmod(0o755)
        doc = doc if doc is not None else usage_doc(work=("ready", 38, 74, SOON), personal=("ready", 2, 0, LATER))
        cfg = dict(ante=ante, provider="subscription", standin=False, ante_home=self.ante_home,
                   usage_runner=lambda: doc)
        cfg.update(over)
        return self.server(**cfg)

    def records(self):
        return [json.loads(x) for x in self.rec.read_text().splitlines()] if self.rec.exists() else []

    def rows(self):
        return [json.loads(x) for x in (self.tmp / "play" / "rounds.jsonl").read_text().splitlines()]

    def assert_store_untouched(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.store.iterdir()}, self.before)

    def test_each_cell_names_exactly_the_chosen_sign_in_and_links_the_one_store(self):
        s = self.sub_server()
        st = s.call("GET", "/api/play/status")[1]
        self.assertEqual((st["live"], st["model"], st["provider"]), (True, "claude-haiku-5-5", "subscription"))
        _, b = s.start(mode="swarm", count=3, assist=True)
        r = s.manager.rounds[b["round"]]
        deadline = time.monotonic() + 20
        while len(self.records()) < 4 and time.monotonic() < deadline:
            time.sleep(0.05)
        s.manager.give_up(r)
        self.wait_end(r)
        recs = self.records()
        self.assertEqual(len(recs), 4)
        for rec in recs:
            self.assertEqual(rec["provider"], "anthropic-subscription")
            self.assertEqual(rec["catalog"], {"providers": {"anthropic-subscription": {"credential": "oauth:anthropic/work"}}})
            self.assertEqual({k: Path(v) for k, v in rec["links"].items()}, {"anthropic": self.store})
            self.assertEqual(rec["entries"], ["anthropic"])
            self.assertEqual((rec["telemetry"], rec["model"]), ("off", "claude-haiku-5-5"))
        self.assertNotIn("personal", json.dumps([x["catalog"] for x in recs]))
        self.assert_store_untouched()
        row = self.rows()[-1]
        self.assertEqual((row["signin"], row["provider"], row["model"]), ("work", "subscription", "claude-haiku-5-5"))
        self.assertNotIn("example.invalid", json.dumps(self.rows()) + self.out.getvalue())
        self.assertIn("via anthropic-subscription signin anthropic/work (usage)", self.out.getvalue())

    def test_the_ante_homes_default_provider_and_model_are_used_with_its_own_sign_ins(self):
        doc = {"subscriptions": [{"credential": "openai/personal", "provider": "openai", "status": "ready",
                                  "windows": [{"label": "Weekly limit", "used_percent": 51,
                                               "resets_at": "2026-11-21T02:02:32Z"}]},
                                 *usage_doc(work=("ready", 1, 1, SOON))["subscriptions"]]}
        s = self.sub_server(doc=doc, labels=("personal",), profile="openai",
                            settings=("openai-subscription", "gpt-6.1-terra"))
        st = s.call("GET", "/api/play/status")[1]
        self.assertEqual((st["live"], st["model"], st["ante_provider"]), (True, "gpt-6.1-terra", "openai-subscription"))
        _, b = s.start()
        r = s.manager.rounds[b["round"]]
        deadline = time.monotonic() + 20
        while not self.records() and time.monotonic() < deadline:
            time.sleep(0.05)
        s.manager.give_up(r)
        self.wait_end(r)
        rec = self.records()[0]
        self.assertEqual((rec["provider"], rec["model"]), ("openai-subscription", "gpt-6.1-terra"))
        self.assertEqual(rec["catalog"], {"providers": {"openai-subscription": {"credential": "oauth:openai/personal"}}})
        self.assertEqual({k: Path(v) for k, v in rec["links"].items()}, {"openai": self.store})
        self.assert_store_untouched()
        self.assertEqual(self.rows()[-1]["signin"], "personal")

    def test_flags_override_the_settings_and_an_unknown_provider_is_refused(self):
        self.assertEqual(play.subscription_defaults(make_store(self.tmp), None, None),
                         ("anthropic-subscription", "claude-haiku-5-5"))
        home = self.tmp / "antehome"
        self.assertEqual(play.subscription_defaults(home, "openai-subscription", "gpt-6.1-terra"),
                         ("openai-subscription", "gpt-6.1-terra"))
        with self.assertRaises(ValueError):   # settings' model belongs to another provider
            play.subscription_defaults(home, "openai-subscription", None)
        with self.assertRaises(ValueError):
            play.subscription_defaults(home, "swarm", "m")

    def test_usage_events_spend_the_round_budget_and_stop_the_round(self):
        s = self.sub_server(mode="usage 1000 6", round_tokens=8000, daily_tokens=1_000_000)
        _, b = s.start(mode="swarm", count=2)
        r = s.manager.rounds[b["round"]]
        end = self.wait_end(r, 30)
        self.assertEqual(end["reason"], "tokens")
        self.assertGreaterEqual(end["stats"]["tokens"], 8000)
        self.assertEqual(s.manager.daily.used, end["stats"]["tokens"])
        cells = self.rows()[-1]["antes"]
        self.assertTrue(all(c["tokens"] == 1000 * c["requests"] for c in cells))
        self.assert_store_untouched()

    def test_the_request_cap_stops_one_ante(self):
        s = self.sub_server(mode="usage 10 5", max_requests=2)
        _, b = s.start()
        r = s.manager.rounds[b["round"]]
        deadline = time.monotonic() + 20
        while r.cells[0].result.get("termination") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(r.cells[0].result["termination"], "cancelled")
        self.assertGreaterEqual(r.cells[0].result["requests"], 2)
        s.manager.give_up(r)
        self.wait_end(r)

    def test_a_limit_ends_the_round_and_the_next_round_falls_to_the_other_sign_in(self):
        s = self.sub_server(mode="limit")
        _, b = s.start()
        end = self.wait_end(s.manager.rounds[b["round"]])
        self.assertEqual(end["reason"], "limit")
        self.assertEqual(self.rows()[-1]["signin"], "work")
        self.assertTrue(s.call("GET", "/api/play/status")[1]["live"])
        self.mode.write_text("usage 10 1")
        _, b = s.start()
        r = s.manager.rounds[b["round"]]
        deadline = time.monotonic() + 20
        while not any("personal" in json.dumps(x["catalog"]) for x in self.records()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.records()[-1]["catalog"]["providers"]["anthropic-subscription"]["credential"],
                         "oauth:anthropic/personal")
        s.manager.give_up(r)
        self.wait_end(r)
        self.assert_store_untouched()

    def test_one_sign_in_at_its_limit_turns_live_off_as_limit(self):
        s = self.sub_server(doc=usage_doc(work=("ready", 38, 74, SOON)), labels=("work",), mode="limit")
        _, b = s.start()
        self.wait_end(s.manager.rounds[b["round"]])
        self.assertEqual(s.call("GET", "/api/play/status")[1]["reason"], "limit")
        self.assertEqual(s.start(), (503, {"error": "unavailable", "reason": "limit"}))

    def test_an_expired_sign_in_with_no_answer_at_all_is_signin(self):
        s = self.sub_server(doc=usage_doc(work=("ready", 38, 74, SOON)), labels=("work",), mode="oauth")
        _, b = s.start()
        end = self.wait_end(s.manager.rounds[b["round"]])
        self.assertEqual(end["reason"], "signin")
        self.assertEqual(s.call("GET", "/api/play/status")[1]["reason"], "signin")
        self.assert_store_untouched()

    def test_usage_saying_no_sign_in_is_usable_refuses_rounds_before_any_cell(self):
        s = self.sub_server(doc=usage_doc(work=("expired", 0, 0, SOON), personal=("ready", 1, 99, LATER)))
        self.assertEqual(s.call("GET", "/api/play/status")[1]["reason"], "limit")
        self.assertEqual(s.start()[0], 503)
        self.assertEqual(self.records(), [])
