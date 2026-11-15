import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.fixtures import DATA_TOOLS
from backend.groq_agent import rate_limit_info, run_groq_agent
from backend.olist_store import list_orders, lookup_order


class Response:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}
        self.is_error = status >= 400

    def json(self):
        return self.payload


class ResponseSequenceClient:
    responses = []
    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).calls = 0

    async def post(self, *args, **kwargs):
        i = type(self).calls
        type(self).calls += 1
        return type(self).responses[i]

    async def aclose(self):
        pass


def chat_response(message):
    return Response(200, {"choices": [{"message": message}],
                     "usage": {"prompt_tokens": 11, "completion_tokens": 4}})


class ProviderLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_rate_limit_classification_and_header_wait(self):
        info = rate_limit_info({"x-ratelimit-reset-tokens": "3s"},
            {"error": {"message": "Limit reached for tokens per minute (TPM)"}},
            attempt=1, remaining_seconds=30, max_retries=2)
        self.assertEqual(info["kind"], "tokens_per_minute")
        self.assertEqual(info["retry_after_seconds"], 3)
        self.assertEqual(info["delay_seconds"], 3)
        self.assertTrue(info["retry"])

        daily = rate_limit_info({"retry-after": "1"},
            {"error": {"message": "Quota exceeded for tokens per day (TPD)"}},
            attempt=1, remaining_seconds=30, max_retries=2)
        self.assertEqual(daily["kind"], "daily_quota_exhausted")
        self.assertFalse(daily["retry"])
        self.assertEqual(daily["stop_reason"], "daily_quota")

    async def test_retries_with_retry_after_and_counts_attempts_and_successes(self):
        order_id = list_orders(1)[0]["order_id"]
        status = lookup_order(order_id)["status"]
        tool_call = {"id": "call_1", "type": "function", "function": {
            "name": "order__details", "arguments": json.dumps({"order_id": order_id})}}
        ResponseSequenceClient.responses = [
            Response(429, {"error": {"message": "Rate limit for requests per minute (RPM)"}}, {"retry-after": "1"}),
            chat_response({"tool_calls": [tool_call]}),
            chat_response({"content": f"Order status is {status}."}),
        ]
        sleep = AsyncMock()
        selected = [tool for tool in DATA_TOOLS if tool.key == "order.details"]
        with patch("backend.groq_agent.httpx.AsyncClient", ResponseSequenceClient), \
             patch("backend.groq_agent.asyncio.sleep", sleep), \
             patch.dict("os.environ", {"GROQ_API_KEY": "test-only", "CONTEXTGRAPH_PROVIDER_MAX_RETRIES": "2"}):
            result = await run_groq_agent(f"Report order {order_id} status.", selected,
                required_order_id=order_id, provider_request_budget=3)
        stats = result["agent"]
        self.assertEqual(stats["provider_requests_attempted"], 3)
        self.assertEqual(stats["provider_responses_received"], 3)
        self.assertEqual(stats["provider_requests_succeeded"], 2)
        self.assertEqual(stats["rate_limit_responses"], 1)
        sleep.assert_awaited_once_with(1.0)
        self.assertEqual(result["outcome"]["code"], "SUCCESS")

    async def test_daily_quota_stops_without_retry(self):
        order_id = list_orders(1)[0]["order_id"]
        ResponseSequenceClient.responses = [Response(429,
            {"error": {"message": "Quota exceeded for requests per day (RPD)"}}, {"retry-after": "1"})]
        selected = [tool for tool in DATA_TOOLS if tool.key == "order.details"]
        with patch("backend.groq_agent.httpx.AsyncClient", ResponseSequenceClient), \
             patch.dict("os.environ", {"GROQ_API_KEY": "test-only", "CONTEXTGRAPH_PROVIDER_MAX_RETRIES": "3"}):
            with self.assertRaises(HTTPException) as caught:
                await run_groq_agent(f"Report order {order_id} status.", selected,
                    required_order_id=order_id, provider_request_budget=4)
        self.assertEqual(ResponseSequenceClient.calls, 1)
        self.assertEqual(caught.exception.provider_error["kind"], "daily_quota_exhausted")
        self.assertEqual(caught.exception.provider_requests_attempted, 1)
        self.assertEqual(caught.exception.provider_responses_received, 1)
        self.assertEqual(caught.exception.provider_requests_succeeded, 0)

    async def test_attempt_budget_includes_retries(self):
        order_id = list_orders(1)[0]["order_id"]
        repeated = Response(429, {"error": {"message": "Temporary requests per minute limit"}}, {"retry-after": "0.5"})
        ResponseSequenceClient.responses = [repeated, repeated]
        sleep = AsyncMock()
        selected = [tool for tool in DATA_TOOLS if tool.key == "order.details"]
        with patch("backend.groq_agent.httpx.AsyncClient", ResponseSequenceClient), \
             patch("backend.groq_agent.asyncio.sleep", sleep), \
             patch.dict("os.environ", {"GROQ_API_KEY": "test-only", "CONTEXTGRAPH_PROVIDER_MAX_RETRIES": "3"}):
            with self.assertRaises(HTTPException) as caught:
                await run_groq_agent(f"Report order {order_id} status.", selected,
                    required_order_id=order_id, provider_request_budget=2)
        self.assertEqual(ResponseSequenceClient.calls, 2)
        self.assertEqual(caught.exception.provider_requests_attempted, 2)
        self.assertEqual(caught.exception.provider_error["stop_reason"], "request_budget")
        sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
