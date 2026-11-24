import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import ai_memory


async def test_official_mcp_client_uses_actual_cli_entrypoint(tmp_path):
    from ai_memory.cli import main

    assert main(["--db", str(tmp_path / "db"), "init"]) == 0
    parameters = StdioServerParameters(
        env={"PYTHONPATH": str(Path(ai_memory.__file__).resolve().parents[1])},
        command=sys.executable,
        args=["-m", "ai_memory", "--db", str(tmp_path / "db"), "mcp"],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {"memory_ask", "memory_status"}
            ask = next(t for t in tools.tools if t.name == "memory_ask")
            assert ask.inputSchema["additionalProperties"] is False
            assert ask.inputSchema["required"] == ["question"]
            assert set(ask.outputSchema["properties"]) == {
                "answer",
                "provenance",
                "error",
                "sources",
                "sync_status",
            }
            assert "Google" in ask.description and "history" in ask.description
            result = await client.call_tool("memory_status", {})
            status = result.structuredContent or json.loads(result.content[0].text)
            assert result.isError is False
            assert status["messages"] == 0
            assert status["notebook"]["configured"] is False
            result = await client.call_tool("memory_ask", {"question": "What did we decide?"})
            answer = result.structuredContent or json.loads(result.content[0].text)
            assert result.isError is True and answer["error"]["code"] == "not_configured"
            invalid = await client.call_tool(
                "memory_ask", {"question": {"SYNTHETIC_PRIVATE_PROMPT": True}}
            )
            assert (
                invalid.isError and invalid.structuredContent["error"]["code"] == "invalid_question"
            )
            assert "SYNTHETIC_PRIVATE_PROMPT" not in str(invalid)
