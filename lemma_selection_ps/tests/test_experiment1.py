"""Offline algorithm, deadline, package and end-to-end CLI acceptance tests."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
PS = PROJECT / "lemma_selection_ps"
CH = PROJECT / "lemma_selection_ch"
sys.path.insert(0, str(PS / "src"))

from experiment import check_common
from gls import Config, Query, Reply, Status
from pool import canonical, candidate_hash, copy_pool, digest, export_pool, load_pool, parse_response, pool_mapping, source_records
from reporting import collect, summarize
from runtime import BudgetExhausted, RecordedProposer, RunContext, parse_ebmc

spec = importlib.util.spec_from_file_location("ch_method_test", CH / "src/method.py")
ch_method = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch_method)

RTL = """module main(input x, input y);
property prop;
  (x == y);
endproperty
endmodule
"""
GOOD = "property suggested; (x == y); endproperty"
BAD = "property suggested_bad; (x == 1'b0 /* BAD */); endproperty"

FAKE_EBMC = r'''#!/usr/bin/env python3
import re,sys
from pathlib import Path
args=sys.argv[1:]
if args==['--version']:
 print('EBMC 5.9 (offline fixture)');sys.exit(0)
text=Path(args[0]).read_text()
property_id=args[args.index('--property')+1] if '--property' in args else 'main.assertion.1'
if '--show-properties' in args:
 for label in re.findall(r'^\s*(gls_query_\w+):\s*assert property',text,re.M):
  print('main.'+label+': always prop')
 sys.exit(0)
if (('fixture_tool_error' in text and '--k-induction' in args)
    or ('fixture_candidate_error' in text and 'property lemma_0' in text)):
 print('error: there is no property suitable for k-induction',file=sys.stderr)
 sys.exit(6)
if '--ignore-initial' in args:
 print('['+property_id+'] witness: REFUTED')
 names=re.findall(r'wire (gls_probe_\w+) =',text)
 for state in (0,1):
  print('Transition system state '+str(state))
  for name in names:
   print(name+' = '+('0' if state==1 and name.endswith('_L02') else '1'))
 sys.exit(10)
if '--k-induction' in args:
 if (('property lemma_0' not in text and 'test_direct' not in text) or 'BAD' in text
     or ('require_pair' in text and text.count('property lemma_') < 2)):
  print('Base Case\nUNSAT: No path\nInduction Step\nSAT: inductive proof failed')
  print('['+property_id+'] assertion: INCONCLUSIVE');sys.exit(10)
 print('['+property_id+'] assertion: PROVED')
else:
 bound=args[args.index('--bound')+1]
 print('['+property_id+'] assertion: PROVED up to bound '+bound)
'''


def make_pool(path, *, records=None):
    if records is None:
        records = [{"group": "main_experiment", "name": "helpers", "rtl": RTL,
                    "lemmas": [GOOD], "generation_cost": {"time_s": 3.0, "calls": 5}},
                   {"group": "hard", "name": "direct", "rtl": RTL + "// test_direct\n",
                    "lemmas": []}]
    return export_pool(records, path, provenance={"test_fixture": True}, scope="smoke")


class PoolTests(unittest.TestCase):
    def test_exact_text_order_copy_hash_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = [{"group": "main_experiment", "name": "empty", "rtl": RTL, "lemmas": []},
                       {"group": "hard", "name": "order", "rtl": RTL,
                        "lemmas": [GOOD, GOOD, GOOD.replace("x == y", "x  == y")]}]
            manifest = make_pool(root / "pool", records=records)
            copy_pool(root / "pool", root / "copy")
            copied, tasks = load_pool(root / "copy")
            self.assertEqual(copied["pool_hash"], manifest["pool_hash"])
            self.assertEqual(tasks[0]["candidates"], [])
            self.assertEqual([row["id"] for row in tasks[1]["candidates"]], ["L01", "L02"])
            self.assertEqual(tasks[1]["candidates"][0]["text"], GOOD)
            self.assertEqual(tasks[1]["candidates"][1]["text"], GOOD.replace("x == y", "x  == y"))
            with self.assertRaisesRegex(ValueError, "replace"):
                make_pool(root / "pool")
            rtl = root / "copy" / tasks[0]["rtl_path"]
            rtl.write_text(rtl.read_text() + "// changed\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_pool(root / "copy")

    def test_unfrozen_empty_directory_is_not_a_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "not ready"):
                load_pool(Path(folder))

    def test_formal_export_requires_all_109_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "78 main and 31 hard"):
                export_pool([{"group": "hard", "name": "one", "rtl": RTL, "lemmas": []}],
                            Path(folder) / "pool", provenance={})
            self.assertFalse((Path(folder) / "pool").exists())

    def test_disabled_file_references_do_not_change_the_frozen_rtl(self):
        rtl = '//`include "disabled.svh"\n/* $readmemh("disabled.hex", mem); */\n' + RTL
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_pool(root / "pool", records=[{"group": "hard", "name": "comments", "rtl": rtl, "lemmas": []}])
            _, tasks = load_pool(root / "pool")
            self.assertEqual((root / "pool" / tasks[0]["rtl_path"]).read_text(), rtl)
            for i, active in enumerate(('`include "active.svh"\n', '$readmemh("active.hex", mem);\n')):
                with self.assertRaisesRegex(ValueError, "external RTL dependencies"):
                    make_pool(root / f"active_{i}", records=[{
                        "group": "hard", "name": "active", "rtl": active + RTL, "lemmas": []}])

    def test_invalid_and_empty_responses_remain_auditable(self):
        self.assertEqual(parse_response({"text": "bad"}), ([], "invalid_json_block"))
        self.assertEqual(parse_response({"text": '<json>{"lemmas":[]}</json>'}), ([], "empty"))
        self.assertEqual(parse_response({"text": '<json>{"lemmas":[{"lemma":"x"}]}</json>'}), (["x"], "parsed"))

    def test_completed_source_cache_is_reextracted_in_original_sample_order(self):
        from diskcache import Cache
        import random
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for group in ("main_experiment", "hard"):
                modules = root / "project/benchmarks" / group
                modules.mkdir(parents=True)
                module = modules / "one.sv"
                module.write_text(RTL)
                group_root = root / "source" / group
                job = group_root / "job"
                (job / "results").mkdir(parents=True)
                (job / "work/prompts").mkdir(parents=True)
                prompt = {"module_name": "one", "model_name": "gpt-6.1-sol",
                          "representation": "fewshot_prompt", "prompt": [{"role": "user", "content": "hello"}]}
                (job / "work/prompts/one.json").write_text(json.dumps(prompt))
                responses = [{"text": '<json>{"lemmas":[{"lemma":' + json.dumps(GOOD + str(i)) + '}]}</json>',
                              "time": i + 1, "input_tokens": 3, "output_tokens": 4} for i in range(5)]
                with Cache(str(job / "llm_cache")) as cache:
                    cache[digest(b"hello-gpt-6.1-sol")] = responses
                sampled = random.Random(42).sample(responses, k=5)
                lemmas = [parse_response(row)[0][0] for row in sampled]
                proposed = {"one": {"gpt-6.1-sol": {"fewshot_prompt": {"lemmas": lemmas}}}}
                (job / "results/proposed_lemmas_fixture.json").write_text(json.dumps(proposed))
                manifest = {"status": "completed", "pipeline": "non_agent", "num_samples": 5,
                            "module_hashes": {"one.sv": digest(module.read_bytes())}}
                (group_root / "live-run.json").write_text(json.dumps(manifest))
            records, _ = source_records(root / "source", root / "project", smoke=True)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["lemmas"], lemmas)
            self.assertEqual(records[0]["generation_cost"]["time_s"], 15)
            final = root / "final"
            groups = {}
            for group in ("main_experiment", "hard"):
                previous = root / "source" / group
                target = final / group
                target.mkdir(parents=True)
                shutil.copyfile(previous / "job/results/proposed_lemmas_fixture.json", target / "candidates.json")
                shutil.copytree(previous / "job/work/prompts", target / "generation/prompts")
                shutil.copytree(previous / "job/llm_cache", target / "generation/llm_cache")
                groups[group] = {"config": json.loads((previous / "live-run.json").read_text()),
                                 "candidates": f"{group}/candidates.json",
                                 "prompts": f"{group}/generation/prompts",
                                 "llm_cache": f"{group}/generation/llm_cache"}
            index = {"schema_version": 1, "kind": "gls_prerun_final", "status": "completed", "groups": groups,
                     "files": {str(p.relative_to(final)): digest(p.read_bytes())
                               for p in final.rglob("*") if p.is_file()}}
            index["manifest_hash"] = digest(canonical(index))
            (final / "manifest.json").write_text(json.dumps(index))
            final_records, final_provenance = source_records(final, root / "project", smoke=True)
            self.assertEqual(records, final_records)
            self.assertNotIn("recovery_attempts", final_provenance["groups"]["hard"]["run_manifest"])
            prompt_path = final / "hard/generation/prompts/one.json"
            prompt_path.write_text(prompt_path.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "file hash mismatch"):
                source_records(final, root / "project", smoke=True)
            active = root / "source/hard/live-run.json"
            manifest["status"] = "running"
            active.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "unfinished"):
                source_records(root / "source", root / "project", smoke=True)


class RuntimeTests(unittest.TestCase):
    def test_tool_failure_is_error_but_valid_nonzero_counterexample_is_not(self):
        for returncode in (6, -6):
            parsed = parse_ebmc("[main.assertion.1] p: PROVED\n",
                                "error: compiler failure\n", returncode, "gls_step")
            self.assertEqual(parsed["verification_result"].name, "ERROR")
            self.assertEqual(parsed["returncode"], returncode)
        parsed = parse_ebmc("", "error: no suitable property\n", 0, "gls_step")
        self.assertEqual(parsed["verification_result"].name, "ERROR")
        parsed = parse_ebmc("[main.assertion.1] p: REFUTED\n", "", 10, "gls_initial")
        self.assertEqual(parsed["verification_result"].name, "CEX")

    def test_bounded_success_is_not_an_inductive_proof(self):
        output = "[main.assertion.1] p: PROVED up to bound 1\n"
        self.assertEqual(parse_ebmc(output, "", 0, "gls_step")["verification_result"].name, "INCONCLUSIVE")
        self.assertEqual(parse_ebmc(output, "", 0, "gls_initial")["verification_result"].name, "PROVEN")
        mixed = "[main.assertion.1] p: PROVED\n[main.assertion.2] q: INCONCLUSIVE\n"
        self.assertEqual(parse_ebmc(mixed, "", 0, "gls_step")["verification_result"].name, "INCONCLUSIVE")
        failed_step = "Base Case\nUNSAT: No path\nInduction Step\nSAT: inductive proof failed\n" + mixed
        # A step refutation does not mean a candidate failed reachable BMC.
        self.assertEqual(parse_ebmc(failed_step, "", 10, "one_induction")["verification_result"].name, "INCONCLUSIVE")
        self.assertEqual(parse_ebmc(failed_step, "", 10, "gls_step")["verification_result"].name, "CEX")

    def test_budget_timeout_counts_actual_process_and_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as folder:
            context = RunContext(Path(folder), 0.15, 1.0)
            context.start()
            with self.assertRaises(subprocess.TimeoutExpired):
                context.execute([sys.executable, "-c", "import time;print('partial',flush=True);time.sleep(2)"], 1)
            self.assertEqual(len(context.queries), 1)
            self.assertTrue(context.queries[0]["executed"])
            self.assertTrue(context.queries[0]["timed_out"])
            self.assertLessEqual(context.queries[0]["timeout_s"], 0.15)
            self.assertIn("partial", (Path(folder) / "formal_logs/query_0001/stdout.log").read_text())
            with self.assertRaises(BudgetExhausted):
                context.execute([sys.executable, "-c", "raise Exception('must not run')"], 1)
            self.assertEqual(len(context.queries), 1)

    def test_failed_launch_is_logged_but_not_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            context = RunContext(Path(folder), 2, 1)
            context.start()
            with self.assertRaises(OSError):
                context.execute(["/nonexistent/experiment-one-ebmc"], 1)
            self.assertFalse(context.queries[0]["executed"])

    def test_llm_raw_request_response_usage_and_error_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / "key"
            key.write_text("fixture-secret")
            context = RunContext(root, 10, 1)
            context.start()
            proposer = RecordedProposer({"key_file": key, "base_url": "https://fixture.test/v1"},
                                        "fixed-model", "xhigh", context)
            response = SimpleNamespace(output_text="invalid proposal", status="completed",
                                       usage=SimpleNamespace(input_tokens=9, output_tokens=7),
                                       model_dump=lambda **_: {"output": ["raw"], "usage": {"input_tokens": 9}})
            with mock.patch("openai.OpenAI") as client:
                client.return_value.responses.create.return_value = response
                proposer.propose("full input", 2)
                client.return_value.responses.create.side_effect = RuntimeError("fixture error")
                with self.assertRaisesRegex(RuntimeError, "fixture error"):
                    proposer.propose("second input", 2)
            self.assertEqual(len(context.llm), 2)
            self.assertEqual(context.llm[0]["input_tokens"], 9)
            self.assertIsNone(context.llm[1]["input_tokens"])
            request = json.loads((root / "llm_logs/request_001/request.json").read_text())
            self.assertEqual(request["input"], "full input")
            self.assertNotIn("fixture-secret", json.dumps(request))
            self.assertTrue((root / "llm_logs/request_001/response.json").is_file())
            self.assertTrue((root / "llm_logs/request_002/error.json").is_file())


class HoudiniTests(unittest.TestCase):
    def test_independent_houdini_starts_full_pool_and_deletes_only_witnessed_candidate(self):
        class Backend:
            def __init__(self):
                self.calls = []
            def initial(self, ids, timeout):
                self.calls.append(("target_initial", ids))
                return Query(Status.UNSAT)
            def step(self, ids, timeout):
                return Query(Status.UNSAT if ids else Status.SAT)
            def bmc(self, depth, timeout):
                return Query(Status.UNSAT)
            def candidates_initial(self, ids, timeout):
                self.calls.append(("candidate_initial", ids))
                return Query(Status.UNSAT)
            def candidates_step(self, ids, timeout):
                return Query(Status.SAT if "L02" in ids else Status.UNSAT)
            def step_witness(self, ids, timeout, *, candidates_only):
                self.calls.append(("witness", candidates_only))
                return Query(Status.SAT, next_state={"L01": True, "L02": False})
        backend = Backend()
        with tempfile.TemporaryDirectory() as folder:
            context = RunContext(Path(folder), 600, 120)
            context.start()
            result = ch_method.run(backend, None, context, {"L01": GOOD, "L02": BAD})
        self.assertEqual(result.status, "PROVED")
        self.assertEqual(result.selected_ids, ("L01",))
        self.assertIn(("candidate_initial", ("L01", "L02")), backend.calls)
        self.assertIn(("witness", True), backend.calls)


class CommandTests(unittest.TestCase):
    def command(self, script, *args, expected=0):
        run = subprocess.run([sys.executable, "-B", str(script), *map(str, args)],
                             text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
        return run

    def test_all_methods_stop_failed_task_and_continue_next_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / "ebmc"
            executable.write_text(FAKE_EBMC)
            executable.chmod(0o700)
            key = root / "key"
            key.write_text("offline-fixture")
            key.chmod(0o600)
            config = root / "config.toml"
            config.write_text('model = "gpt-6.1-sol"\nmodel_provider = "fixture"\n'
                              '[model_providers.fixture]\nwire_api = "responses"\n'
                              'base_url = "https://fixture.test/v1"\n'
                              '[model_providers.fixture.auth]\ncommand = "cat"\n'
                              'args = [' + json.dumps(str(key)) + ']\n')
            for marker in ("fixture_tool_error", "fixture_candidate_error"):
                package = root / marker / "pool"
                make_pool(package, records=[
                    {"group": "main_experiment", "name": "a_error", "rtl": RTL + f"// {marker}\n",
                     "lemmas": [GOOD]},
                    {"group": "main_experiment", "name": "z_ok", "rtl": RTL + "// test_direct\n",
                     "lemmas": []},
                ])
                output = root / marker / "runs"
                common = ["--pool", package, "--output-root", output, "--ebmc-path", executable,
                          "--smoke", "--repeats", 1]
                for method, script in (("ps", PS / "scripts/run.py"),
                                       ("ch", CH / "scripts/run.py"),
                                       ("gls", PS / "scripts/run_gls_reference.py")):
                    with self.subTest(marker=marker, method=method):
                        extra = ["--codex-config", config] if method == "gls" else []
                        self.command(script, *common, *extra, expected=1)
                        directory = output / method / "repeat_1/main_experiment"
                        failed = json.loads((directory / "a_error/result.json").read_text())
                        success = json.loads((directory / "z_ok/result.json").read_text())
                        self.assertEqual(failed["status"], "ERROR")
                        self.assertEqual(failed["error"]["returncode"], 6)
                        self.assertIn("no property suitable", failed["error"]["backend_log"])
                        self.assertEqual(failed["llm_calls"], 0)
                        queries = [json.loads(row) for row in
                                   (directory / "a_error/formal_queries.jsonl").read_text().splitlines()]
                        self.assertEqual(queries[-1]["returncode"], 6)
                        failed_queries = [q for q in queries if q["returncode"] not in (0, 10)]
                        self.assertEqual(len(failed_queries), 2 if marker == "fixture_tool_error" else 1)
                        self.assertTrue(all(q["returncode"] == 6 for q in failed_queries))
                        self.assertNotIn("--buechi", failed_queries[0]["command"])
                        if marker == "fixture_tool_error":
                            self.assertIn("--buechi", failed_queries[1]["command"])
                        self.assertEqual(failed["formal_queries"], len(queries))
                        self.assertEqual(success["status"], "PROVED")
                        batch = json.loads((directory / "run.json").read_text())
                        self.assertEqual(batch["status"], "completed")
                        self.assertEqual(batch["config"]["formal_error_policy"], "fail_fast")

    def test_resume_restarts_incomplete_task_without_retaining_old_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_pool(root / "pool")
            executable = root / "ebmc"
            executable.write_text(FAKE_EBMC)
            executable.chmod(0o700)
            output = root / "runs"
            common = ["--pool", root / "pool", "--output-root", output,
                      "--ebmc-path", executable, "--smoke", "--repeats", 1]
            for method, script in (("ps", PS / "scripts/run.py"),
                                   ("ch", CH / "scripts/run.py")):
                with self.subTest(method=method):
                    self.command(script, *common)
                    interrupted = output / method / "repeat_1/main_experiment/helpers"
                    completed = output / method / "repeat_1/hard/direct/result.json"
                    previous_result = completed.read_bytes()
                    (interrupted / "result.json").unlink()
                    (interrupted / "interrupted_only.log").write_text("discard this partial run")
                    self.command(script, *common, "--resume")
                    self.assertEqual(completed.read_bytes(), previous_result)
                    self.assertEqual(json.loads((interrupted / "result.json").read_text())["status"], "PROVED")
                    self.assertFalse((interrupted / "interrupted_only.log").exists())
                    self.assertFalse(list((output / method).rglob(".incomplete")))

    def test_three_repeats_ps_ch_gls_and_paired_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "pool"
            manifest = make_pool(package)
            executable = root / "ebmc"
            executable.write_text(FAKE_EBMC)
            executable.chmod(0o700)
            output = root / "runs"
            common = ["--pool", package, "--output-root", output, "--ebmc-path", executable, "--smoke"]
            self.command(PS / "scripts/run.py", *common)
            self.command(CH / "scripts/run.py", *common)
            self.command(PS / "scripts/run.py", *common, expected=2)
            before = {str(p): p.read_bytes() for p in output.glob("ps/**/result.json")}
            self.command(PS / "scripts/run.py", *common, "--resume")
            self.assertEqual(before, {str(p): p.read_bytes() for p in output.glob("ps/**/result.json")})
            key = root / "key"
            key.write_text("offline-fixture")
            key.chmod(0o600)
            config = root / "config.toml"
            config.write_text('model = "gpt-6.1-sol"\nmodel_provider = "fixture"\n'
                              '[model_providers.fixture]\nwire_api = "responses"\n'
                              'base_url = "https://fixture.test/v1"\n'
                              '[model_providers.fixture.auth]\ncommand = "cat"\n'
                              'args = [' + json.dumps(str(key)) + ']\n')
            self.command(PS / "scripts/run_gls_reference.py", *common, "--codex-config", config,
                         "--tasks", "hard/direct")
            for method in ("ps", "ch", "gls"):
                for repeat in (1, 2, 3):
                    result = json.loads((output / method / f"repeat_{repeat}/hard/direct/result.json").read_text())
                    self.assertEqual(result["status"], "PROVED")
                    self.assertEqual(result["pool_hash"], manifest["pool_hash"])
                    self.assertEqual(result["formal_queries"], 4)
                    self.assertEqual(result["verification_queries"], 2)
                    self.assertEqual(result["property_discovery_queries"], 2)
                    self.assertGreaterEqual(result["solver_s"], result["property_discovery_s"])
                    self.assertEqual(result["llm_calls"], 0)
                    self.assertEqual(result["selected_ids"], [])
            for repeat in (1, 2, 3):
                result = json.loads((output / "ch" / f"repeat_{repeat}/main_experiment/helpers/result.json").read_text())
                self.assertEqual(result["selected_ids"], ["L01"])
                self.assertEqual(result["formal_queries"], 16)
                self.assertEqual(result["verification_queries"], 7)
                query_files = list((output / "ch" / f"repeat_{repeat}/main_experiment/helpers/formal_logs").glob("*/query.json"))
                self.assertEqual(result["formal_queries"], len(query_files))
                for query_file in query_files:
                    query = json.loads(query_file.read_text())
                    if query["phase"] == "ch_step":
                        text = (query_file.parent / "input.sv").read_text()
                        self.assertIn("assert property(lemma_0)", text)
                        self.assertNotIn("assert property(prop and", text)
            self.command(PS / "scripts/summarize.py", "--pool", package, "--results", output,
                         "--output", root / "summary", "--smoke")
            report = json.loads((root / "summary/summary.json").read_text())
            self.assertEqual(report["groups"]["all"]["ps"]["proved_per_repeat"]["mean"], 2)
            self.assertEqual(report["groups"]["all"]["gls"]["per_repeat"][0]["statuses"]["MISSING"], 1)
            self.assertEqual(len(report["paired_ch_gls"]), 3)
            self.assertTrue((root / "summary/cactus_all.pdf").is_file())
            self.assertTrue((root / "summary/speedup_all.png").is_file())
            self.assertTrue((root / "summary/cost_all.pdf").is_file())
            self.assertTrue((root / "summary/metrics.csv").is_file())
            self.command(PS / "scripts/summarize.py", "--pool", package, "--results", output,
                         "--output", root / "wrong", "--no-plots", expected=2)
            self.command(PS / "scripts/run.py", *common, "--budget-s", "nan", expected=2)

    def test_ps_keeps_bounded_correct_noninductive_candidates_for_prefixes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "pool"
            make_pool(package, records=[{"group": "hard", "name": "pair", "rtl": RTL + "// require_pair\n",
                                        "lemmas": [GOOD, "property other; (y == x); endproperty"]}])
            executable = root / "ebmc"
            executable.write_text(FAKE_EBMC)
            executable.chmod(0o700)
            self.command(PS / "scripts/run.py", "--pool", package, "--output-root", root / "runs",
                         "--ebmc-path", executable, "--smoke", "--repeats", 1)
            result = json.loads((root / "runs/ps/repeat_1/hard/pair/result.json").read_text())
            self.assertEqual(result["status"], "PROVED")
            self.assertEqual(result["selected_ids"], ["L01", "L02"])
            self.assertEqual(result["formal_queries"], 32)
            self.assertEqual(result["verification_queries"], 15)

    def test_real_query_path_records_and_counts_candidate_witness(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "pool"
            make_pool(package, records=[{"group": "hard", "name": "delete_bad", "rtl": RTL,
                                        "lemmas": [GOOD, BAD]}])
            executable = root / "ebmc"
            executable.write_text(FAKE_EBMC)
            executable.chmod(0o700)
            self.command(CH / "scripts/run.py", "--pool", package, "--output-root", root / "runs",
                         "--ebmc-path", executable, "--smoke", "--repeats", 1)
            directory = root / "runs/ch/repeat_1/hard/delete_bad"
            result = json.loads((directory / "result.json").read_text())
            self.assertEqual(result["status"], "PROVED")
            self.assertEqual(result["selected_ids"], ["L01"])
            self.assertEqual(result["formal_queries"], 22)
            self.assertEqual(result["verification_queries"], 10)
            self.assertIn("candidate_step_witness", result["query_time_by_phase_s"])
            self.assertEqual(len(list((directory / "formal_logs").glob("*/input.sv"))), 22)

    def test_dry_run_does_not_launch_backend_or_create_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_pool(root / "pool")
            run = self.command(PS / "scripts/run.py", "--pool", root / "pool", "--output-root", root / "runs",
                               "--ebmc-path", "/does/not/exist", "--smoke", "--dry-run")
            self.assertEqual(json.loads(run.stdout)["task_runs"], 6)
            self.assertFalse((root / "runs").exists())

    def test_missing_pool_stops_before_any_backend_action(self):
        with tempfile.TemporaryDirectory() as folder:
            run = self.command(CH / "scripts/run.py", "--pool", folder, "--output-root", Path(folder) / "runs", expected=2)
            self.assertIn("not ready", run.stderr)
            self.assertFalse((Path(folder) / "runs").exists())

    def test_common_copies_and_reference_algorithm_sources_are_unchanged(self):
        self.assertTrue(check_common())
        hashes = json.loads((PS / "reference_sources.json").read_text())
        # The original live entry points can evolve independently of these
        # copied runtimes. Only unchanged algorithm sources are a prerequisite.
        for relative in ("src/gls.py", "src/evaluation.py", "src/gls_step_witness.py", "src/formal_sva.py"):
            self.assertEqual(digest((PROJECT / relative).read_bytes()), hashes[relative], relative)
        manifests = []
        for relative in ("data/frozen_pool/experiment1_v1", "lemma_selection_ps/data/frozen_pool/experiment1_v1",
                         "lemma_selection_ch/data/frozen_pool/experiment1_v1"):
            if (PROJECT / relative / "manifest.json").exists():
                manifest, _ = load_pool(PROJECT / relative)
                manifests.append(manifest["pool_hash"])
        if manifests:
            self.assertEqual(len(manifests), 3)
            self.assertEqual(len(set(manifests)), 1)


if __name__ == "__main__":
    unittest.main()
