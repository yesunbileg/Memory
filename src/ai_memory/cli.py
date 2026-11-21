"""Small explicit CLI; personal conversations are never collected implicitly."""

import argparse
import asyncio
import errno
import json
import os
import sys
from pathlib import Path

from .archive_files import backup_archive, export_segments, restore_archive
from .diagnostics import SAFE_FAILURES, PublicInputError, failure_code, provider_failure
from .importer import collect_into
from .segments import SegmentBuilder
from .store import Store


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default inserts raw argv, including private questions or credentials.
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments. Run {self.prog} --help for syntax.\n")


def parser():
    p = _PrivateArgumentParser(description="Lossless AI archive → NotebookLM → MCP")
    p.add_argument(
        "--db", type=Path, default=Path(os.environ.get("AI_MEMORY_DB", "data/conversations.sqlite"))
    )
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create an archive and optionally bind one notebook")
    init.add_argument("--notebook-id")
    init.add_argument("--storage", type=Path)
    collect = sub.add_parser("collect", help="Import an explicitly selected local history")
    collect.add_argument("platform", choices=["codex", "opencode"])
    collect.add_argument("path", type=Path)
    collect.add_argument("--account", default="local")
    collect.add_argument(
        "--approve-upload",
        action="store_true",
        help="Explicitly approve imported text for Google upload",
    )
    approve = sub.add_parser(
        "approve",
        help="Allow future Google uploads of these conversations, including later continuations (no network)",
    )
    approve.add_argument("conversation_ids", nargs="+")
    sub.add_parser("pending", help="List local conversations awaiting Google upload permission")
    preview = sub.add_parser(
        "preview", help="Review exact masked Markdown locally, without granting consent"
    )
    preview.add_argument("conversation_id")
    revoke = sub.add_parser("revoke", help="Stop future uploads; do not delete old Google copies")
    revoke.add_argument("conversation_ids", nargs="+")
    backup = sub.add_parser(
        "backup", help="Make a portable private SQLite backup including pending snapshots"
    )
    backup.add_argument("destination", type=Path)
    restore = sub.add_parser("restore", help="Restore a backup into a new --db archive path")
    restore.add_argument("backup", type=Path)
    build = sub.add_parser("build", help="Generate approved Markdown segments, without networking")
    build.add_argument("--output", type=Path)
    recover = sub.add_parser(
        "republish", help="Authorize republishing a confirmed missing cloud source"
    )
    recover.add_argument("segment_id")
    sub.add_parser("sync", help="Upload changed segments, verify, then retire old sources")
    ask = sub.add_parser(
        "ask", help="Retrieve prior context from NotebookLM through the memory gateway"
    )
    ask.add_argument("question")
    sub.add_parser("status", help="Local archive and sync counts (no network)")
    sub.add_parser("check", help="Check NotebookLM connectivity via memory_status")
    show = sub.add_parser("show", help="Read the original conversation by archive ID")
    show.add_argument("conversation_id")
    serve = sub.add_parser(
        "serve", help="Authenticated loopback ingest for the hstry browser extension"
    )
    serve.add_argument("--port", type=int, default=41770)
    serve.add_argument("--token-file", type=Path)
    connect = sub.add_parser(
        "connect", help="Register a browser extension with a local AI Memory archive"
    )
    connect.add_argument(
        "--extension-id",
        help="Register the installed browser extension ID for private Native Messaging",
    )
    connect.add_argument("--browser", choices=["brave", "chrome", "chromium"], default="brave")
    connect.add_argument(
        "--browser-user-data-dir",
        type=Path,
        help="Optional browser user-data root (not a profile subdirectory)",
    )
    connect.add_argument("--port", type=int, default=41770)
    sub.add_parser("mcp", help="Serve memory_ask and memory_status over stdio")
    hook = sub.add_parser("hook", help="Import one completed session from a native hook on stdin")
    clients = hook.add_subparsers(dest="platform", required=True)
    for platform in ("codex", "opencode"):
        client = clients.add_parser(platform)
        client.add_argument("--account", default="local")
        if platform == "opencode":
            client.add_argument(
                "--source", type=Path, required=True, help="Absolute OpenCode SQLite path"
            )
    demo = sub.add_parser(
        "demo", help="Run repeatable synthetic Codex/OpenCode → SQLite → Markdown demo"
    )
    demo.add_argument("--output", type=Path, default=Path("data/demo"))
    return p


async def _gateway_call(db_path, name, arguments):
    from .gateway import MemoryApplication

    return await MemoryApplication(db_path).call(name, arguments)


async def sync_archive(store):
    from .archive_lock import archive_lock
    from .notebook import NotebookLMAdapter
    from .sync import SyncEngine

    # Hold the same lock as notebook rebinding before reading its settings.
    with archive_lock(store.path):
        notebook_id = store.get_setting("notebook_id")
        if not notebook_id:
            raise PublicInputError("Set a notebook ID with init --notebook-id before syncing")
        storage = store.get_setting("storage_path")
        adapter = None
        try:
            adapter = NotebookLMAdapter(notebook_id, Path(storage) if storage else None)
            result = await SyncEngine(
                store, SegmentBuilder(store), adapter, store.workspace / "sync"
            )._run_once_locked()
            errors = _preflight_errors(result)
            store.set_setting("sync_preflight_errors", errors)
            result["status"]["sync_preflight_errors"] = errors
            return result
        except Exception as error:
            store.set_setting("sync_preflight_errors", [{"reason": failure_code(error)}])
            raise
        finally:
            if adapter is not None:
                await adapter.close()


