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
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


async def audit(db, output=None):
    sys.path.insert(0, str(ROOT))
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    from mem import (
        AiBlock,
        JobDialog,
        Mem,
        NotebookDialog,
        PromptInput,
        UserBlock,
        find_cmd,
    )
    from mem_backend import Backend
    from mem_flow import FlowCanvas

    class OfflineBackend(Backend):
        def __init__(self, archive=db):
            super().__init__(db=archive, repo=ROOT)
            self.operations = []
            self.indexing = asyncio.Event()
            self.release = asyncio.Event()
            self.sync_activity = {}
            self.ask_started = asyncio.Event()
            self.ask_stopped = asyncio.Event()
            self.auto_changes = []

        def snapshot(self):
            snapshot = super().snapshot()
            if self.sync_activity:
                snapshot.update(auto_sync=True, auto_sync_preference=True)
                snapshot["sync_activity"] = self.sync_activity
            return snapshot

        def set_auto_sync(self, enabled):
            self.auto_changes.append(enabled)
            return super().set_auto_sync(enabled)

        async def call(self, operation, question="", *, on_progress=None):
            self.operations.append(operation)
            if operation == "docs-connect":
                return {}
            if operation == "sync":
                for phase in ("upload", "readiness"):
                    on_progress({"phase": phase, "segment_id": "SEG-001"})
                    if phase == "readiness":
                        self.indexing.set()
                        await self.release.wait()
                return {"completed": 1, "deferred": 0, "docs_web_write_payload_bytes": 888}
            if operation == "ask":
                if question == "Synthetic audit question":
                    return {"answer": "Synthetic audit answer", "sources": [], "citations": []}
                if question == "Long Markdown question":
                    answer = "## Opening heading\n\n"
                    answer += "\n".join(f"- Synthetic list row {i}" for i in range(60))
                    return {"answer": answer, "sources": [], "citations": []}
                if question == "Question to stop":
                    self.ask_started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        self.ask_stopped.set()
            raise AssertionError(operation)

    class OfflineMem(Mem):
        def __init__(self, backend):
            super().__init__(backend)
            self.toasts = []

        def poll_notebook(self):
            pass

        def poll_connection(self):
            pass

        def toast(self, msg, kind="information"):
            self.toasts.append(msg)
            super().toast(msg, kind)

    async def until(pilot, condition):
        for _ in range(30):
            await pilot.pause(0.05)
            if condition():
                return
        raise AssertionError("TUI condition did not complete")

    async def enter(app, pilot, text):
        prompt = app.q("#p-home").query_one(PromptInput)
        prompt.focus()
        prompt.value = text
        await pilot.press("enter")

    assert all(find_cmd(command) is None for command in ("new", "resume", "sessions"))
    assert all(find_cmd(command) is None for command in ("sync-all", "syns-all"))
    assert find_cmd("sync") is not None
    results = []
    if output is not None:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            assert app.focused.id == "sync"
            await pilot.press("enter")
            await until(pilot, backend.indexing.is_set)
            assert isinstance(app.screen, JobDialog)
            assert "хүлээж" in app.job.steps[-1].detail
            if output is not None:
                app.save_screenshot(f"indexing-{width}x{height}.svg", path=str(output))
            backend.release.set()
            await until(pilot, lambda app=app: app.job.status in {"done", "partial"})
            assert "888 байт" in app.job.steps[-1].detail
            await pilot.press("escape")
            await enter(app, pilot, "Synthetic audit question")
            await until(
                pilot, lambda app=app: app.ask_messages and not app.ask_messages[-1]["pending"]
            )
            assert app.ask_messages[-1]["text"] == "Synthetic audit answer"
            assert backend.operations == ["docs-connect", "sync", "ask"]

            # Returning from another tab keeps the same transcript.
            app.cmd_dashboard()
            await pilot.press("escape")
            assert app.route == "home"
            await enter(app, pilot, "Long Markdown question")
            await until(
                pilot,
                lambda app=app: len(app.ask_messages) == 2 and not app.ask_messages[-1]["pending"],
            )
            box = app.q("#msgs", VerticalScroll)
            await until(pilot, lambda box=box: box.max_scroll_y > 0)
            assert len(box.query(UserBlock)) == 2
            answer = box.query(AiBlock).last()
            assert (
                box.scrollable_content_region.y
                <= answer.region.y
                < (box.scrollable_content_region.bottom)
            ), (box.scrollable_content_region, answer.region, box.scroll_y)
            assert box.scroll_y < box.max_scroll_y

            backend.sync_activity = {"phase": "readiness", "automatic": True}
            await app.refresh_archive(force=True)
            assert app.q("#sb-auto", Static).render().plain == "Auto …"
            await enter(app, pilot, "Question to stop")
            await until(pilot, backend.ask_started.is_set)
            await pilot.press("ctrl+x")
            await until(pilot, backend.ask_stopped.is_set)
            assert not backend.auto_changes
            assert app.auto_sync
            assert not app.ask_messages[-1]["pending"]
            assert "зогсоосон" in app.ask_messages[-1]["text"]
            results.append({"size": [width, height], "status": "PASS", "bytes_displayed": 888})

    from ai_memory.store import Store

    with TemporaryDirectory(prefix="memory-tui-first-run-") as directory:
        archive = Path(directory) / "first-run.sqlite"
        for initialized in (False, True):
            if initialized:
                with Store(archive):
                    pass
            backend = OfflineBackend(archive)
            app = OfflineMem(backend)
            async with app.run_test(size=(54, 24)) as pilot:
                await until(pilot, lambda app=app: not app.archive_loading)
                await enter(app, pilot, "Question before setup")
                await until(
                    pilot, lambda app=app: app.ask_messages and not app.ask_messages[-1]["pending"]
                )
                assert "/setup" in app.ask_messages[-1]["text"]
                assert app.q("#sb-auto", Static).render().plain == "Auto /setup"
                await enter(app, pilot, "/auto on")
                await until(pilot, lambda app=app: app.toasts and "/setup" in app.toasts[-1])
                assert app.q("#sb-auto", Static).render().plain == "Auto /setup"
                assert bool(backend.auto_changes) == initialized
                await enter(app, pilot, "/setup")
                await until(pilot, lambda app=app: isinstance(app.screen, NotebookDialog))
                assert app.screen.query_one("#create").display
                assert app.screen.query_one("#attach").display
                assert not backend.operations
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="Synthetic audit SQLite only")
    parser.add_argument("--output", type=Path, help="Save synthetic TUI screenshots here")
    args = parser.parse_args()
    print(json.dumps({"remote": "simulated", "screens": asyncio.run(audit(args.db, args.output))}))
