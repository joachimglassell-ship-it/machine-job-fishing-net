import asyncio
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

import main


class A2ATests(unittest.TestCase):
    def setUp(self):
        main.A2A_TASKS.clear()
        main.A2A_MESSAGE_TASKS.clear()
        main.A2A_REQUEST_TIMES.clear()
        main.A2A_ACTIVE_JOBS = 0
        self.client = TestClient(main.app)

    def test_agent_card_is_v1_and_does_not_run_job(self):
        with patch.object(main, "run_company_delta") as engine:
            response = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        card = response.json()
        interface = card["supportedInterfaces"][0]
        self.assertEqual(interface["protocolBinding"], "JSONRPC")
        self.assertEqual(interface["protocolVersion"], "1.0")
        self.assertTrue(interface["url"].endswith("/a2a/company-delta"))
        self.assertEqual(card["skills"][0]["id"], "company_delta")
        self.assertIn("max-age=300", response.headers["cache-control"])
        self.assertIn("etag", response.headers)
        engine.assert_not_called()

    def test_validation_rejects_bad_input_without_running_job(self):
        request = {
            "jsonrpc": "2.0", "id": "bad-1", "method": "SendMessage",
            "params": {"message": {
                "messageId": "msg-bad", "role": "ROLE_USER",
                "parts": [{"data": {"company": "Intel", "since": "yesterday"}}],
            }},
        }
        with patch.object(main, "run_company_delta") as engine:
            response = self.client.post("/a2a/company-delta", json=request)
        self.assertEqual(response.json()["error"]["code"], -32602)
        engine.assert_not_called()

    def test_send_message_reuses_engine_and_get_task_returns_result(self):
        completed = {
            "status": "ok", "company": "Intel Corporation",
            "period_from": "2024-12-01", "period_to": "2026-10-09",
            "changes": [], "material_changes_found": 0,
            "checked_at": "2026-10-09T00:00:00+00:00",
        }

        async def exercise():
            with patch.object(main, "run_company_delta", return_value=completed) as engine:
                response = await main.a2a_company_delta(_request({
                    "jsonrpc": "2.0", "id": "rpc-1", "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": "msg-1", "role": "ROLE_USER",
                            "parts": [{"data": {
                                "company": "Intel Corporation", "since": "2024-12-01",
                            }}],
                        },
                        "configuration": {"returnImmediately": True},
                    },
                }))
                task_id = response["result"]["task"]["id"]
                await asyncio.gather(*list(main.A2A_BACKGROUND_TASKS))
                polled = await main.a2a_company_delta(_request({
                    "jsonrpc": "2.0", "id": "rpc-2", "method": "GetTask",
                    "params": {"id": task_id},
                }))
                engine.assert_called_once_with("Intel Corporation", "2024-12-01")
                self.assertEqual(polled["result"]["status"]["state"], "TASK_STATE_COMPLETED")
                result = polled["result"]["artifacts"][0]["parts"][0]["data"]
                self.assertEqual(result["material_changes_found"], 0)
                self.assertEqual(result["request_id"], "rpc-1")

        asyncio.run(exercise())

    def test_unknown_task_and_existing_routes(self):
        response = self.client.post("/a2a/company-delta", json={
            "jsonrpc": "2.0", "id": 2, "method": "GetTask",
            "params": {"id": "missing"},
        })
        self.assertEqual(response.json()["error"]["code"], -32001)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/.well-known/agent.json").status_code, 200)


class _Request:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = None

    async def json(self):
        return json.loads(json.dumps(self._body))


def _request(body):
    return _Request(body)


if __name__ == "__main__":
    unittest.main()
