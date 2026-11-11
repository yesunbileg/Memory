import pytest
from test_store import conversation

from ai_memory.segments import CapacityError, SegmentBuilder
from ai_memory.store import Store, digest


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
        assert 'models: ["test-model"]' in text
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
        rows = store.connection.execute(
            "SELECT id,state,revision FROM segments ORDER BY id"
        ).fetchall()
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
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM segment_parents WHERE segment_id=?", (merge_id,)
            ).fetchone()[0]
            >= 2
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM segments WHERE state != 'retired'"
            ).fetchone()[0]
            == 2
        )


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
        store.ingest(
            conversation(
                external_id="other", messages=[dict(c["messages"][0], text="other message")]
            ),
            consent=True,
        )
        b.build()
        previous = b.render(first["id"])
        store.ingest(
            conversation(
                models=["new-model"],
                updated_at="2026-11-11T01:00:00Z",
                messages=[dict(c["messages"][0], native_id="m2", sequence=2, text="continuation")],
            )
        )
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
        assert [
            m[0]
            for m in store.connection.execute(
                "SELECT m.text FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=? ORDER BY sm.position",
                (first,),
            )
        ] == ["message 0", "message 1", "message 2"]
        # The first source is now at its ceiling; the next adjacent window still fits.
        second = b.merge_oldest()
        assert [
            m[0]
            for m in store.connection.execute(
                "SELECT m.text FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=? ORDER BY sm.position",
                (second,),
            )
        ] == ["message 3", "message 4", "message 5"]


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
        values = [
            m[0]
            for m in store.connection.execute(
                "SELECT m.text FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=? ORDER BY sm.position",
                (merged,),
            )
        ]
        assert values == ["message 0", "message 1", "message 2", "message 3"]


def test_full_oldest_segment_does_not_block_feasible_later_merge(tmp_path):
    with Store(tmp_path / "db") as store:
        builder = SegmentBuilder(store, target_words=2, max_words=6)
        for n, text in enumerate(
            ["one two three four five six", "later first", "later second", "open message"]
        ):
            c = conversation(external_id=str(n))
            c["messages"][0]["text"] = text
            store.ingest(c, consent=True)
        builder.build()
        merged = builder.merge_oldest()
        text = builder.render(merged)
        assert "later first" in text and "later second" in text
        assert "one two three four five six" not in text


def test_merge_rejects_rendered_snapshot_over_500000_before_retiring_parents(tmp_path):
    with Store(tmp_path / "db") as store:
        builder = SegmentBuilder(store, target_words=250_000, max_words=500_000)
        for n in range(3):
            c = conversation(external_id=f"large-{n}")
            c["messages"][0]["text"] = "word " * 250_000 if n < 2 else "tail"
            store.ingest(c, consent=True)
        builder.build()
        parent_rows = store.connection.execute(
            "SELECT * FROM segments WHERE state='sealed' ORDER BY id"
        ).fetchall()
        parents = [r["id"] for r in parent_rows]
        assert len(parents) == 2
        with store.connection:
            for i, parent in enumerate(parent_rows):
                store.connection.execute(
                    "INSERT INTO source_map VALUES(?,?,?)",
                    (parent["id"], f"remote-{i}", parent["revision"]),
                )
            store.connection.execute(
                """INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id,
                old_source_ids,attempts,next_attempt,error) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    parents[0],
                    parent_rows[0]["revision"],
                    "retry.md",
                    "blocked",
                    "",
                    "[]",
                    3,
                    123.0,
                    "NetworkError",
                ),
            )
        before_maps = [
            tuple(r)
            for r in store.connection.execute("SELECT * FROM source_map ORDER BY segment_id")
        ]
        before_jobs = [tuple(r) for r in store.connection.execute("SELECT * FROM sync_jobs")]
        with pytest.raises(CapacityError):
            builder.merge_oldest()
        states = dict(store.connection.execute("SELECT id,state FROM segments"))
        assert [states[sid] for sid in parents] == ["sealed", "sealed"]
        assert store.connection.execute("SELECT COUNT(*) FROM segment_parents").fetchone()[0] == 0
        assert [
            tuple(r)
            for r in store.connection.execute("SELECT * FROM source_map ORDER BY segment_id")
        ] == before_maps
        assert [
            tuple(r) for r in store.connection.execute("SELECT * FROM sync_jobs")
        ] == before_jobs


def test_appending_many_messages_uses_constant_open_segment_count_queries(tmp_path):
    def sqlite_work(message_count):
        with Store(tmp_path / f"db-{message_count}") as store:
            c = conversation()
            c["messages"][0]["text"] = "seed"
            store.ingest(c, consent=True)
            builder = SegmentBuilder(store, target_words=100_000)
            builder.build()
            additions = [
                dict(c["messages"][0], native_id=f"m{n}", sequence=n + 2, text="next")
                for n in range(message_count)
            ]
            store.ingest(conversation(messages=additions))
            steps = [0]
            store.connection.set_progress_handler(
                lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100
            )
            builder.build()
            store.connection.set_progress_handler(None, 0)
            return steps[0]

    # Doubling the batch should stay close to linear SQLite work. Per-message
    # rescans of the growing open segment make this ratio approach four.
    assert sqlite_work(64) < 3 * sqlite_work(32)


def test_reopened_builder_preserves_multiconversation_order_and_revision(tmp_path):
    with Store(tmp_path / "db") as store:
        first = conversation(external_id="first")
        first["messages"][0]["text"] = "first one"
        second = conversation(external_id="second")
        second["messages"][0]["text"] = "second two"
        store.ingest(first, consent=True)
        store.ingest(second, consent=True)
        builder = SegmentBuilder(store, target_words=5)
        before = builder.build()[0]

        first_next = dict(first["messages"][0], native_id="first-2", sequence=2, text="third words")
        second_next = dict(second["messages"][0], native_id="second-2", sequence=2, text="fourth")
        store.ingest(conversation(external_id="first", messages=[first_next]))
        store.ingest(conversation(external_id="second", messages=[second_next]))
        reopened = SegmentBuilder(store, target_words=5)
        changed = reopened.build()

        assert [row["id"] for row in changed] == ["SEG-001", "SEG-002"]
        assert store.connection.execute(
            "SELECT state,revision FROM segments WHERE id='SEG-001'"
        ).fetchone()[:] == ("sealed", before["revision"] + 1)
        rows = store.connection.execute("""SELECT s.id,m.text,sm.position FROM segment_messages sm
            JOIN segments s ON s.id=sm.segment_id JOIN messages m ON m.id=sm.message_id
            WHERE s.state!='retired' ORDER BY s.id,sm.position""").fetchall()
        assert [(r["id"], r["text"], r["position"]) for r in rows] == [
            ("SEG-001", "first one", 0),
            ("SEG-001", "second two", 1),
            ("SEG-002", "third words", 0),
            ("SEG-002", "fourth", 1),
        ]
        for segment in store.connection.execute(
            "SELECT id,revision,content_hash FROM segments WHERE state!='retired'"
        ):
            rendered = reopened.render(segment["id"])
            assert segment["content_hash"] == digest(rendered)
            assert len(rendered.split()) <= 500_000


def test_active_segment_rejects_rendered_snapshot_over_500000(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "word " * 500_000
        store.ingest(c, consent=True)
        builder = SegmentBuilder(store, target_words=500_000, max_words=500_000)
        with pytest.raises(CapacityError):
            builder.build()
        assert store.connection.execute("SELECT COUNT(*) FROM segment_messages").fetchone()[0] == 0
