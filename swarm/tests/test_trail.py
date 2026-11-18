"""Trail (the snake-genre cartridge): the rules model, the scripted policy, the `./trail` view, and the browser twin
(front-solo/trail-core.js) agreeing with the Python tick for tick on seeded games."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarm import trail as tr  # noqa: E402

FRONT = Path(__file__).resolve().parents[1] / "front-solo"
NODE = shutil.which("node")


def model(players=2, size="s", seed=1, **kw):
    p = tr.params(size, seed, players, **kw)
    return tr.TrailModel(p, ["you"] + [f"a{k}" for k in range(1, players)])


class RulesTests(unittest.TestCase):
    def test_seats_and_starting_trails(self):
        m = model(5)
        self.assertEqual([pl.heading for pl in m.players], ["E", "W", "E", "W", "E"])
        # lanes: one row each, nobody starts facing another head, and every trail has most of the width ahead
        self.assertEqual(len({m.xy(pl.body[0])[1] for pl in m.players}), 5)
        for pl in m.players:
            self.assertGreaterEqual(m.look(pl.who)["ahead"], m.cols // 2)
        for n in range(1, tr.MAX_PLAYERS + 1):
            for size in tr.SIZES:
                m = model(n, size=size)
                for pl in m.players:
                    x, y = m.xy(pl.body[0])
                    self.assertTrue(2 <= y <= m.rows - 3 and all(0 <= m.xy(c)[0] < m.cols for c in pl.body), (n, size))
                    self.assertGreaterEqual(m.look(pl.who)["safe"], m.cols // 2, (n, size, pl.who))
        for pl in m.players:
            self.assertEqual(len(pl.body), tr.START_LEN)
        self.assertEqual(len(m.tokens), 6)
        self.assertEqual(len(set(m.tokens) & m._occupied()), 0)

    def test_a_turn_applies_once_then_the_trail_drives_straight(self):
        m = model(1)
        you = m.by["you"]
        self.assertEqual(m.turn("you", "L")["next_heading"], "N")
        m.step()
        self.assertEqual(you.heading, "N")
        m.step()
        self.assertEqual(you.heading, "N")   # no new call: straight on
        self.assertEqual(m.turn("you", "R")["next_heading"], "E")
        self.assertEqual(m.turn("you", "S")["next_heading"], "N")   # S cancels the turn made this tick
        self.assertIn("error", m.turn("you", "X"))

    def test_a_plan_takes_one_move_per_tick_and_a_turn_overrides_it(self):
        m = model(1, size="m")
        you = m.by["you"]
        out = m.call("you", "plan", "slsr")
        self.assertEqual((out["plan"], out["ok"], len(out["board"]), out["heading"]), ("SLSR", True, m.rows, "E"))
        headings = []
        for _ in range(5):
            m.step()
            headings.append(you.heading)
        self.assertEqual(headings, ["E", "N", "N", "E", "E"])   # then straight on when the plan runs out
        m.call("you", "plan", "LLLL")
        m.step()
        self.assertEqual((you.heading, "".join(you.plan)), ("N", "LLL"))
        out = m.call("you", "turn", "R")
        self.assertEqual((out["next_heading"], out["plan"], you.plan), ("E", "", []))   # a turn clears the plan
        m.call("you", "plan", "LL")
        self.assertIsNone(you.pending)                                                   # a plan replaces a turn
        for bad in ("", "LX", "L" * (tr.MAX_PLAN + 1), None):
            self.assertIn("usage", m.call("you", "plan", bad)["error"])
        self.assertTrue(m.steer("you", "E"))
        self.assertEqual(you.plan, [])                                                   # the human's steer too

    def test_safe_follows_the_plan_to_the_first_wall_or_trail(self):
        m = model(1, size="m")
        you = m.by["you"]
        x, y = m.xy(you.body[0])
        look = m.look("you")
        self.assertEqual((look["safe"], look["hits"], look["plan"]), (m.cols - 1 - x, "wall", ""))
        out = m.call("you", "plan", "L")
        self.assertEqual((out["safe"], out["hits"]), (y, "wall"))                        # north to the top wall
        out = m.call("you", "plan", "SLLL")   # a square back onto its own trail
        self.assertEqual((out["safe"], out["hits"]), (3, "trail"))
        m.by["you"].body = [m.cols * 9 + 10, m.cols * 9 + 9, m.cols * 9 + 8]
        m.tokens, m.by["you"].plan = [], []
        self.assertEqual(m.look("you")["safe"], tr.DANGER_CAP if m.cols - 11 >= tr.DANGER_CAP else m.cols - 11)

    def test_the_human_cannot_reverse_onto_itself(self):
        m = model(1)
        self.assertFalse(m.steer("you", "W"))
        self.assertTrue(m.steer("you", "N"))
        self.assertTrue(m.steer("you", "E"))
        self.assertIsNone(m.by["you"].pending)

    def test_a_token_grows_the_trail_and_respawns(self):
        m = model(1, tokens=1)
        you = m.by["you"]
        head = you.body[0]
        m.tokens = [head + 1]
        n = len(you.body)
        ev = m.step()
        self.assertEqual(ev["eaten"], [["you", head + 1]])
        self.assertEqual((you.tokens, len(you.body)), (1, n + 1))
        self.assertEqual(len(m.tokens), 1)
        self.assertNotIn(head + 1, m.tokens)

    def test_walls_trails_and_heads_put_trails_out(self):
        m = model(1)
        m.by["you"].body = [m.cols - 1, m.cols - 2, m.cols - 3]   # facing east at the right edge
        m.step()
        self.assertEqual((m.by["you"].alive, m.by["you"].cause, m.over, m.reason), (False, "wall", True, "out"))
        # head on: two heads on one cell
        m = model(2)
        a, b = m.by["you"], m.by["a1"]
        a.body, a.heading = [5 * m.cols + 5, 5 * m.cols + 4, 5 * m.cols + 3], "E"
        b.body, b.heading = [5 * m.cols + 7, 5 * m.cols + 8, 5 * m.cols + 9], "W"
        m.tokens = []
        m.step()
        self.assertEqual((a.cause, b.cause, a.hit, b.hit), ("head", "head", "a1", "you"))
        self.assertTrue(m.over)
        self.assertIsNone(m.winner)   # same tokens, both out on the same tick: a draw
        # into a trail
        m = model(2)
        a, b = m.by["you"], m.by["a1"]
        a.body, a.heading = [5 * m.cols + 5, 5 * m.cols + 4, 5 * m.cols + 3], "E"
        b.body, b.heading = [4 * m.cols + 6, 5 * m.cols + 6, 6 * m.cols + 6], "N"
        m.tokens = []
        m.step()
        self.assertEqual((a.alive, a.cause, a.hit, b.alive), (False, "trail", "a1", True))
        self.assertEqual(a.body, [])
        self.assertEqual(m.winner, "a1")

    def test_most_tokens_wins_even_when_out_and_ties_go_to_the_survivor(self):
        m = model(2)
        m.by["you"].tokens, m.by["a1"].tokens = 3, 2
        m.by["you"].alive, m.by["you"].died = False, 10
        self.assertEqual(m.leader(), "you")
        m.by["a1"].tokens = 3
        self.assertEqual(m.leader(), "a1")

    def test_the_clock_ends_the_round(self):
        m = model(2, clock=5)
        for _ in range(5):
            for pl in m.players:
                if pl.alive:
                    m.turn(pl.who, tr.choose(tr.view_of(m, pl.who)))
            m.step()
        self.assertTrue(m.over)
        self.assertEqual(m.reason, "clock")

    def test_look_draws_the_board_and_the_policy_reads_it_back(self):
        m = model(5, size="m", seed=7)
        for _ in range(12):
            for pl in m.players:
                if pl.alive:
                    m.turn(pl.who, tr.choose(tr.view_of(m, pl.who)))
            m.step()
        for pl in m.players:
            if not pl.alive:
                continue
            look = m.look(pl.who)
            self.assertEqual(len(look["board"]), m.rows)
            self.assertEqual(sum(row.count("@") for row in look["board"]), 1)
            self.assertEqual(tr.choose(tr.view_from_look(json.loads(json.dumps(look)))), tr.choose(tr.view_of(m, pl.who)))
        self.assertEqual(m.call("you", "fly")["error"], tr.USAGE)

    def test_the_stand_in_policy_looks_then_turns(self):
        m = model(2)
        pol = tr.TrailPolicy()
        step = pol.next(None)
        self.assertEqual(step["command"], "./trail look")
        step = pol.next(json.dumps(m.look("a1")))
        self.assertRegex(step["command"], r"^\./trail turn [LRS]$")
        self.assertEqual(pol.next(json.dumps({"ok": True}))["command"], "./trail look")
        self.assertEqual(pol.next(json.dumps({"error": "out", "done": True}))["op"], "final")

    def test_simulation_is_deterministic_and_ends(self):
        a, b = tr.simulate(tr.params("m", 2026, 5)), tr.simulate(tr.params("m", 2026, 5))
        self.assertEqual(a, b)
        self.assertTrue(a["over"])
        self.assertIn(a["reason"], ("clock", "out"))


def team_model(n=3, size="l", seed=1, **kw):
    """You against a swarm of n on one team, as a live swarm round seats it."""
    names = ["you"] + [f"a{k:02d}" for k in range(1, n + 1)]
    return tr.TrailModel(tr.params(size, seed, n + 1, **kw), names, ["you"] + ["swarm"] * n)


class TeamTests(unittest.TestCase):
    """Swarm style "shared": one team, the sum of its tokens, shared plans, a private notes board."""

    def test_the_team_scores_the_sum_and_a_crashed_member_keeps_its_tokens(self):
        m = team_model()
        m.by["you"].tokens = 5
        m.by["a01"].tokens, m.by["a02"].tokens, m.by["a03"].tokens = 2, 2, 0
        self.assertEqual((m.team_tokens("swarm"), m.leader()), (4, "you"))
        m.by["a01"].alive, m.by["a01"].died, m.by["a01"].body = False, 4, []
        m.by["a03"].tokens = 2
        self.assertEqual((m.team_tokens("swarm"), m.leader()), (6, "swarm"))   # a01 is out but still counts
        m.by["a03"].tokens = 1
        self.assertIsNone(m.leader())                                            # 5 against 5, both running: a draw
        m.by["you"].alive, m.by["you"].died = False, 9
        self.assertEqual(m.leader(), "swarm")                                    # then the one that lasted longer
        self.assertEqual(m.team_rank("swarm"), (5, m.clock + 1))
        self.assertEqual([t["tokens"] for t in m.frame()["teams"]], [5, 5])

    def test_the_round_ends_when_you_are_out_or_the_whole_team_is(self):
        m = team_model()
        m.by["a01"].body = [m.cols - 1, m.cols - 2, m.cols - 3]
        m.by["a01"].heading = "E"
        m.tokens = []
        m.step()
        self.assertEqual((m.by["a01"].alive, m.over), (False, False))             # one member out: the team plays on
        m = team_model()
        you = m.by["you"]
        you.body, you.heading = [m.cols - 1, m.cols - 2, m.cols - 3], "E"
        m.step()
        self.assertEqual((you.alive, m.over, m.reason, m.winner), (False, True, "out", "swarm"))
        m = team_model(clock=3)
        for pl in m.players[1:]:
            pl.alive, pl.died, pl.body = False, 1, []
        m.step()
        self.assertEqual((m.over, m.reason, m.winner), (True, "out", "you"))
        m = team_model(clock=2)
        m.step()
        m.step()
        self.assertEqual((m.over, m.reason), (True, "clock"))

    def test_look_shows_teammates_compactly_and_rivals_without_them(self):
        m = team_model()
        m.call("a02", "plan", "LSSR")
        look = m.look("a01")
        self.assertEqual([r["who"] for r in look["rivals"]], ["you"])
        self.assertEqual([t["who"] for t in look["team"]], ["a02", "a03"])
        a02 = look["team"][0]
        self.assertEqual((a02["plan"], a02["heading"], a02["at"]), ("LSSR", "E", list(m.xy(m.by["a02"].body[0]))))
        self.assertEqual(look["score"], {"team": 0, "you": 0})
        board = "".join(look["board"])
        self.assertEqual((board.count("@"), board.count("M"), board.count("H")), (1, 2, 1))
        self.assertNotIn("team", m.look("you"))                                   # a team of one has no team view
        m.call("a03", "turn", "L")
        self.assertEqual(m.look("a01")["team"][1]["plan"], "L")                   # a pending turn shows as a move

    def test_team_lists_every_plan_path_and_recent_token(self):
        m = team_model()
        m.call("a02", "plan", "LS")
        a01 = m.by["a01"]
        m.tokens = [a01.body[0] - 1]   # right ahead of a01 (heading west)
        m.step()
        view = m.call("a01", "team")
        self.assertEqual((view["team"], view["tokens"], view["rivals"]), ("swarm", 1, {"you": 0}))
        rows = {r["who"]: r for r in view["members"]}
        self.assertEqual(rows["a01"]["recent"], [[1, list(m.xy(a01.body[0]))]])
        self.assertTrue(rows["a01"]["you"])
        self.assertEqual(rows["a02"]["plan"], "S")
        x, y = rows["a02"]["at"]
        self.assertEqual(rows["a02"]["path"], [[x, y - 1]])
        self.assertIn("error", m.call("you", "fly"))

    def test_notes_are_sanitised_capped_and_private_to_the_team(self):
        m = team_model()
        out = m.call("a01", "note", "claim  12,5\x07\n<b>é</b>")
        self.assertEqual(out["notes"], ["a01 t0: claim 12,5 <b> </b>"])
        self.assertIn("usage", m.call("a01", "note", " \t ")["error"])
        long = m.call("a02", "note", "x" * 500)
        self.assertEqual((len(long["notes"][-1]), long["cut"]), (len("a02 t0: ") + tr.NOTE_LEN, True))
        self.assertNotIn("cut", m.call("a02", "note", "y" * tr.NOTE_LEN))
        for k in range(30):
            m.call("a03", "note", f"n{k}")
        notes = m.call("a01", "team")["notes"]
        self.assertEqual((len(notes), notes[-1], notes[0]), (tr.NOTES, "a03 t0: n29", "a03 t0: n10"))
        look = m.look("a02")
        self.assertEqual((look["notes"], look["notes_total"]), (notes[-tr.LOOK_NOTES:], tr.NOTES))
        # the human and the page never see them: not in the human's look, not in the frame
        m.call("you", "note", "the human's own note")
        self.assertEqual(m.look("you")["rivals"][0]["who"], "a01")
        self.assertNotIn("notes", m.look("you"))
        frame = json.dumps(m.frame())
        self.assertNotIn("n29", frame)
        self.assertNotIn("claim", frame)
        self.assertNotIn("human's own", json.dumps(m.look("a01")))

    def test_safe_warns_when_a_path_crosses_a_teammates_plan(self):
        m = team_model(size="l")
        a01, a02 = m.by["a01"], m.by["a02"]
        a01.body, a01.heading = [10 * m.cols + 20, 10 * m.cols + 21, 10 * m.cols + 22], "W"
        a02.body, a02.heading = [13 * m.cols + 16, 13 * m.cols + 15, 13 * m.cols + 14], "E"
        m.tokens = []
        m.call("a02", "plan", "LSSS")   # north up column 16, through row 10
        look = m.call("a01", "plan", "SSS")
        self.assertEqual(look["crosses"], {"who": "a02", "in": 4})   # a01 heads west from column 20
        self.assertIn("a02", look["warning"])

    def test_the_scripted_team_splits_tokens_and_never_meets_head_on(self):
        self.assertEqual(tr.claims(10, [("a", 0), ("b", 9)], [1, 8, 5]), {"a": 1, "b": 8})
        self.assertEqual(tr.claims(10, [("a", 0), ("b", 1)], [2]), {"b": 2})
        heads = 0
        for seed in range(12):
            r = tr.simulate(tr.params("l", seed, 4, clock=150), ["you", "a01", "a02", "a03"],
                            teams=["you", "swarm", "swarm", "swarm"])
            heads += sum(1 for p in r["players"][1:] if p["cause"] == "head")
            self.assertEqual(r["teams"][1]["tokens"], sum(p["tokens"] for p in r["players"][1:]))
        self.assertEqual(heads, 0)
        # the team plays better together than the same three trails each for itself
        together = alone = 0
        for seed in range(20):
            p = tr.params("l", seed * 31 + 7, 4, clock=225)
            names = ["you", "a01", "a02", "a03"]
            together += sum(q["tokens"] for q in tr.simulate(p, names, teams=["you"] + ["swarm"] * 3)["players"][1:])
            alone += sum(q["tokens"] for q in tr.simulate(p, names)["players"][1:])
        self.assertGreater(together, alone)

    def test_the_stand_in_reads_its_team_and_pins_one_note(self):
        m = team_model()
        pol = tr.TrailPolicy()
        pol.next(None)
        self.assertRegex(pol.next(json.dumps(m.look("a01")))["command"], r"^\./trail note ")
        cmd = pol.next(json.dumps(m.look("a01")))["command"]
        self.assertRegex(cmd, r"^\./trail turn [LRS]$")
        v = tr.view_for_team(json.loads(json.dumps(m.look("a01"))))
        turns = tr.team_turns(m)
        self.assertEqual(tr.choose(v), turns["a01"])   # what the look shows is enough to play the team policy
        solo = tr.TrailModel(tr.params("s", 1, 2), ["you", "ante"])
        self.assertRegex(tr.TrailPolicy().next(json.dumps(solo.look("ante")))["command"], r"^\./trail turn ")

    def test_prompts(self):
        team = tr.prompt("swarm", 3, 0.8)
        for want in ("ONE team", "./trail team", "./trail note", "claim", "cut off the human", "a01, a02, a03"):
            self.assertIn(want, team)
        solo = tr.prompt("single", 1, 0.8)
        self.assertNotIn("./trail note", solo)
        self.assertNotIn("<", solo.replace("<moves>", "").replace("<TICK>", "x"))


@unittest.skipUnless(NODE, "node not installed")
class TwinTests(unittest.TestCase):
    HARNESS = """
