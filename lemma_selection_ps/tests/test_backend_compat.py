"""Input lowering preserves expression widths; isolation needs compiler evidence."""

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend_compat import candidate_error_id, lower_rank_reductions


class CompatibilityTests(unittest.TestCase):
    def test_constant_array_reductions_keep_casts_and_line_numbers(self):
        source = """module main #(parameter NUM_REQ = 4, localparam NUM_REQ_W = 3)();
logic [NUM_REQ_W-1:0] ranks [NUM_REQ-1:0];
property a; (ranks.or() with (NUM_REQ'(1'b1) << item)) == '1; endproperty
property b; ranks.and() with (item < NUM_REQ); endproperty
endmodule
"""
        lowered, changes = lower_rank_reductions(source)
        self.assertEqual(lowered.count("\n"), source.count("\n"))
        self.assertEqual([row["operation"] for row in changes], ["or", "and"])
        for i in range(4):
            self.assertIn(f"(NUM_REQ'(1'b1) << ranks[{i}])", lowered)
            self.assertIn(f"(ranks[{i}] < NUM_REQ)", lowered)
        self.assertIn(" | ", lowered)
        self.assertIn(" & ", lowered)

    def test_multiline_cast_with_comments_keeps_compiler_line_numbers(self):
        source = """module main #(parameter NUM_REQ = 4, localparam NUM_REQ_W = 3)();
logic [NUM_REQ_W-1:0] ranks [NUM_REQ-1:0];
property a; ranks.or() with (NUM_REQ /* width */ '(
1'b1) << item); endproperty
endmodule
"""
        lowered, changes = lower_rank_reductions(source)
        self.assertEqual(lowered.count("\n"), source.count("\n"))
        self.assertEqual(len(changes), 1)
        self.assertIn("NUM_REQ'(1'b1) << ranks[3]", lowered)

    def test_unknown_shapes_and_other_scopes_are_not_rewritten(self):
        prefix = ("module main #(parameter NUM_REQ = 4, localparam NUM_REQ_W = 3)();\n"
                  "logic [NUM_REQ_W-1:0] ranks [NUM_REQ-1:0];\n")
        for source in (prefix + "ranks.or() with (item + 1); endmodule",
                       prefix + "// ranks.or() with (NUM_REQ'(1) << item)\nendmodule",
                       prefix + "ranks.or() with (NUM_REQ'(1) << item); endmodule\nmodule other; endmodule",
                       prefix.replace("NUM_REQ = 4", "NUM_REQ = OTHER") +
                       "ranks.or() with (NUM_REQ'(1) << item); endmodule"):
            self.assertEqual(lower_rank_reductions(source), (source, []))

    def test_only_diagnostics_inside_generated_candidate_are_isolated(self):
        source = ("module main;\nproperty prop; ok; endproperty\n"
                  "property lemma_0;\n missing;\nendproperty\n"
                  "gls_query: assert property(prop and lemma_0);\nendmodule\n")
        names = {"lemma_0": "L12"}
        self.assertEqual(candidate_error_id("file input.sv line 4: unknown identifier missing\n",
                                            source, names), "L12")
        for log in ("file input.sv line 2: unknown identifier missing\n",
                    "file input.sv line 6: syntax error\n",
                    "error: no property suitable for k-induction\n",
                    "file input.sv line 4: unknown identifier missing\nBacktrace:\n",
                    "file input.sv line 4: unknown identifier missing\n"
                    "file input.sv line 2: syntax error\n"):
            self.assertIsNone(candidate_error_id(log, source, names))


if __name__ == "__main__":
    unittest.main()
