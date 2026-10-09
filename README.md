# machine-job-fishing-net
V0 experiment for machine-callable jobs and agent demand discovery

## Company Delta A2A v1 experiment

The service publishes a minimal A2A 1.0 Agent Card at
`/.well-known/agent-card.json` and accepts JSON-RPC at
`/a2a/company-delta`. `SendMessage` starts a tracked Company Delta task and
`GetTask` polls it. Discovery and polling never run the analysis engine.

Send structured input as an A2A data part:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "message-1",
      "role": "ROLE_USER",
      "parts": [
        {"data": {"company": "Intel Corporation", "since": "2024-12-01"}}
      ]
    },
    "configuration": {"returnImmediately": true}
  }
}
```

Poll the returned task ID with `GetTask`. Completed output is an
`application/json` artifact. Cost safeguards default to one concurrent A2A job
and three accepted jobs per client IP per hour. Configure them with
`A2A_MAX_CONCURRENT_JOBS` and `A2A_RATE_LIMIT_PER_HOUR`.

For controlled production verification, set `A2A_INTERNAL_TEST_TOKEN` and send
its value only in the `X-A2A-Test-Token` header. This labels the traffic as
internal in structured logs without logging the token.

Local setup and tests:

```text
python -m venv .venv
.venv/Scripts/activate
python -m pip install -r requirements-dev.txt
set OPENAI_API_KEY=your-key
python -m pytest -q
uvicorn main:app --reload
```

The tests mock the costly OpenAI-backed analysis. A real Company Delta job
requires `OPENAI_API_KEY`; never commit that value.

## TSRE MCP server

The service exposes one free MCP tool over the standard Streamable HTTP
transport:

- Endpoint: `https://machine-job-fishing-net.onrender.com/mcp/`
- Tool: `get_sweden_market_intelligence`
- Input: none
- Output: the latest structured TSRE Daily Investment Desk from
  `feeds/tsre_daily.json`

The feed includes Swedish equity market regime, breadth, sector rotation,
RS20 candidates, Tactical Radar, data-quality information and the original
research contract. It is research decision support, not autonomous investment
advice, a buy/sell signal, or an order generator.

Connect any MCP client that supports Streamable HTTP to the endpoint above,
then list and call the tool. For example, with the official Python SDK:

```python
import asyncio
from mcp import Client


async def main():
    async with Client("https://machine-job-fishing-net.onrender.com/mcp/") as client:
        tools = await client.list_tools()
        result = await client.call_tool("get_sweden_market_intelligence", {})
        print(result.structured_content)


asyncio.run(main())
```
