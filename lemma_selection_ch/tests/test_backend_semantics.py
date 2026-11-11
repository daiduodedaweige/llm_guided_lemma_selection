"""Regression tests for real EBMC obligations, not experiment-one batch runs."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation import VerificationResult
from formal_sva import PropertyResolutionError, resolve_property_id, run_obligation
from gls import Config, FormalQueryError, Query, Selector, Status
from gls_step_witness import find_step_witness, parse_state_predicate
from method import run
from runtime import ExperimentBackend, ExperimentEvaluator, RunContext, parse_ebmc


EBMC = os.getenv("EBMC_59_PATH")


class ParserTests(unittest.TestCase):
    def test_property_resolution_uses_exact_label_and_full_instance_id(self):
        listed = CompletedProcess([], 0, "main.child.gls_query_abc_extra: p\n"
                                  "main.outer.actual.gls_query_abc: q\n", "")
        self.assertEqual(resolve_property_id(listed, "gls_query_abc"),
                         "main.outer.actual.gls_query_abc")
        with self.assertRaises(PropertyResolutionError) as caught:
            resolve_property_id(listed, "gls_query_absent")
        self.assertEqual(caught.exception.reason_code, "property_not_found")

    def test_discovery_failure_is_not_treated_as_an_empty_property_list(self):
        listed = CompletedProcess([], 6, "", "error: syntax error\n")
        with self.assertRaises(PropertyResolutionError) as caught:
            resolve_property_id(listed, "gls_query_abc")
        self.assertEqual(caught.exception.reason_code, "property_discovery_failed")
        self.assertIn("syntax error", caught.exception.log)

    def test_discovery_and_proof_share_one_query_timeout_and_same_options(self):
        calls = []
        def execute(args, cap):
            calls.append((args, cap))
            return CompletedProcess(args, 0, "top.inst.gls_query_abc: p\n", "")
        command = ["ebmc", "input.sv", "--top", "top", "-D", "N=2",
                   "--k-induction", "--buechi", "--bound", "1"]
        with mock.patch("formal_sva.time.monotonic", side_effect=[10, 11.25]):
            _, selected = run_obligation(command, "gls_query_abc", 5, execute=execute)
        self.assertEqual(selected, "top.inst.gls_query_abc")
        self.assertEqual(calls, [(command + ["--show-properties"], 5),
                                 (command + ["--property", selected], 3.75)])
        calls.clear()
        with mock.patch("formal_sva.time.monotonic", side_effect=[10, 16]):
            with self.assertRaises(TimeoutExpired):
                run_obligation(command, "gls_query_abc", 5, execute=execute)
        self.assertEqual(len(calls), 1)

    def test_parse_selected_assertion_without_combining_other_results(self):
        output = "[main.candidates] p: PROVED\n[main.goal] q: INCONCLUSIVE\n"
        answer = parse_ebmc(output, "", 0, "ch_step", "main.candidates")
        self.assertEqual(answer["verification_result"], VerificationResult.PROVEN)
        answer = parse_ebmc(output, "", 0, "ch_step", "main.missing")
        self.assertEqual(answer["verification_result"], VerificationResult.INCONCLUSIVE)
        self.assertEqual(answer["reason_code"], "missing_obligation_result")

    def test_result_id_with_generate_indices_is_not_confused_with_expression_indices(self):
        selected = "main.bank[0].actual_checker.gls_query_abc"
        output = "[" + selected + "] always x[0] == 0: PROVED\n"
        answer = parse_ebmc(output, "", 0, "ch_step", selected)
        self.assertEqual(answer["verification_result"], VerificationResult.PROVEN)

    def test_same_cycle_implication_is_normalized_but_delays_are_retained(self):
        predicate = parse_state_predicate("property l; @(posedge clk) disable iff (rst) ((a && b) |-> c); endproperty")
        self.assertIn("!(a && b)", predicate.expression)
        self.assertEqual(predicate.clock, ("posedge", "clk"))
        self.assertEqual(predicate.disable, "rst")
        for body in ("a |=> b", "a ##1 b", "a == $past(b)"):
            self.assertIsNone(parse_state_predicate("property l; " + body + "; endproperty"))

    def test_bounded_obligations_never_manufacture_a_houdini_fixed_point(self):
        class Backend:
            def initial(self, ids, timeout): return Query(Status.UNSAT)
            def step(self, ids, timeout): return Query(Status.SAT)
            def bmc(self, depth, timeout): return Query(Status.UNSAT)
            def candidates_initial(self, ids, timeout): return Query(Status.UNSAT)
            def candidates_step(self, ids, timeout): return Query(Status.SAT)
            def step_witness(self, ids, timeout, **kwargs):
                return Query(Status.UNKNOWN, executed=False)
            def candidates_obligation(self, ids, timeout):
                return Query(Status.UNSAT, reason_code="no_bounded_deletion_witness")
        with tempfile.TemporaryDirectory() as folder:
            context = RunContext(Path(folder), 10, 2)
            context.start()
            result = run(Backend(), None, context, {"L01": "property l; a; endproperty"})
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.reason_code, "no_bounded_deletion_witness")


class InductionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / "design.sv"
        path.write_text("module main; property prop; 1'b1; endproperty\nendmodule\n")
        self.context = RunContext(self.root, 30, 5)
        self.evaluator = ExperimentEvaluator(path, "ebmc", self.context, {})
        self.evaluator.module.query_property_label = "gls_query_test"
        self.clock = 100.0
        patch = mock.patch("runtime.time.monotonic", side_effect=lambda: self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.context.start()

    def test_state_property_uses_native_induction_without_conversion(self):
        proof = CompletedProcess([], 0, "[main.gls_query_test] p: PROVED\n", "")
        with mock.patch("runtime.run_obligation", return_value=(proof, "main.gls_query_test")) as run:
            result = self.evaluator.run_ebmc("input.sv", "gls_step")
        self.assertEqual(result["verification_result"], VerificationResult.PROVEN)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--buechi", run.call_args.args[0])
        self.assertEqual(self.context.events, [])

    def test_exact_unsuitable_diagnostic_retries_with_remaining_budget(self):
        for discovery_failure in (False, True):
            with self.subTest(discovery_failure=discovery_failure):
                self.clock = 100.0
                calls = []
                def run(command, label, cap, **kwargs):
                    calls.append((command, label, cap))
                    if len(calls) == 1:
                        self.clock += 3
                        failed = CompletedProcess(command, 6, "",
                            "error: there is no property suitable for k-induction\n")
                        if discovery_failure:
                            raise PropertyResolutionError("unsuitable", failed, "property_discovery_failed")
                        return failed, "main.gls_query_test"
                    return CompletedProcess(command, 0,
                        "[main.gls_query_test] p: PROVED\n", ""), "main.gls_query_test"
                with mock.patch("runtime.run_obligation", side_effect=run):
                    result = self.evaluator.run_ebmc("input.sv", "ch_step")
                self.assertEqual(result["verification_result"], VerificationResult.PROVEN)
                self.assertEqual([row[2] for row in calls], [5, 2])
                self.assertEqual(calls[1][0], calls[0][0] + ["--buechi"])
                self.assertEqual(calls[1][1], calls[0][1])
                self.assertEqual(result["time"], 3)
                self.assertEqual(self.context.events[-1]["kind"], "induction_translation_fallback")

    def test_no_fallback_for_crash_syntax_error_or_inconclusive(self):
        for returncode, log in ((-11, "segmentation fault"),
                                (2, "error: syntax error"),
                                (10, "[main.gls_query_test] p: INCONCLUSIVE\n")):
            with self.subTest(returncode=returncode):
                completed = CompletedProcess([], returncode, log, "")
                with mock.patch("runtime.run_obligation", return_value=(completed, "main.gls_query_test")) as run:
                    result = self.evaluator.run_ebmc("input.sv", "one_induction")
                self.assertEqual(run.call_count, 1)
                expected = VerificationResult.INCONCLUSIVE if returncode == 10 else VerificationResult.ERROR
                self.assertEqual(result["verification_result"], expected)

    def test_fallback_does_not_restart_expired_query_timeout(self):
        def run(*args, **kwargs):
            self.clock += 5
            return CompletedProcess([], 6, "",
                "error: there is no property suitable for k-induction\n"), "main.gls_query_test"
        with mock.patch("runtime.run_obligation", side_effect=run) as calls:
            result = self.evaluator.run_ebmc("input.sv", "one_inductive_with_prop")
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result["verification_result"], VerificationResult.TIMEOUT)

    def test_compiler_crash_is_not_isolated_even_with_candidate_diagnostic(self):
        self.evaluator.pool = {"L01": "property bad; missing; endproperty"}
        calls = []
        def execute(command, cap):
            calls.append(command)
            if len(calls) == 1:
                return CompletedProcess(command, 0,
                    "main." + self.evaluator.module.query_property_label + ": p\n", "")
            source = Path(command[1]).read_text()
            line = source[:source.index("missing")].count("\n") + 1
            return CompletedProcess(command, -11, "",
                f"file input.sv line {line}: unknown identifier missing\n")
        with mock.patch.object(self.context, "execute", side_effect=execute):
            with self.assertRaises(FormalQueryError):
                self.evaluator.prepare_candidates()
        self.assertEqual(self.evaluator.isolated_candidates, [])
        self.assertEqual(len(calls), 2)


@unittest.skipUnless(EBMC, "set EBMC_59_PATH for real EBMC regression tests")
class NativeBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def backend(self, rtl, pool):
        path = self.root / "design.sv"
        path.write_text(rtl)
        context = RunContext(self.root, 30, 5)
        context.start()
        evaluator = ExperimentEvaluator(path, EBMC, context, pool)
        return ExperimentBackend(evaluator, pool), context

    def test_delayed_goal_reaches_induction_without_a_false_proof(self):
        rtl = """module main(input clk, input a, input b);
