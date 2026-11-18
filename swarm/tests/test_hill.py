"""Hill (the stacking cartridge): pieces, drops, clears, garbage, outs, the scripted policy, the `./hill` view, and the
browser twin (front-solo/hill-core.js) agreeing with the Python drop for drop on seeded games."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarm import hill as hl  # noqa: E402

FRONT = Path(__file__).resolve().parents[1] / "front-solo"
NODE = shutil.which("node")
W, H = hl.WIDTH, hl.HEIGHT


def row(r, cells, fill=1, hole=None):
    for x in range(W):
        if x != hole:
            cells[r * W + x] = fill


class PieceTests(unittest.TestCase):
    def test_the_set_is_the_cartridges_own(self):
        self.assertEqual(hl.NAMES, ("pip", "bar", "elbow", "cup", "flag", "angle", "post"))
        sizes = sorted(len(c) for _, c in hl.PIECES)
        self.assertEqual(sizes, [2, 3, 3, 5, 5, 5, 5])   # no four-cell pieces at all
        for rots in hl.SHAPES:
            self.assertEqual(len(rots), 4)
            self.assertEqual(hl._rotate(rots[3]), rots[0])   # four turns come back round

    def test_everyone_gets_the_same_bags(self):
        a, b = hl.Sequence(9), hl.Sequence(9)
        seq = [a[k] for k in range(21)]
        self.assertEqual(seq, [b[k] for k in range(21)])
        for bag in range(3):
            self.assertEqual(sorted(seq[bag * 7:bag * 7 + 7]), list(range(7)))


class DropTests(unittest.TestCase):
    def test_a_piece_lands_on_the_floor_or_the_stack(self):
        cells = [0] * (W * H)
        bar = hl.SHAPES[1][0]
        self.assertEqual(hl.land(cells, bar, 0), H - 1)
        cells[(H - 1) * W + 1] = 1
        self.assertEqual(hl.land(cells, bar, 0), H - 2)
        self.assertIsNone(hl.land(cells, bar, W - 2))

    def test_full_rows_clear_and_the_rest_falls(self):
        cells = [0] * (W * H)
        row(H - 1, cells, hole=0)
        row(H - 2, cells, hole=0)
        cells[(H - 3) * W + 4] = 1
        out, cleared, over = hl.place(cells, hl.SHAPES[0][1], 0)   # the pip stood up fills the two holes
        self.assertEqual((cleared, over), (2, False))
        self.assertEqual(out[(H - 1) * W + 4], 1)
        self.assertEqual(sum(out), 1)

    def test_two_rows_send_one_garbage_row_round_the_ring(self):
        m = hl.HillModel(3, ["you", "a01", "a02"])
        you = m.by["you"]
        row(H - 1, you.cells, hole=0)
        row(H - 2, you.cells, hole=0)
        while m.piece("you") != 0:     # skip to a pip in the sequence for this well
            you.k += 1
        r = m.drop("you", 0, 1)
        self.assertEqual((r["cleared"], r["sent"], r["to"]), (2, 1, "a01"))
        self.assertEqual(m.by["a01"].pending, [1])
        a = m.by["a01"]
        m.drop("a01", 0, 0)
        self.assertEqual(a.got, 1)
        bottom = a.cells[(H - 1) * W:]
        self.assertEqual(bottom.count(0), 1)       # one hole in a garbage row
        self.assertTrue(all(v in (0, 2) for v in bottom))

    def test_topping_out_puts_a_well_out_and_the_last_one_standing_wins(self):
        m = hl.HillModel(5, ["you", "ante"])
        you = m.by["you"]
        for r in range(1, H):
            row(r, you.cells, hole=r % W)
        you.cells[0 * W + 4] = 1
        for c in range(W):
            if you.out:
                break
            shape = hl.SHAPES[m.piece("you")][0]
            if c > W - hl.size(shape)[0]:
                break
            m.drop("you", c, 0)
        self.assertTrue(you.out)
        self.assertTrue(m.over)
        self.assertEqual((m.reason, m.winner), ("out", "ante"))
        self.assertEqual(m.drop("ante", 0, 0)["error"], "over")

    def test_bad_drops_are_refused_without_spending_the_piece(self):
        m = hl.HillModel(1, ["you"])
        k = m.by["you"].k
        self.assertIn("error", m.drop("you", W, 0))
        self.assertIn("error", m.drop("you", 0, 4))
        self.assertIn("error", m.call("you", "drop", "x y"))
        self.assertEqual(m.by["you"].k, k)

    def test_at_the_clock_most_rows_wins_then_the_lower_stack(self):
        m = hl.HillModel(1, ["you", "ante"])
        m.by["you"].rows, m.by["ante"].rows = 4, 3
        m.finish("clock")
        self.assertEqual(m.winner, "you")
        m = hl.HillModel(1, ["you", "ante"])
        m.by["you"].cells[(H - 1) * W] = 1
        m.finish("clock")
        self.assertEqual(m.winner, "ante")


class PolicyTests(unittest.TestCase):
    def test_look_and_the_policy_agree(self):
        m = hl.HillModel(11, ["you", "ante"])
        for _ in range(10):
            m.drop("ante", *hl.choose(m.by["ante"].cells, m.piece("ante")))
        look = json.loads(json.dumps(m.look("ante")))
        self.assertEqual(len(look["well"]), H)
        self.assertEqual(len(look["piece"]["rotations"]), 4)
        self.assertEqual(hl.cells_from_look(look), [1 if v else 0 for v in m.by["ante"].cells])
        pol = hl.HillPolicy()
        self.assertEqual(pol.next(None)["command"], "./hill look")
        col, rot = hl.choose(m.by["ante"].cells, m.piece("ante"))
        self.assertEqual(pol.next(json.dumps(look))["command"], f"./hill drop {col} {rot}")
        self.assertEqual(m.call("ante", "drop", f"{col} {rot}")["ok"], True)

    def test_the_stand_in_names_the_piece_id_and_waits_when_told(self):
        m = hl.LiveHill(11, ["you", "ante"], min_drop_s=0)
        pol = hl.HillPolicy()
        cmd = pol.next(json.dumps(m.look("ante")))["command"]
        self.assertRegex(cmd, r"^\./hill drop \d \d [0-9a-f]{6}$")
        out = m.call("ante", "drop", cmd.split(" ", 2)[2])
        self.assertTrue(out["ok"])
        self.assertRegex(pol.next(json.dumps(out))["command"], r"^\./hill drop \d \d [0-9a-f]{6}$")   # from `now`
        self.assertEqual(pol.next(json.dumps({"error": "too soon", "wait_s": 0.4, "done": False}))["command"],
                         "sleep 0.40; ./hill look")
        self.assertEqual(pol.next(json.dumps({"error": "stale piece id: look", "done": False}))["command"], "./hill look")


class LiveRuleTests(unittest.TestCase):
    """hill.LiveHill: the rules a live round adds for agents (piece ids, drop spacing). The human is not touched."""

    def setUp(self):
        self.t = [100.0]
        self.m = hl.LiveHill(5, ["you", "ante"], min_drop_s=1.0, clock=lambda: self.t[0])

    def test_a_drop_must_name_the_piece_last_looked_at(self):
        m = self.m
        k = m.by["ante"].k
        self.assertIn("stale", m.call("ante", "drop", "0 0 abcdef")["error"])          # never looked
        self.assertIn("usage", m.call("ante", "drop", "0 0")["error"])
        pid = m.look("ante")["piece"]["id"]
        self.assertEqual(m.look("ante")["piece"]["id"], pid)                              # same piece, same id
        self.assertIn("stale", m.call("ante", "drop", "0 0 000000" if pid != "000000" else "0 0 111111")["error"])
        self.assertEqual(m.by["ante"].k, k)                                              # refusals change nothing
        out = m.call("ante", "drop", f"0 0 {pid}")
        self.assertTrue(out["ok"])
        self.assertEqual(out["now"]["piece"]["name"], hl.NAMES[m.piece("ante")])
        self.assertNotEqual(out["now"]["piece"]["id"], pid)
        self.t[0] += 5
        self.assertIn("stale", m.call("ante", "drop", f"0 0 {pid}")["error"])          # the old id is spent
        self.assertEqual(m.refused["ante"], 3)
        # a fuse drop (the server's own `drop`) spends the id too
        pid = m.look("ante")["piece"]["id"]
        m.drop("ante", *hl.spawn(m.piece("ante")))
        self.assertIn("stale", m.call("ante", "drop", f"0 0 {pid}")["error"])

    def test_drops_are_spaced_but_the_human_is_not(self):
        m = self.m
        out = m.call("ante", "drop", f"0 0 {m.look('ante')['piece']['id']}")
        pid = out["now"]["piece"]["id"]
        self.assertEqual(out["now"]["wait_s"], 1.0)
        self.t[0] += 0.4
        out = m.call("ante", "drop", f"0 0 {pid}")
        self.assertEqual((out["wait_s"], out["done"]), (0.6, False))
        self.t[0] += 0.6
        self.assertTrue(m.call("ante", "drop", f"0 0 {pid}")["ok"])
        for _ in range(3):
            self.assertTrue(m.drop("you", 0, 0)["ok"])                                  # the human's drops: no id, no gap

    def test_a_scripted_game_is_unchanged(self):
        a, b = hl.HillModel(9, ["p0", "p1"]), hl.LiveHill(9, ["p0", "p1"], min_drop_s=0)
        for _ in range(20):
            for w in ("p0", "p1"):
                col, rot = hl.choose(a.by[w].cells, a.piece(w))
                ra = a.drop(w, col, rot)
                rb = b.call(w, "drop", f"{col} {rot} {b.look(w)['piece'].get('id', '-')}") if not b.by[w].out else b.drop(w, col, rot)
                self.assertEqual({k: v for k, v in rb.items() if k != "now"}, ra)
        self.assertEqual(a.frame(), b.frame())

    def test_the_policy_clears_rows(self):
        out = hl.simulate(2026, 1, 80)
        self.assertGreater(out["wells"][0]["rows"], 10)


SWARM = ["you", "a01", "a02", "a03", "a04"]
POOL = ["you"] + ["swarm"] * 4


class PooledTests(unittest.TestCase):
    """Swarm style "pooled": own wells, own sequences, pooled rows, garbage team to team."""

    def test_each_swarm_well_draws_its_own_seeded_sequence(self):
        m = hl.HillModel(77, SWARM, POOL)
        firsts = [[w.seq[k] for k in range(14)] for w in m.wells]
        self.assertEqual(firsts[0], [hl.Sequence(77)[k] for k in range(14)])            # the human keeps the base
        for seat in range(1, 5):
            self.assertEqual(firsts[seat], [hl.Sequence(hl.well_seed(77, seat))[k] for k in range(14)])
        self.assertEqual(len({tuple(f) for f in firsts}), 5)
        self.assertEqual(hl.well_seed(77, 0), 77)
        self.assertEqual(hl.well_seed(hl.MASK32, 3), (hl.MASK32 + 3 * 0x9E3779B9) & hl.MASK32)
        again = hl.HillModel(77, SWARM, POOL)
        self.assertEqual([again.piece(w) for w in SWARM], [m.piece(w) for w in SWARM])   # seeded: the same again
        lone = hl.HillModel(77, ["you", "ante"], ["you", "ante"])
        self.assertIs(lone.by["ante"].seq, lone.by["you"].seq)                           # ante x1: the same pieces

    def test_rows_pool_and_compare_per_well(self):
        m = hl.HillModel(1, SWARM, POOL)
        m.by["you"].rows = 5
        for who, n in (("a01", 4), ("a02", 8), ("a03", 4), ("a04", 0)):
            m.by[who].rows = n
        self.assertEqual((m.team_rows("swarm"), m.per_well("swarm")), (16, 4.0))
        m.finish("clock")
        self.assertEqual(m.winner, "you")              # 5 rows against 16 / 4 = 4 per well
        frame = m.frame()["teams"]
        self.assertEqual((frame[1]["rows"], frame[1]["per_well"], frame[1]["standing"]), (16, 4.0, 4))
        m = hl.HillModel(1, SWARM, POOL)
        m.by["you"].rows, m.by["a01"].rows = 1, 5
        m.finish("clock")
        self.assertEqual(m.winner, "swarm")            # 1 against 1.25
        m = hl.HillModel(1, SWARM, POOL)
        m.by["you"].rows, m.by["a01"].rows = 1, 4
        m.finish("clock")
        self.assertIsNone(m.winner)                    # 1 against 1.0, both stacks empty: a draw

    def test_the_swarm_garbage_lands_in_your_well_and_yours_goes_round_the_swarm(self):
        m = hl.HillModel(3, SWARM, POOL)
        targets = [m.target(m.by["a0%d" % k]).who for k in (1, 2, 3, 4)]
        self.assertEqual(targets, ["you"] * 4)
        self.assertEqual([m.target(m.by["you"]).who for _ in range(6)], ["a01", "a02", "a03", "a04", "a01", "a02"])
        m.by["a03"].out = True
        self.assertEqual([m.target(m.by["you"]).who for _ in range(3)], ["a04", "a01", "a02"])   # standing wells only
        # through a real drop: two rows from a swarm well send one garbage row to you
        m = hl.HillModel(3, SWARM, POOL)
        a = m.by["a02"]
        row(H - 1, a.cells, hole=0)
        row(H - 2, a.cells, hole=0)
        while m.piece("a02") != 0:
            a.k += 1
        r = m.drop("a02", 0, 1)
        self.assertEqual((r["cleared"], r["sent"], r["to"]), (2, 1, "you"))
        self.assertEqual(m.by["you"].pending, [1])

    def test_the_swarm_loses_only_when_all_its_wells_are_out(self):
        m = hl.HillModel(3, SWARM, POOL)
        for who in ("a01", "a02", "a03"):
            m._out(m.by[who])
            m._check()
        self.assertFalse(m.over)
        m._out(m.by["a04"])
        m._check()
        self.assertEqual((m.over, m.reason, m.winner), (True, "out", "you"))
        m = hl.HillModel(3, SWARM, POOL)
        m._out(m.by["you"])
        m._check()
        self.assertEqual((m.over, m.winner), (True, "swarm"))

    def test_look_shows_the_pool_and_the_human_as_the_rival(self):
        m = hl.LiveHill(3, SWARM, min_drop_s=0, teams=POOL)
        m.by["a02"].rows = 3
        look = m.look("a01")
        self.assertEqual([r["who"] for r in look["rivals"]], ["you"])
        self.assertEqual((look["team"]["rows"], look["team"]["per_well"]), (3, 0.75))
        self.assertEqual([t["who"] for t in look["team"]["members"]], ["a02", "a03", "a04"])
        self.assertEqual(look["piece"]["name"], hl.NAMES[hl.Sequence(hl.well_seed(3, 1))[0]])
        self.assertEqual(len(m.look("you")["rivals"]), 4)
        self.assertNotIn("team", m.look("you"))

    def test_prompts(self):
        team = hl.prompt("swarm", 4, 90, 12)
        for want in ("ONE team", "own piece sequence", "rows per well", "to the human's well", "a01, a02, a03, a04"):
            self.assertIn(want, team)
        self.assertIn("same pieces", hl.prompt("single", 1, 90, 12))

    def test_a_scripted_swarm_plays_its_wells_to_the_end(self):
        out = hl.simulate(2026, 5, 300, POOL)
        self.assertTrue(out["over"])
        self.assertEqual(out["teams"][1]["rows"], sum(w["rows"] for w in out["wells"][1:]))
        self.assertEqual(out["wells"][0]["got"], sum(w["sent"] for w in out["wells"][1:]))


@unittest.skipUnless(NODE, "node not installed")
class TwinTests(unittest.TestCase):
    HARNESS = """
