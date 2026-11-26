import asyncio
import json
from pathlib import Path

import pytest
from notebooklm.exceptions import SourceNotFoundError
from test_cold_archive import ingest
from test_sync import Remote, make_retries_due
from test_web_docs_sync import Docs, Notebook

from ai_memory import cold_archive, web_docs_background
from ai_memory.archive_lock import archive_lock
from ai_memory.notebook import NotebookLMError, _safe_error
from ai_memory.publication import allowed_sources
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine, republish
from ai_memory.web_docs_publication import enable_web_docs


def setup_docs(store, remote, docs, *, target_words=150_000):
    store.set_setting("notebook_id", "synthetic")
    enable_web_docs(store, 1)
    return SyncEngine(
        store,
        SegmentBuilder(store, target_words=target_words),
        remote,
        store.path.parent / "snapshots",
        docs_client=docs,
    )


async def test_cold_cleanup_waits_for_every_child_after_keeper_republish(tmp_path):
    class UnreadyChild(Notebook):
        unavailable = None

        async def wait_ready(self, source_id):
            if source_id == self.unavailable:
                raise NotebookLMError("Synthetic unready source", "not_ready")
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        remote = UnreadyChild()
        engine = setup_docs(store, remote, Docs())
        cold = [ingest(store, f"cold-{n}", 31) for n in range(3)]
        active = ingest(store, "active", 5)
        db = store.connection
        with db:
            for group in (cold[:1], cold[1:2], [cold[2], active]):
                segment = engine.builder._new("sealed")
                for position, chat in enumerate(group):
                    db.execute(
                        "INSERT INTO segment_messages SELECT ?,id,? FROM messages WHERE conversation_id=?",
                        (segment, position, chat),
                    )
                engine.builder._refresh(segment)
        assert not (await engine.run_once())["blocked"]
        parents = set(remote.sources)
        assert len(parents) == 3

        store.set_setting(cold_archive.ENABLED, True)
        remote.fail_delete = True
        with archive_lock(store.path):
            await engine._run_once_locked(archive_only=True)
        plan = store.get_setting(cold_archive.PLAN)
        keeper, sibling = plan["children"]
        mappings = dict(db.execute("SELECT segment_id,source_id FROM source_map"))
        del remote.sources[mappings[keeper]]
        remote.unavailable = mappings[sibling]
        remote.sources[remote.unavailable]["ready"] = False
        remote.fail_delete = False
        await engine.run_once()

        republish(store, keeper)
        make_retries_due(store)
        await engine.run_once()
        assert parents <= set(remote.sources)
        assert store.get_setting(cold_archive.PLAN)

        new_keeper = db.execute(
            "SELECT source_id FROM source_map WHERE segment_id=?", (keeper,)
        ).fetchone()[0]
        remote.sources[new_keeper]["ready"] = False
        remote.unavailable = None
        make_retries_due(store)
        assert not (await engine.run_once())["blocked"]
        assert store.get_setting(cold_archive.PLAN) is None
        assert parents.isdisjoint(remote.sources)
        assert store.status()["ready_conversations"] == 4


async def test_docs_growth_keeps_notebook_replacement_slot(tmp_path):
    with Store(tmp_path / "db") as store:
        remote = Notebook()
        foreign = {f"foreign-{n}": {"title": "Foreign", "ready": True} for n in range(47)}
        remote.sources.update(foreign)
        engine = setup_docs(store, remote, Docs(), target_words=1)
        for name in ("old-one", "old-two"):
            ingest(store, name, 45)
        assert not (await engine.run_once())["blocked"]
        assert len(remote.sources) == 49

        store.set_setting(cold_archive.ENABLED, True)
        ingest(store, "new-one", 1)
        await engine.run_once()
        assert set(foreign) <= set(remote.sources)
        assert len(remote.sources) <= 49
        assert store.status()["ready_conversations"] == 3


async def test_docs_retries_create_after_background_worker_did_not_start(tmp_path, monkeypatch):
    storage = tmp_path / "storage.json"
    storage.write_text("{}")
    worker = web_docs_background.BackgroundDocsTransport(storage)

    class RemoteDocs(Docs):
        async def create(self, title):
            return await worker.create(title)

        async def call(self, action, **payload):
            if action == "find":
                return {"ids": []}
            return await super().call(action, **payload)

    async def cannot_start(*args, **kwargs):
        raise FileNotFoundError("Synthetic missing Brave executable")

    with Store(tmp_path / "db") as store:
        docs = RemoteDocs()
        engine = setup_docs(store, Notebook(), docs)
        ingest(store, "new-doc", 1)
        with monkeypatch.context() as patch:
            patch.setattr(web_docs_background.shutil, "which", lambda _: "/fixture/brave")
            patch.setattr(asyncio, "create_subprocess_exec", cannot_start)
            assert (await engine.run_once())["blocked"][0]["reason"] == "docs_web_unavailable"

        async def start(*arguments):
            payload = Path(arguments[-1]).read_text().splitlines()[0]
            assert (
                json.loads(payload.removeprefix("const input = ").removesuffix(";"))["action"]
                == "create"
            )
            doc = "synthetic-document-1234"
            docs.documents[doc] = ""
            return f'AI_MEMORY_DOCS_RESULT={{"id":"{doc}"}}', 0

        monkeypatch.setattr(worker, "_run_script", start)
        make_retries_due(store)
        assert not (await engine.run_once())["blocked"]
        assert len(docs.documents) == 1
        assert store.status()["ready_conversations"] == 1


@pytest.mark.parametrize("use_docs", [False, True])
async def test_recreates_missing_unpublished_notebook_source(tmp_path, use_docs):
    class Disappears(Notebook if use_docs else Remote):
        missing = None

        async def wait_ready(self, source_id):
            if self.missing is None:
                self.missing = source_id
                self.sources.pop(source_id)
            if source_id == self.missing and source_id not in self.sources:
                raise _safe_error(SourceNotFoundError(source_id))
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        remote = Disappears()
        engine = (
            setup_docs(store, remote, Docs())
            if use_docs
            else SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "snapshots")
        )
        ingest(store, "first-publication", 1)
        assert (await engine.run_once())["blocked"]
        assert not allowed_sources(store)
        make_retries_due(store)
        assert not (await engine.run_once())["blocked"]
        assert len(allowed_sources(store)) == 1
        assert not any(action == "delete" for action, _ in remote.operations)


async def test_unready_secondary_docs_part_is_rechecked(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_memory.web_docs_journal.DOCS_LIMIT", 400)

    class UnreadyPart(Notebook):
        unavailable = None

        async def wait_ready(self, source_id):
            if source_id == self.unavailable:
                raise NotebookLMError("Synthetic unready source", "not_ready")
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        remote = UnreadyPart()
        engine = setup_docs(store, remote, Docs())
        ingest(store, "multipart", 1, "word " * 200)
        assert not (await engine.run_once())["blocked"]
        assert len(remote.sources) > 1
        secondary = next(reversed(remote.sources))
        remote.sources[secondary]["ready"] = False
        remote.unavailable = secondary
        blocked = await engine.run_once()
        assert blocked["blocked"][0]["reason"] == "not_ready"
        assert blocked["status"]["pending_jobs"] == 1
        assert store.auto_sync_due() == blocked["blocked"][0]["next_attempt"]

        remote.unavailable = None
        remote.operations.clear()
        make_retries_due(store)
        assert not (await engine.run_once())["blocked"]
        assert ("ready", secondary) in remote.operations
        assert store.status()["ready_conversations"] == 1
