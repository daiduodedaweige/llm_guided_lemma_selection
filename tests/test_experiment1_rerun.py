"""Selection, artifact validation, statistics and rollback for single-record reruns."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lemma_selection_ps/src"))
spec = importlib.util.spec_from_file_location("ex1_rerun", ROOT / "scripts/rerun_experiment1.py")
rerun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rerun)

TASK = {"task_id": "hard/example", "group": "hard", "name": "example", "candidate_pool_hash": "c",
        "rtl_sha256": "r", "property_sha256": "p", "candidates": [{"id": "L01"}]}
MANIFEST = {"pool_hash": "pool"}


def result(status="ERROR", method="gls"):
    return {"method": method, "repeat": 3, "task_id": TASK["task_id"], "group": "hard", "task_name": "example",
            "run_scope": "formal", "pool_hash": "pool", "candidate_pool_hash": "c", "rtl_sha256": "r",
            "property_sha256": "p", "config_hash": "new", "status": status, "selected_ids": ["L01"] if status == "PROVED" else [],
            "wall_s": 2.0, "formal_queries": 1, "solver_s": 1.5, "llm_s": 0, "llm_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "generation_s": 5.0, "end_to_end_s": 7.0}


def artifacts(folder, row):
    folder.mkdir(parents=True)
    rerun.write(folder / "result.json", row)
    query = {"index": 1, "executed": True, "elapsed_s": 1.5}
    qdir = folder / "formal_logs/query_0001"
    rerun.write(qdir / "query.json", query)
    for name in ("input.sv", "stdout.log", "stderr.log"):
        (qdir / name).write_text("fixture")
    (folder / "formal_queries.jsonl").write_text(json.dumps(query) + "\n")


class RerunTests(unittest.TestCase):
    def test_selection_is_per_repeat_and_rejects_duplicate(self):
        log = ("gls repeat=1 hard/example: ERROR 1.000s 1 queries\n"
               "gls repeat=2 hard/example: PROVED 2.000s 2 queries\n"
               "gls repeat=3 hard/example: ERROR 3.000s 3 queries\n")
        targets = rerun.select_errors(log)
        self.assertEqual([t["repeat"] for t in targets], [1, 3])
        updated = rerun.update_log(log, result("PROVED"))
        self.assertEqual(updated.splitlines()[:2], log.splitlines()[:2])
        self.assertEqual(updated.splitlines()[2], "gls repeat=3 hard/example: PROVED 2.000s 1 queries")
        with self.assertRaises(ValueError):
            rerun.select_errors(log + log)

    def test_ch_selection_log_and_result_identity(self):
        log = ("ch repeat=1 hard/example: ERROR 1.000s 1 queries\n"
               "ch repeat=2 hard/example: PROVED 2.000s 2 queries\n"
               "ch repeat=3 hard/example: ERROR 3.000s 3 queries\n")
        targets = rerun.select_errors(log, "ch")
        self.assertEqual([t["key"] for t in targets], ["ch/1/hard/example", "ch/3/hard/example"])
        updated = rerun.update_log(log, result("PROVED", "ch"))
        self.assertEqual(updated.splitlines()[:2], log.splitlines()[:2])
        self.assertEqual(updated.splitlines()[2], "ch repeat=3 hard/example: PROVED 2.000s 1 queries")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "task"
            artifacts(folder, result("PROVED", "ch"))
            rerun.validate_task(folder, TASK, 3, MANIFEST, "ch")
            with self.assertRaisesRegex(ValueError, "identity/input mismatch"):
                rerun.validate_task(folder, TASK, 3, MANIFEST, "gls")

    def test_task_filter_does_not_rerun_other_errors_or_successes(self):
        log = ("gls repeat=1 hard/selected: ERROR 1.000s 1 queries\n"
               "gls repeat=2 hard/selected: PROVED 2.000s 2 queries\n"
               "gls repeat=3 hard/selected: ERROR 3.000s 3 queries\n"
               "gls repeat=1 hard/other: ERROR 4.000s 4 queries\n")
        targets = rerun.select_errors(log, task_ids=["hard/selected"])
        self.assertEqual([entry["key"] for entry in targets],
                         ["gls/1/hard/selected", "gls/3/hard/selected"])
        targets = rerun.select_errors(log, task_ids=["hard/selected"], repeats=[3])
        self.assertEqual([entry["key"] for entry in targets], ["gls/3/hard/selected"])
        self.assertEqual(rerun.select_errors(log, task_ids=["hard/missing"]), [])

    def test_ch_publish_updates_own_log_and_shared_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            operation = results / "logs/reruns/ch_fixture"
            relative = "ch/repeat_3/hard/example"
            current = results / relative
            artifacts(current, result("ERROR", "ch"))
            rerun.write(current.parent / "run.json", {"config_hash": "batch"})
            (results / "logs").mkdir()
            original_log = "ch repeat=3 hard/example: ERROR 1.000s 1 queries\n"
            (results / "logs/ch.log").write_text(original_log)
            (results / "logs/gls.log").write_text("gls repeat=3 hard/example: ERROR 1.000s 1 queries\n")
            rerun.export_summary(results / "summary", [result("ERROR", "ch")], MANIFEST)
            artifacts(operation / "executions" / relative, result("PROVED", "ch"))
            entry = {"method": "ch", "relative": relative, "key": "ch/3/hard/example", "repeat": 3,
                     "task_id": "hard/example", "original_sha256": rerun.sha(current / "result.json")}
            state = {"targets": [entry]}
            with mock.patch.object(rerun, "collect_current", side_effect=[
                (MANIFEST, [TASK], [result("ERROR", "ch")]),
                (MANIFEST, [TASK], [result("PROVED", "ch")])]):
                rerun.publish(results, root / "pool", operation, state, entry, TASK, MANIFEST)
            self.assertEqual(entry["phase"], "published")
            self.assertIn("PROVED", (results / "logs/ch.log").read_text())
            self.assertIn("ERROR", (results / "logs/gls.log").read_text())
            self.assertEqual(rerun.read(results / "summary/summary.json")["groups"]["all"]["ch"]["per_repeat"][2]["statuses"]["PROVED"], 1)
            self.assertIn("ch/3/hard/example", rerun.read(results / "logs/rerun_index.json")["records"])

    def test_stale_queries_and_inconsistent_counts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "task"
            artifacts(folder, result())
            rerun.validate_task(folder, TASK, 3, MANIFEST)
            extra = folder / "formal_logs/query_0002"
            extra.mkdir()
            with self.assertRaisesRegex(ValueError, "stale"):
                rerun.validate_task(folder, TASK, 3, MANIFEST)
            extra.rmdir()
            row = result()
            row["formal_queries"] = 2
            rerun.write(folder / "result.json", row)
            with self.assertRaisesRegex(ValueError, "Query count"):
                rerun.validate_task(folder, TASK, 3, MANIFEST)

    def test_new_success_updates_counts_and_paired_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            rerun.export_summary(output, [result(), result("PROVED", "ch")], MANIFEST)
            before = rerun.read(output / "summary.json")
            self.assertEqual(before["paired_ch_gls"], [])
            rerun.export_summary(output, [result("PROVED"), result("PROVED", "ch")], MANIFEST)
            after = rerun.read(output / "summary.json")
            self.assertEqual(after["groups"]["all"]["gls"]["per_repeat"][2]["statuses"]["PROVED"], 1)
            self.assertEqual(len(after["paired_ch_gls"]), 1)
            self.assertEqual(set(p.name for p in output.iterdir()), set(rerun.SUMMARY_FILES))

    def test_publish_failure_restores_original_task_log_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            operation = results / "logs/reruns/test_operation"
            relative = "gls/repeat_3/hard/example"
            current = results / relative
            artifacts(current, result())
            rerun.write(current.parent / "run.json", {"config_hash": "batch"})
            (results / "logs").mkdir()
            log = "gls repeat=3 hard/example: ERROR 1.000s 1 queries\n"
            (results / "logs/gls.log").write_text(log)
            rerun.export_summary(results / "summary", [result()], MANIFEST)
            original = {name: (results / "summary" / name).read_bytes() for name in rerun.SUMMARY_FILES}
            original_result = (current / "result.json").read_bytes()
            artifacts(operation / "executions" / relative, result("PROVED"))
            entry = {"relative": relative, "key": "gls/3/hard/example", "repeat": 3,
                     "task_id": "hard/example", "original_sha256": rerun.sha(current / "result.json")}
            state = {"targets": [entry]}
            with mock.patch.object(rerun, "collect_current", side_effect=[(MANIFEST, [TASK], [result()]), ValueError("publish check failed")]):
                with self.assertRaisesRegex(ValueError, "publish check failed"):
                    rerun.publish(results, root / "pool", operation, state, entry, TASK, MANIFEST)
            self.assertEqual((current / "result.json").read_bytes(), original_result)
            self.assertEqual((results / "logs/gls.log").read_text(), log)
            self.assertFalse((results / "logs/rerun_index.json").exists())
            for name, data in original.items():
                self.assertEqual((results / "summary" / name).read_bytes(), data)

    def test_summary_accepts_only_the_registered_replacement_bytes(self):
        import reporting
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            current = results / "gls/repeat_3/hard/example"
            artifacts(current, result())
            batch = {"config_hash": "batch", "pool_hash": "pool", "common_source_hash": "s",
                     "ebmc_sha256": "e", "ebmc_version": "5.9", "config": {"budget_s": 600, "formal_timeout_s": 120},
                     "concurrency": 1, "machine": "m", "platform": "p", "python": "v"}
            rerun.write(current.parent / "run.json", batch)
            manifest = dict(MANIFEST, scope="formal")
            with mock.patch.object(reporting, "load_pool", return_value=(manifest, [TASK])):
                with self.assertRaisesRegex(ValueError, "configuration hash mismatch"):
                    reporting.collect(results, results, ["gls"], [3], "formal")
                rerun.write(results / "logs/rerun_index.json", {"records": {"gls/3/hard/example": {
                    "result_sha256": rerun.sha(current / "result.json"), "config_hash": "new", "batch_config_hash": "batch"}}})
                _, _, rows = reporting.collect(results, results, ["gls"], [3], "formal")
                self.assertEqual(len(rows), 1)
                modified = result()
                modified["wall_s"] = 3
                rerun.write(current / "result.json", modified)
                with self.assertRaisesRegex(ValueError, "replacement identity mismatch"):
                    reporting.collect(results, results, ["gls"], [3], "formal")


if __name__ == "__main__":
    unittest.main()