const H = require(process.argv[process.argv.length - 1] + '/hill-core.js');
const out = [];
for (const seed of [1, 2026, 4294967295]) for (const n of [1, 2, 5]) out.push(H.simulate(seed, n, 150));
console.log(JSON.stringify({ out, shapes: H.SHAPES }));
"""

    POOLED = """
const H = require(process.argv[process.argv.length - 1] + '/hill-core.js');
const out = [];
for (const seed of [1, 2026, 4294967295]) out.push(H.simulate(seed, 5, 200, ['you', 'swarm', 'swarm', 'swarm', 'swarm']));
console.log(JSON.stringify({ out, seeds: [0, 1, 2, 3, 4].map((k) => H.wellSeed(4294967295, k)) }));
"""

    def test_python_and_javascript_play_the_same_pooled_games(self):
        r = subprocess.run([NODE, "-e", self.POOLED, str(FRONT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = json.loads(r.stdout)
        self.assertEqual(js["seeds"], [hl.well_seed(4294967295, k) for k in range(5)])
        for k, seed in enumerate((1, 2026, 4294967295)):
            with self.subTest(seed=seed):
                py = hl.simulate(seed, 5, 200, POOL)
                self.assertEqual(py["log"], js["out"][k]["log"])
                self.assertEqual(py, js["out"][k])

    def test_python_and_javascript_play_the_same_games(self):
        r = subprocess.run([NODE, "-e", self.HARNESS, str(FRONT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = json.loads(r.stdout)
        self.assertEqual(js["shapes"], [[[list(c) for c in s] for s in rots] for rots in hl.SHAPES])
        k = 0
        for seed in (1, 2026, 4294967295):
            for n in (1, 2, 5):
                with self.subTest(seed=seed, players=n):
                    py = hl.simulate(seed, n, 150)
                    self.assertEqual(py["log"], js["out"][k]["log"])
                    self.assertEqual(py, js["out"][k])
                k += 1


if __name__ == "__main__":
    unittest.main()
