"""Offline checks for selection, safe feedback, and fallback."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gls import Config, Query, Reply, Selector, Status
from gls_runtime import EbmcBackend, RelayProposer, legacy_entries, run_selection
from gls_step_witness import find_step_witness, parse_state_predicate
from evaluation import CacheMode, LemmaEvaluator, VerificationResult


class ScriptedBackend:
    def __init__(self, initials=None, steps=None, bmc=None):
        self.initials = initials or {}
        self.steps = steps or {}
        self.bmc_result = bmc or Query(Status.UNSAT)
        self.calls = []

    def initial(self, ids, timeout_s):
        self.calls.append(("initial", ids))
        return self.initials.get(ids, Query(Status.UNSAT))

    def step(self, ids, timeout_s):
        self.calls.append(("step", ids))
        return self.steps.get(ids, Query(Status.SAT, detail="step failed"))

    def bmc(self, depth, timeout_s):
        self.calls.append(("bmc", depth))
        return self.bmc_result


class ScriptedProposer:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts = []

    def propose(self, prompt, timeout_s):
        self.prompts.append(json.loads(prompt))
        return Reply(self.payloads.pop(0), 0.01, 11, 7)


def proposal(groups, ranking=None):
    return json.dumps({"ranked_ids": ranking or [],
                       "groups": [{"ids": ids, "reason": "test"} for ids in groups]})


class SelectorTests(unittest.TestCase):
    def test_precheck_tool_error_stops_before_any_model_or_later_query(self):
        for phase, count in (("initial", 1), ("step", 2), ("bmc", 3)):
            with self.subTest(phase=phase):
                error = Query(Status.ERROR, detail="tool rejected property", returncode=6)
                backend = ScriptedBackend(
                    initials={(): error} if phase == "initial" else None,
                    steps={(): error} if phase == "step" else None,
                    bmc=error if phase == "bmc" else None,
                )
                proposer = ScriptedProposer([])
                result = Selector("RTL", "p", ["a"], backend, proposer).run()
                self.assertEqual(result.status, "ERROR")
                self.assertEqual(result.error["phase"], phase)
                self.assertEqual(result.error["returncode"], 6)
                self.assertEqual(len(backend.calls), count)
                self.assertEqual(result.formal_queries, count)
                self.assertEqual(proposer.prompts, [])
                self.assertEqual(result.events[-1]["kind"], "finish")

    def test_prescreen_tool_error_stops_instead_of_trying_a_subset(self):
        for phase in ("initial", "step"):
            with self.subTest(phase=phase):
                error = Query(Status.ERROR, detail="malformed candidate", returncode=-6)
                full = ("L01", "L02")
                backend = ScriptedBackend(
                    initials={full: error} if phase == "initial" else None,
                    steps={full: error} if phase == "step" else None,
                )
                result = Selector("RTL", "p", ["a", "b"], backend, ScriptedProposer([])).run()
                self.assertEqual(result.status, "ERROR")
                self.assertEqual(result.error["ids"], list(full))
                self.assertEqual(backend.calls[-1], (phase, full))
                self.assertEqual(result.llm_calls, 0)
                self.assertFalse(any(e["kind"] == "fallback" for e in result.events))

    def test_selection_tool_error_prevents_repair_or_fallback(self):
        for phase in ("initial", "step"):
            with self.subTest(phase=phase):
                error = Query(Status.ERROR, detail="unsupported induction", returncode=6)
                initials = {("L01", "L02"): Query(Status.UNKNOWN, executed=False)}
                if phase == "initial":
                    initials[("L01",)] = error
                backend = ScriptedBackend(
                    initials=initials, steps={("L01",): error} if phase == "step" else None,
                )
                proposer = ScriptedProposer([proposal([["L01"]])])
                result = Selector("RTL", "p", ["a", "b"], backend, proposer).run()
                self.assertEqual(result.status, "ERROR")
                self.assertEqual(result.error["phase"], phase)
                self.assertEqual(backend.calls[-1], (phase, ("L01",)))
                self.assertEqual(result.llm_calls, 1)
                self.assertFalse(any(e["kind"] == "fallback" for e in result.events))

    def test_auxiliary_query_tool_error_also_terminates(self):
        class Backend(ScriptedBackend):
            def step_witness(self, ids, timeout_s):
                self.calls.append(("step_witness", ids))
                return Query(Status.ERROR, detail="probe compiler crashed", returncode=-6)

        backend = Backend()
        result = Selector("RTL", "p", ["a"], backend, ScriptedProposer([])).run()
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.error["phase"], "step_witness")
        self.assertEqual(result.llm_calls, 0)
        self.assertEqual(backend.calls[-1][0], "step_witness")

    def test_failed_backend_launch_is_not_counted_as_executed_query(self):
        class Backend(ScriptedBackend):
            def initial(self, ids, timeout_s):
                raise FileNotFoundError("missing EBMC")

        result = Selector("RTL", "p", ["a"], Backend(), ScriptedProposer([])).run()
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.formal_queries, 0)
        self.assertFalse(result.error["executed"])

    def test_prescreen_unresolved_full_pool_still_checks_llm_subset(self):
        for failure in (Status.TIMEOUT, Status.UNKNOWN):
            with self.subTest(failure=failure):
                backend = ScriptedBackend(
                    initials={("L01", "L02"): Query(failure, detail="full pool unavailable")},
                    steps={("L01",): Query(Status.UNSAT)},
                )
                proposer = ScriptedProposer([proposal([["L01"]], ["L01"])])
                result = Selector("RTL", "p", ["good", "bad"], backend, proposer).run()
                self.assertEqual(result.status, "PROVED")
                self.assertEqual(result.selected_ids, ("L01",))
                self.assertEqual(result.llm_calls, 1)
                self.assertEqual(result.selection_queries, 2)
                self.assertEqual(result.formal_queries, 6)
                self.assertEqual(proposer.prompts[0]["candidates"],
                                 {"L01": "good", "L02": "bad"})
                self.assertTrue(any(e["kind"] == "houdini_prescreen_unresolved"
                                    and e["status"] == failure.value for e in result.events))

    def test_prescreen_prunes_candidates_from_initial_and_repair_prompts(self):
        class PruningBackend(ScriptedBackend):
            def __init__(self):
                super().__init__()
                self.single_steps = 0

            def initial(self, ids, timeout_s):
                self.calls.append(("initial", ids))
                return Query(Status.SAT if ids == ("L01", "L02") else Status.UNSAT)

            def reachable(self, ids, timeout_s):
                self.calls.append(("reachable", ids))
                return Query(Status.SAT if ids == ("L02",) else Status.UNSAT)

            def step(self, ids, timeout_s):
                self.calls.append(("step", ids))
                if not ids:
                    return Query(Status.SAT)
                self.single_steps += 1
                return Query(Status.SAT if self.single_steps == 1 else Status.UNSAT)

        backend = PruningBackend()
        proposer = ScriptedProposer([proposal([["L01"]], ["L01"])])
        selector = Selector("RTL", "p", ["good", "reachable bad"], backend, proposer)
        result = selector.run()
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(selector.disabled, {"L02"})
        self.assertEqual(result.llm_calls, 1)
        self.assertEqual(proposer.prompts[0]["candidates"], {"L01": "good"})
        self.assertEqual(proposer.prompts[0]["max_group_size"], 1)
        self.assertEqual(json.loads(selector._prompt(True))["candidates"],
                         {"L01": "good"})
        with self.assertRaisesRegex(ValueError, "invalid candidate group"):
            selector._parse_reply(proposal([["L02"]]), ["L01"], 4, 1)

    def test_prescreen_keeps_budget_for_model_and_fallback(self):
        class CappedBackend(ScriptedBackend):
            def __init__(self):
                super().__init__()
                self.full_pool_cap = None

            def initial(self, ids, timeout_s):
                if ids == ("L01", "L02"):
                    self.full_pool_cap = timeout_s
                    return Query(Status.UNKNOWN, detail="prescreen inconclusive")
                return Query(Status.UNSAT)

        backend = CappedBackend()
        backend.steps[("L01",)] = Query(Status.UNSAT)
        proposer = ScriptedProposer([proposal([["L01"]])])
        result = Selector(
            "RTL", "p", ["good", "bad"], backend, proposer,
            Config(budget_s=600, formal_timeout_s=160),
        ).run()
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(result.llm_calls, 1)
        self.assertIsNotNone(backend.full_pool_cap)
        self.assertLess(backend.full_pool_cap, 60)

    def test_step_witness_parser_rejects_temporal_and_mixed_sampling(self):
        simple = parse_state_predicate(
            "property l; @(posedge clk) disable iff (rst) (x == y); endproperty"
        )
        self.assertEqual(simple.clock, ("posedge", "clk"))
        self.assertEqual(simple.disable, "rst")
        implication = parse_state_predicate(
            "property l; @(posedge clk) disable iff (rst) (x |-> y); endproperty"
        )
        self.assertEqual(implication.expression, "(!(x) || (y))")
        self.assertIsNone(parse_state_predicate(
            "property l; @(posedge clk) ($past(x) == y); endproperty"
        ))
        fake = SimpleNamespace(module=SimpleNamespace(get_property=lambda: "x == y"))
        mismatch = find_step_witness(
            fake, {"L01": "property l; @(posedge clk) (x == y); endproperty"},
            ("L01",), 1,
        )
        self.assertEqual(mismatch.status, Status.UNKNOWN)
        self.assertFalse(mismatch.executed)

    def test_relay_uses_configured_responses_model_and_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "key"
            key.write_text("test-secret\n")
            response = SimpleNamespace(
                status="completed", output_text=proposal([["L01"]]),
                usage=SimpleNamespace(input_tokens=9, output_tokens=4),
            )
            with mock.patch.dict(os.environ, {
                "LLM_WIRE_API": "responses", "LLM_BASE_URL": "https://relay.test/v1",
                "LLM_API_KEY_FILE": str(key), "LLM_REASONING_EFFORT": "xhigh",
            }), mock.patch("openai.OpenAI") as api:
                api.return_value.responses.create.return_value = response
                reply = RelayProposer("gpt-6.1-sol").propose("prompt", 17)
            self.assertEqual(reply.input_tokens, 9)
            self.assertEqual(reply.output_tokens, 4)
            self.assertEqual(api.call_args.kwargs["base_url"], "https://relay.test/v1")
            self.assertEqual(api.call_args.kwargs["timeout"], 17)
            self.assertEqual(api.return_value.responses.create.call_args.kwargs,
                             {"model": "gpt-6.1-sol", "input": "prompt",
                              "reasoning": {"effort": "xhigh"}})

    def test_non_prefix_group_and_counterexample_coverage(self):
        backend = ScriptedBackend(steps={
            ("L01", "L03"): Query(Status.SAT, source={
                "L01": True, "L02": False, "L03": True,
            }, p_next=False),
            ("L01", "L02"): Query(Status.UNSAT),
        })
        proposer = ScriptedProposer([
            proposal([["L01", "L03"], ["L01", "L02"]],
                     ["L01", "L03", "L02"]),
        ])
        selector = Selector("RTL", "p", ["a", "b", "c"], backend, proposer)
        result = selector.run()
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(result.selected_ids, ("L01", "L02"))
        self.assertEqual(result.llm_calls, 1)
        self.assertEqual(result.selection_queries, 4)
        self.assertEqual([x["status"] for x in result.checked],
                         ["STEP_SAT", "PROVED"])
        self.assertEqual(proposer.prompts[0]["candidates"]["L03"], "c")
        self.assertTrue(any(row["lemma"] == ["a", "b"] and row[
            "1-inductive with property"]["verification_result"]
            == VerificationResult.PROVEN for row in legacy_entries(selector)))

    def test_initial_failure_disables_only_independently_refuted_candidate(self):
        backend = ScriptedBackend(initials={
            ("L01", "L02"): Query(Status.SAT),
            ("L02",): Query(Status.SAT),
        }, steps={("L01",): Query(Status.UNSAT)})
        selector = Selector("RTL", "p", ["a", "b"], backend,
                            ScriptedProposer([proposal([["L01", "L02"]])]))
        result = selector.run()
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(result.selected_ids, ("L01",))
        self.assertEqual(selector.disabled, {"L02"})

    def test_unknown_target_initiality_cannot_refute_candidate(self):
        backend = ScriptedBackend(
            initials={(): Query(Status.UNKNOWN), ("L01",): Query(Status.SAT)},
            bmc=Query(Status.UNKNOWN),
        )
        selector = Selector("RTL", "p", ["a"], backend,
                            ScriptedProposer([proposal([["L01"]])]),
                            Config(max_additional_calls=0))
        result = selector.run()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(selector.disabled, set())
        # With Houdini prescreen, initial(("L01",)) is called once (prescreen
        # exits early because target initiality is unproved and the candidate
        # cannot be independently refuted without it).
        self.assertGreaterEqual(
            sum(1 for call in backend.calls if call == ("initial", ("L01",))), 1
        )

    def test_candidate_only_query_does_not_blame_target_failure(self):
        class CandidateOnlyBackend(ScriptedBackend):
            def reachable(self, ids, timeout_s):
                self.calls.append(("reachable", ids))
                return Query(Status.UNSAT)

        backend = CandidateOnlyBackend(
            initials={(): Query(Status.UNKNOWN), ("L01",): Query(Status.SAT)},
            bmc=Query(Status.UNKNOWN),
        )
        selector = Selector("RTL", "p", ["a"], backend,
                            ScriptedProposer([proposal([["L01"]])]),
                            Config(max_additional_calls=0))
        self.assertEqual(selector.run().status, "UNKNOWN")
        self.assertEqual(selector.disabled, set())
        self.assertIn(("reachable", ("L01",)), backend.calls)

    def test_invalid_output_retried_once_and_counted(self):
        # Houdini prescreen calls step(("L01",)) first; return SAT with no
        # next_state so Houdini exits without a proof and falls through to the
        # LLM path.  The second step(("L01",)) call (inside _check) returns
        # UNSAT so the LLM-guided check proves the goal.  Only count calls
        # targeting ("L01",); the run() preamble also calls step(()).
        class StepOnSecondCallBackend(ScriptedBackend):
            def __init__(self):
                super().__init__()
                self._l01_step_calls = 0

            def step(self, ids, timeout_s):
                self.calls.append(("step", ids))
                if ids == ("L01",):
                    self._l01_step_calls += 1
                    if self._l01_step_calls >= 2:
                        return Query(Status.UNSAT)
                return Query(Status.SAT)

        backend = StepOnSecondCallBackend()
        proposer = ScriptedProposer(['{"ranked_ids": ["bad"], "groups": []}',
                                     proposal([["L01"]])])
        selector = Selector("RTL", "p", ["a"], backend, proposer)
        result = selector.run()
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(result.llm_calls, 2)
        self.assertEqual(len(proposer.prompts), 2)

    def test_provider_503_aborts_instead_of_falling_back(self):
        class ProviderUnavailable(Exception):
            status_code = 503

        proposer = mock.Mock()
        proposer.propose.side_effect = ProviderUnavailable("model group unavailable")
        selector = Selector("RTL", "p", ["a"], ScriptedBackend(), proposer)
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            selector.run()

    def test_connection_error_aborts_instead_of_falling_back(self):
        class APIConnectionError(Exception):
            pass

        proposer = mock.Mock()
        proposer.propose.side_effect = APIConnectionError("Connection error")
        selector = Selector("RTL", "p", ["a"], ScriptedBackend(), proposer)
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            selector.run()

    def test_missing_step_witness_never_creates_false_pool_conclusion(self):
        backend = ScriptedBackend()
        selector = Selector("RTL", "p", ["a"], backend,
                            ScriptedProposer([proposal([["L01"]])]),
                            Config(max_additional_calls=0))
        result = selector.run()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIn("no induction-step witness", result.reason)

    def test_houdini_deletes_only_witness_refuted_lemma(self):
        backend = ScriptedBackend(steps={
            ("L01", "L02"): Query(Status.SAT,
                                   next_state={"L01": False, "L02": True},
                                   p_next=True),
            ("L02",): Query(Status.UNSAT),
        })
        proposer = ScriptedProposer([])
        selector = Selector("RTL", "p", ["a", "b"], backend, proposer,
                            Config(budget_s=400))
        result = selector.run()
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(result.selected_ids, ("L02",))
        self.assertEqual(result.llm_calls, 0)
        self.assertTrue(any(e["kind"] == "houdini_remove" and e["ids"] == ["L01"]
                            for e in result.events))

    def test_target_step_witness_can_establish_no_proof_in_fixed_pool(self):
        backend = ScriptedBackend(steps={
            ("L01",): Query(Status.SAT, source={"L01": True},
                            next_state={"L01": True}, p_next=False),
        })
        result = Selector("RTL", "p", ["a"], backend, ScriptedProposer([]),
                          Config(budget_s=400)).run()
        self.assertEqual(result.status, "NO_PROOF_IN_POOL")

    def test_unsupported_witness_does_not_consume_formal_query_quota(self):
        class NoWitnessBackend(ScriptedBackend):
            def step_witness(self, ids, timeout_s):
                return Query(Status.UNKNOWN, detail="unsupported", executed=False)

        backend = NoWitnessBackend()
        selector = Selector("RTL", "p", ["a"], backend,
                            ScriptedProposer([proposal([["L01"]])]),
                            Config(max_additional_calls=0))
        result = selector.run()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.selection_queries, 2)
        self.assertTrue(any(e["phase"] == "step_witness" and not e["executed"]
                            for e in result.events if e["kind"] == "formal"))

    def test_initial_counterexample_ends_unsafe_without_model(self):
        backend = ScriptedBackend(initials={(): Query(Status.SAT, p_initial=False)})
        proposer = ScriptedProposer([])
        result = Selector("RTL", "p", ["a"], backend, proposer).run()
        self.assertEqual(result.status, "UNSAFE")
        self.assertEqual(result.llm_calls, 0)
        self.assertEqual(backend.calls, [("initial", ())])

    def test_direct_proof_has_legacy_proof_row(self):
        backend = ScriptedBackend(steps={(): Query(Status.UNSAT)})
        selector = Selector("RTL", "p", ["a"], backend, ScriptedProposer([]))
        self.assertEqual(selector.run().status, "PROVED")
        self.assertTrue(any(row["lemma"] == [] and row[
            "1-inductive with property"]["verification_result"]
            == VerificationResult.PROVEN for row in legacy_entries(selector)))

    @unittest.skipUnless(os.getenv("EBMC_59_PATH"), "set EBMC_59_PATH for integration")
    def test_real_ebmc_can_prove_joint_non_prefix_invariant(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks/main_experiment/swap2.sv"
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "swap2.sv"
            module.write_bytes(source.read_bytes())
            evaluator = LemmaEvaluator(
                temp, "ebmc", str(module), cache={}, cache_mode=CacheMode.NO_CACHE,
                ebmc_path=os.environ["EBMC_59_PATH"], timeout=10,
            )
            lemmas = [
                "property weak; a == 1; endproperty",
                "property support; (a == 1 || a == 5 || a == 7) && "
                "(b == 1 || b == 5 || b == 7) && "
                "(c == 1 || c == 5 || c == 7); endproperty",
            ]
            proposer = ScriptedProposer([proposal([["L02"]])])
            selector = Selector(module.read_text(), evaluator.module.get_property(),
                                lemmas, None, proposer, Config(formal_timeout_s=10))
            selector.backend = EbmcBackend(evaluator, selector.pool)
            weak_step = selector.backend.step(("L01",), 10)
            self.assertEqual(weak_step.status, Status.UNKNOWN)
            self.assertIn("induction base refuted", weak_step.detail)
            result = selector.run()
            self.assertEqual(result.status, "PROVED", result.events)
            self.assertEqual(result.selected_ids, ("L02",))
            self.assertEqual(sum(e["kind"] == "formal" and e["phase"] == "bmc"
                                 for e in result.events), 1)

    @unittest.skipUnless(os.getenv("EBMC_59_PATH"), "set EBMC_59_PATH for integration")
    def test_real_malformed_candidate_ends_with_error_without_model(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks/main_experiment/swap2.sv"
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "swap2.sv"
            module.write_bytes(source.read_bytes())
            evaluator = LemmaEvaluator(
                temp, "ebmc", str(module), cache={}, cache_mode=CacheMode.NO_CACHE,
                ebmc_path=os.environ["EBMC_59_PATH"], timeout=10,
            )
            proposer = ScriptedProposer([])
            selector = Selector(module.read_text(), evaluator.module.get_property(),
                                ["property malformed; a |-> ; endproperty"],
                                None, proposer, Config(formal_timeout_s=10))
            selector.backend = EbmcBackend(evaluator, selector.pool)
            result = selector.run()
            self.assertEqual(result.status, "ERROR", result.events)
            self.assertEqual(result.llm_calls, 0)
            self.assertEqual(result.error["ids"], ["L01"])
            self.assertIsNotNone(result.error["returncode"])
            self.assertTrue(result.error["backend_log"])

    @unittest.skipUnless(os.getenv("EBMC_59_PATH"), "set EBMC_59_PATH for integration")
    def test_real_step_witness_drives_local_support_repair(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks/main_experiment/swap2.sv"
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "swap2.sv"
            module.write_bytes(source.read_bytes())
            evaluator = LemmaEvaluator(
                temp, "ebmc", str(module), cache={}, cache_mode=CacheMode.NO_CACHE,
                ebmc_path=os.environ["EBMC_59_PATH"], timeout=10,
            )
            lemmas = [
                "property b_in_set; (b==1 || b==5 || b==7); endproperty",
                "property support; (a==1 || a==5 || a==7) && "
                "(b==1 || b==5 || b==7) && "
                "(c==1 || c==5 || c==7); endproperty",
                "property extra; a == b; endproperty",
            ]
            selector = Selector(
                module.read_text(), evaluator.module.get_property(), lemmas,
                None, ScriptedProposer([proposal([["L01"]], ["L01", "L02"])]),
                Config(budget_s=120, formal_timeout_s=10),
            )
            class UnresolvedFullPool(EbmcBackend):
                def initial(self, ids, timeout_s):
                    if len(ids) == 3:
                        return Query(Status.UNKNOWN, detail="prescreen inconclusive", executed=False)
                    return super().initial(ids, timeout_s)

            selector.backend = UnresolvedFullPool(evaluator, selector.pool)
            result = selector.run()
            self.assertEqual(result.status, "PROVED", result.events)
            self.assertEqual(result.selected_ids, ("L01", "L02"))
            self.assertEqual(result.llm_calls, 1)
            self.assertTrue(any(e["kind"] == "houdini_prescreen_unresolved"
                                and e["status"] == "UNKNOWN" for e in result.events))
            self.assertEqual(evaluator.timeout, 10)
            self.assertEqual([row["status"] for row in result.checked],
                             ["STEP_SAT", "PROVED"])
            witnesses = [e for e in result.events if e["kind"] == "formal"
                         and e["phase"] == "step_witness"]
            self.assertEqual(len(witnesses), 1)
            self.assertIs(witnesses[0]["source_values"]["L02"], False)
            self.assertIs(witnesses[0]["next_values"]["L01"], False)
            self.assertEqual(result.selection_queries, 5)

    @unittest.skipUnless(os.getenv("EBMC_59_PATH"), "set EBMC_59_PATH for integration")
    def test_real_houdini_removes_reachable_invalid_candidate(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks/main_experiment/swap2.sv"
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "swap2.sv"
            module.write_bytes(source.read_bytes())
            evaluator = LemmaEvaluator(
                temp, "ebmc", str(module), cache={}, cache_mode=CacheMode.NO_CACHE,
                ebmc_path=os.environ["EBMC_59_PATH"], timeout=10,
            )
            lemmas = [
                "property weak; a==1; endproperty",
                "property support; (a==1 || a==5 || a==7) && "
                "(b==1 || b==5 || b==7) && "
                "(c==1 || c==5 || c==7); endproperty",
            ]
            selector = Selector(module.read_text(), evaluator.module.get_property(),
                                lemmas, None, None,
                                Config(budget_s=80, formal_timeout_s=10))
            selector.backend = EbmcBackend(evaluator, selector.pool)
            result = selector.run()
            self.assertEqual(result.status, "PROVED", result.events)
            self.assertEqual(result.selected_ids, ("L02",))
            self.assertEqual(result.llm_calls, 0)
            self.assertTrue(any(e["kind"] == "houdini_remove" and e["ids"] == ["L01"]
                                for e in result.events))

    @unittest.skipUnless(os.getenv("EBMC_59_PATH"), "set EBMC_59_PATH for integration")
    def test_clocked_reset_step_witness_is_formal(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks/main_experiment/ex2.sv"
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "ex2.sv"
            module.write_bytes(source.read_bytes())
            evaluator = LemmaEvaluator(
                temp, "ebmc", str(module), cache={}, cache_mode=CacheMode.NO_CACHE,
                ebmc_path=os.environ["EBMC_59_PATH"], timeout=10,
            )
            lemma = "property l; @(posedge clk) disable iff (rst) (x == y && y == z); endproperty"
            backend = EbmcBackend(evaluator, {"L01": lemma})
            self.assertEqual(backend.step((), 10).status, Status.SAT)
            witness = backend.step_witness((), 10)
            self.assertEqual(witness.status, Status.SAT, witness.detail)
            self.assertIs(witness.p_next, False)
            self.assertIs(witness.source["L01"], True)

    @unittest.skipUnless(os.getenv("EBMC_59_PATH"), "set EBMC_59_PATH for integration")
    def test_existing_evaluation_shape_and_audit_file(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks/main_experiment/swap2.sv"
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "swap2.sv"
            module.write_bytes(source.read_bytes())
            evaluator = LemmaEvaluator(
                temp, "ebmc", str(module), cache={}, cache_mode=CacheMode.NO_CACHE,
                ebmc_path=os.environ["EBMC_59_PATH"], timeout=10,
            )
            lemma = ("property support; (a == 1 || a == 5 || a == 7) && "
                     "(b == 1 || b == 5 || b == 7) && "
                     "(c == 1 || c == 5 || c == 7); endproperty")
            audit_path = Path(temp) / "logs" / "gls.json"
            with mock.patch("gls_runtime.RelayProposer",
                            return_value=ScriptedProposer([proposal([["L01"]])])):
                entries, property_status = run_selection(
                    evaluator, "gpt-6.1-sol", [lemma], audit_path
                )
            self.assertEqual(property_status, VerificationResult.INCONCLUSIVE.value)
            self.assertEqual(len(entries), 1)
            self.assertEqual(set(entries[0]), {
                "lemma", "correct", "1-inductive", "implies", "implied",
                "1-inductive with property",
            })
            self.assertEqual(entries[0]["1-inductive with property"][
                "verification_result"], VerificationResult.PROVEN)
            audit = json.loads(audit_path.read_text())
            self.assertEqual(audit["status"], "PROVED")
            self.assertEqual(audit["selected_ids"], ["L01"])


if __name__ == "__main__":
    unittest.main()
