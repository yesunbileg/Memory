import pytest
from test_store import conversation

from ai_memory.segments import CapacityError, SegmentBuilder
from ai_memory.store import Store


def test_only_approved_messages_and_exact_markdown_are_packaged(tmp_path):
    with Store(tmp_path / "db") as store:
        key = store.ingest(conversation())
        builder = SegmentBuilder(store)
        assert builder.build() == []
        store.approve(key)
        segments = builder.build()
        assert len(segments) == 1
        text = builder.render(segments[0]["id"])
        assert conversation()["messages"][0]["text"] in text
        assert "timezone: Asia/Ulaanbaatar" in text
        assert "models: [\"test-model\"]" in text
        assert f"### {key} · 0001 · User ·" in text
        assert 'external_id: "session-1"' in text
        assert builder.build() == []


def test_rollover_continuation_and_edit_sealed_segment(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "first message"
        store.ingest(c, consent=True)
        builder = SegmentBuilder(store, target_words=3)
        builder.build()
        second = dict(c["messages"][0], native_id="m2", sequence=2, text="second message")
        store.ingest(conversation(messages=[second]))
        builder.build()
        rows = store.connection.execute("SELECT id,state,revision FROM segments ORDER BY id").fetchall()
        assert [r["state"] for r in rows] == ["sealed", "open"]
        assert "0001" in builder.render(rows[0]["id"])
        assert "0002" not in builder.render(rows[0]["id"])
        assert "0002" in builder.render(rows[1]["id"])
        c["messages"][0]["text"] = "edited first"
        store.ingest(c)
        changed = builder.build()
        assert rows[0]["id"] in [s["id"] for s in changed]
        assert "edited first" in builder.render(rows[0]["id"])
        assert store.connection.execute("SELECT COUNT(*) FROM segment_messages").fetchone()[0] == 2


def test_overflow_merge_retains_every_message_and_parent_sources(tmp_path):
    with Store(tmp_path / "db") as store:
        builder = SegmentBuilder(store, target_words=2, max_words=20, max_active=3)
        for n in range(3):
            c = conversation(external_id=f"c{n}")
            c["messages"][0]["text"] = f"message {n}"
            store.ingest(c, consent=True)
        builder.build()
        merge_id = builder.merge_oldest()
        rendered = builder.render(merge_id)
        assert "message 0" in rendered and "message 1" in rendered
        assert store.status()["messages"] == 3
        assert store.connection.execute("SELECT COUNT(*) FROM segment_parents WHERE segment_id=?", (merge_id,)).fetchone()[0] >= 2
        assert store.connection.execute("SELECT COUNT(*) FROM segments WHERE state != 'retired'").fetchone()[0] == 2


def test_oversized_message_never_truncated_and_exhaustion_never_summarizes(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "word " * 21
        key = store.ingest(c, consent=True)
        builder = SegmentBuilder(store, target_words=10, max_words=20)
        with pytest.raises(CapacityError):
            builder.build()
        assert store.get_conversation(key)["messages"][0]["text"] == "word " * 21
        assert store.connection.execute("SELECT COUNT(*) FROM segment_messages").fetchone()[0] == 0


def test_missing_timestamp_stays_unknown(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation(created_at=None, models=[], title="---\nmisleading: YAML")
        c["messages"][0]["timestamp"] = None
        store.ingest(c, consent=True)
        builder = SegmentBuilder(store)
        s = builder.build()[0]
        rendered = builder.render(s["id"])
        assert " · Unknown" in rendered
        assert "created_at: null" in rendered
        assert 'title: "---\\nmisleading: YAML"' in rendered


def test_continuation_does_not_rewrite_sealed_metadata(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "old message"
        store.ingest(c, consent=True)
        b = SegmentBuilder(store, target_words=2)
        first = b.build()[0]
        store.ingest(conversation(external_id="other", messages=[dict(c["messages"][0], text="other message")]), consent=True)
        b.build()
        previous = b.render(first["id"])
        store.ingest(conversation(models=["new-model"], updated_at="2026-11-11T01:00:00Z", messages=[dict(c["messages"][0], native_id="m2", sequence=2, text="continuation")]))
        changed = b.build()
        assert first["id"] not in [s["id"] for s in changed]
        assert b.render(first["id"]) == previous


def test_merges_find_oldest_feasible_adjacent_window_and_preserve_chronology(tmp_path):
    with Store(tmp_path / "db") as store:
        b = SegmentBuilder(store, target_words=2, max_words=6)
        for n in range(7):
            c = conversation(external_id=f"c{n}")
            c["messages"][0]["text"] = f"message {n}"
            store.ingest(c, consent=True)
        b.build()
        first = b.merge_oldest()
        assert [m[0] for m in store.connection.execute("SELECT m.text FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=? ORDER BY sm.position", (first,))] == ["message 0", "message 1", "message 2"]
        # The first source is now at its ceiling; the next adjacent window still fits.
        second = b.merge_oldest()
        assert [m[0] for m in store.connection.execute("SELECT m.text FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=? ORDER BY sm.position", (second,))] == ["message 3", "message 4", "message 5"]


def test_remerged_segments_keep_original_arrival_order(tmp_path):
    with Store(tmp_path / "db") as store:
        b = SegmentBuilder(store, target_words=2, max_words=100)
        for n in range(4):
            c = conversation(external_id=f"c{n}")
            c["messages"][0]["text"] = f"message {n}"
            store.ingest(c, consent=True)
        b.build()
        b.merge_oldest()
        # Seal the open source by appending the next one.
        c = conversation(external_id="c4")
        c["messages"][0]["text"] = "message 4"
        store.ingest(c, consent=True)
        b.build()
        merged = b.merge_oldest()
        values = [m[0] for m in store.connection.execute("SELECT m.text FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=? ORDER BY sm.position", (merged,))]
        assert values == ["message 0", "message 1", "message 2", "message 3"]


def test_full_oldest_segment_does_not_block_feasible_later_merge(tmp_path):
    with Store(tmp_path / "db") as store:
        builder = SegmentBuilder(store, target_words=2, max_words=6)
        for n, text in enumerate(["one two three four five six", "later first", "later second", "open message"]):
            c = conversation(external_id=str(n))
            c["messages"][0]["text"] = text
            store.ingest(c, consent=True)
        builder.build()
        merged = builder.merge_oldest()
        text = builder.render(merged)
        assert "later first" in text and "later second" in text
        assert "one two three four five six" not in text
