"""Regression checks for property declarations in all three evaluator copies."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
EVALUATORS = []
for index, relative in enumerate(("src/evaluation.py", "lemma_selection_ps/src/evaluation.py",
                                  "lemma_selection_ch/src/evaluation.py")):
    spec = importlib.util.spec_from_file_location(f"property_renaming_evaluator_{index}",
                                                PROJECT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    EVALUATORS.append((relative, module))


class PropertyRenamingTests(unittest.TestCase):
    def test_rename_changes_only_the_declaration_name(self):
        originals = [
            "property plain; @(posedge clk) x; endproperty",
            "property indexed(input int unsigned k); @(posedge clk) a[k]; endproperty",
            "property indexed$default (\n input int unsigned i = 1\n); a[i]; endproperty",
            "  property empty(); x; endproperty",
            "property commented ; x /* property untouched; */; endproperty",
        ]
        expected = [
            "property lemma_7; @(posedge clk) x; endproperty",
            "property lemma_8(input int unsigned k); @(posedge clk) a[k]; endproperty",
            "property lemma_9 (\n input int unsigned i = 1\n); a[i]; endproperty",
            "  property lemma_10(); x; endproperty",
            "property lemma_11 ; x /* property untouched; */; endproperty",
        ]
        for relative, module in EVALUATORS:
            with self.subTest(evaluator=relative):
                evaluator = module.LemmaEvaluator.__new__(module.LemmaEvaluator)
                evaluator.module = SimpleNamespace(num_lemmas=7)
                lemmas = originals.copy()
                evaluator.rename_lemmas(lemmas)
                self.assertEqual(lemmas, expected)
                self.assertEqual(evaluator.module.num_lemmas, 12)

    def test_parameterized_properties_reach_query_construction_intact(self):
        rtl = ("module main(input logic clk, input logic [3:0] a);\n"
               "property prop; @(posedge clk) a[0]; endproperty\nendmodule\n")
        lemmas = [
            "property indexed(input int unsigned k = 0); @(posedge clk) a[k]; endproperty",
            "property ordinary; @(posedge clk) a[0]; endproperty",
        ]
        for relative, module in EVALUATORS:
            with self.subTest(evaluator=relative), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "module.sv"
                path.write_text(rtl)
                evaluator = module.LemmaEvaluator(
                    folder, "ebmc", str(path), cache={}, cache_result=False,
                    cache_mode=module.CacheMode.NO_CACHE,
                )
                inputs = []

                def capture_query(query_path, **kwargs):
                    inputs.append((Path(query_path).name, Path(query_path).read_text()))
                    return {"verification_result": module.VerificationResult.INCONCLUSIVE,
                            "time": 0.0}

                with mock.patch.object(evaluator, "run_ebmc", side_effect=capture_query):
                    evaluator.process_lemmas(lemmas, mode="gls_step")
                self.assertEqual(len(inputs), 1)
                name, text = inputs[0]
                self.assertIn("lemmas0_1", name)
                self.assertIn("property lemma_0(input int unsigned k = 0); @(posedge clk) a[k]; endproperty", text)
                self.assertIn("property lemma_1; @(posedge clk) a[0]; endproperty", text)
                self.assertIn("assert property(prop and lemma_0 and lemma_1);", text)
                self.assertEqual(lemmas[0], "property indexed(input int unsigned k = 0); @(posedge clk) a[k]; endproperty")
                self.assertEqual(path.read_text(), rtl)

    def test_existing_frozen_parameterized_candidates_keep_their_declarations(self):
        path = PROJECT / "data/frozen_pool/experiment1_v1/tasks.json"
        if not path.exists():
            self.skipTest("Frozen experiment-one pool is not present")
        task = next(task for task in json.loads(path.read_text())
                    if task["task_id"] == "hard/fifo_ref_16_ebmc")
        originals = [row["text"] for row in task["candidates"] if row["id"] in ("L11", "L14")]
        self.assertEqual(len(originals), 2)
        for relative, module in EVALUATORS:
            with self.subTest(evaluator=relative):
                evaluator = module.LemmaEvaluator.__new__(module.LemmaEvaluator)
                evaluator.module = SimpleNamespace(num_lemmas=0)
                lemmas = originals.copy()
                evaluator.rename_lemmas(lemmas)
                for index, (original, renamed) in enumerate(zip(originals, lemmas)):
                    self.assertEqual(renamed, original.replace("property lemma_4", f"property lemma_{index}", 1))
                    name = module.VerilogModule.get_property_name(None, renamed)
                    self.assertEqual(name, f"lemma_{index}")

    def test_multiline_lemma_is_declared_before_the_generated_assertion(self):
        rtl = ("module main(input clk, input x);\n"
               "property prop; @(posedge clk) x; endproperty\nendmodule\n")
        lemma = "property multiline;\n @(posedge clk)\n x;\nendproperty"
        for relative, module in EVALUATORS:
            with self.subTest(evaluator=relative), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "module.sv"
                path.write_text(rtl)
                design = module.VerilogModule(str(path))
                design.insert_lemmas_description([lemma])
                output = Path(folder) / "query.sv"
                design.insert_directive_for_properties([lemma], "gls_step", str(output))
                source = output.read_text()
                self.assertLess(source.index(lemma) + len(lemma),
                                source.index("assert property(prop and multiline)"))
                self.assertEqual(path.read_text(), rtl)


if __name__ == "__main__":
    unittest.main()
