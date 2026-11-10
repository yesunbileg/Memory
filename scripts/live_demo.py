"""Explicit live Google demo using synthetic data only; requires browser auth."""

import argparse
import asyncio
import json
from pathlib import Path

from ai_memory.cli import _gateway_call, sync_archive
from ai_memory.demo import run_demo
from ai_memory.notebook import NotebookLMAuthError, NotebookLMError
from ai_memory.store import Store


async def live_demo(output: Path):
    # The offline preparation clearly marks all input as synthetic.
    result = run_demo(output)
    db = output.resolve() / "conversations.sqlite"
    with Store(db) as store:
        if not store.get_setting("notebook_id"):
            from notebooklm import NotebookLMClient
            try:
                async with NotebookLMClient.from_storage() as client:
                    title = "AI Memory — synthetic integration demo"
                    existing = [n for n in await client.notebooks.list() if n.title == title]
                    if len(existing) > 1:
                        raise ValueError("Multiple demo notebooks exist; configure the chosen ID with ai-memory init")
                    notebook = existing[0] if existing else await client.notebooks.create(title)
                    store.set_setting("notebook_id", notebook.id)
            except (FileNotFoundError, PermissionError):
                raise NotebookLMAuthError() from None
            except (ValueError, NotebookLMError):
                raise
            except Exception:
                raise NotebookLMError("Live notebook creation failed. Check browser login and allowed Google domains.") from None
        sync = await sync_archive(store)
        result["sync"] = sync
        if sync["blocked"] or sync["status"]["sync_status"] != "up_to_date":
            result["live_notebooklm_test"] = "blocked"
            return result
    question = "What decisions did the synthetic Codex and OpenCode conversations make about SQLite, NotebookLM, and source replacement? Include conversation IDs."
    result["answer"] = await _gateway_call(db, "memory_ask", {"question": question})
    result["live_notebooklm_test"] = "failed" if "error" in result["answer"] else "executed_review_answer"
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("data/live-demo"))
    args = p.parse_args()
    try:
        result = asyncio.run(live_demo(args.output))
    except NotebookLMError as exc:
        result = {"live_notebooklm_test": "blocked", "error": {"code": exc.code, "message": str(exc)}}
    except Exception:
        result = {"live_notebooklm_test": "blocked", "error": {"code": "configuration_error", "message": "Check output directory, login, and notebook configuration."}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("live_notebooklm_test") == "executed_review_answer" else 1


if __name__ == "__main__":
    raise SystemExit(main())
