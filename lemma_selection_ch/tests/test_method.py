"""Independent CH checks for incomplete feedback and target-only failures."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gls import Query, Status
from method import run
from runtime import RunContext


class Backend:
    def initial(self, ids, timeout):
        return Query(Status.UNSAT)
    def step(self, ids, timeout):
        return Query(Status.SAT)
    def bmc(self, depth, timeout):
        return Query(Status.UNSAT)
    def candidates_initial(self, ids, timeout):
        return Query(Status.UNSAT)
    def candidates_step(self, ids, timeout):
        return Query(Status.SAT)
    def step_witness(self, ids, timeout, *, candidates_only):
        return Query(Status.UNKNOWN, executed=False)


class MethodTests(unittest.TestCase):
    def test_missing_witness_does_not_delete_candidates_or_prove(self):
        with tempfile.TemporaryDirectory() as folder:
            context = RunContext(Path(folder), 600, 120)
            context.start()
            result = run(Backend(), None, context, {"L01": "candidate"})
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(context.events, [])

    def test_goal_step_counterexample_is_not_a_reachable_unsafe_verdict(self):
        class InductiveCandidates(Backend):
            def candidates_step(self, ids, timeout):
                return Query(Status.UNSAT)
        with tempfile.TemporaryDirectory() as folder:
            context = RunContext(Path(folder), 600, 120)
            context.start()
            result = run(InductiveCandidates(), None, context, {"L01": "candidate"})
        self.assertEqual(result.status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
