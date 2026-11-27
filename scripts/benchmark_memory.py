"""Time deliberate, sequential, source-restricted live MCP retrievals (no retries)."""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def benchmark(db, question, repetitions):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_memory", "--db", str(db.resolve()), "mcp"],
        env={**os.environ, "AI_MEMORY_TRACE": "1"},
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as client:
        await client.initialize()
        for i in range(1, repetitions + 1):
            start = time.monotonic_ns()
            print("agent_call", i, start, "epoch_ms", time.time_ns() // 1_000_000, flush=True)
            result = await client.call_tool("memory_ask", {"question": question})
            end = time.monotonic_ns()
            data = result.structuredContent or {}
            print(
                "agent_received",
                i,
                end,
                "epoch_ms",
                time.time_ns() // 1_000_000,
                "seconds",
                round((end - start) / 1e9, 3),
                "error",
                data.get("error", {}).get("code"),
                "citations",
                len(data.get("sources", [])),
                flush=True,
            )
            if result.isError:
                return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/conversations.sqlite"))
    parser.add_argument("--question", required=True)
    parser.add_argument("--repetitions", type=int, default=2, choices=range(1, 6))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(benchmark(args.db, args.question, args.repetitions)))
