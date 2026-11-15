import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.fixtures import DATA_TOOLS
from backend.real_evaluation import (REAL_CASES, STRATEGIES, _decorate_payload,
    _load_checkpoint, _normalize_checkpoint, _save_checkpoint, compare_real_agent)


class EvaluationCheckpointTests(unittest.TestCase):
    def test_legacy_checkpoint_is_preserved_and_explicitly_classified(self):
        order_id = "a" * 32
        task = REAL_CASES[0][1].format(id=order_id)
        items = [{"product_id": "prod", "seller_id": "seller", "category": "books"}]
        payload = {"run_id": "legacy", "state": "paused_provider_error", "model": "openai/gpt-oss-20b",
            "case_tasks": [{"name": "line_items", "task": task, "order_id": order_id}],
            "strategies": list(STRATEGIES), "results": [
                {"case": "line_items", "strategy": "full_context", "state": "complete", "task": task,
                 "answer": "I cannot verify the requested line items.",
                 "tool_outputs": [{"tool": "order.items", "result": {"data": items}, "error": False}],
                 "entity_resolution_correct": True, "provider_requests": 4,
                 "execution": {"iterations": 4, "invalid_calls": 0}, "outcome": {"code": "old"},
                 "actual_tool_calls": 3, "actual_tokens": 4737, "context_tokens_est": 1000,
                 "input_tokens": 4031, "output_tokens": 706, "retrieval_ms": 0.0, "end_to_end_ms": 2538.62},
                {"case": "line_items", "strategy": "flat_lexical", "state": "complete", "task": task,
                 "answer": "I cannot retrieve the requested line items.",
                 "tool_outputs": [{"tool": "order.items", "result": {"error": "Required tool dependencies were not executed first: order.details"}, "error": True}],
                 "entity_resolution_correct": True, "provider_requests": 4,
                 "execution": {"iterations": 4, "invalid_calls": 0}, "outcome": {"code": "old"},
                 "actual_tool_calls": 1, "actual_tokens": 2910, "context_tokens_est": 700,
                 "input_tokens": 2346, "output_tokens": 564, "retrieval_ms": 0.09, "end_to_end_ms": 2457.09}],
            "provider_requests_used": 18, "actual_tokens_used": 13611, "tool_calls_used": 4,
            "failed_case": "line_items", "failed_strategy": "flat_dependency_expanded",
            "provider_error": "Groq rate limit reached; no retry window remained within the bounded request budget.",
            "budget": {"provider_requests": 30, "actual_tokens": 60000, "tool_calls": 60}}
        _decorate_payload(_normalize_checkpoint(payload))
        statuses = {row["strategy"]: row for row in payload["strategy_status"]}
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(statuses["full_context"]["completion_status"], "COMPLETE")
        self.assertEqual(statuses["full_context"]["outcome"], "UNVERIFIED")
        self.assertEqual(statuses["flat_lexical"]["outcome"], "BLOCKED")
        self.assertEqual(statuses["flat_dependency_expanded"]["completion_status"], "RATE_LIMITED")
        self.assertEqual(statuses["flat_dependency_expanded"]["outcome"], "NOT_RUN")
        self.assertEqual(statuses["flat_dependency_expanded"]["provider_requests_attempted"], 10)
        self.assertIsNone(statuses["flat_dependency_expanded"]["provider_requests_succeeded"])
        self.assertFalse(statuses["flat_dependency_expanded"]["provider_accounting_exact"])
        self.assertIsNone(payload["provider_attempts_without_response"])
        self.assertEqual(statuses["hydradb_graph"]["completion_status"], "NOT_RUN")
        self.assertEqual(statuses["hydradb_graph"]["outcome"], "NOT_RUN")
        self.assertEqual(payload["provider_requests_remaining"], 12)
        self.assertEqual(payload["results"][1]["outcome"]["code"], "BLOCKED")


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_checkpoint_fallback_keeps_snapshots_when_hydradb_write_fails(self):
        class BrokenWriteDB:
            @staticmethod
            def id_for(kind, key): return 47
            async def query(self, query, params=None):
                if query.startswith("MATCH"):
                    return ["payload"], [], 0.1
                raise RuntimeError("HydraDB mutation unavailable")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CONTEXTGRAPH_EVALUATION_CHECKPOINT_DIR": directory}):
            payload = {"run_id": "fallback", "state": "running", "results": [], "budget": {}}
            await _save_checkpoint(BrokenWriteDB(), "fallback", payload)
            payload["state"] = "paused_provider_error"
            await _save_checkpoint(BrokenWriteDB(), "fallback", payload)
            restored = await _load_checkpoint(BrokenWriteDB(), "fallback")
        self.assertEqual(restored["_checkpoint_revision"], 2)
        self.assertEqual(restored["state"], "paused_provider_error")

    async def test_resume_skips_completed_pairs_and_saves_each_new_pair(self):
        order_id = "b" * 32
        orders = [{"order_id": "0" * 32}, {"order_id": "1" * 32}, {"order_id": order_id}]
        task = REAL_CASES[0][1].format(id=order_id)
        old_results = []
        for strategy in STRATEGIES[:2]:
            old_results.append({"case": "line_items", "strategy": strategy, "state": "complete", "task": task,
                "answer": "", "tool_outputs": [], "outcome": {"code": "UNVERIFIED"},
                "execution": {"iterations": 4, "invalid_calls": 0}, "provider_requests": 4,
                "provider_requests_attempted": 4, "provider_requests_succeeded": 4,
                "actual_tool_calls": 1, "actual_tokens": 20, "context_tokens_est": 10,
                "input_tokens": 15, "output_tokens": 5, "retrieval_ms": 0.1, "end_to_end_ms": 2.0,
                "entity_resolution_correct": True})
        checkpoint = {"run_id": "kept", "suite": "groq_paired_context_pilot_v1", "state": "paused_provider_error",
            "model": "openai/gpt-oss-20b", "case_tasks": [{"name": "line_items", "task": task, "order_id": order_id}],
            "strategies": list(STRATEGIES), "results": old_results, "provider_requests_used": 18,
            "provider_requests_attempted": 18, "provider_responses_received": 8, "provider_requests_succeeded": 8,
            "provider_attempts_unclassified": 10, "accounting_version": 2,
            "attempts_by_case_strategy": {"line_items|full_context": 4, "line_items|flat_lexical": 4,
                "line_items|flat_dependency_expanded": 10},
            "successful_responses_by_case_strategy": {"line_items|full_context": 4, "line_items|flat_lexical": 4,
                "line_items|flat_dependency_expanded": None}, "actual_tokens_used": 13611, "tool_calls_used": 4,
            "failed_case": "line_items", "failed_strategy": "flat_dependency_expanded",
            "provider_error": "Groq rate limit reached", "provider_error_info": {"kind": "legacy_rate_limit_unclassified", "http_status": 429},
            "budget": {"provider_requests": 30, "actual_tokens": 60000, "tool_calls": 60}}

        class FakeDB:
            @staticmethod
            def id_for(kind, key): return 5
            async def query(self, query, params=None):
                return ["update_id", "tool", "dependency"], [[7, "order.items", "order.details"]], 0.1

        items = [{"product_id": "prod", "seller_id": "seller", "category": "books"}]
        async def fake_agent(*args, **kwargs):
            return {"answer": "Line items: books; product id prod; seller ID seller.",
                "tool_calls": 1, "tool_outputs": [{"tool": "order.items", "result": {"data": items}, "error": False}],
                "trace": [], "execution": {"invalid_calls": 0, "iterations": 1},
                "agent": {"provider_requests_attempted": 1, "provider_responses_received": 1,
                    "provider_requests_succeeded": 1, "provider_calls": 1, "input_tokens": 15, "output_tokens": 5}}
        route = {"tools": [{"key": tool.key} for tool in DATA_TOOLS],
            "resolved_entities": [{"type": "order", "id": order_id}], "retrieval_ms": 0.1, "edges": []}
        saved = AsyncMock()
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-only"}), \
             patch("backend.real_evaluation.list_orders", return_value=orders), \
             patch("backend.real_evaluation._load_checkpoint", new=AsyncMock(return_value=checkpoint)), \
             patch("backend.real_evaluation._save_checkpoint", new=saved), \
             patch("backend.real_evaluation.candidate_skills", return_value=[]), \
             patch("backend.real_evaluation.route_graph_candidates", new=AsyncMock(return_value=route)), \
             patch("backend.real_evaluation.flat_candidates", return_value=DATA_TOOLS), \
             patch("backend.real_evaluation.run_groq_agent", new=AsyncMock(side_effect=fake_agent)) as agent:
            result = await compare_real_agent(FakeDB(), case_count=1, max_provider_requests=30,
                max_total_tokens=60000, max_tool_calls=60, resume=True)
        self.assertEqual(agent.await_count, 3)
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["provider_requests_attempted"], 21)
        self.assertEqual(result["attempts_by_case_strategy"]["line_items|flat_dependency_expanded"], 11)
        self.assertTrue(all(row["completion_status"] == "COMPLETE" for row in result["strategy_status"]))
        self.assertEqual(saved.await_count, 4)


if __name__ == "__main__":
    unittest.main()
