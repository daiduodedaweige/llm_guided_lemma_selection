"""Offline regression checks for interrupted fixed-pool evaluations."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluation import CacheMode, LemmaEvaluator, VerificationResult
from gls import Query, Reply, Status
from gls_runtime import atomic_write_json, run_selection
from scripts.evaluate_lemmas import evaluate_lemmas


class Backend:
    def __init__(self, outcome):
        self.outcome = outcome

    def initial(self, ids, timeout_s):
        if self.outcome == "ERROR":
            return Query(Status.ERROR, detail="unsupported induction", returncode=6)
        if self.outcome == "TIMEOUT":
            return Query(Status.TIMEOUT)
        if self.outcome == "UNSAFE":
            return Query(Status.SAT, p_initial=False)
        return Query(Status.UNSAT)

    def step(self, ids, timeout_s):
        if ids:
            return Query(Status.UNSAT)
        return Query(Status.SAT, source={}, next_state={}, p_next=False)

    def bmc(self, depth, timeout_s):
        return Query(Status.UNSAT)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ebmc = self.root / "ebmc"
        self.ebmc.write_text("#!/bin/sh\nexit 99\n")
        self.ebmc.chmod(0o755)
        self.environment = mock.patch.dict(os.environ, {
            "LLM_GLS_ENABLED": "1", "LLM_GLS_BUDGET_S": "600",
            "LLM_GLS_PRIORS_FILE": "", "LLM_GLS_RESUME_COMPLETED": "0",
            "LLM_REASONING_EFFORT": "xhigh", "LLM_WIRE_API": "responses",
            "LLM_BASE_URL": "https://example.test/v1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def evaluator(self, name):
        source = self.root / f"{name}.sv"
        if not source.exists():
            source.write_text("module main(input logic clock);\n"
                              "property prop; @(posedge clock) 1; endproperty\n"
                              "endmodule\n")
        return LemmaEvaluator(str(self.root), "ebmc", str(source), cache={},
                              cache_mode=CacheMode.NO_CACHE,
                              ebmc_path=str(self.ebmc), timeout=120)

    def audit_path(self, name):
        tag = hashlib.sha256(f"{name}:test-model:rep".encode()).hexdigest()[:12]
        return self.root / "logs" / f"gls_{tag}.json"

    def create_completed(self, name, outcome):
        lemmas = [] if outcome == "NO_PROOF_IN_POOL" else [
            "property lemma_1; @(posedge clock) 1; endproperty"]
        proposer = mock.Mock()
        proposer.propose.return_value = Reply(
            '{"ranked_ids":["L01"],"groups":[{"ids":["L01"],"reason":"test"}]}',
            0.001,
        )
        with mock.patch("gls_runtime.EbmcBackend", return_value=Backend(outcome)), \
                mock.patch("gls_runtime.RelayProposer", return_value=proposer):
            result = run_selection(self.evaluator(name), "test-model", lemmas,
                                   self.audit_path(name))
        self.assertEqual(json.loads(self.audit_path(name).read_text())["status"], outcome)
        return lemmas, result

    def test_resume_keeps_success_timeout_and_empty_pool_without_calls_or_rewrites(self):
        for outcome in ("PROVED", "TIMEOUT", "NO_PROOF_IN_POOL", "UNSAFE", "ERROR"):
            with self.subTest(outcome=outcome):
                lemmas, expected = self.create_completed(outcome, outcome)
                path = self.audit_path(outcome)
                original, mtime = path.read_bytes(), path.stat().st_mtime_ns
                with mock.patch.dict(os.environ, {"LLM_GLS_RESUME_COMPLETED": "1"}), \
                        mock.patch("gls_runtime.EbmcBackend", side_effect=AssertionError("formal rerun")), \
                        mock.patch("gls_runtime.RelayProposer", side_effect=AssertionError("LLM rerun")):
                    actual = run_selection(self.evaluator(outcome), "test-model", lemmas, path)
                self.assertEqual(actual, expected)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_resume_rejects_changed_inputs_configuration_and_incomplete_records(self):
        lemmas, _ = self.create_completed("sample", "PROVED")
        path = self.audit_path("sample")
        original = path.read_bytes()
        source = self.root / "sample.sv"
        source_text = source.read_text()
        for change in ("pool", "source", "binary", "budget", "effort", "provider",
                       "config", "prompt", "no_finish", "missing_field", "truncated"):
            with self.subTest(change=change):
                path.write_bytes(original)
                source.write_text(source_text)
                self.ebmc.write_text("#!/bin/sh\nexit 99\n")
                env = {"LLM_GLS_RESUME_COMPLETED": "1"}
                current_lemmas = lemmas
                audit = json.loads(original)
                if change == "pool":
                    current_lemmas = lemmas + ["property extra; 1; endproperty"]
                elif change == "source":
                    source.write_text(source_text + "// changed\n")
                elif change == "binary":
                    self.ebmc.write_text("#!/bin/sh\nexit 98\n")
                elif change == "budget":
                    env["LLM_GLS_BUDGET_S"] = "1800"
                elif change == "effort":
                    env["LLM_REASONING_EFFORT"] = "low"
                elif change == "provider":
                    env["LLM_BASE_URL"] = "https://changed.test/v1"
                elif change == "config":
                    audit["config"]["max_additional_calls"] = 99
                elif change == "prompt":
                    audit["prompt_version"] = "different-version"
                elif change == "no_finish":
                    audit["events"] = []
                elif change == "missing_field":
                    del audit["wall_s"]
                if change == "truncated":
                    path.write_text('{"status":')
                else:
                    path.write_text(json.dumps(audit))
                before = path.read_bytes()
                with mock.patch.dict(os.environ, env), \
                        mock.patch("gls_runtime.EbmcBackend", side_effect=AssertionError("formal rerun")), \
                        mock.patch("gls_runtime.RelayProposer", side_effect=AssertionError("LLM rerun")):
                    with self.assertRaises(ValueError):
                        run_selection(self.evaluator("sample"), "test-model", current_lemmas, path)
                self.assertEqual(path.read_bytes(), before)

    def test_partial_batch_rebuilds_aggregate_and_runs_only_the_missing_module(self):
        proposed, snapshots = {}, {}
        for name, outcome in (("a", "PROVED"), ("b", "TIMEOUT"),
                              ("c", "NO_PROOF_IN_POOL")):
            lemmas, _ = self.create_completed(name, outcome)
            proposed[name] = {"test-model": {"rep": {"lemmas": lemmas}}}
            snapshots[self.audit_path(name)] = self.audit_path(name).read_bytes()
        self.evaluator("d")
        proposed["d"] = {"test-model": {"rep": {"lemmas": []}}}
        input_path = self.root / "proposed.json"
        output_path = self.root / "evaluated.json"
        input_path.write_text(json.dumps(proposed))

        def evaluator_factory(*args, **kwargs):
            return self.evaluator(Path(args[2]).stem)

        with mock.patch.dict(os.environ, {"LLM_GLS_RESUME_COMPLETED": "1"}), \
                mock.patch("scripts.evaluate_lemmas.LemmaEvaluator", side_effect=evaluator_factory), \
                mock.patch("gls_runtime.EbmcBackend", return_value=Backend("UNSAFE")) as backend, \
                mock.patch("gls_runtime.RelayProposer"):
            evaluate_lemmas(str(self.root), str(input_path), str(self.root), "ebmc",
                            evaluation_cache_mode=CacheMode.NO_CACHE,
                            output_json_path=str(output_path))
        self.assertEqual(backend.call_count, 1)
        result = json.loads(output_path.read_text())
        self.assertEqual(set(result), {"a", "b", "c", "d"})
        self.assertEqual(result["a"]["test-model"]["rep"][0]["1-inductive with property"],
                         VerificationResult.PROVEN.value)
        self.assertEqual(result["b"]["property stats"], VerificationResult.TIMEOUT.value)
        self.assertEqual(result["d"]["property stats"], VerificationResult.CEX.value)
        for path, original in snapshots.items():
            self.assertEqual(path.read_bytes(), original)

    def test_resume_is_opt_in_for_independent_runs(self):
        lemmas, _ = self.create_completed("sample", "PROVED")
        proposer = mock.Mock()
        with mock.patch("gls_runtime.EbmcBackend", return_value=Backend("UNSAFE")) as backend, \
                mock.patch("gls_runtime.RelayProposer", return_value=proposer):
            run_selection(self.evaluator("sample"), "test-model", lemmas,
                          self.audit_path("sample"))
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(json.loads(self.audit_path("sample").read_text())["status"], "UNSAFE")

    def test_failed_atomic_write_preserves_previous_record(self):
        path = self.root / "audit.json"
        atomic_write_json(path, {"status": "TIMEOUT"})
        original = path.read_bytes()
        with self.assertRaises(TypeError):
            atomic_write_json(path, {"not_json": {1, 2}})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("audit.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