property prop; @(posedge clk) a |-> ##2 b; endproperty
endmodule
"""
        backend, context = self.backend(rtl, {})
        answer = backend.step((), 5)
        self.assertIn("Induction Step", answer.log)
        self.assertIn("INCONCLUSIVE", answer.log)
        self.assertNotEqual(answer.status, Status.UNSAT, answer.log)
        self.assertEqual(len(context.queries), 4)
        self.assertTrue(all("--buechi" not in query["command"] for query in context.queries[:2]))
        self.assertTrue(all("--buechi" in query["command"] for query in context.queries[2:]))
        for query in context.queries:
            self.assertEqual(query["command"][query["command"].index("--bound") + 1], "1")
            self.assertIn("a |-> ##2 b", Path(query["command"][1]).read_text())
        self.assertEqual((self.root / "design.sv").read_text(), rtl)

    def test_candidate_compilation_isolates_unknown_names_without_proving_or_editing_them(self):
        rtl = """module main(input clk, input a);
property prop; @(posedge clk) a; endproperty
endmodule
"""
        pool = {"L01": "property bad; @(posedge clk) missing; endproperty",
                "L02": "property good; @(posedge clk) 1'b1; endproperty",
                "L03": "property false_lemma; @(posedge clk) 1'b0; endproperty"}
        backend, context = self.backend(rtl, pool)
        eligible = backend.evaluator.prepare_candidates()
        self.assertEqual(eligible, {key: pool[key] for key in ("L02", "L03")})
        self.assertEqual(len(pool), 3)
        self.assertEqual([row["id"] for row in backend.evaluator.isolated_candidates], ["L01"])
        self.assertEqual((self.root / "design.sv").read_text(), rtl)
        self.assertEqual(len(context.queries), 3)
        self.assertTrue(all(row["kind"] == "property_discovery" for row in context.queries))
        self.assertTrue(all(row["phase"] == "candidate_validation" for row in context.queries))

    def test_design_compilation_failure_is_not_attributed_to_a_candidate(self):
        rtl = """module main(input clk);
