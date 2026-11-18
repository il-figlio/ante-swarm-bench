"""Check h01: .github/workflows/ci.yml is a real CI workflow — it runs
the test suite on push and pull_request, on a runner with a checkout
and a Python setup step. Parsed with the YAML-subset parser in
tests/hygiene.py, so the assertions are semantic, not textual."""
import re
import unittest

from tests.hygiene import ROOT, parse_yaml, workflow_events, workflow_steps

WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEST_RUN = re.compile(r"python3?\s+-m\s+unittest\s+discover")


def load_workflow(test):
    if not WORKFLOW.is_file():
        test.fail(".github/workflows/ci.yml does not exist")
    try:
        wf = parse_yaml(WORKFLOW.read_text())
    except ValueError as exc:
        test.fail("ci.yml did not parse: %s" % exc)
    if not isinstance(wf, dict):
        test.fail("ci.yml should be a mapping at the top level")
    return wf


class TestCiWorkflow(unittest.TestCase):
    def test_file_exists(self):
        load_workflow(self)

    def test_triggers_push_and_pr(self):
        events = workflow_events(load_workflow(self).get("on"))
        self.assertIn("push", events,
                      "workflow must trigger on push (got %s)"
                      % sorted(events))
        self.assertIn("pull_request", events,
                      "workflow must trigger on pull_request (got %s)"
                      % sorted(events))

    def test_a_job_declares_a_runner(self):
        wf = load_workflow(self)
        jobs = wf.get("jobs")
        self.assertIsInstance(jobs, dict, "workflow needs a jobs: mapping")
        self.assertTrue(jobs, "jobs: is empty")
        runners = [j.get("runs-on") for j in jobs.values()
                   if isinstance(j, dict)]
        self.assertTrue(any(runners),
                        "no job declares runs-on")

    def test_steps_checkout_setup_and_test(self):
        steps = workflow_steps(load_workflow(self))
        self.assertTrue(steps, "no job has a steps: list")
        uses = [str(s.get("uses", "")) for s in steps]
        runs = [str(s.get("run", "")) for s in steps]
        self.assertTrue(any("checkout" in u for u in uses),
                        "no step checks out the repo "
                        "(expected uses: actions/checkout...)")
        self.assertTrue(any("setup-python" in u for u in uses),
                        "no step sets up Python "
                        "(expected uses: actions/setup-python...)")
        self.assertTrue(any(TEST_RUN.search(r) for r in runs),
                        "no step runs `python3 -m unittest discover`")


if __name__ == "__main__":
    unittest.main()
