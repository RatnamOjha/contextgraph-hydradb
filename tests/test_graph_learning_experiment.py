import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "graph_learning_experiment.py"
NODE = ROOT / "hydradb-upstream" / "target" / "debug" / "graph-node"


@unittest.skipUnless(NODE.is_file(), "Build the local HydraDB server-runtime binary to run the isolated graph test")
class IsolatedGraphLearningTests(unittest.TestCase):
    def test_real_edge_mutation_heldout_retest_and_rollback(self):
        for _ in range(2):
            completed = subprocess.run([sys.executable, str(SCRIPT), "--heldout", "3"], cwd=ROOT,
                check=True, capture_output=True, text=True, timeout=90)
            result = json.loads(completed.stdout)
            self.assertEqual(result["runtime_probes"]["literal_unwind_set"]["http_status"], 400)
            self.assertEqual(result["runtime_probes"]["null_property_parameter"]["http_status"], 400)
            self.assertTrue(result["heldout_disjoint"])
            self.assertEqual(result["initial_graph"]["edges"], [])
            self.assertEqual(result["initial_graph"]["heldout_completion"], 0.0)
            self.assertEqual(result["corrected_graph"]["edges"], [["order.items", "order.details", 1.0]])
            self.assertEqual(result["corrected_graph"]["heldout_completion"], 1.0)
            self.assertEqual(len(result["heldout_initial"]), 3)
            self.assertEqual(len(result["heldout_corrected"]), 3)
            self.assertEqual(result["flat_lexical"]["heldout_completion"], 1.0)
            self.assertEqual(result["flat_dependency_expanded"]["heldout_completion"], 1.0)
            self.assertLess(result["corrected_graph"]["mean_context_tokens_est"],
                result["flat_dependency_expanded"]["mean_context_tokens_est"])
            self.assertEqual(result["validated_corrections"], 1)
            self.assertEqual(result["regressions"], 0)
            self.assertTrue(result["rollback"]["graph_restored"])
            self.assertTrue(result["learning_record"]["readback_confirmed"])
            self.assertEqual(result["real_model_calls"], 0)
            controls = result["regression_controls"]
            self.assertEqual(controls["snapshots"]["before_edges"], [])
            self.assertEqual(controls["snapshots"]["after_edges"], result["corrected_graph"]["edges"])
            details = controls["details_only"]
            self.assertEqual(details["before"]["selected_tools"], ["order.details"])
            self.assertEqual(details["after"]["selected_tools"], ["order.details"])
            self.assertEqual(details["before"]["outcome"]["code"], "SUCCESS")
            self.assertEqual(details["after"]["outcome"]["code"], "SUCCESS")
            self.assertEqual(details["before"]["context_tokens_est"], details["after"]["context_tokens_est"])
            payments = controls["payments"]
            self.assertEqual(payments["before"]["selected_tools"], ["order.details", "order.payments"])
            self.assertEqual(payments["after"]["selected_tools"], ["order.details", "order.payments"])
            self.assertEqual(payments["before"]["outcome"]["code"], "SUCCESS")
            self.assertEqual(payments["after"]["outcome"]["code"], "SUCCESS")
            for control in (details, payments):
                for side in ("before", "after"):
                    self.assertEqual(control[side]["dependency_failures"], [])
                    self.assertNotIn("order.items", control[side]["selected_tools"])
                    self.assertEqual(control[side]["selected_tools"], control[side]["executed_tools"])
                self.assertFalse(control["regressions"]["completion"])
                self.assertFalse(control["regressions"]["tool_selection"])
                self.assertFalse(control["regressions"]["context_overhead"])
                self.assertFalse(control["regressions"]["unexpected_order_items"])
                self.assertEqual(control["regressions"]["context_tokens_delta"], 0)
            paired = result["paired_same_order_controls"]
            self.assertEqual(len(paired["cases"]), 3)
            self.assertEqual(paired["status_only_completion_before"], 1.0)
            self.assertEqual(paired["status_only_completion_after"], 1.0)
            self.assertEqual(paired["categories_completion_before"], 0.0)
            self.assertEqual(paired["categories_completion_after"], 1.0)
            for case in paired["cases"]:
                status = case["status_only"]
                self.assertEqual(status["before"]["selected_tools"], ["order.details"])
                self.assertEqual(status["after"]["selected_tools"], ["order.details"])
                self.assertEqual(status["before"]["answer"], status["after"]["answer"])
                self.assertEqual(status["before"]["context_tokens_est"], status["after"]["context_tokens_est"])
                self.assertNotIn("order.items", status["after"]["selected_tools"])
                self.assertFalse(status["regressions"]["answer_correctness"])
                self.assertFalse(status["regressions"]["context_overhead"])
                categories = case["categories"]
                self.assertEqual(categories["before"]["selected_tools"], ["order.items"])
                self.assertEqual(categories["after"]["selected_tools"], ["order.details", "order.items"])
                self.assertIn({"tool": "order.items", "missing_dependency": "order.details"},
                    categories["before"]["dependency_failures"])
                self.assertEqual(categories["after"]["dependency_failures"], [])
                self.assertEqual(categories["after"]["executed_tools"], ["order.details", "order.items"])
                self.assertFalse(categories["regressions"]["answer_correctness_after"])
                self.assertFalse(categories["regressions"]["context_overhead"])


if __name__ == "__main__":
    unittest.main()
