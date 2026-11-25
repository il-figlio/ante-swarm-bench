#!/usr/bin/env python3
"""Tests for bench/build-cohorts.py, bench/protocol/gate-v3.sh, and bench/protocol/summarize_run.py."""
import json
from itertools import product
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_COHORTS = ROOT / "bench" / "build-cohorts.py"
GATE_SH = ROOT / "bench" / "protocol" / "gate-v3.sh"
SUMMARIZE = ROOT / "bench" / "protocol" / "summarize_run.py"


def build_synthetic_package(td: Path, overrides=None, meta_overrides=None, levels=(1, 8), repeats=(1, 2, 3),
                            models=("claude-sonnet-5-5",), harnesses=("claude-code", "ante", "pi", "hermes"),
                            matrix_id="shared-v3-claude-20261031", revision="shared-v3"):
    runs = td / "runs"
    runs.mkdir()
    log_lines = []
    rid_n = 210
    for rep, lvl, model, harness in product(repeats, levels, models, harnesses):
        rid = f"r{rid_n}"
        rid_n += 1
        d = runs / rid
        d.mkdir()
        rec = {
            "run_id": rid,
            "matrix_id": matrix_id,
            "model": model,
            "harness": harness,
            "harness_version": "2.1.312 (Claude Code)",
            "cells": lvl,
            "repeat": rep,
            "passed": 32,
            "attempted": 32,
            "total_secs": 100.0,
            "peak_process_tree_rss_kb": 50000,
            "effort": "medium",
            "provider_route": "managed model gateway · anthropic(gateway)",
            "telemetry": "off",
            "protocol_revision": revision,
            "protocol": "shared-v3-claude",
            "pack": "toolbox",
            "task_revision": "rev-aaa",
            "protocol_sha256": "proto-sha",
            "model_verified": True,
            "cancelled": False,
            "results": [{"task": f"t01-{i:02d}", "passed": True} for i in range(1, 33)],
        }
        meta = {"run_id": rid, "matrix_id": rec["matrix_id"], "model": model, "harness": harness, "cells": lvl, "repeat": rep}
        if overrides and rid == "r210":
            rec.update(overrides)
        if meta_overrides and rid == "r210":
            meta.update(meta_overrides)
        (d / "receipts.json").write_text(json.dumps(rec))
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "samples.json").write_text("[]")
        log_lines.append(
            f"2026-11-01T00:00:00Z {rid} model={rec['model']} harness={rec['harness']} "
            f"level=x{rec['cells']} repeat={rec['repeat']} passed/attempted={rec['passed']}/{rec['attempted']} "
            f"total_secs={rec['total_secs']} exit=0"
        )
    log = td / "progress.log"
    log.write_text("\n".join(log_lines) + "\n")
    out = td / "cohorts.json"
    proc = subprocess.run([sys.executable, str(BUILD_COHORTS), "--runs", str(runs), "--log", str(log), "--out", str(out)],
                          capture_output=True, text=True)
    return proc, (json.loads(out.read_text()) if out.exists() else None)


class BuildCohortsTests(unittest.TestCase):
    def test_valid_claude_cohort_and_null_rss(self):
        with tempfile.TemporaryDirectory() as td:
            proc, doc = build_synthetic_package(Path(td), overrides={"peak_process_tree_rss_kb": None})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(doc["sources"]["run_count"], 24)
            self.assertIn("Claude Sonnet 5.5", doc["protocol_line"])
            self.assertNotIn("GPT-6.1", doc["protocol_line"])
            self.assertEqual(doc["cohorts"][0]["harnesses"][0]["version"], "2.1.312")
            self.assertIsNone(doc["cohorts"][0]["cells"][0]["peak_process_tree_rss_kb"])

    def test_complete_legacy_matrix(self):
        with tempfile.TemporaryDirectory() as td:
            proc, doc = build_synthetic_package(Path(td), matrix_id="shared-v2-20261030", revision="shared-v2",
                                                models=("openai/gpt-6.1-sol", "openai/gpt-6.1-terra"),
                                                harnesses=("ante", "pi", "hermes", "ante-pi"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(doc["sources"]["run_count"], 48)

    def test_rejects_unverified_cancelled_test_upstream_mismatches_and_bad_grid(self):
        cases = [
            ("model_verified", {"overrides": {"model_verified": False}}),
            ("cancelled", {"overrides": {"cancelled": True}}),
            ("test_upstream", {"overrides": {"test_upstream": "http://127.0.0.1:8791"}}),
            ("task_revision", {"overrides": {"task_revision": "rev-bbb"}}),
            ("results", {"overrides": {"results": [{"task": "t01-01", "passed": False}] * 32}}),
            ("meta.json", {"meta_overrides": {"harness": "ante"}}),
            ("grid", {"levels": (1, 4)}),
            ("grid", {"harnesses": ("ante", "pi", "claude-code")}),
            ("grid", {"matrix_id": "shared-v3-codex-20261031", "harnesses": ("codex",),
                      "models": ("openai/gpt-6.1-sol",)}),
        ]
        for expected_err, kw in cases:
            with self.subTest(case=expected_err), tempfile.TemporaryDirectory() as td:
                proc, _ = build_synthetic_package(Path(td), **kw)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(expected_err, proc.stderr)


class GateAndSummarizeTests(unittest.TestCase):
    def test_gate_rejects_unknown_cohort_argument(self):
        proc = subprocess.run(["bash", str(GATE_SH), "cluade"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("GATE PASS", proc.stdout)

    def test_summarize_run_missing_receipt_contract_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run([sys.executable, str(SUMMARIZE), td, "r999"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "r999 NO-RECEIPT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
