import json
import random
import time
import unittest

from swarm import stats

CONTROL = {"state": "done", "noisy": False, "key": "k1",
           "median": {"first_request_s": 0.1, "step_gap_s": 0.02, "cpu_s": 1.0, "wall_s": 10.0}}


def entry(start=0.2, cohort="c1", **over):
    result = {"harness": "h", "attempts": 4, "passes": 4, "first_request_s_median": start, "step_gap_s_median": 0.04,
              "cpu_s_median": 2.0, "wall_s_median": 8.0, "batch_wall_s_total": 20.0, "peak_rss_mb_median": 50.0,
              "successes_per_minute": 3.0, "mem_per_agent_mb": 40.0, "disk_kb_median": 100.0, "context_slope_median": 5.0,
              "agents_mem_mb_peak": 200.0, "first_request_bytes_median": 9000}
    out = {"run": "r", "error": None, "state": "done", "result": result, "control": json.loads(json.dumps(CONTROL)),
           "gate": {}, "cohort": cohort, "canary": {}}
    out.update(over)
    return out


def race(series, size=4, lanes=("a", "b")):
    """series: {lane: [start values per repetition]}"""
    n = max(len(v) for v in series.values())
    reps = [{"k": k, "order": "control_first", "lanes": {l: entry(v[k]) for l, v in series.items() if k < len(v) and v[k] is not None}}
            for k in range(n)]
    return {"id": "race1", "size": size, "repeats": n, "seed": 1, "reps": reps}


def noise(n, base, seed):
    r = random.Random(seed)
    return [base + r.uniform(-0.01, 0.01) for _ in range(n)]


class PairedTests(unittest.TestCase):
    def test_identical_series_tie(self):
        x = noise(8, 1.0, 1)
        p = stats.paired(x, list(x), "s")
        self.assertEqual((p["rule"], p["n"], p["tie"], p["diff_median"]), ("bootstrap", 8, True, 0))

    def test_shifted_series_not_a_tie(self):
        b = noise(8, 1.0, 2)
        p = stats.paired([v + 1 for v in b], b, "s")
        self.assertFalse(p["tie"])
        self.assertGreater(p["ci95"][0], 0)
        self.assertEqual(p["rule"], "bootstrap")

    def test_five_pairs_use_gap_rule(self):
        b = noise(5, 1.0, 3)
        p = stats.paired([v + 1 for v in b], b, "s")
        self.assertEqual((p["rule"], p["ci95"], p["tie"]), ("gap10", None, False))
        self.assertTrue(stats.paired([1.0] * 5, [1.05] * 5, "s")["tie"])

    def test_interval_matches_direct_resampling(self):
        d = [0.3, 1.2, 0.9, 2.2, 1.7, 0.4, 1.1, 0.8]
        # the medians are discrete, so a quantile can sit one step away from the sampled one
        lo, hi = stats.boot_ci(d, random.Random(5))
        r, meds = random.Random(9), []
        for _ in range(20000):
            meds.append(stats.median([r.choice(d) for _ in d]))
        meds.sort()
        self.assertAlmostEqual(lo, meds[int(0.025 * 20000)], delta=0.4)
        self.assertAlmostEqual(hi, meds[int(0.975 * 20000)], delta=0.4)

    def test_missing_pair_dropped(self):
        p = stats.paired([1, 2, None, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, None], "s")
        self.assertEqual(p["n"], 5)
        self.assertEqual(p["rule"], "gap10")


