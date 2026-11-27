"""Synthetic regression checks for the archive disclosure and recovery boundaries."""

import time

from test_store import conversation
from test_sync import Remote, make_retries_due

from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine


async def test_registered_source_readiness_timeout_polls_only(tmp_path):
    from ai_memory.notebook import NotebookLMError

    class Polling(Remote):
        slow = True

        async def wait_ready(self, source_id):
            if self.slow:
                raise NotebookLMError("Safe test timeout", "timeout")
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "  Монгол 😺\r\n$x\r\n\r код\r "
        store.ingest(c)
        remote = Polling()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        result = await engine.run_once()
        assert result["blocked"][0]["reason"] == "timeout"
        make_retries_due(store)
        remote.slow = False
        assert not (await engine.run_once())["blocked"]
        assert len([op for op in remote.operations if op[0] == "upload"]) == 1
        assert c["messages"][0]["text"] in next(iter(remote.sources.values()))["text"]


async def test_wrong_body_after_lost_response_never_disclosed_or_deleted(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c)
        remote = Remote()
        remote.lose_response = True
        builder = SegmentBuilder(store, target_words=1)
        engine = SyncEngine(store, builder, remote, tmp_path / "md")
        await engine.run_once()
        foreign = next(iter(remote.sources))
        remote.sources[foreign]["text"] = "synthetic foreign data, same filename"
        store.ingest(conversation(external_id="independent"))
        make_retries_due(store)
        await engine.run_once()
        assert foreign in remote.sources
        assert remote.sources[foreign]["text"].startswith("synthetic foreign")
        mapped = [r[0] for r in store.connection.execute("SELECT source_id FROM source_map")]
        assert foreign not in mapped
        assert len(mapped) == 1
        assert len([op for op in remote.operations if op[0] == "upload"]) == 2


def test_unchanged_build_skips_sealed_renders(tmp_path, monkeypatch):
    with Store(tmp_path / "db") as store:
        builder = SegmentBuilder(store, target_words=1)
        for i in range(12):
            store.ingest(conversation(external_id=str(i)))
        builder.build()
        before = {
            r[0]: builder.render(r[0]) for r in store.connection.execute("SELECT id FROM segments")
        }
        original = builder.render
        calls = []

        def counted(segment_id, *a, **kw):
            calls.append(segment_id)
            return original(segment_id, *a, **kw)

        monkeypatch.setattr(builder, "render", counted)
        started = time.monotonic()
        assert not builder.build()
        assert len(calls) <= 1
        assert time.monotonic() - started < 2
        assert before == {k: original(k) for k in before}


async def test_oversized_message_or_title_does_not_block_other_conversation(tmp_path):
    with Store(tmp_path / "db") as store:
        base = conversation()["messages"][0]
        message_id = store.ingest(
            conversation(external_id="long-message", messages=[dict(base, text="бие " * 101)]),
        )
        title_id = store.ingest(conversation(external_id="long-title", title="урт " * 500001))
        good_id = store.ingest(conversation(external_id="simple"))
        remote = Remote()
        result = await SyncEngine(
            store, SegmentBuilder(store, max_words=100, target_words=50), remote, tmp_path / "md"
        ).run_once()
        assert {r["conversation_id"] for r in result["blocked"]} == {message_id, title_id}
        assert len(remote.sources) == 1
        only = next(iter(remote.sources.values()))["text"]
        assert good_id in only and message_id not in only and title_id not in only
        assert store.get_conversation(title_id)["title"] == "урт " * 500001


async def test_reconcile_stale_uncertain_while_new_revision_proceeds(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c)
        remote = Remote()
        remote.lose_response = True
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        unknown = next(iter(remote.sources))
        remote.sources[unknown]["text"] = "Unrelated synthetic source."
        c["messages"][0]["text"] = "Newest safe content"
        store.ingest(c)
        result = await engine.run_once()
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped != unknown and "Newest safe content" in remote.sources[mapped]["text"]
        assert unknown in remote.sources
        assert result["status"]["pending_jobs"] == 1


async def test_crash_after_readiness_resumes_without_duplicate(tmp_path):
    remote = Remote()
    with Store(tmp_path / "db") as store:
        store.ingest(conversation())
        # An aborted mapping transaction simulates a stop after provider readiness.
        store.connection.execute(
            "CREATE TRIGGER interrupt_map BEFORE INSERT ON source_map BEGIN SELECT RAISE(ABORT, 'stop'); END"
        )
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        assert store.connection.execute("SELECT stage FROM sync_jobs").fetchone()[0] == "ready"
        assert store.connection.execute("SELECT COUNT(*) FROM source_map").fetchone()[0] == 0
        with store.connection:
            store.connection.execute("DROP TRIGGER interrupt_map")
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        assert not (await engine.run_once())["blocked"]
        assert len([op for op in remote.operations if op[0] == "upload"]) == 1
        assert store.connection.execute("SELECT stage FROM sync_jobs").fetchone()[0] == "done"


async def test_uncertain_finalize_with_registration_id_polls_only(tmp_path):
    from ai_memory.notebook import NotebookLMError

    class LostFinalize(Remote):
        async def upload(self, path, title):
            sid = await super().upload(path, title)
            error = NotebookLMError("Synthetic lost finalize response", "network_error")
            error.source_id = sid
            raise error

    remote = LostFinalize()
    with Store(tmp_path / "db") as store:
        store.ingest(conversation())
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        make_retries_due(store)
        await engine.run_once()
        assert len([op for op in remote.operations if op[0] == "upload"]) == 1
        assert store.status()["sync_status"] == "up_to_date"


async def test_late_proof_cleans_stale_attempt_after_new_mapping_finished(tmp_path):
    class DelayedIndex(Remote):
        indexed = False

        async def get_source_text(self, source_id):
            return await super().get_source_text(source_id) if self.indexed else ""

    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c)
        remote = DelayedIndex()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        remote.lose_response = True
        await engine.run_once()
        late = next(iter(remote.sources))
        c["messages"][0]["text"] = "newer revision"
        store.ingest(c)
        await engine.run_once()
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped != late and set(remote.sources) == {late, mapped}
        remote.indexed = True
        for _ in range(2):
            make_retries_due(store)
            await engine.run_once()
        assert set(remote.sources) == {mapped}
        assert store.status()["sync_status"] == "up_to_date"
