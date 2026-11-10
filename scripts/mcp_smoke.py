"""Genuine stdio MCP client/server smoke test, without an LLM provider call."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(db: Path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "ai_memory", "--db", str(db.resolve()), "mcp"])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            if names != ["memory_ask", "memory_status"]:
                raise RuntimeError("Unexpected MCP tool contract")
            result = await client.call_tool("memory_status", {})
            if result.isError:
                raise RuntimeError("memory_status MCP call failed")
            status = result.structuredContent or json.loads(result.content[0].text)
            return {"mcp_stdio": "passed", "tools": names, "status": status}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/demo/conversations.sqlite"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(args.db)), ensure_ascii=False, indent=2))