property prop; @(posedge clk) missing_rtl; endproperty
endmodule
"""
        backend, context = self.backend(rtl, {"L01": "property good; 1'b1; endproperty"})
        with self.assertRaises(FormalQueryError):
            backend.evaluator.prepare_candidates()
        self.assertEqual(backend.evaluator.isolated_candidates, [])
        self.assertEqual(len(context.queries), 1)

    def test_ps_temporal_candidate_can_be_proved_with_fixed_translation(self):
        rtl = """module main(input clk, input a);
reg b;
always @(posedge clk) b <= a;
property prop; @(posedge clk) 1'b1; endproperty
endmodule
"""
        lemma = "property l; @(posedge clk) 1'b1 |=> b == $past(a); endproperty"
        backend, context = self.backend(rtl, {"L01": lemma})
        for check in (backend.evaluator.is_one_inductive,
                      backend.evaluator.is_one_inductive_with_property):
            with self.subTest(check=check.__name__):
                answer = check(lemma)
                self.assertEqual(answer["verification_result"], VerificationResult.PROVEN, answer)
        self.assertEqual(len(context.queries), 8)
        self.assertEqual(sum("--buechi" in query["command"] for query in context.queries), 4)
        self.assertEqual(sum(event["kind"] == "induction_translation_fallback"
                             for event in context.events), 2)

    def test_original_failing_target_does_not_change_candidate_result(self):
        rtl = """module main(input clk);
