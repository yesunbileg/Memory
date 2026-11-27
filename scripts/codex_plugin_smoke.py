"""Verify native plugin + MCP discovery in a disposable Codex home without a model turn."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "ai-memory@yesko-memory-local"


async def smoke(tmp):
    profile = tmp / "codex"
    profile.mkdir()
    env = {"HOME": str(tmp), "CODEX_HOME": str(profile), "PATH": os.environ["PATH"]}
    db = tmp / "synthetic.sqlite"
    cli = Path(sys.executable).parent / "ai-memory"
    commands = [
        [cli, "--db", db, "init"],
        [sys.executable, ROOT / "config/install_hooks.py", "--cli", cli, "--db", db],
        ["codex", "plugin", "marketplace", "add", ROOT],
        ["codex", "plugin", "add", PLUGIN_ID],
    ]
    for command in commands:
        subprocess.run(command, cwd=tmp, env=env, stdout=subprocess.DEVNULL, check=True, timeout=30)
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "app-server",
        "--listen",
        "stdio://",
        cwd=tmp,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async def call(id_, method, params):
        proc.stdin.write(json.dumps(dict(id=id_, method=method, params=params)).encode() + b"\n")
        await proc.stdin.drain()
        async with asyncio.timeout(30):
            while line := await proc.stdout.readline():
                msg = json.loads(line)
                if msg.get("id") == id_:
                    if "error" in msg:
                        raise RuntimeError(f"Codex rejected {method}: {msg['error']}")
                    return msg["result"]
            raise RuntimeError(f"Codex closed before replying to {method}")

    try:
        await call(
            1,
            "initialize",
            {
                "clientInfo": {"name": "ai-memory-smoke", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        hook_result = await call(2, "hooks/list", {"cwds": [str(tmp)]})
        hooks = [
            h
            for scope in hook_result["data"]
            for h in scope["hooks"]
            if h.get("pluginId") == PLUGIN_ID
        ]
        if [(h["eventName"], h["trustStatus"]) for h in hooks] != [("stop", "untrusted")]:
            raise RuntimeError("Expected one new Stop hook for review")
        result = await call(3, "mcpServerStatus/list", {"serverName": "ai_memory"})
        servers = [s for s in result["data"] if s.get("pluginId") == PLUGIN_ID]
        if len(servers) != 1 or set(servers[0]["tools"]) != {
            "memory_ask",
            "memory_ask_all_goals_requirements",
            "memory_status",
        }:
            raise RuntimeError("Installed plugin did not provide all MCP tools")
        print("PASS: installed plugin, native Stop awaiting review, all local MCP tools available")
    finally:
        proc.terminate()
        await proc.wait()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ai-memory-codex-") as folder:
        asyncio.run(smoke(Path(folder)))
