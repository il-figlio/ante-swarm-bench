"""Shared check for every log-triage task.

The runner executes ``python3 -m unittest tests.check_analysis`` from the
repo root after the agent exits. There is exactly one expected deliverable:
``analysis/host-NN.md``. The host is derived from the filename and the file
is validated against ``tests/manifest.json`` (the planted ground truth):

* exactly one ``analysis/*.md`` file, named after a known host
* a verdict declaration: ANOMALY for affected hosts, CLEAN for the rest
* affected hosts: the stated window must include timestamps within +-60 s
  of the true window start and end
* every file:line citation must resolve to a real line; for affected hosts
  at least one citation must point at genuine incident evidence (a line
  carrying the signature, a timestamp inside the window, or a line inside
  a manifest evidence span)
* a clean-host file must not claim the incident window
* a one-line grep-style command that reproduces the evidence

The manifest sits in tests/ where an agent can read it. That is a known,
documented temptation: every harness faces it, and an honest derivation of
the same facts is indistinguishable from a copied one.
"""
import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())

HOST_RE = re.compile(r"^host-\d{2}$")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*Z?")
# bare HH:MM:SS times are interpreted on the corpus date (the logs only
# ever cover that one day)
BARE_TS_RE = re.compile(r"(?<![\d:T])\b(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*Z?\b")
CITE_RE = re.compile(
    r"((?:[\w.-]+/)*[\w.-]+\.log)\s*[:#]\s*L?(\d+)(?:\s*[-–—~]\s*L?(\d+))?")
GREP_RE = re.compile(
    r"^\s*`{0,3}\s*(?:\$\s*|>\s*)?(?:zgrep|egrep|fgrep|grep|rg|awk|sed|cat)\b",
    re.M | re.I)

WINDOW_TOL = timedelta(seconds=60)
TS_EVIDENCE_TOL = timedelta(seconds=120)
SPAN_MARGIN = 60

CLEAN_PHRASES = re.compile(
    r"no[\s_-]*anomal|not\s+affected|unaffected|uninvolved|not\s+involved|"
    r"not\s+part\s+of|no\s+evidence|no\s+signs?\s+of|\bclean\b|"
    r"nothing\s+unusual|no\s+incident|normal\s+traffic|unrelated|"
    r"not\s+impacted|no\s+impact|healthy", re.I)
ANOMALY_PHRASES = re.compile(
    r"anomal|affected|involved|incident|confirmed|impact|degrad|"
    r"cascade|\byes\b", re.I)
VERDICT_LINE = re.compile(
    r"verdict|conclusion|finding|result|status\s*[:=]|bottom\s*line", re.I)


def parse_ts(s):
    s = s.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify_line(line):
    if CLEAN_PHRASES.search(line):
        return "clean"
    if ANOMALY_PHRASES.search(line):
        return "anomaly"
    return None


def classify(text):
    """Return 'anomaly', 'clean', or None for the file's declared verdict."""
    lines = text.splitlines()
    for line in lines:  # explicit verdict-style lines win
        if VERDICT_LINE.search(line):
            c = classify_line(line)
            if c:
                return c
    for line in lines:  # otherwise first keyword-bearing line decides
        c = classify_line(line)
        if c:
            return c
    return None


def all_timestamps(text):
    out = []
    for m in TS_RE.finditer(text):
        try:
            out.append(parse_ts(m.group(0)))
        except ValueError:
            pass
    day = MANIFEST["corpus"]["log_date"]
    for m in BARE_TS_RE.finditer(text):
        try:
            out.append(parse_ts(f"{day}T{m.group(0).replace('Z', '').strip()}Z"))
        except ValueError:
            pass
    return out


class TestLogTriageAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        adir = ROOT / "analysis"
        cls.md_files = sorted(adir.glob("*.md")) if adir.is_dir() else []
        cls.host = None
        cls.hinfo = None
        cls.text = ""
        cls.path = None
        if len(cls.md_files) == 1:
            cls.path = cls.md_files[0]
            stem = cls.path.stem
            if HOST_RE.match(stem) and stem in MANIFEST["hosts"]:
                cls.host = stem
                cls.hinfo = MANIFEST["hosts"][stem]
                cls.text = cls.path.read_text(errors="replace")
        cls._file_cache = {}

    # -- helpers ---------------------------------------------------------

    def _resolve(self, ref):
        """Resolve a citation path to a real log file, or None."""
        ref = ref.strip().lstrip("./")
        candidates = [ROOT / ref,
                      ROOT / "logs" / ref,
                      ROOT / "logs" / (self.host or "") / ref]
        for c in candidates:
            if c.is_file():
                return c
        if self.host:
            hits = list((ROOT / "logs" / self.host).rglob(Path(ref).name))
            if hits:
                return hits[0]
        return None

    def _lines(self, path):
        key = str(path)
        if key not in self._file_cache:
            self._file_cache[key] = path.read_text(errors="replace").splitlines()
        return self._file_cache[key]

    def _citations(self):
        """[(ref, path_or_None, start_line, end_line)] for the file."""
        out = []
        for m in CITE_RE.finditer(self.text):
            ref, a, b = m.group(1), int(m.group(2)), m.group(3)
            out.append((ref, self._resolve(ref), a, int(b) if b else a))
        return out

    def _is_evidence(self, path, a, b):
        """Does the cited line/range point at genuine incident evidence?"""
        hinfo = self.hinfo
        lines = self._lines(path)
        w0 = parse_ts(hinfo["window_utc"]["start"]) - TS_EVIDENCE_TOL
        w1 = parse_ts(hinfo["window_utc"]["end"]) + TS_EVIDENCE_TOL
        scan_end = min(len(lines), b, a + 400)
        for i in range(a - 1, scan_end):
            text = lines[i]
            if any(sig in text for sig in hinfo["signatures"]):
                return True
            m = TS_RE.search(text)
            if m:
                try:
                    t = parse_ts(m.group(0))
                except ValueError:
                    continue
                if w0 <= t <= w1:
                    return True
        rel = path.relative_to(ROOT).as_posix()
        for span in hinfo.get("evidence", []):
            if span["file"] == rel and not (
                    b < span["first_line"] - SPAN_MARGIN
                    or a > span["last_line"] + SPAN_MARGIN):
                return True
        return False

    def _window_claimed(self):
        """True if the file states both ends of the fleet incident window."""
        w0 = parse_ts(MANIFEST["incident"]["window_utc"]["start"])
        w1 = parse_ts(MANIFEST["incident"]["window_utc"]["end"])
        ts = all_timestamps(self.text)
        near_s = any(abs(t - w0) <= WINDOW_TOL for t in ts)
        near_e = any(abs(t - w1) <= WINDOW_TOL for t in ts)
        return near_s and near_e

    # -- tests ------------------------------------------------------------

    def test_single_analysis_file(self):
        self.assertEqual(len(self.md_files), 1,
                         f"expected exactly one analysis/*.md file, found "
                         f"{[f.name for f in self.md_files] or 'none'}")
        self.assertIsNotNone(self.host,
                             f"{self.path.name} is not named host-NN.md for a "
                             f"known host")
        self.assertGreater(len(self.text.strip()), 40,
                           "analysis file is effectively empty")

    def test_verdict(self):
        self.assertIsNotNone(self.host, "no valid analysis file (see file test)")
        verdict = classify(self.text)
        if self.hinfo["affected"]:
            self.assertEqual(
                verdict, "anomaly",
                f"{self.host} was part of the incident; the file must "
                f"declare an anomaly (found: {verdict or 'no verdict'})")
        else:
            self.assertEqual(
                verdict, "clean",
                f"{self.host} was not part of the incident; the file must "
                f"declare it clean (found: {verdict or 'no verdict'})")

    def test_window(self):
        self.assertIsNotNone(self.host, "no valid analysis file")
        if not self.hinfo["affected"]:
            self.skipTest("clean host: no incident window required")
        w = self.hinfo["window_utc"]
        ts = all_timestamps(self.text)
        w0, w1 = parse_ts(w["start"]), parse_ts(w["end"])
        near_s = [t for t in ts if abs(t - w0) <= WINDOW_TOL]
        near_e = [t for t in ts if abs(t - w1) <= WINDOW_TOL]
        self.assertTrue(
            near_s and near_e,
            f"file must state the incident window within {WINDOW_TOL.seconds}s "
            f"of {w['start']} .. {w['end']}")

    def test_clean_host_claims_no_window(self):
        self.assertIsNotNone(self.host, "no valid analysis file")
        if self.hinfo["affected"]:
            self.skipTest("affected host")
        verdict = classify(self.text)
        if verdict == "clean":
            return  # citing the reported window to rule it out is legitimate
        self.assertFalse(
            self._window_claimed(),
            f"{self.host} is clean per ground truth, but the file states "
            f"timestamps matching the incident window "
            f"({MANIFEST['incident']['window_utc']['start']} .. "
            f"{MANIFEST['incident']['window_utc']['end']})")

    def test_citations_resolve(self):
        self.assertIsNotNone(self.host, "no valid analysis file")
        cites = self._citations()
        self.assertTrue(cites, "file must cite at least one log file:line")
        for ref, path, a, b in cites:
            self.assertIsNotNone(path, f"citation does not resolve: {ref}:{a}")
            n = len(self._lines(path))
            self.assertLessEqual(a, n,
                                 f"{ref}:{a} out of range (file has {n} lines)")
            self.assertLessEqual(b, n,
                                 f"{ref}:{b} out of range (file has {n} lines)")
            self.assertGreaterEqual(a, 1)

    def test_citations_support_verdict(self):
        self.assertIsNotNone(self.host, "no valid analysis file")
        if not self.hinfo["affected"]:
            self.skipTest("clean host: resolvable citations suffice")
        cites = self._citations()
        ok = [f"{r}:{a}" for r, p, a, b in cites
              if p is not None and self._is_evidence(p, a, b)]
        self.assertTrue(
            ok,
            f"no citation points at incident evidence for {self.host} "
            f"(signature {self.hinfo['signatures']} in window "
            f"{self.hinfo['window_utc']['start']} .. "
            f"{self.hinfo['window_utc']['end']}; first occurrence at "
            f"{self.hinfo['first_occurrence']['file']}:"
            f"{self.hinfo['first_occurrence']['line']})")

    def test_grep_one_liner(self):
        self.assertIsNotNone(self.host, "no valid analysis file")
        hits = [l for l in self.text.splitlines()
                if GREP_RE.match(l) and ("log" in l or "logs/" in l)]
        self.assertTrue(hits, "file must end with a one-line grep (or "
                              "equivalent) command referencing the logs")


if __name__ == "__main__":
    unittest.main()