reg x = 0;
always @(posedge clk) x <= 0;
property prop; @(posedge clk) 1'b0; endproperty
assert property(prop);
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) x == 0; endproperty"}
        backend, context = self.backend(rtl, pool)
        answer = backend.candidates_step(("L01",), 5)
        self.assertEqual(answer.status, Status.UNSAT, answer.log)
        self.assertEqual(len(context.queries), 2)
        self.assertEqual(context.queries[0]["kind"], "property_discovery")
        command = context.queries[1]["command"]
        self.assertIn("--property", command)
        selected = command[command.index("--property") + 1]
        self.assertIn("[" + selected + "]", answer.log)
        saved = Path(command[1]).read_text()
        self.assertIn("assert property(prop);", saved)
        self.assertEqual((self.root / "design.sv").read_text(), rtl)

    def test_nested_instances_use_instance_names_instead_of_module_types(self):
        rtl = """module checker_impl(input clk);
reg x = 0;
always @(posedge clk) x <= 0;
property prop; @(posedge clk) 1'b0; endproperty
assert property(prop);
endmodule
module wrapper(input clk);
checker_impl actual_checker(clk);
endmodule
module main(input clk);
wrapper outer_instance(clk);
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) x == 0; endproperty"}
        backend, context = self.backend(rtl, pool)
        answer = backend.candidates_step(("L01",), 5)
        self.assertEqual(answer.status, Status.UNSAT, answer.log)
        command = context.queries[-1]["command"]
        selected = command[command.index("--property") + 1]
        self.assertTrue(selected.startswith("main.outer_instance.actual_checker.gls_query_"), selected)
        self.assertIn("[" + selected + "]", answer.log)
        self.assertNotIn("[main.outer_instance.actual_checker.assertion.1]", answer.log)

    def test_parameterized_instance_under_generate_uses_elaborated_id(self):
        rtl = """module checker_impl #(parameter WIDTH=1)(input clk);
reg [WIDTH-1:0] x = 0;
always @(posedge clk) x <= 0;
property prop; @(posedge clk) x == 0; endproperty
endmodule
module main(input clk);
generate for (genvar i=0; i<1; i=i+1) begin: bank
checker_impl #(.WIDTH(4)) actual_checker(clk);
end endgenerate
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) x == 0; endproperty"}
        backend, context = self.backend(rtl, pool)
        answer = backend.candidates_step(("L01",), 5)
        self.assertEqual(answer.status, Status.UNSAT, answer.log)
        command = context.queries[-1]["command"]
        selected = command[command.index("--property") + 1]
        self.assertIn("bank[0].actual_checker.gls_query_", selected)
        self.assertIn(selected + ":", (self.root / "formal_logs/query_0001/stdout.log").read_text())

    def test_multiple_instances_report_ambiguity_before_any_proof(self):
        rtl = """module checker_impl(input clk);
property prop; @(posedge clk) 1'b1; endproperty
endmodule
module main(input clk);
checker_impl first(clk), second(clk);
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) 1'b1; endproperty"}
        backend, context = self.backend(rtl, pool)
        with self.assertRaises(FormalQueryError) as caught:
            backend.candidates_step(("L01",), 5)
        self.assertEqual(caught.exception.answer.reason_code, "ambiguous_property")
        self.assertIn("main.first.gls_query_", str(caught.exception))
        self.assertIn("main.second.gls_query_", str(caught.exception))
        self.assertEqual(len(context.queries), 1)
        self.assertEqual(context.queries[0]["kind"], "property_discovery")

    def test_nested_witness_and_native_obligation_read_hierarchical_probes(self):
        rtl = """module checker_impl(input clk);
