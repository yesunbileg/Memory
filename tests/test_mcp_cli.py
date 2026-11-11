import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_official_mcp_client_uses_actual_cli_entrypoint(tmp_path):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "ai_memory", "--db", str(tmp_path / "db"), "mcp"]
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {"memory_ask", "memory_status"}
            result = await client.call_tool("memory_status", {})
            status = result.structuredContent or json.loads(result.content[0].text)
            assert result.isError is False
            assert status["messages"] == 0
            assert status["notebook"]["configured"] is False
            result = await client.call_tool("memory_ask", {"question": "What did we decide?"})
            answer = result.structuredContent or json.loads(result.content[0].text)
            assert answer["error"]["code"] == "not_configured"
