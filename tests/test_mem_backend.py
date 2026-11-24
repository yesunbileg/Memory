import asyncio
import sys
import time
from pathlib import Path

import pytest
from test_store import conversation
from test_sync import Remote

from ai_memory.archive_lock import ArchiveBusyError
from ai_memory.cli import sync_archive
from ai_memory.store import Store
from ai_memory.sync_activity import read_sync_activity

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mem_backend import Backend  # noqa: E402


async def test_status_counts_current_chats_and_does_not_treat_empty_as_current(tmp_path):
    from ai_memory.segments import SegmentBuilder
    from ai_memory.sync import SyncEngine

    with Store(tmp_path / "archive.sqlite") as store:
        assert store.status()["sync_status"] == "no_sources"

        first = conversation()
        store.ingest(first, consent=True)
        remote = Remote()
        await SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md").run_once()
        store.ingest(conversation(external_id="local-only"))
        status = store.status()
        assert status["conversations"] == 2
        assert status["ready_conversations"] == 1
        assert status["pending_conversations"] == 1
        assert status["sync_status"] == "pending"

        first["messages"][0]["text"] = "Changed since upload"
        store.ingest(first)
        status = Backend(store.path).snapshot()["status"]
        assert status["ready_conversations"] == 0
        assert status["pending_conversations"] == 2


async def test_auto_sync_detects_new_chats_edits_and_retry_deadlines(tmp_path, monkeypatch):
    remote = Remote()

    async def close():
        pass

    remote.close = close
    monkeypatch.setattr("ai_memory.notebook.NotebookLMAdapter", lambda *_: remote)
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook-test")
        backend = Backend(store.path)
        assert backend.snapshot()["auto_sync"] is False
        backend.set_auto_sync(True)
        assert Backend(store.path).snapshot()["auto_sync"] is True
        assert backend.snapshot()["auto_sync_due"] is None

        chat = conversation()
        store.ingest(chat)
        assert backend.snapshot()["auto_sync_due"] == 0
        backend.set_auto_sync(False)
        assert await sync_archive(store, automatic=True) == {"automatic": "idle"}
        assert not remote.operations
        backend.set_auto_sync(True)
        assert (await sync_archive(store, automatic=True))["completed"] == 1
        assert backend.snapshot()["auto_sync_due"] is None
        previous = list(remote.operations)
        assert await sync_archive(store, automatic=True) == {"automatic": "idle"}
        assert remote.operations == previous

        chat["messages"][0]["text"] = "Edited after first sync"
        store.ingest(chat)
        assert backend.snapshot()["auto_sync_due"] == 0
        remote.fail_upload = True
        result = await sync_archive(store, all_chats=True)
        deadline = result["blocked"][0]["next_attempt"]
        assert backend.snapshot()["auto_sync_due"] == deadline > time.time()
        assert await sync_archive(store, automatic=True) == {"automatic": "idle"}

        store.ingest(conversation(external_id="another-chat"))
        assert backend.snapshot()["auto_sync_due"] == 0
        backend.set_auto_sync(False)
        assert Backend(store.path).snapshot()["auto_sync"] is False


async def test_auto_sync_respects_external_stop_and_exclusive_archive_lock(tmp_path, monkeypatch):
    entered = asyncio.Event()

    class PausedRemote(Remote):
        async def list_sources(self):
            entered.set()
            await asyncio.Future()

        async def close(self):
            pass

    monkeypatch.setattr("ai_memory.notebook.NotebookLMAdapter", lambda *_: PausedRemote())
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook-test")
        backend = Backend(store.path)
        backend.set_auto_sync(True)
        store.ingest(conversation())
        job = asyncio.create_task(sync_archive(store, automatic=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert read_sync_activity(store.path)["automatic"] is True
            with pytest.raises(ArchiveBusyError):
                await sync_archive(store, automatic=True)
            backend.set_auto_sync(False)
            assert await asyncio.wait_for(job, timeout=3) == {"automatic": "stopped"}
            assert read_sync_activity(store.path) is None
            assert store.status()["pending_conversations"] == 1
        finally:
            if not job.done():
                job.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await job


def test_auto_sync_waits_for_manual_recovery_of_a_missing_source(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook-test")
        backend = Backend(store.path)
        backend.set_auto_sync(True)
        with store.connection as conn:
            conn.execute("INSERT INTO segments(id,state,created_at) VALUES('S','open','today')")
            conn.execute(
                "INSERT INTO sync_jobs(segment_id,revision,title,stage) VALUES('S',0,'S','missing')"
            )
        assert backend.snapshot()["auto_sync_due"] is None
