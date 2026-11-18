"""Explicit live Google demo using synthetic data only; requires browser auth."""

import argparse
import asyncio
import json
from pathlib import Path

from ai_memory.cli import _gateway_call, sync_archive
from ai_memory.demo import run_demo
from ai_memory.notebook import NotebookLMAdapter, NotebookLMError
from ai_memory.store import Store


async def live_demo(output: Path, *, notebook_id: str):
    """Never adopt data from a prior local run or select a notebook by title."""
    if not notebook_id or not notebook_id.strip():
        raise ValueError("An explicit empty demo notebook ID is required")
    output = Path(output).expanduser()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Live demo requires an empty output directory")
    # All records are produced inside a new private directory; no retained marker,
    # DB, auth override or previously approved transcript is an input to this run.
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh = output / "synthetic"
    fresh.mkdir(mode=0o700)  # exclusive even if two live commands race
    result = run_demo(fresh)
    db = fresh.resolve() / "conversations.sqlite"
    with Store(db) as store:
        rows = store.connection.execute(
            "SELECT id,platform,account,external_id,consent FROM conversations"
        ).fetchall()
        expected = {
            ("codex", "synthetic-demo", "memory-demo-codex", 1),
            ("opencode", "synthetic-demo", "ses_memory", 1),
        }
        if (
            {tuple(row)[1:] for row in rows} != expected
            or {row["id"] for row in rows} != set(result["conversation_ids"])
            or store.get_setting("notebook_id")
            or store.get_setting("storage_path")
        ):
            raise ValueError("Archive does not contain exactly the generated synthetic fixture")
        notebook_id = notebook_id.strip()
        adapter = NotebookLMAdapter(notebook_id)
        try:
            if await adapter.list_sources():
                raise ValueError("Choose an empty notebook created for this demo")
        finally:
            await adapter.close()
        store.set_setting("notebook_id", notebook_id)
        sync = await sync_archive(store)
        result["sync"] = sync
        if sync["blocked"] or sync["status"]["sync_status"] != "up_to_date":
            result["live_notebooklm_test"] = "blocked"
            return result
    question = "What decisions did the synthetic Codex and OpenCode conversations make about SQLite, NotebookLM, and source replacement? Include conversation IDs."
    result["answer"] = await _gateway_call(db, "memory_ask", {"question": question})
    result["live_notebooklm_test"] = (
        "failed" if "error" in result["answer"] else "executed_review_answer"
    )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("data/live-demo"))
    p.add_argument("--notebook-id", required=True, help="ID of an empty dedicated demo notebook")
    args = p.parse_args()
    try:
        result = asyncio.run(live_demo(args.output, notebook_id=args.notebook_id))
    except NotebookLMError as exc:
        result = {
            "live_notebooklm_test": "blocked",
            "error": {"code": exc.code, "message": str(exc)},
        }
    except Exception:
        result = {
            "live_notebooklm_test": "blocked",
            "error": {
                "code": "configuration_error",
                "message": "Check output directory, login, and notebook configuration.",
            },
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("live_notebooklm_test") == "executed_review_answer" else 1


if __name__ == "__main__":
    raise SystemExit(main())