reg x;
always @(posedge clk) x <= !x;
property prop; @(posedge clk) 1'b0; endproperty
endmodule
module wrapper(input clk);
checker_impl actual_checker(clk);
endmodule
module main(input clk);
wrapper outer_instance(clk);
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) x; endproperty"}
        backend, _ = self.backend(rtl, pool)
        witness = backend.step_witness(("L01",), 5, candidates_only=True)
        self.assertEqual(witness.status, Status.SAT, witness.log)
        self.assertIs(witness.next_state["L01"], False)
        obligation = backend.candidates_obligation(("L01", "L01"), 5)
        self.assertEqual(obligation.status, Status.SAT, obligation.log)
        self.assertEqual(obligation.next_state, {"L01": False})

    def test_original_evaluator_also_resolves_child_instance(self):
        from evaluation import CacheMode, LemmaEvaluator
        rtl = """module checker_impl(input clk);
reg x = 0;
always @(posedge clk) x <= 0;
property prop; @(posedge clk) x == 0; endproperty
endmodule
module main(input clk);
checker_impl actual_checker(clk);
endmodule
"""
        path = self.root / "design.sv"
        path.write_text(rtl)
        evaluator = LemmaEvaluator(str(self.root), "ebmc", str(path),
                                  cache_mode=CacheMode.NO_CACHE, cache_result=False,
                                  init_cache_with_no_cache_mode=False, ebmc_path=EBMC, timeout=5)
        answer = evaluator.process_lemmas([], "gls_step")
        self.assertEqual(answer["verification_result"], VerificationResult.PROVEN, answer)

    def test_candidate_witness_does_not_parse_or_use_temporal_goal(self):
        rtl = """module main(input clk, input a);
reg x;
always @(posedge clk) x <= !x;
property prop; @(posedge clk) a |=> x; endproperty
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) x; endproperty"}
        backend, _ = self.backend(rtl, pool)
        backend.evaluator.module.get_property = lambda: (_ for _ in ()).throw(AssertionError("goal was read"))
        answer = backend.step_witness(("L01",), 5, candidates_only=True)
        self.assertEqual(answer.status, Status.SAT, answer.detail)
        self.assertIs(answer.next_state["L01"], False)

    def test_whole_candidate_set_supports_each_obligation(self):
        rtl = """module main(input clk);
reg x, y;
always @(posedge clk) begin x <= y; y <= x; end
property prop; @(posedge clk) x |=> y; endproperty
endmodule
"""
        pool = {"L01": "property a; @(posedge clk) x; endproperty",
                "L02": "property b; @(posedge clk) y; endproperty"}
        backend, context = self.backend(rtl, pool)
        full = backend.candidates_obligation(("L01", "L01", "L02"), 5)
        self.assertEqual(full.status, Status.UNSAT, full.detail)
        self.assertEqual(full.reason_code, "no_bounded_deletion_witness")
        single = backend.candidates_obligation(("L01", "L01"), 5)
        self.assertEqual(single.status, Status.SAT, single.log)
        self.assertEqual(single.next_state, {"L01": False})
        self.assertIsNone(single.source)
        self.assertEqual(len(context.queries), 4)
        self.assertTrue(all(q["phase"] == "candidate_induction_obligation" for q in context.queries))

    def test_delayed_implication_is_checked_at_the_next_cycle(self):
        rtl = """module main(input clk, input a);
