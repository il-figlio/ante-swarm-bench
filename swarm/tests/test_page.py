"""The page's counting and ranking rules (front/rank.js with front/heat.js), run in node.

From the 2026-11-12 x256 board: Ante read "best on 6 of 7" after being ranked against nanocode alone, a harness
that never ran read as 256 failures, and an empty chart said "too fast" when nothing had started.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

FRONT = Path(__file__).resolve().parents[1] / "front"
NODE = shutil.which("node")

HARNESS = """
global.window = {};
const dir = process.argv[process.argv.length - 1];
const Heat = require(dir + '/heat.js'), Rank = require(dir + '/rank.js');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const rows = input.rows || [];
if (input.scopes) Rank.markScopes(rows);
const { score, positions } = Rank.score(rows, Heat.rank);
const out = { positions, lead: {}, compared: {}, excluded: {}, tally: (input.attempts || []).map(Rank.tally),
  peaks: input.series ? Rank.peaks(input.series, input.rss || []) : null,
  empty: (input.empty || []).map(([s, a, done]) => Rank.emptyWhy(s, a, done)) };
const field = rows.filter((x) => x.r.h !== 'mini').length;
rows.forEach(({ r, m }, i) => {
  out.lead[r.h] = Rank.leadText(score.get(r.h), field);
  out.compared[r.h] = {}; out.excluded[r.h] = {};
  for (const [k, [key, , gated]] of Object.entries(Rank.HEAT)) {
    out.compared[r.h][k] = Rank.compared(rows, i, positions[k], key, gated);
    out.excluded[r.h][k] = Rank.excluded(r, m, key, gated);
  }
});
console.log(JSON.stringify(out));
"""


def page(**data) -> dict:
    done = subprocess.run([NODE, "-e", HARNESS, str(FRONT)], input=json.dumps(data), capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def lane(h, name, passed, n=256, **m):
    base = {"passed": passed, "n": n, "timeouts": 0, "per": None, "rss": None, "cpu": None, "disk": None, "start": None, "gap": None,
            "slope": None, "mem": None, "wall": None}
    return {"r": {"h": h, "name": name}, "m": {**base, **m}}


@unittest.skipUnless(NODE, "node is needed to run the page's rules")
class PageRuleTests(unittest.TestCase):
    def board(self):
        """The x256 board: two harnesses passed, three timed out, Hermes never ran."""
        return [
            lane("ante", "Ante", 256, per=20, rss=30, cpu=0.4, disk=0.4, start=1.0, gap=0.02, slope=900, mem=5000, wall=40),
            lane("nanocode", "nanocode", 250, per=30, rss=40, cpu=0.9, disk=0.07, start=2.0, gap=0.05, slope=1500, mem=7000, wall=90),
            lane("codex", "Codex", 20, timeouts=236, per=70, rss=90, cpu=3.0, disk=3.2, start=30.0, gap=0.5, slope=4000, mem=7600, wall=600),
            lane("claude-code", "Claude Code", 10, timeouts=246, per=60, rss=200, cpu=5.0, disk=0.06, start=50.0, gap=0.9, slope=5000, mem=7500, wall=600),
            lane("hermes", "Hermes", 0),
        ]

    def test_the_lead_says_who_it_was_compared_with(self):
        out = page(rows=self.board())
        # Ranked on resources against nanocode alone (the others had under half pass), on startup against all with data.
        self.assertRegex(out["lead"]["ante"], r"^best on \d+ of \d+ · resources vs nanocode only$")
        self.assertEqual(out["lead"]["hermes"], "")
        self.assertEqual(out["compared"]["ante"]["disk"], "against nanocode")
        self.assertEqual(out["compared"]["ante"]["start"], "against nanocode, Codex, Claude Code")
        self.assertRegex(page(rows=self.board()[:2])["lead"]["ante"], r"^best on \d+ of \d+$")  # a race of two needs no qualifier
        three = self.board()
        three[2]["m"].update(passed=200, timeouts=56)
        self.assertRegex(page(rows=three)["lead"]["ante"], r"· resources vs 2 of 4 others$")

    def test_startup_is_ranked_for_every_harness_with_data_and_resources_only_for_passers(self):
        out = page(rows=self.board())
        start = dict(zip(["ante", "nanocode", "codex", "claude-code", "hermes"], out["positions"]["start"]))
        self.assertEqual((start["ante"], start["claude-code"], start["hermes"]), (1, 0, None))
        self.assertIsNotNone(start["codex"])
        disk = dict(zip(["ante", "nanocode", "codex", "claude-code", "hermes"], out["positions"]["disk"]))
        self.assertEqual(disk, {"ante": 0, "nanocode": 1, "codex": None, "claude-code": None, "hermes": None})
        # The unranked numbers are marked as such instead of sitting there looking neutral.
        self.assertEqual(out["excluded"]["codex"]["disk"], "under half passed: not ranked")
        self.assertIsNone(out["excluded"]["codex"]["start"])
        self.assertIsNone(out["excluded"]["ante"]["disk"])

    def test_wall_time_of_a_wave_stopped_at_the_limit_is_not_compared(self):
        rows = [lane("a", "A", 32, n=32, wall=40), lane("b", "B", 30, n=32, wall=44), lane("c", "C", 20, n=32, timeouts=12, wall=600)]
        out = page(rows=rows)
        self.assertEqual(out["positions"]["wall"], [None, None, None])  # A and B are within 10%; C takes no part
        self.assertEqual(out["excluded"]["c"]["wall"], "12 agents stopped at the time limit: not compared")
        rows[1]["m"]["wall"] = 80
        self.assertEqual(page(rows=rows)["positions"]["wall"], [1, 0, None])

    def test_heat_keeps_the_ten_percent_gap_and_mini_stays_out(self):
        rows = [lane("a", "A", 32, n=32, cpu=1.00), lane("b", "B", 32, n=32, cpu=1.05), lane("mini", "mini", 32, n=32, cpu=0.01)]
        out = page(rows=rows)
        self.assertEqual(out["positions"]["cpu"], [None, None, None])
        self.assertEqual(out["positions"]["pass"], [None, None, None])  # a tie is nobody's win
        self.assertEqual((out["lead"]["a"], out["excluded"]["mini"]["cpu"]), ("", "floor: not ranked"))

    def test_attempts_the_runner_failed_are_not_counted_as_the_harness_failing(self):
        hermes = [{"passed": False, "failure": "infra", "infra": {"reason": "disk_full", "stage": "prepare"}, "ran": False}] * 256
        mixed = ([{"passed": True, "failure": None}] * 5 + [{"passed": False, "failure": "timeout"}] * 3 +
                 [{"passed": False, "failure": "check_failed"}] + [{"passed": False, "failure": "cancelled"}] * 2 +
                 [{"passed": False, "failure": "infra", "infra": {"reason": "setup_failed"}}])
        a, b = page(attempts=[hermes, mixed])["tally"]
        self.assertEqual((a["passed"], a["failed"], a["notRun"], a["failClass"]), (0, 0, 256, None))
        self.assertIn("disk was full", a["notRunWhy"])
        self.assertEqual((b["passed"], b["failed"], b["notRun"], b["failClass"], b["timeouts"], b["notRunWhy"]), (5, 4, 3, "timeout", 3, "cancelled"))
        held = page(attempts=[[{"passed": False, "failure": "admission_timeout"}] * 4 + [{"passed": True}]])["tally"][0]
        self.assertEqual((held["failed"], held["notRun"]), (0, 4))
        self.assertIn("never admitted", held["notRunWhy"])

    def test_attempts_that_never_ran_do_not_push_a_harness_out_of_the_ranking(self):
        # 10 of 32 lost to a full disk; 16 of the 22 that ran passed. That is not "under half passed".
        rows = [lane("a", "A", 16, n=32, notRun=10, cpu=1.0), lane("b", "B", 32, n=32, cpu=2.0), lane("c", "C", 10, n=32, cpu=0.1)]
        out = page(rows=rows)
        self.assertEqual(out["positions"]["cpu"], [1, 0, None])
        self.assertEqual(out["excluded"]["a"]["pass"], "10 attempts not run: not compared")
        self.assertEqual(out["positions"]["pass"], [None, 1, 0])

    def test_memory_measured_differently_is_not_compared(self):
        rows = [lane("a", "A", 32, n=32, per=20, mem=600, memScope="cgroup"), lane("b", "B", 32, n=32, per=30, mem=900, memScope="cgroup"),
                lane("c", "C", 32, n=32, per=70, mem=2100, memScope="rss-sum")]
        out = page(rows=rows, scopes=True)
        self.assertEqual((out["positions"]["per"], out["positions"]["peakmem"]), ([1, 0, None], [1, 0, None]))
        self.assertIn("measured differently", out["excluded"]["c"]["per"])

    def test_memory_per_agent_is_swarm_memory_over_agents_alive(self):
        series = [[0, 0, 0, 5, 7000], [1, 128, 2000, 90, 5000], [2, 256, 5120, 100, 2000], [3, 256, 4000, 100, 3000], [4, 3, 600, 20, 6400]]
        peaks = page(series=series, rss=[{"rss_mb": 70}, {"rss_mb": 72}])["peaks"]
        self.assertEqual((peaks["per"], peaks["mem"], peaks["alive"]), (20, 5120, 256))  # not 600 / 3 from the tail
        brief = page(series=[[0, 0, 0, 1, 7000]], rss=[{"rss_mb": 70}])["peaks"]
        self.assertEqual((brief["per"], brief["mem"]), (None, 70))  # nothing sampled: one agent's largest process is a floor

    def test_an_empty_chart_says_why(self):
        idle = [[0, 0, 0, 88, 7000], [1, 0, 0, 90, 7000]]
        ran = [{"ran": True}]
        out = page(empty=[[[], [], False], [idle, [], False], [idle, [{"ran": False}], True], [idle, ran, True], [[[0, 2, 0, 1, 1]], [], False]])
        self.assertEqual(out["empty"], ["no samples yet", "no agent running yet", "no agent started", "too brief to sample", "too brief to sample"])


CONTROL_HARNESS = """
global.window = {};
const dir = process.argv[process.argv.length - 1];
const Heat = require(dir + '/heat.js'), Rank = require(dir + '/rank.js');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
if (input.lanes) {
  const odd = Rank.oddControls(Object.fromEntries(input.lanes.map((l) => [l.h, l.control && l.control.key])));
  const rows = input.lanes.map((l) => ({ r: { h: l.h }, m: Rank.relative({ ...l.m, mismatch: odd[l.h] }, l.control, input.size || 1) }));
  out.rows = rows.map(({ m }) => ({ ratio: m.ratio, noControl: m.noControl, start: m.start }));
  out.rank = {};
  for (const k of input.keys) out.rank[k] = Heat.rank(Rank.rankable(rows, k), 'low');
}
if (input.pos) out.pos = input.pos.map((p) => Rank.positions(p.values, p.better || 'low', p.ties, Heat.rank));
if (input.stats) out.rule = input.stats.map((s) => Rank.ruleText(s));
console.log(JSON.stringify(out));
"""


def control(**kw):
    base = {"state": "done", "noisy": False, "key": "k1", "median": {"first_request_s": 0.02, "step_gap_s": 0.001, "cpu_s": 0.5, "wall_s": 2.0, "rss_mb": 30}}
    return {**base, **kw}


def run_control(**data) -> dict:
    done = subprocess.run([NODE, "-e", CONTROL_HARNESS, str(FRONT)], input=json.dumps(data), capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page's rules")
class ControlAndTieTests(unittest.TestCase):
    def test_startup_is_ranked_on_the_ratio_to_the_lanes_own_control(self):
        lanes = [{"h": "a", "m": {"start": 0.10, "gap": 0.02, "cpu": 1.0, "wall": 5.0, "per": 20}, "control": control(median={"first_request_s": 0.02, "step_gap_s": 0.001, "cpu_s": 0.5, "wall_s": 2.0})},
                 {"h": "b", "m": {"start": 0.16, "gap": 0.02, "cpu": 1.0, "wall": 5.0, "per": 40}, "control": control(median={"first_request_s": 0.04, "step_gap_s": 0.001, "cpu_s": 0.5, "wall_s": 2.0})}]
        out = run_control(lanes=lanes, keys=["start", "per"])
        self.assertAlmostEqual(out["rows"][0]["ratio"]["start"], 5)
        self.assertAlmostEqual(out["rows"][1]["ratio"]["start"], 4)
        self.assertEqual(out["rank"]["start"], [0, 1])  # raw says a is faster; the ratio says b is
        self.assertEqual(out["rank"]["per"], [1, 0])  # memory stays on raw values

    def test_a_lane_without_a_usable_control_is_unranked_on_time_but_still_shown(self):
        m = {"start": 0.10, "gap": 0.02, "cpu": 1.0, "wall": 5.0, "per": 20}
        lanes = [{"h": "a", "m": m, "control": control()}, {"h": "b", "m": {**m, "start": 0.3, "per": 60}, "control": control(median={"first_request_s": 0.01, "step_gap_s": 0.001, "cpu_s": 0.5, "wall_s": 2.0})},
                 {"h": "c", "m": {**m, "start": 0.2, "per": 40}, "control": None}]
        out = run_control(lanes=lanes, keys=["start", "gap", "cpu", "wall", "per"])
        self.assertEqual(out["rows"][2]["noControl"], "no control")
        self.assertEqual(out["rows"][2]["start"], 0.2)  # raw stays on the row
        for k in ("start", "gap", "cpu", "wall"):
            self.assertIsNone(out["rank"][k][2], k)
        self.assertEqual(out["rank"]["per"], [1, 0, 0.5])
        reasons = {}
        for name, c, odd in (("off", control(state="off"), 0), ("failed", control(state="failed"), 0), ("skip", control(state="skipped"), 0),
                             ("noisy", control(noisy=True), 0)):
            lone = run_control(lanes=[{"h": "x", "m": m, "control": c}], keys=[])
            reasons[name] = lone["rows"][0]["noControl"]
            self.assertEqual(lone["rows"][0]["ratio"], {})
        self.assertEqual(reasons, {"off": "no control", "failed": "control failed", "skip": "no control", "noisy": "control noisy"})
        odd = run_control(lanes=[{"h": h, "m": m, "control": control(key=k)} for h, k in (("a", "k1"), ("b", "k1"), ("c", "k2"))], keys=[])
        self.assertEqual([r["noControl"] for r in odd["rows"]], [None, None, "control differs from other lanes"])

    def test_wall_is_normalised_at_size_one_only(self):
        lane = {"h": "a", "m": {"start": 0.1, "wall": 5.0}, "control": control()}
        self.assertAlmostEqual(run_control(lanes=[lane], size=1, keys=[])["rows"][0]["ratio"]["wall"], 2.5)
        self.assertNotIn("wall", run_control(lanes=[lane], size=32, keys=[])["rows"][0]["ratio"])

    def test_a_pair_marked_tie_shares_a_shade_and_neither_is_best(self):
        values = {"a": 1.0, "b": 1.5, "c": 3.0, "d": 5.0}
        pos = run_control(pos=[{"values": values, "ties": [{"a": "a", "b": "b", "tie": True}, {"a": "a", "b": "c", "tie": False}]},
                               {"values": values, "ties": [{"a": "b", "b": "c", "tie": True}]}])["pos"]
        self.assertEqual((pos[0]["a"], pos[0]["b"]), (1, 1))  # tied with the best: both position 1, so neither is the sole best
        self.assertLess(pos[0]["c"], 1)
        self.assertEqual(pos[0]["d"], 0)
        self.assertEqual(pos[1]["b"], pos[1]["c"])  # one shade for the tied pair
        self.assertEqual((pos[1]["a"], pos[1]["d"]), (1, 0))
        self.assertEqual(sum(1 for t in pos[1].values() if t == 1), 1)  # sole best here: a is tied with nobody
        # best vs worst tied: the column is blank
        blank = run_control(pos=[{"values": values, "ties": [{"a": "a", "b": "d", "tie": True}]}])["pos"][0]
        self.assertEqual(set(blank.values()), {None})

    def test_without_stats_positions_are_the_ten_percent_rule_unchanged(self):
        out = run_control(pos=[{"values": {"a": 1.00, "b": 1.05}, "ties": None}, {"values": {"a": 1.0, "b": 2.0, "c": None}, "ties": None}])["pos"]
        self.assertEqual(out[0], {"a": None, "b": None})
        self.assertEqual(out[1], {"a": 1, "b": 0, "c": None})

    def test_the_rule_text_names_the_rule(self):
        n6 = {"n": 6, "rule": "bootstrap", "min_pairs": 6}
        n5 = {"n": 5, "rule": "gap10", "min_pairs": 6}
        rule = run_control(stats=[None, {"n": 1, "rule": "gap10"}, n5, n6])["rule"]
        self.assertEqual(rule[0], rule[1])
        self.assertEqual(rule[0], "n = 1 · shading uses the 10% gap rule, a fallback, not a test")
        self.assertIn("n = 5", rule[2])
        self.assertIn("10% gap", rule[2])
        self.assertEqual(rule[3], "n = 6 repetitions · tie = 95% bootstrap interval of the paired difference includes 0 · time metrics relative to each lane’s control")

    def test_first_request_weight_is_an_ungated_lower_is_better_metric(self):
        out = subprocess.run([NODE, "-e", "global.window={};const R=require(process.argv[1]+'/rank.js');console.log(JSON.stringify(R.HEAT.first))", str(FRONT)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(out.stdout), ["first", "low"])


@unittest.skipUnless(NODE, "node is needed to check the page's scripts")
class PageFilesTests(unittest.TestCase):
    def test_scripts_parse_and_the_page_loads_the_rules_before_the_app(self):
        for name in ("heat.js", "rank.js", "app.js"):
            done = subprocess.run([NODE, "--check", str(FRONT / name)], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
        html = (FRONT / "index.html").read_text()
        self.assertLess(html.index("heat.js"), html.index("rank.js"))
        self.assertLess(html.index("rank.js"), html.index("app.js"))

    def test_every_cell_state_the_lane_reports_is_drawn_and_named_in_the_legend(self):
        import re
        import sys
        sys.path.insert(0, str(FRONT.parent / "src"))
        serve = (FRONT.parent / "src" / "swarm" / "serve.py").read_text()
        block = serve[serve.index("# Every state here has a name in the page's legend."):serve.index("agents.append({")]
        states = set(re.findall(r'"(waiting|preparing|starting|working|checking|passed|failed|notrun|[a-z]+)"', block)) - \
            {"launching", "infra", "cancelled", "phase", "failure", "agent_exited"}
        self.assertEqual(states, {"waiting", "preparing", "starting", "working", "checking", "passed", "failed", "notrun"})
        app, css, html = (FRONT / "app.js").read_text(), (FRONT / "style.css").read_text(), (FRONT / "index.html").read_text()
        cell = dict(re.findall(r"(\w+): '(cell[ a-z]*)'", app[app.index("const CELL = {"):app.index("};", app.index("const CELL = {"))]))
        legend = {"cell": "k-wait", "cell prep": "k-prep", "cell start": "k-start", "cell alive": "k-alive", "cell check": "k-check",
                  "cell passed": "k-pass", "cell failed": "k-fail", "cell norun": "k-norun"}
        for state in states:
            self.assertIn(state, cell, state)
            if cell[state] != "cell":
                self.assertIn("." + cell[state].replace(" ", "."), css, state)
            self.assertIn(f'class="{legend[cell[state]]}"', html, state)
            self.assertIn("." + legend[cell[state]] + " {", css, state)


NODE_JS = """global.window = {}; const dir = process.argv[process.argv.length - 1];
const Heat = require(dir + '/heat.js'), Rank = require(dir + '/rank.js');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8')); console.log(JSON.stringify(eval(input.js)));"""


