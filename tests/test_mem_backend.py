import sys
import time
from pathlib import Path

from test_store import conversation
from test_sync import Remote

from ai_memory.cli import sync_archive
from ai_memory.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mem_backend import Backend  # noqa: E402


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
        await sync_archive(store, all_chats=True)
        assert backend.snapshot()["auto_sync_due"] is None

        chat["messages"][0]["text"] = "Edited after first sync"
        store.ingest(chat)
        assert backend.snapshot()["auto_sync_due"] == 0
        remote.fail_upload = True
        result = await sync_archive(store, all_chats=True)
        deadline = result["blocked"][0]["next_attempt"]
        assert backend.snapshot()["auto_sync_due"] == deadline > time.time()

        store.ingest(conversation(external_id="another-chat"))
        assert backend.snapshot()["auto_sync_due"] == 0
        backend.set_auto_sync(False)
        assert Backend(store.path).snapshot()["auto_sync"] is False


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
