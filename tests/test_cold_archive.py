import time
from datetime import UTC, datetime, timedelta

from test_store import conversation
from test_sync import make_retries_due
from test_web_docs_sync import Docs, Notebook

from ai_memory import cold_archive
from ai_memory.archive_lock import archive_lock
from ai_memory.publication import allowed_sources
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine
from ai_memory.web_docs_publication import enable_web_docs
from ai_memory.web_docs_transport import DocsWebError


def ingest(store, name, age, text=None):
    updated = (datetime.now(UTC) - timedelta(days=age)).isoformat()
    chat = conversation(external_id=name, created_at=updated, updated_at=updated)
    chat["messages"][0].update(text=text or name, timestamp=updated)
    return store.ingest(chat, consent=True)


async def test_compacts_mixed_sources_only_when_both_replacements_are_ready(tmp_path):
    class FailingHotReplacement(Notebook):
        fail_hot = False

        async def refresh_document(self, source_id, *args, **kwargs):
            if self.fail_hot and source_id == "drive-document-3":
                raise DocsWebError("not_ready")
            await super().refresh_document(source_id, *args, **kwargs)

    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "synthetic")
        enable_web_docs(store, 1)
        builder = SegmentBuilder(store)
        archive = store.connection
        ids = [ingest(store, f"cold-{n}", 31) for n in range(3)]
        ids.append(ingest(store, "active", 5))
        with archive:
            for group in ((ids[0],), (ids[1],), (ids[2], ids[3])):
                segment = builder._new("sealed")
                for position, chat in enumerate(group):
                    archive.execute(
                        """INSERT INTO segment_messages SELECT ?,id,? FROM messages
                        WHERE conversation_id=?""",
                        (segment, position, chat),
                    )
                builder._refresh(segment)
        remote, docs = FailingHotReplacement(), Docs()
        engine = SyncEngine(store, builder, remote, tmp_path / "snapshots", docs_client=docs)
        assert (await engine.run_once())["blocked"] == []
        old = set(remote.sources)
        assert len(old) == 3
        assert await engine.adapter.list_sources()

        store.set_setting(cold_archive.ENABLED, True)
        assert store.auto_sync_due() == 0
        not_yet_uploaded = ingest(store, "new-active", 1)
        remote.fail_hot = True
        with archive_lock(store.path):
            assert (await engine._run_once_locked(archive_only=True))["blocked"]
        assert set(allowed_sources(store)) == old
        assert old <= set(remote.sources)
        assert not any(op[0] == "delete" for op in remote.operations)
        assert store.get_setting(cold_archive.PLAN)
        uploads = sum(op[0] == "upload" for op in remote.operations)
        assert uploads == 1

        remote.fail_hot = False
        make_retries_due(store)
        restarted = SyncEngine(store, builder, remote, tmp_path / "snapshots", docs_client=docs)
        with archive_lock(store.path):
            assert (await restarted._run_once_locked(archive_only=True))["blocked"] == []
        assert store.get_setting(cold_archive.PLAN) is None
        assert len(remote.sources) == 2
        assert old.isdisjoint(remote.sources)
        assert sum(op[0] == "upload" for op in remote.operations) == uploads
        markdown = next(source["text"] for source in remote.sources.values() if "text" in source)
        assert all(chat_id in markdown for chat_id in ids[:3])
        assert ids[3] not in markdown
        assert ids[3] in docs.documents["document-3"]
        assert all(not_yet_uploaded not in content for content in docs.documents.values())
        assert store.status()["ready_conversations"] == 4


async def test_old_import_bypasses_docs_and_respects_rendered_word_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cold_archive, "MAX_WORDS", 100)
    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "synthetic")
        enable_web_docs(store, 1)
        store.set_setting(cold_archive.ENABLED, True)
        old = [ingest(store, f"cold-{n}", 40, f"old-{n} " + "word " * 39) for n in range(2)]
        warm = ingest(store, "warm", 29)
        remote, docs = Notebook(), Docs()
        engine = SyncEngine(
            store, SegmentBuilder(store), remote, tmp_path / "snapshots", docs_client=docs
        )
        assert (await engine.run_once())["blocked"] == []
        markdown = [source["text"] for source in remote.sources.values() if "text" in source]
        assert len(markdown) == 2
        assert all(len(source.split()) <= 100 for source in markdown)
        assert all(chat in "".join(markdown) for chat in old)
        assert len(docs.documents) == 1
        assert warm in next(iter(docs.documents.values()))
        assert store.status()["ready_conversations"] == 3


async def test_automatic_archival_can_run_while_an_unrelated_docs_job_is_delayed(tmp_path):
    class OneUnavailableDoc(Docs):
        fail = False

        async def write(self, doc, *args, **kwargs):
            if self.fail and doc == "document-3":
                raise DocsWebError("docs_web_not_sent")
            return await super().write(doc, *args, **kwargs)

    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "synthetic")
        enable_web_docs(store, 1)
        builder = SegmentBuilder(store)
        db = store.connection
        for index in range(3):
            chat = ingest(store, f"old-{index}", 35)
            with db:
                segment = builder._new("sealed")
                db.execute(
                    "INSERT INTO segment_messages SELECT ?,id,0 FROM messages WHERE conversation_id=?",
                    (segment, chat),
                )
                builder._refresh(segment)
        notebook, docs = Notebook(), OneUnavailableDoc()
        engine = SyncEngine(store, builder, notebook, tmp_path / "snapshots", docs_client=docs)
        assert (await engine.run_once())["completed"] == 3
        store.set_setting(cold_archive.ENABLED, True)
        ingest(store, "recent", 5)
        docs.fail = True
        with archive_lock(store.path):
            result = await engine._run_once_locked(automatic=True)
        assert [b["reason"] for b in result["blocked"]] == ["docs_web_not_sent"]
        assert list(notebook.sources.values())[0]["title"].endswith(".md")
        assert len(notebook.sources) == 1
        assert store.get_setting(cold_archive.PLAN) is None
        assert store.auto_sync_due() > time.time()