def node_eval(js: str):
    done = subprocess.run([NODE, "-e", NODE_JS, str(FRONT)], input=json.dumps({"js": js}), capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page's rules")
class SeriesAndFirstRequestTests(unittest.TestCase):
    def test_first_request_segments_sum_to_the_total(self):
        whole = {"canonical_bytes": 10000, "system_bytes": 3000, "tools_bytes": 4000, "user_bytes": 2500, "instruction_bytes": 500, "injected_bytes": 1800}
        unsplit = {**whole, "instruction_bytes": None, "injected_bytes": None}
        out = node_eval(f"[{json.dumps(whole)}, {json.dumps(unsplit)}].map((f) => Rank.firstRequestSegments(f))")
        for segs, total in zip(out, (10000, 10000)):
            self.assertEqual(sum(s["bytes"] for s in segs), total)
        self.assertEqual([s["key"] for s in out[0]], ["system", "tools", "injected", "task", "other"])
        self.assertEqual({s["key"]: s["bytes"] for s in out[0]}["other"], 700)  # whatever is not named stays in the sum
        self.assertEqual([s["key"] for s in out[1]], ["system", "tools", "task", "other"])  # not verbatim: no injected figure invented
        self.assertEqual(node_eval("Rank.firstRequestSegments({canonical_bytes: null, system_bytes: 1})"), None)
        self.assertEqual(node_eval("Rank.firstRequestSegments({canonical_bytes: 10, system_bytes: 9, tools_bytes: 9, user_bytes: 9})"), None)

    def test_strip_geometry_leaves_failed_repetitions_hollow_and_off_the_axis(self):
        g = node_eval("Rank.strip([1, null, 3, 2, null, 4], 4, 100)")
        self.assertEqual([d["x"] for d in g["dots"]], [25, None, 75, 50, None, 100])
        self.assertEqual(g["missing"], 2)
        self.assertEqual((g["median"], g["box"], g["range"]), (62.5, [43.75, 81.25], [25, 100]))  # median of 1,2,3,4 is 2.5
        none = node_eval("Rank.strip([null, null], 4, 100)")
        self.assertEqual((none["median"], none["box"], none["range"]), (None, None, None))
        self.assertTrue(all(d["x"] is None for d in none["dots"]))  # never drawn at 0
        given = node_eval("Rank.strip([1, 2], 4, 100, {median: 2, p25: 1, p75: 3, min: 1, max: 5})")
        self.assertEqual((given["median"], given["range"]), (50, [25, 100]))  # /stats numbers win; max is clipped to the axis

    def test_the_rule_sentence_follows_stats_rule_for_one_five_and_six(self):
        got = node_eval("[{n:1,rule:'bootstrap'},{n:5,rule:'gap10',min_pairs:6},{n:6,rule:'bootstrap',min_pairs:6},{n:6,rule:'gap10',min_pairs:6}].map(Rank.ruleText)")
        self.assertTrue(got[0].startswith("n = 1 · "))
        self.assertIn("10% gap rule", got[0])
        self.assertIn("fewer than 6 valid pairs", got[1])
        self.assertIn("95% bootstrap interval", got[2])
        self.assertNotIn("bootstrap", got[1] + got[3])  # the sentence never claims a test the stats did not run

    def test_series_ties_reach_the_score_and_a_lane_without_control_stays_unranked(self):
        rows = [{"r": {"h": h, "name": h}, "m": {"passed": 1, "n": 1, "timeouts": 0, "start": s, "ratio": {"start": q}, "noControl": None}}
                for h, s, q in (("a", 0.1, 5.0), ("b", 0.16, 4.0), ("c", 0.2, 9.0))]
        rows[2]["m"]["noControl"] = "no control"
        js = f"(() => {{ const rows = {json.dumps(rows)}; const t = {{start: [{{a: 'a', b: 'b', tie: true}}]}}; " \
             "const s = Rank.score(rows, Heat.rank, t); return [s.positions.start, Rank.score(rows, Heat.rank).positions.start, Rank.excluded(rows[2].r, rows[2].m, 'start', 0)]; })()"
        tied, plain, why = node_eval(js)
        self.assertEqual(tied, [None, None, None])  # a and b tie, c has no control: only two comparable and they tie, so no shade
        self.assertEqual(plain[:2], [0, 1])  # on the ratio b is better than a, though its raw number is slower
        self.assertIsNone(plain[2])
        self.assertEqual(why, "no control: shown raw, not ranked")


@unittest.skipUnless(NODE, "node is needed to check the page's scripts")
class PageStructureTests(unittest.TestCase):
    def test_every_element_the_app_looks_up_is_in_the_page(self):
        import re
        app, html = (FRONT / "app.js").read_text(), (FRONT / "index.html").read_text()
        for ident in set(re.findall(r"\$\('#([\w-]+)'", app)):
            self.assertIn(f'id="{ident}"', html, ident)
        self.assertIn('id="first-sec" hidden', html)  # hidden until a lane reports first-request fields

    def test_run_states_before_the_agents_are_worded_on_the_page(self):
        serve = (FRONT.parent / "src" / "swarm" / "serve.py").read_text()
        app = (FRONT / "app.js").read_text()
        block = app[app.index("const RUN_WORDS = {"):app.index("};", app.index("const RUN_WORDS = {"))]
        for state, words in (("checking", "checking the machine is quiet"), ("control", "running this VM’s control")):
            self.assertIn(f'rec["state"] = "{state}"', serve)
            self.assertIn(f"{state}: '{words}'", block)
        self.assertIn("not run: lane not quiet (", app)
        self.assertIn("harness changed", app)
        self.assertIn("rep ${view} of ${race.repeats}", app)


if __name__ == "__main__":
    unittest.main()
