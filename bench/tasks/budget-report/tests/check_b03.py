"""Check b03: friday-update.md — the weekly cost note. Required `##`
sections in order, the headline figures present, and anti-hallucination:
every dollar figure and percentage cited must match a real value in the
manifest."""
import unittest

from tests import truth

MD = truth.ROOT / "friday-update.md"
SECTIONS = ["total spend", "by team", "over cap", "week over week"]


def required_usd():
    """Figures the note must actually cite."""
    m = truth.MANIFEST
    out = [m["totals"]["spend_usd"], m["totals"]["cap_usd"],
           m["totals"]["prev_spend_usd"], abs(m["totals"]["wow_delta_usd"])]
    out += [t["spend_usd"] for t in m["teams"].values()]
    for o in m["over_cap"]:
        out += [o["spend_usd"], o["cap_usd"], o["overage_usd"]]
    return out


class TestFridayUpdate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tampered = truth.tampered_inputs()
        cls.exists = MD.is_file()
        cls.text = MD.read_text(errors="replace") if cls.exists else ""
        cls.amounts = truth.money_amounts(cls.text)
        cls.headings = [h.casefold().replace("-", " ")
                        for h in truth.HEADING_RE.findall(cls.text)]

    def test_inputs_untouched(self):
        self.assertFalse(
            self.tampered,
            "input files changed or missing — the exports, caps.json "
            "and prices.json are read-only evidence: %s" % self.tampered)

    def test_file_exists(self):
        self.assertTrue(self.exists, "friday-update.md does not exist")

    def test_sections_in_order(self):
        if not self.exists:
            self.fail("friday-update.md does not exist")
        pos, missing = 0, []
        for want in SECTIONS:
            hit = next((i for i, h in enumerate(self.headings)
                        if i >= pos and h.startswith(want)), None)
            if hit is None:
                missing.append("## %s" % want)
            else:
                pos = hit + 1
        self.assertFalse(
            missing,
            "missing/out-of-order ## sections %s — want, in order: %s; "
            "got %s" % (missing,
                        ["## " + s for s in SECTIONS], self.headings))

    def test_required_figures_present(self):
        if not self.exists:
            self.fail("friday-update.md does not exist")
        missing = [v for v in required_usd()
                   if not truth.near(v, self.amounts)]
        self.assertFalse(
            missing,
            "required figures not cited in the note: %s" % missing)

    def test_over_cap_keys_named(self):
        if not self.exists:
            self.fail("friday-update.md does not exist")
        missing = [o["key"] for o in truth.MANIFEST["over_cap"]
                   if o["key"] not in self.text]
        self.assertFalse(missing,
                         "over-cap keys not named: %s" % missing)

    def test_no_invented_dollar_figures(self):
        if not self.exists:
            self.fail("friday-update.md does not exist")
        allowed = truth.allowed_usd()
        bad = [a for a in self.amounts if not truth.near(a, allowed)]
        self.assertFalse(
            bad, "dollar figures that match no real value in the "
                 "manifest — invented or mis-derived: %s" % bad)

    def test_no_invented_percentages(self):
        if not self.exists:
            self.fail("friday-update.md does not exist")
        allowed = truth.allowed_pct()
        bad = [p for p in truth.percents(self.text)
               if not truth.near(p, allowed, truth.PCT_EPS)]
        self.assertFalse(
            bad, "percentages that match no real value: %s" % bad)


if __name__ == "__main__":
    unittest.main()