reg b;
always @(posedge clk) b <= a;
property prop; @(posedge clk) a |=> b; endproperty
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) a |=> b; endproperty",
                "L02": "property bad; @(posedge clk) a |=> !b; endproperty"}
        backend, _ = self.backend(rtl, pool)
        good = backend.candidates_obligation(("L01", "L01"), 5)
        self.assertEqual(good.status, Status.UNSAT, good.log)
        bad = backend.candidates_obligation(("L02", "L02"), 5)
        self.assertEqual(bad.status, Status.SAT, bad.log)
        self.assertEqual(bad.next_state, {"L02": False})

    def test_past_and_disable_conditions_remain_native(self):
        rtl = """module main(input clk, input rst, input a);
reg b;
always @(posedge clk) b <= a;
property prop; @(posedge clk) disable iff (rst) a |=> b; endproperty
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) disable iff (rst) b == $past(a); endproperty"}
        backend, _ = self.backend(rtl, pool)
        answer = backend.candidates_obligation(("L01", "L01"), 5)
        self.assertEqual(answer.status, Status.UNSAT, answer.log)
        query = json.loads((self.root / "formal_logs/query_0001/query.json").read_text())
        saved = Path(query["command"][1]).read_text()
        self.assertIn("$past(a)", saved)
        self.assertIn("disable iff (rst)", saved)
        self.assertNotIn("--reset", query["command"])

    def test_sequence_delay_is_checked_without_rewriting_it_as_boolean(self):
        rtl = """module main(input clk, input a);
reg b;
always @(posedge clk) b <= a;
property prop; @(posedge clk) a |=> b; endproperty
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) a |-> ##1 b; endproperty"}
        backend, _ = self.backend(rtl, pool)
        answer = backend.candidates_obligation(("L01", "L01"), 5)
        self.assertEqual(answer.status, Status.UNSAT, answer.log)
        query = json.loads((self.root / "formal_logs/query_0001/query.json").read_text())
        self.assertIn("##1", Path(query["command"][1]).read_text())

    def test_actual_multiple_clocks_have_an_explicit_support_reason(self):
        rtl = """module main(input clk, input other, input a);
property prop; a; endproperty
endmodule
"""
        pool = {"L01": "property l; @(posedge clk) a |=> a; endproperty",
                "L02": "property l; @(posedge other) a |=> a; endproperty"}
        backend, context = self.backend(rtl, pool)
        answer = backend.candidates_obligation(("L01", "L01", "L02"), 5)
        self.assertEqual(answer.status, Status.UNKNOWN)
        self.assertEqual(answer.reason_code, "unsupported_sampling")
        self.assertEqual(context.queries, [])

    def temporal_pool(self):
        rtl = """module main(input clk);
reg x = 1, y = 1, v = 0, w = 0;
always @(posedge clk) begin x <= y; y <= x; v <= v; w <= v; end
property prop; @(posedge clk) x; endproperty
endmodule
"""
        pool = {"L01": "property core; @(posedge clk) x && y; endproperty",
                "L02": "property extra; @(posedge clk) w == $past(w); endproperty"}
        return rtl, pool

    def test_ch_native_fallback_deletes_then_finishes_with_original_induction(self):
        rtl, pool = self.temporal_pool()
        backend, context = self.backend(rtl, pool)
        result = run(backend, backend.evaluator, context, pool)
        self.assertEqual(result.status, "PROVED", result.reason)
        self.assertEqual(result.selected_ids, ("L01",))
        self.assertTrue(any(q["phase"] == "candidate_induction_obligation" for q in context.queries))
        self.assertEqual(context.queries[-1]["phase"], "gls_step")

    def test_gls_prescreen_uses_same_native_obligation_without_a_model_call(self):
        rtl, pool = self.temporal_pool()
        backend, context = self.backend(rtl, pool)
        class NoModel:
            def propose(self, *args):
                raise AssertionError("prescreen should finish without a model")
        selector = Selector(rtl, backend.evaluator.module.get_property(), list(pool.values()),
                            backend, NoModel(), Config(budget_s=600, formal_timeout_s=5))
        result = selector.run()
        self.assertEqual(result.status, "PROVED", result.events)
        self.assertEqual(result.selected_ids, ("L01",))
        self.assertEqual(result.llm_calls, 0)
        self.assertTrue(any(q["phase"] == "joint_induction_obligation" for q in context.queries))


if __name__ == "__main__":
    unittest.main()