def _preflight_errors(result):
    """Jobless blockers need their own safe diagnostics, distinct from queue state."""
    reasons = {
        blocker.get("reason")
        if isinstance(blocker.get("reason"), str) and blocker["reason"] in SAFE_FAILURES
        else "operation_failed"
        for blocker in result.get("blocked", [])
        if not blocker.get("segment_id")
    }
    return [{"reason": reason} for reason in sorted(reasons)]


def execute(args):
    if args.command == "restore":
        return restore_archive(args.db, args.backup)
    if args.command == "demo":
        from .demo import run_demo

        return run_demo(args.output)
    if args.command == "mcp":
        from .gateway import stdio_main

        asyncio.run(stdio_main(args.db))
        return None
    if args.command == "hook":
        from .hooks import capture_event

        with Store(args.db, existing=True) as store:
            capture_event(
                store, args.platform, args.account, getattr(args, "source", None), sys.stdin.buffer
            )
        return None
    if args.command == "connect" and args.extension_id:
        from .native import register

        return register(args.db, args.extension_id, args.browser, args.browser_user_data_dir)
    if args.command == "connect":
        from .connection import (
            check_running_receiver,
            prepare_connection,
            write_connection_file,
        )
        from .ingest import make_server

        files = prepare_connection(args.db, args.port, write_config=False)
        try:
            server = make_server(args.db, files.token, port=files.port)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            status = check_running_receiver(files.port, files.token, args.db)
            if status == "ok":
                write_connection_file(files)
                print(
                    "AI Memory is already running.\n"
                    "Legacy HTTP client configuration (for browser pairing, run connect --extension-id ID):\n"
                    f"{files.connection_file}\n"
                    "Keep the receiver's terminal open while you use the extension.",
                    flush=True,
                )
                return None
            if status == "different_archive":
                raise PublicInputError(
                    f"Port {files.port} is serving a different archive. "
                    "Stop that receiver before selecting this archive, or use a separate data directory and port."
                ) from None
            if status == "unauthorized":
                raise PublicInputError(
                    f"Port {files.port} is using a different connection token. "
                    "Use that receiver's connection file, or choose another port with --port."
                ) from None
            raise PublicInputError(
                f"Port {files.port} is already in use by an incompatible receiver. "
                "Stop it before running connect, or choose another port with --port."
            ) from None
        try:
            write_connection_file(files)
            print(
                "AI Memory is ready.\n"
                "Legacy HTTP client configuration (for browser pairing, run connect --extension-id ID):\n"
                f"{files.connection_file}\n"
                "Keep this terminal open while you use the extension. Press Ctrl+C to stop.",
                flush=True,
            )
            server.serve_forever()
        finally:
            server.server_close()
        return None
    if args.command == "serve":
        from .ingest import make_server

        token = (
            args.token_file.read_text().strip()
            if args.token_file
            else os.environ.get("AI_MEMORY_INGEST_TOKEN", "")
        )
        if len(token) < 24:
            raise PublicInputError(
                "Provide a private token of at least 24 characters via --token-file or AI_MEMORY_INGEST_TOKEN"
            )
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
    try:
        archive = Store(args.db, existing=args.command not in {"init", "collect"})
    except FileNotFoundError:
        return {"error": {"code": "archive_not_found", "message": "Archive does not exist."}}
    with archive as store:
        if args.command == "init":
            if args.notebook_id:
                store.set_setting("notebook_id", args.notebook_id)
            if args.storage:
                store.set_setting("storage_path", str(args.storage.expanduser().resolve()))
            return store.status()
        if args.command == "collect":
            return collect_into(store, args.platform, args.path, args.account, args.approve_upload)
        if args.command == "approve":
            return {
                "approved": store.approve_many(args.conversation_ids),
                "includes_future_continuations": True,
                "uploaded": False,
            }
        if args.command == "revoke":
            return {
                "revoked": store.revoke_many(args.conversation_ids),
                "old_google_copies_deleted": False,
            }
        if args.command == "pending":
            from .consent import pending

            return pending(store)
        if args.command == "preview":
            from .consent import preview

            return preview(store, args.conversation_id)
        if args.command == "backup":
            return backup_archive(store, args.destination)
        if args.command == "build":
            builder = SegmentBuilder(store)
            builder.build()
            return export_segments(store, builder, args.output)
        if args.command == "republish":
            from .sync import republish

            return republish(store, args.segment_id)
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
        if args.command == "hook":
            # Hooks share a host protocol on stdout. Never echo event content or paths.
            print("AI Memory capture failed. Check the archive, event and source.", file=sys.stderr)
            return 1
        from .notebook import NotebookLMError

        if args.command == "connect":
            message = (
                str(exc)
                if isinstance(exc, PublicInputError)
                else "Connection setup failed. Check the archive directory and try again."
            )
            print(f"AI Memory could not connect. {message}", flush=True)
            return 1
        if isinstance(exc, FileNotFoundError) and args.command in {"status", "check"}:
            result = {"error": {"code": "archive_not_found", "message": "Archive does not exist."}}
        elif isinstance(exc, NotebookLMError):
            result = {"error": provider_failure(exc)}
        elif isinstance(exc, ValueError):
            result = {
                "error": {
                    "code": "invalid_configuration",
                    "message": str(exc)
                    if isinstance(exc, PublicInputError)
                    else "Invalid input or configuration; check the selected archive and command arguments.",
                }
            }
        else:
            result = {
                "error": {
                    "code": failure_code(exc),
                    "message": "Operation failed; archive retained. Check configuration and retry.",
                }
            }
        print(json.dumps(result, ensure_ascii=False))
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if (
            "error" in result
            or (args.command == "check" and not result.get("notebook", {}).get("connected"))
            or result.get("blocked")
            or result.get("failed_files")
            or result.get("status", {}).get("sync_status") == "pending"
            and args.command == "sync"
        ):
            return 1
    return 0
