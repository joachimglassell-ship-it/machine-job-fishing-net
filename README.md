# machine-job-fishing-net
V0 experiment for machine-callable jobs and agent demand discovery

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
