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
            self.assertEqual(result["flat_lexical"]["heldout_completion"], 1.0)
            self.assertEqual(result["flat_dependency_expanded"]["heldout_completion"], 1.0)
            self.assertLess(result["corrected_graph"]["mean_context_tokens_est"],
                result["flat_dependency_expanded"]["mean_context_tokens_est"])
            self.assertEqual(result["validated_corrections"], 1)
            self.assertEqual(result["regressions"], 0)
            self.assertTrue(result["rollback"]["graph_restored"])
            self.assertTrue(result["learning_record"]["readback_confirmed"])
            self.assertEqual(result["real_model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