const T = require(process.argv[process.argv.length - 1] + '/trail-core.js');
const out = [];
for (const size of ['s', 'm', 'l']) for (const seed of [1, 2026, 4294967295]) for (const n of [1, 2, 5]) out.push(T.simulate(T.params(size, seed, n)));
console.log(JSON.stringify(out));
"""

    PLANS = """
const T = require(process.argv[process.argv.length - 1] + '/trail-core.js');
const plans = JSON.parse(process.argv[process.argv.length - 2]), out = [];
for (const size of ['s', 'm']) for (const n of [2, 5]) {
  const p = T.params(size, 7, n, 80), m = new T.Model(p, Array.from({ length: n }, (_, k) => `p${k}`)), log = [];
  while (!m.over) {
    const plan = plans[m.tick];
    if (plan && m.by.p0.alive) m.setPlan('p0', plan);
    for (const pl of m.players.slice(1)) if (pl.alive) m.turn(pl.who, T.choose(T.viewOf(m, pl.who)));
    const ev = m.step();
    log.push(m.players.map((pl) => pl.alive ? pl.heading + pl.body[0] : '-').join(' ') + '|' + ev.out.join(','));
  }
  out.push(log);
}
console.log(JSON.stringify(out));
"""
    PLAN_SCRIPT = {0: "SSRSSL", 3: "LLSSSSRRSSSL", 9: "R", 14: "SSSSSSLSSSSSR"[:12], 30: "LSLSRSRS"}

    def test_python_and_javascript_follow_the_same_plans(self):
        r = subprocess.run([NODE, "-e", self.PLANS, json.dumps({str(k): v for k, v in self.PLAN_SCRIPT.items()}), str(FRONT)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = json.loads(r.stdout)
        k = 0
        for size in "sm":
            for n in (2, 5):
                m = tr.TrailModel(tr.params(size, 7, n, 80), [f"p{i}" for i in range(n)])
                log = []
                while not m.over:
                    plan = self.PLAN_SCRIPT.get(m.tick)
                    if plan and m.by["p0"].alive:
                        m.set_plan("p0", plan)
                    for pl in m.players[1:]:
                        if pl.alive:
                            m.turn(pl.who, tr.choose(tr.view_of(m, pl.who)))
                    ev = m.step()
                    log.append(" ".join(f"{pl.heading}{pl.body[0]}" if pl.alive else "-" for pl in m.players) + "|" + ",".join(ev["out"]))
                with self.subTest(size=size, players=n):
                    self.assertEqual(log, js[k])
                k += 1

    TEAMS = """