class RaceTests(unittest.TestCase):
    def test_same_input_same_bytes(self):
        r = race({"a": noise(8, 0.2, 1), "b": noise(8, 0.3, 2)})
        one = json.dumps(stats.race_stats(r), sort_keys=True)
        self.assertEqual(one, json.dumps(stats.race_stats(json.loads(json.dumps(r))), sort_keys=True))
        self.assertEqual(stats.race_stats(r)["rule"], "bootstrap")

    def test_missing_repetition_lowers_n(self):
        a, b = noise(8, 0.2, 1), noise(8, 0.3, 2)
        r = race({"a": a, "b": b})
        r["reps"][3]["lanes"].pop("b")
        s = stats.race_stats(r)["metrics"]["start"]
        self.assertEqual(s["pairs"][0]["n"], 7)
        self.assertEqual(s["lanes"]["b"]["reasons"][3], "missing")
        self.assertIsNone(s["lanes"]["b"]["values"][3])

    def test_error_and_noisy_control_give_none(self):
        r = race({"a": noise(8, 0.2, 1), "b": noise(8, 0.3, 2)})
        r["reps"][0]["lanes"]["a"]["error"] = "offline"
        r["reps"][1]["lanes"]["a"]["control"]["noisy"] = True
        r["reps"][2]["lanes"]["a"]["control"]["state"] = "failed"
        r["reps"][3]["lanes"]["a"]["control"]["key"] = "other"
        v, why = stats.rep_values(r)
        self.assertEqual(v["start"]["a"][:4], [None] * 4)
        self.assertEqual(why["start"]["a"][:4], ["error", "control_noisy", "control_failed", "control_key"])
        self.assertIsNotNone(v["start"]["b"][0])
        self.assertIsNotNone(v["pass"]["a"][1])  # raw metrics do not need the control

    def test_two_cohorts_no_statistics(self):
        r = race({"a": noise(8, 0.2, 1), "b": noise(8, 0.3, 2)})
        r["reps"][5]["lanes"]["a"]["cohort"] = "c2"
        m = stats.race_stats(r)["metrics"]["start"]
        self.assertEqual(m["lanes"]["a"]["excluded"], "cohort_changed")
        self.assertIsNone(m["lanes"]["a"]["median"])
        self.assertEqual(m["lanes"]["a"]["n"], 0)
        self.assertEqual(m["pairs"], [])

    def test_ratio_against_same_repetition_control(self):
        r = race({"a": [0.2, 0.3, 0.4], "b": [0.2, 0.2, 0.2]})
        r["reps"][1]["lanes"]["a"]["control"]["median"]["first_request_s"] = 0.3
        v, _ = stats.rep_values(r)
        self.assertEqual(v["start"]["a"], [2.0, 1.0, 4.0])
        self.assertEqual(stats.race_stats(r)["metrics"]["start"]["lanes"]["a"]["values"], [2.0, 1.0, 4.0])

    def test_wall_normalised_only_at_size_one(self):
        one, many = race({"a": [0.2]}, size=1), race({"a": [0.2]}, size=4)
        self.assertEqual(stats.rep_values(one)[0]["wall"]["a"], [0.8])
        self.assertEqual(stats.rep_values(many)[0]["wall"]["a"], [8.0])
        self.assertFalse(stats.race_stats(many)["metrics"]["wall"]["relative_to_control"])

    def test_floor_lane_not_ranked_and_old_race(self):
        r = race({"a": [0.2], "mini": [0.1]})
        s = stats.race_stats(r)["metrics"]["start"]
        self.assertEqual(s["lanes"]["mini"]["excluded"], "floor: not ranked")
        self.assertEqual(s["pairs"], [])
        old = {"id": "o", "size": 1, "lanes": {"a": entry(), "b": {"run": None, "error": "offline"}}}
        self.assertEqual(stats.race_stats(old)["n"], 1)
        self.assertEqual(stats.race_stats(old)["metrics"]["pass"]["lanes"]["b"]["excluded"], "error")

    def test_unknown_stays_none(self):
        r = race({"a": [0.2] * 3})
        del r["reps"][0]["lanes"]["a"]["result"]["cpu_s_median"]
        self.assertIsNone(stats.rep_values(r)[0]["cpu"]["a"][0])

    def test_zero_ran_cell_is_excluded_from_gated_metrics(self):
        e = entry()
        e["result"].update(attempts=4, passes=0, not_run=4)
        self.assertEqual(stats.entry_reason(e, gated=True), "under_half_passed")
        self.assertIsNone(stats.entry_reason(e, gated=False))

    def test_speed(self):
        rnd = random.Random(4)
        lanes = [f"l{i}" for i in range(6)]
        r = race({l: [rnd.uniform(0.1, 0.3) for _ in range(10)] for l in lanes})
        for rep in r["reps"]:
            for e in rep["lanes"].values():
                e["result"] = {k: (rnd.uniform(1, 100) if isinstance(v, float) else v) for k, v in e["result"].items()}
        t = time.time()
        out = stats.race_stats(r)
        self.assertLess(time.time() - t, 10)
        self.assertEqual(len(out["metrics"]["pass"]["pairs"]), 15)
        json.dumps(out)


if __name__ == "__main__":
    unittest.main()
