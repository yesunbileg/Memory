"""Small explicit CLI; personal conversations are never collected implicitly."""

import argparse
import asyncio
import errno
import hashlib
import json
import os
import time
from pathlib import Path

from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store


def parser():
    p = argparse.ArgumentParser(description="Lossless AI archive → NotebookLM → MCP")
    p.add_argument("--db", type=Path, default=Path(os.environ.get("AI_MEMORY_DB", "data/conversations.sqlite")))
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create an archive and optionally bind one notebook")
    init.add_argument("--notebook-id")
    init.add_argument("--storage", type=Path)
    collect = sub.add_parser("collect", help="Import an explicitly selected local history")
    collect.add_argument("platform", choices=["codex", "opencode"])
    collect.add_argument("path", type=Path)
    collect.add_argument("--account", default="local")
    collect.add_argument("--approve-upload", action="store_true", help="Explicitly approve imported text for Google upload")
    approve = sub.add_parser("approve", help="Approve specific conversations for cloud upload")
    approve.add_argument("conversation_ids", nargs="+")
    build = sub.add_parser("build", help="Generate approved Markdown segments, without networking")
    build.add_argument("--output", type=Path)
    sub.add_parser("sync", help="Upload changed segments, verify, then retire old sources")
    ask = sub.add_parser("ask", help="Retrieve prior context from NotebookLM through the memory gateway")
    ask.add_argument("question")
    sub.add_parser("status", help="Local archive and sync counts (no network)")
    sub.add_parser("check", help="Check NotebookLM connectivity via memory_status")
    show = sub.add_parser("show", help="Read the original conversation by archive ID")
    show.add_argument("conversation_id")
    serve = sub.add_parser("serve", help="Authenticated loopback ingest for the hstry browser extension")
    serve.add_argument("--port", type=int, default=41770)
    serve.add_argument("--token-file", type=Path)
    connect = sub.add_parser("connect", help="Create a private extension connection file and start AI Memory")
    connect.add_argument("--port", type=int, default=41770)
    sub.add_parser("mcp", help="Serve memory_ask and memory_status over stdio")
    watch = sub.add_parser("watch", help="Incrementally scan selected sources; sync only approved text")
    watch.add_argument("--codex-path", type=Path)
    watch.add_argument("--opencode-path", type=Path)
    watch.add_argument("--account", default="local")
    watch.add_argument("--interval", type=float, default=30)
    watch.add_argument("--sync-interval", type=float, default=43200)
    demo = sub.add_parser("demo", help="Run repeatable synthetic Codex/OpenCode → SQLite → Markdown demo")
    demo.add_argument("--output", type=Path, default=Path("data/demo"))
    return p


def collect_into(store, platform, path, account="local", consent=False):
    from ai_memory.collectors import collect

    path = Path(path).expanduser().resolve(strict=True)
    files = sorted(f for f in (path.rglob("*") if path.is_dir() else [path]) if f.is_file())
    inventory = [(str(f.relative_to(path) if path.is_dir() else f.name), f.stat().st_size, f.stat().st_mtime_ns) for f in files]
    fingerprint = hashlib.sha256(json.dumps(inventory).encode()).hexdigest()
    key = f"{platform}:{account}:{path}"
    checkpoint = store.connection.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
    if checkpoint and checkpoint[0] == fingerprint and not consent:
        return {"imported_conversations": 0, "conversation_ids": [], "unchanged": True}
    ids = []
    for c in collect(platform, path, account=account):
        # Use native identities for already archived parts, append newly exposed
        # parts after existing sequence numbers on partial/reordered snapshots.
        existing = store.connection.execute("SELECT id FROM conversations WHERE platform=? AND account=? AND external_id=?", (c["platform"], c["account"], c["external_id"])).fetchone()
        if existing:
            previous = {m["native_id"]: m["sequence"] for m in store.get_conversation(existing[0])["messages"]}
            last = max(previous.values(), default=0)
            for m in c["messages"]:
                if m["native_id"] in previous:
                    m["sequence"] = previous[m["native_id"]]
                else:
                    last += 1
                    m["sequence"] = last
        ids.append(store.ingest(c, consent=consent))
    with store.connection:
        store.connection.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?)", (key, fingerprint))
    return {"imported_conversations": len(ids), "conversation_ids": ids, "unchanged": False}


async def _gateway_call(db_path, name, arguments):
    from ai_memory.gateway import create_server

    server = create_server(db_path)
    # FastMCP's lifecycle also closes the lazy notebook client for CLI use.
    async with server._mcp_server.lifespan(server._mcp_server):
        result = await server.call_tool(name, arguments)
    if isinstance(result, tuple):
        return result[1]
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


async def sync_archive(store):
    from ai_memory.notebook import NotebookLMAdapter
    from ai_memory.sync import SyncEngine

    notebook_id = store.get_setting("notebook_id")
    if not notebook_id:
        raise ValueError("Set a notebook ID with init --notebook-id before syncing")
    storage = store.get_setting("storage_path")
    adapter = NotebookLMAdapter(notebook_id, Path(storage) if storage else None)
    try:
        return await SyncEngine(store, SegmentBuilder(store), adapter, store.path.parent / "sync").run_once()
    finally:
        await adapter.close()