const T = require(process.argv[process.argv.length - 1] + '/trail-core.js');
const out = [];
for (const size of ['m', 'l']) for (const seed of [1, 2026, 4294967295]) for (const n of [3, 4]) {
  const names = ['you', ...Array.from({ length: n }, (_, k) => `a${String(k + 1).padStart(2, '0')}`)];
  out.push(T.simulate(T.params(size, seed, n + 1, 225), names, null, ['you', ...names.slice(1).map(() => 'swarm')]));
}
console.log(JSON.stringify(out));
"""

    def test_python_and_javascript_play_the_same_team_games(self):
        r = subprocess.run([NODE, "-e", self.TEAMS, str(FRONT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = json.loads(r.stdout)
        k = 0
        for size in "ml":
            for seed in (1, 2026, 4294967295):
                for n in (3, 4):
                    names = ["you"] + [f"a{i:02d}" for i in range(1, n + 1)]
                    with self.subTest(size=size, seed=seed, players=n):
                        py = tr.simulate(tr.params(size, seed, n + 1, clock=225), names, teams=["you"] + ["swarm"] * n)
                        self.assertEqual(py["log"], js[k]["log"])
                        self.assertEqual(py, js[k])
                    k += 1

    def test_python_and_javascript_play_the_same_games(self):
        r = subprocess.run([NODE, "-e", self.HARNESS, str(FRONT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = json.loads(r.stdout)
        k = 0
        for size in "sml":
            for seed in (1, 2026, 4294967295):
                for n in (1, 2, 5):
                    with self.subTest(size=size, seed=seed, players=n):
                        py = tr.simulate(tr.params(size, seed, n))
                        self.assertEqual(py["log"], js[k]["log"])
                        self.assertEqual(py, js[k])
                    k += 1


if __name__ == "__main__":
    unittest.main()
