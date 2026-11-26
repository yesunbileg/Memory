import pytest
from test_cold_archive import ingest
from test_web_docs_sync import Docs, Notebook

from ai_memory import cold_archive
from ai_memory.archive_lock import archive_lock
from ai_memory.publication import allowed_sources
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine
from ai_memory.web_docs_publication import enable_web_docs


@pytest.mark.parametrize("limiting_capacity", ["notebook", "segments"])
async def test_compaction_uses_cold_subset_when_mixed_group_cannot_fit(
    tmp_path, monkeypatch, limiting_capacity
):
    if limiting_capacity == "notebook":
        monkeypatch.setattr(cold_archive, "SOURCE_LIMIT", 4)
    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "synthetic")
        enable_web_docs(store, 1)
        builder = SegmentBuilder(store, max_active=4 if limiting_capacity == "segments" else 49)
        old_chats = [ingest(store, f"old-{n}", 35) for n in range(3)]
        active_chat = ingest(store, "active", 2)
        db = store.connection
        with db:
            for group in ((old_chats[0],), (old_chats[1],), (old_chats[2], active_chat)):
                segment = builder._new("sealed")
                for position, chat in enumerate(group):
                    db.execute(
                        """INSERT INTO segment_messages SELECT ?,id,? FROM messages
                        WHERE conversation_id=?""",
                        (segment, position, chat),
                    )
                builder._refresh(segment)
        remote = Notebook()
        engine = SyncEngine(store, builder, remote, tmp_path / "snapshots", docs_client=Docs())
        assert (await engine.run_once())["completed"] == 3
        original = allowed_sources(store)
        mixed_source = next(
            sid for sid, source in original.items() if source["segment_id"] == "SEG-003"
        )

        inventory = await remote.list_sources()
        with archive_lock(store.path):
            plan = cold_archive.stage_replacement(builder, inventory)
            assert plan == {"parents": ["SEG-001", "SEG-002"], "children": ["SEG-004"]}
            assert set(allowed_sources(store)) == set(original)
            assert [
                tuple(row) for row in db.execute("SELECT id,state FROM segments ORDER BY id")
            ] == [
                ("SEG-001", "sealed"),
                ("SEG-002", "sealed"),
                ("SEG-003", "sealed"),
                ("SEG-004", "sealed"),
            ]
            assert (await engine._run_once_locked(archive_only=True))["blocked"] == []

        assert len(remote.sources) == 2
        assert mixed_source in remote.sources
        assert store.get_setting(cold_archive.PLAN) is None
        text = next(source["text"] for source in remote.sources.values() if "text" in source)
        assert all(chat in text for chat in old_chats[:2])
        assert old_chats[2] not in text and active_chat not in text