def _watch(args):
    if args.interval < 5 or args.sync_interval < 300:
        raise ValueError("watch requires interval >= 5 seconds and sync-interval >= 300 seconds")
    sources = [(p, path) for p, path in (("codex", args.codex_path), ("opencode", args.opencode_path)) if path]
    if not sources:
        raise ValueError("watch requires at least one explicit source path")
    next_sync = 0
    while True:
        with Store(args.db) as store:
            for platform, path in sources:
                collect_into(store, platform, path, account=args.account)
            if time.monotonic() >= next_sync and store.get_setting("notebook_id"):
                # Postpone optional remote work if local CPU load is high.
                if not hasattr(os, "getloadavg") or os.getloadavg()[0] <= (os.cpu_count() or 1):
                    result = asyncio.run(sync_archive(store))
                    # Pending jobs retain their own backoff deadline in SQLite;
                    # revisit them during scans instead of waiting the full batch period.
                    delay = args.interval if result["status"]["sync_status"] == "pending" else args.sync_interval
                    next_sync = time.monotonic() + delay
        time.sleep(args.interval)


def execute(args):
    if args.command == "demo":
        from ai_memory.demo import run_demo
        return run_demo(args.output)
    if args.command == "mcp":
        from ai_memory.gateway import create_server
        create_server(args.db).run(transport="stdio")
        return None
    if args.command == "watch":
        return _watch(args)
    if args.command == "connect":
        from ai_memory.connection import (
            check_running_receiver,
            prepare_connection,
            write_connection_file,
        )
        from ai_memory.ingest import make_server

        files = prepare_connection(args.db, args.port, write_config=False)
        try:
            server = make_server(args.db, files.token, port=files.port)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            status = check_running_receiver(files.port, files.token)
            if status == "ok":
                write_connection_file(files)
                print(
                    "AI Memory is already running.\n"
                    "In extension settings, choose this private connection file:\n"
                    f"{files.connection_file}\n"
                    "Keep the receiver's terminal open while you use the extension.",
                    flush=True,
                )
                return None
            if status == "unauthorized":
                raise ValueError(
                    f"Port {files.port} is using a different connection token. "
                    "Use that receiver's connection file, or choose another port with --port."
                ) from None
            raise ValueError(
                f"Port {files.port} is already in use by an incompatible receiver. "
                "Stop it before running connect, or choose another port with --port."
            ) from None
        try:
            write_connection_file(files)
            print(
                "AI Memory is ready.\n"
                "In extension settings, choose this private connection file:\n"
                f"{files.connection_file}\n"
                "Keep this terminal open while you use the extension. Press Ctrl+C to stop.",
                flush=True,
            )
            server.serve_forever()
        finally:
            server.server_close()
        return None
    if args.command == "serve":
        from ai_memory.ingest import make_server
        token = args.token_file.read_text().strip() if args.token_file else os.environ.get("AI_MEMORY_INGEST_TOKEN", "")
        if len(token) < 24:
            raise ValueError("Provide a private token of at least 24 characters via --token-file or AI_MEMORY_INGEST_TOKEN")
        server = make_server(args.db, token, port=args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return None
    if args.command in {"ask", "check"}:
        tool = "memory_ask" if args.command == "ask" else "memory_status"
        arguments = {"question": args.question} if args.command == "ask" else {}
        return asyncio.run(_gateway_call(args.db, tool, arguments))
    with Store(args.db) as store:
        if args.command == "init":
            if args.notebook_id:
                store.set_setting("notebook_id", args.notebook_id)
            if args.storage:
                store.set_setting("storage_path", str(args.storage.expanduser().resolve()))
            return store.status()
        if args.command == "collect":
            return collect_into(store, args.platform, args.path, args.account, args.approve_upload)
        if args.command == "approve":
            for key in args.conversation_ids:
                store.approve(key)
            return {"approved": args.conversation_ids}
        if args.command == "build":
            builder = SegmentBuilder(store)
            builder.build()
            output = args.output or store.path.parent / "segments"
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            ids = []
            for r in store.connection.execute("SELECT id FROM segments WHERE state!='retired' ORDER BY rowid"):
                path = output / f"{r[0]}.md"
                path.write_text(builder.render(r[0]), encoding="utf-8", newline="")
                path.chmod(0o600)
                ids.append(r[0])
            return {"segments": ids, "output": str(output)}
        if args.command == "sync":
            return asyncio.run(sync_archive(store))
        if args.command == "show":
            return store.get_conversation(args.conversation_id)
        return store.status()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from ai_memory.notebook import NotebookLMError
        if args.command == "connect":
            message = str(exc) if isinstance(exc, ValueError) else "Connection setup failed. Check the archive directory and try again."
            print(f"AI Memory could not connect. {message}", flush=True)
            return 1
        if isinstance(exc, NotebookLMError):
            result = {"error": {"code": exc.code, "message": str(exc)}}
        elif isinstance(exc, ValueError):
            result = {"error": {"code": "invalid_configuration", "message": str(exc)}}
        else:
            result = {"error": {"code": type(exc).__name__, "message": "Operation failed; archive retained. Check configuration and retry."}}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if "error" in result or result.get("blocked") or result.get("status", {}).get("sync_status") == "pending" and args.command == "sync":
            return 1
    return 0
