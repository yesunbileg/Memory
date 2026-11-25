# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Exercise actual Textual screens against a synthetic archive, without remote mutations."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def audit(db):
    sys.path.insert(0, str(ROOT))
    from mem import JobDialog, Mem, NotebookDialog, PromptInput
    from mem_backend import Backend
    from mem_flow import FlowCanvas

    class OfflineBackend(Backend):
        def __init__(self):
            super().__init__(db=db, repo=ROOT)
            self.operations = []
            self.indexing = asyncio.Event()
            self.release = asyncio.Event()

        async def call(self, operation, question="", *, on_progress=None):
            self.operations.append(operation)
            if operation == "docs-connect":
                return {}
            if operation == "sync-all":
                for phase in ("upload", "readiness"):
                    on_progress({"phase": phase, "segment_id": "SEG-001"})
                    if phase == "readiness":
                        self.indexing.set()
                        await self.release.wait()
                return {"completed": 1, "deferred": 0, "docs_web_write_payload_bytes": 888}
            if operation == "ask":
                assert question == "Synthetic audit question"
                return {"answer": "Synthetic audit answer", "sources": [], "citations": []}
            raise AssertionError(operation)

    class OfflineMem(Mem):
        def poll_notebook(self):
            pass

        def poll_connection(self):
            pass

    async def until(pilot, condition):
        for _ in range(30):
            await pilot.pause(0.05)
            if condition():
                return
        raise AssertionError("TUI condition did not complete")

    results = []
    for width, height in ((140, 42), (54, 24)):
        backend = OfflineBackend()
        app = OfflineMem(backend)
        async with app.run_test(size=(width, height)) as pilot:
            await until(pilot, lambda app=app: not app.archive_loading)
            assert app.archive_status["notebook_configured"]
            assert not app.auto_sync
            app.cmd_connect()
            await until(pilot, lambda app=app: isinstance(app.screen, NotebookDialog))
            assert app.focused.id == "login"
            await pilot.press("tab")
            assert app.focused.id == "docs-connect"
            await pilot.press("enter")
            await until(pilot, lambda app=app: app.job and app.job.status == "done")
            assert isinstance(app.screen, JobDialog)
            assert "Google Docs sync" in app.job.steps[-1].detail
            await pilot.press("escape")
            app.cmd_connect()
            await until(pilot, lambda app=app: isinstance(app.screen, NotebookDialog))
            await pilot.press("tab", "tab", "tab", "enter")
            await until(pilot, lambda app=app: isinstance(app.screen, FlowCanvas))
            assert app.screen.has_class("-vertical") == (width < 116)
            assert "Docs индексжсэн" in app.screen.status_labels()["sync"]
            assert app.focused.id == "connect"
            await pilot.press("tab", "tab")
            assert app.focused.id == "sync-all"
            await pilot.press("enter")
            await until(pilot, backend.indexing.is_set)
            assert isinstance(app.screen, JobDialog)
            assert "хүлээж" in app.job.steps[-1].detail
            backend.release.set()
            await until(pilot, lambda app=app: app.job.status in {"done", "partial"})
            assert "888 байт" in app.job.steps[-1].detail
            await pilot.press("escape")
            prompt = app.screen.query_one("#p-home").query_one(PromptInput)
            prompt.focus()
            prompt.value = "Synthetic audit question"
            await pilot.press("enter")
            await until(
                pilot, lambda app=app: app.cur_sess() and not app.cur_sess().msgs[-1].get("pending")
            )
            assert app.cur_sess().msgs[-1]["text"] == "Synthetic audit answer"
            assert backend.operations == ["docs-connect", "sync-all", "ask"]
            results.append({"size": [width, height], "status": "PASS", "bytes_displayed": 888})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="Synthetic audit SQLite only")
    print(
        json.dumps({"remote": "simulated", "screens": asyncio.run(audit(parser.parse_args().db))})
    )
