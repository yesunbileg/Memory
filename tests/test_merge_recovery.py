import pytest
from test_store import conversation
from test_sync import Remote

from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine


@pytest.mark.asyncio
async def test_real_source_budget_recovers_with_a_pair_when_three_render_too_large(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        builder = SegmentBuilder(store)
        for group in range(3):
            messages = [
                {
                    "native_id": f"m-{i}",
                    "sequence": i + 1,
                    "role": "user",
                    "timestamp": None,
                    "text": f"conversation {group} message {i} " + "word " * 16,
                }
                for i in range(7500)
            ]
            store.ingest(
                conversation(external_id=f"large-{group}", messages=messages), consent=True
            )
        builder.build()
        with store.connection:
            store.connection.execute("UPDATE segments SET state='sealed'")
        for n in range(46):
            key = store.ingest(
                conversation(
                    external_id=f"filler-{n}",
                    messages=[
                        {
                            "native_id": "one",
                            "sequence": 1,
                            "role": "user",
                            "timestamp": None,
                            "text": f"filler {n}",
                        }
                    ],
                ),
                consent=True,
            )
            with store.connection:
                sid = builder._new("sealed")
                mid = store.connection.execute(
                    "SELECT id FROM messages WHERE conversation_id=?", (key,)
                ).fetchone()[0]
                store.connection.execute("INSERT INTO segment_messages VALUES(?,?,0)", (sid, mid))
                builder._refresh(sid)
        remote = Remote()
        with store.connection:
            for row in store.connection.execute("SELECT id,revision FROM segments").fetchall():
                source_id = f"old-{row['id']}"
                store.connection.execute(
                    "INSERT INTO source_map VALUES(?,?,?)", (row["id"], source_id, row["revision"])
                )
                remote.sources[source_id] = {
                    "title": source_id,
                    "ready": True,
                    "text": builder.render(row["id"]),
                }
        store.ingest(
            conversation(
                external_id="after-capacity",
                messages=[
                    {
                        "native_id": "later",
                        "sequence": 1,
                        "role": "user",
                        "timestamp": None,
                        "text": "New visible continuation",
                    }
                ],
            ),
            consent=True,
        )
        engine = SyncEngine(store, builder, remote, tmp_path / "snapshots")
        result = await engine.run_once()
        assert result["blocked"] == []
        assert result["status"]["sync_status"] == "up_to_date"
        assert result["status"]["unassigned_messages"] == 0
        assert result["status"]["messages"] == 22_547
        assert len(remote.sources) == 49
        assert remote.peak_sources == 50
        assert len([op for op in remote.operations if op[0] == "upload"]) == 2
        first_ready = next(i for i, op in enumerate(remote.operations) if op[0] == "ready")
        deletes = [i for i, op in enumerate(remote.operations) if op[0] == "delete"]
        assert len(deletes) == 2 and first_ready < min(deletes)
        retired = [
            r[0]
            for r in store.connection.execute(
                "SELECT id FROM segments WHERE state='retired' ORDER BY id"
            )
        ]
        assert retired == ["SEG-001", "SEG-002"]
        assert store.connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 51
        source_lines = {
            line for source in remote.sources.values() for line in source["text"].splitlines()
        }
        originals = {row[0] for row in store.connection.execute("SELECT text FROM messages")}
        assert originals <= source_lines
        assert all(len(source["text"].split()) <= 500_000 for source in remote.sources.values())
        operations = list(remote.operations)
        assert (await engine.run_once())["completed"] == 0
        assert remote.operations == operations


def test_rendered_size_failure_does_not_hide_a_feasible_later_window(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        builder = SegmentBuilder(store, target_words=250_000, max_words=500_000)
        for n, text in enumerate(["first " * 250_000, "second " * 250_000, "tail"]):
            c = conversation(external_id=f"window-{n}")
            c["messages"][0]["text"] = text
            store.ingest(c, consent=True)
        builder.build()
        with store.connection:
            store.connection.execute("UPDATE segments SET state='sealed'")
        merged = builder.merge_oldest()
        parents = [
            r[0]
            for r in store.connection.execute(
                "SELECT parent_id FROM segment_parents WHERE segment_id=? ORDER BY parent_id",
                (merged,),
            )
        ]
        assert parents == ["SEG-002", "SEG-003"]
        assert (
            store.connection.execute("SELECT state FROM segments WHERE id='SEG-001'").fetchone()[0]
            == "sealed"
        )
        assert store.connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 4
        assert len(builder.render(merged).split()) < 500_000
