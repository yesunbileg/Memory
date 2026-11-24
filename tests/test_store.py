import copy
import sqlite3

import pytest

from ai_memory.store import Store


def conversation(**overrides):
    value = {
        "platform": "codex",
        "account": "local",
        "external_id": "session-1",
        "title": "Монгол demo",
        "project": "Memory",
        "models": ["test-model"],
        "created_at": "2026-11-10T15:00:00+08:00",
        "updated_at": None,
        "source_url": None,
        "messages": [
            {
                "native_id": "m1",
                "sequence": 1,
                "role": "user",
                "kind": "text",
                "text": "  Эх текст\n\n```python\nx = 1\n```\n ",
                "timestamp": "2026-11-10T15:00:00+08:00",
            }
        ],
    }
    value.update(overrides)
    return value


def test_exact_text_dedup_revision_and_account_isolation(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        c = conversation()
        key = store.ingest(c)
        assert store.ingest(c) == key
        assert store.status()["messages"] == 1
        archived = store.get_conversation(key)
        assert archived["messages"][0]["text"] == c["messages"][0]["text"]
        assert archived["consent"] is True
        assert archived["revision"] == 1
        changed = copy.deepcopy(c)
        changed["messages"][0]["text"] += "edited"
        assert store.ingest(changed) == key
        assert store.get_conversation(key)["revision"] == 2
        store.ingest(conversation(account="other"))
        assert store.status()["conversations"] == 2
        assert store.status()["messages"] == 2


def test_default_publication_migrates_staged_chats_once_and_keeps_revocations(tmp_path):
    from ai_memory.segments import SegmentBuilder

    path = tmp_path / "archive.sqlite"
    with Store(path) as store:
        held = store.ingest(conversation(external_id="revoked"))
        SegmentBuilder(store).build()
        store.revoke_many([held])
        staged = store.ingest(conversation(external_id="legacy-staged"), consent=False)
        with store.connection:
            store.connection.execute("DELETE FROM settings WHERE key='default_publication_v3'")

    with Store(path, existing=True) as store:
        assert store.get_conversation(staged)["consent"] is True
        assert store.get_conversation(held)["consent"] is False
        store.ingest(conversation(external_id="revoked", title="Changed after revocation"))
        assert store.get_conversation(held)["consent"] is False
        explicit = store.ingest(conversation(external_id="new-paused"), consent=False)

    with Store(path, existing=True) as store:
        assert store.get_conversation(explicit)["consent"] is False


def test_partial_incremental_snapshot_never_deletes_archive(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        c = conversation()
        key = store.ingest(c)
        second = dict(c["messages"][0], native_id="m2", sequence=2, text="Continuation")
        store.ingest(conversation(messages=[second]))
        assert [m["text"] for m in store.get_conversation(key)["messages"]] == [
            c["messages"][0]["text"],
            "Continuation",
        ]
        assert store.status()["messages"] == 2


@pytest.mark.parametrize("order", [("new", "old"), ("old", "new")])
def test_later_snapshot_wins_independent_of_import_order_and_keeps_partial_history(tmp_path, order):
    with Store(tmp_path / "archive.sqlite") as store:
        base = conversation()
        second = dict(base["messages"][0], native_id="m2", sequence=2, text="history")
        newer = conversation(
            title="new title",
            updated_at="2026-11-10T16:00:00Z",
            messages=[
                dict(base["messages"][0], text="newer edit", timestamp="2026-11-10T15:00:00Z")
            ],
        )
        older = conversation(
            title="stale title",
            updated_at="2026-11-10T15:00:00Z",
            messages=[
                dict(base["messages"][0], text="stale edit", timestamp="2026-11-10T15:00:00Z"),
                second,
            ],
        )
        versions = {"new": newer, "old": older}
        key = store.ingest(versions[order[0]])
        store.ingest(versions[order[1]])
        archived = store.get_conversation(key)
        assert len(archived["messages"]) == 2
        assert archived["messages"][0]["text"] == "newer edit"
        assert archived["messages"][0]["timestamp"] == "2026-11-10T15:00:00Z"
        assert archived["updated_at"] == "2026-11-10T16:00:00Z"
        assert archived["title"] == "new title"


def test_open_current_schema_does_not_attempt_ddl(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite"
    with Store(path):
        pass
    statements = []
    original_connect = sqlite3.connect

    def deny_ddl(action, arg1, arg2, database, trigger):
        if action in {
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_ALTER_TABLE,
        }:
            statements.append((action, arg1, arg2))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect_with_ddl_denied(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_authorizer(deny_ddl)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_with_ddl_denied)
    with Store(path):
        pass
    assert statements == []


def test_wal_reader_can_read_during_uncommitted_writer_transaction(tmp_path):
    path = tmp_path / "archive.sqlite"
    with Store(path) as setup:
        setup.ingest(conversation())
    with Store(path) as writer:
        writer.connection.execute("BEGIN IMMEDIATE")
        writer.connection.execute(
            "UPDATE conversations SET title='uncommitted' WHERE external_id='session-1'"
        )
        with Store(path) as reader:
            assert (
                reader.connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
            )
            assert (
                reader.connection.execute("SELECT title FROM conversations").fetchone()[0]
                == "Монгол demo"
            )
        writer.connection.rollback()


def test_invalid_event_or_conflicting_sequence_rolls_back(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        c = conversation()
        store.ingest(c)
        for bad in ({"role": "tool"}, {"kind": "hidden_reasoning"}, {"sequence": 0}):
            invalid = conversation(external_id="invalid")
            invalid["messages"][0].update(bad)
            with pytest.raises(ValueError):
                store.ingest(invalid)
        conflict = dict(c["messages"][0], native_id="different")
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.ingest(conversation(messages=[conflict]))
        assert store.status()["conversations"] == 1
        assert store.status()["messages"] == 1


def test_models_are_merged_without_inference_and_consent_persists(tmp_path):
    with Store(tmp_path / "archive.sqlite") as store:
        key = store.ingest(conversation(), consent=True)
        store.ingest(conversation(models=["second-model"]))
        c = store.get_conversation(key)
        assert c["models"] == ["test-model", "second-model"]
        assert c["consent"] is True
        store.set_setting("notebook_id", "demo-notebook")
        assert store.get_setting("notebook_id") == "demo-notebook"
    with Store(tmp_path / "archive.sqlite") as store:
        assert store.get_setting("notebook_id") == "demo-notebook"


def test_synthetic_secrets_preserved_locally_and_flagged_for_cloud(tmp_path):
    from ai_memory.privacy import find_sensitive

    text = "my api_key = sk-" + "demo1234567890" * 3
    with Store(tmp_path / "archive.sqlite") as store:
        key = store.ingest(conversation(messages=[dict(conversation()["messages"][0], text=text)]))
        assert store.get_conversation(key)["messages"][0]["text"] == text
        assert find_sensitive(text)
        assert not find_sensitive("Our decision: use SQLite, no local LLM.")


def test_edit_marks_existing_source_stale_before_builder_runs(tmp_path):
    from ai_memory.segments import SegmentBuilder

    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        builder = SegmentBuilder(store)
        s = builder.build()[0]
        with store.connection:
            store.connection.execute(
                "INSERT INTO source_map VALUES(?,?,?)", (s["id"], "source", s["revision"])
            )
            store.save_outbound(s["id"], s["revision"], builder.render_outbound(s["id"]))
            store.attest_source("source", s["id"], s["revision"])
        assert store.status()["sync_status"] == "up_to_date"
        c["messages"][0]["text"] = "edited before next build"
        store.ingest(c)
        assert store.status()["sync_status"] == "pending"


def test_notebook_binding_cannot_change_while_first_upload_is_unresolved(tmp_path):
    from ai_memory.segments import SegmentBuilder

    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "notebook-A")
        store.ingest(conversation(), consent=True)
        s = SegmentBuilder(store).build()[0]
        with store.connection:
            store.connection.execute(
                "INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id) VALUES(?,?,?,'processing',?)",
                (s["id"], s["revision"], "pending.md", "source-in-A"),
            )
        with pytest.raises(ValueError, match="notebook"):
            store.set_setting("notebook_id", "notebook-B")
        assert store.get_setting("notebook_id") == "notebook-A"


def snapshot(*ids, **overrides):
    return conversation(
        messages=[
            dict(conversation()["messages"][0], native_id=mid, sequence=i, text=mid)
            for i, mid in enumerate(ids, 1)
        ],
        **overrides,
    )


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [
        (("a", "b", "c", "d"), ["a", "b", "c", "d"]),
        (("a", "b", "d"), ["a", "b", "c", "d"]),
        (("b", "between", "d"), ["b", "c", "between", "d"]),
        (("unanchored",), ["b", "c", "d", "unanchored"]),
        (("d", "ambiguous", "b"), ["b", "c", "d", "ambiguous"]),
    ],
)
def test_reconciled_snapshot_anchors_new_turns_without_losing_absent_history(
    tmp_path, incoming, expected
):
    with Store(tmp_path / "db") as store:
        key = store.ingest(snapshot("b", "c", "d"))
        original_ids = {m["native_id"]: m["id"] for m in store.get_conversation(key)["messages"]}
        packet = snapshot(*incoming)
        untouched = copy.deepcopy(packet)
        store.ingest(packet, reconcile_order=True)
        saved = store.get_conversation(key)
        assert [m["native_id"] for m in saved["messages"]] == expected
        assert [m["sequence"] for m in saved["messages"]] == list(range(1, len(expected) + 1))
        assert all(
            m["id"] == original_ids[m["native_id"]]
            for m in saved["messages"]
            if m["native_id"] in original_ids
        )
        assert packet == untouched
        store.ingest(packet, reconcile_order=True)
        assert store.get_conversation(key)["revision"] == saved["revision"]


def test_stale_overlapping_snapshot_reorders_but_does_not_replace_newer_text(tmp_path):
    with Store(tmp_path / "db") as store:
        key = store.ingest(snapshot("b", updated_at="2026-11-12T00:00:00Z"), consent=True)
        from ai_memory.segments import SegmentBuilder

        segment = SegmentBuilder(store).build()[0]
        with store.connection:
            store.connection.execute(
                "UPDATE segments SET state='sealed' WHERE id=?", (segment["id"],)
            )
            store.connection.execute(
                "INSERT INTO source_map VALUES(?,?,?)",
                (segment["id"], "synthetic-source", segment["revision"]),
            )
        older = snapshot("a", "b", updated_at="2026-11-11T00:00:00Z")
        older["messages"][1]["text"] = "stale text"
        store.ingest(older, reconcile_order=True)
        saved = store.get_conversation(key)
        assert [(m["sequence"], m["text"]) for m in saved["messages"]] == [(1, "a"), (2, "b")]
        assert (
            store.connection.execute(
                "SELECT content_hash FROM segments WHERE id=?", (segment["id"],)
            ).fetchone()[0]
            == ""
        )
        assert store.status()["pending_segments"] == 1


def test_concurrent_ingest_serializes_metadata_reads_with_writes(tmp_path):
    path = tmp_path / "db"
    with Store(path) as first, Store(path) as second:
        key = first.ingest(conversation(updated_at="2026-11-10T00:00:00Z"))
        second.connection.execute("PRAGMA busy_timeout=0")
        newer = conversation(
            title="latest title", models=["latest-model"], updated_at="2026-11-12T00:00:00Z"
        )
        newer["messages"][0]["text"] = "latest text"
        attempted = blocked = False

        def interleave(cursor, values):
            nonlocal attempted, blocked
            row = sqlite3.Row(cursor, values)
            if not attempted and "external_id" in row.keys():
                attempted = True
                try:
                    second.ingest(newer)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc):
                        raise
                    blocked = True
            return row

        first.connection.row_factory = interleave
        first.ingest(
            conversation(
                title="older title", models=["older-model"], updated_at="2026-11-11T00:00:00Z"
            )
        )
        assert attempted
        if blocked:
            second.ingest(newer)
        saved = first.get_conversation(key)
        assert saved["title"] == "latest title"
        assert saved["updated_at"] == "2026-11-12T00:00:00Z"
        assert "latest-model" in saved["models"]
        assert saved["messages"][0]["text"] == "latest text"


def test_notebook_change_cannot_pass_source_check_during_archive_operation(tmp_path):
    import fcntl

    path = tmp_path / "db"
    with Store(path) as store:
        store.set_setting("notebook_id", "notebook-A")
        with path.with_suffix(".sync.lock").open("w") as active:
            fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="active"):
                store.set_setting("notebook_id", "notebook-B")
            assert store.get_setting("notebook_id") == "notebook-A"
            store.set_setting("unrelated", "allowed")
        store.set_setting("notebook_id", "notebook-B")
        assert store.get_setting("notebook_id") == "notebook-B"


def test_native_alias_resolves_existing_message_and_keeps_segment_identity(tmp_path):
    from ai_memory.segments import SegmentBuilder

    with Store(tmp_path / "db") as store:
        key = store.ingest(snapshot("event"), consent=True)
        original = store.get_conversation(key)["messages"][0]
        segment = SegmentBuilder(store).build()[0]
        response = snapshot("response")
        response["messages"][0]["native_aliases"] = ["event", "codex-record:1"]
        response["messages"][0]["text"] = "canonical text"
        store.ingest(response)
        saved = store.get_conversation(key)
        assert len(saved["messages"]) == 1
        assert saved["messages"][0]["id"] == original["id"]
        assert saved["messages"][0]["native_id"] == "event"
        assert saved["messages"][0]["text"] == "canonical text"
        assert (
            store.connection.execute(
                "SELECT message_id FROM segment_messages WHERE segment_id=?", (segment["id"],)
            ).fetchone()[0]
            == original["id"]
        )
    with Store(tmp_path / "db") as store:
        # Replaying only the response ID still resolves the archived event row.
        replay = snapshot("response")
        replay["messages"][0]["text"] = "canonical text"
        store.ingest(replay)
        assert store.get_conversation(key)["revision"] == saved["revision"]


def test_conflicting_native_aliases_roll_back_without_merging_real_messages(tmp_path):
    with Store(tmp_path / "db") as store:
        key = store.ingest(snapshot("first", "second"))
        before = store.get_conversation(key)
        ambiguous = snapshot("canonical")
        ambiguous["messages"][0]["native_aliases"] = ["first", "second"]
        with pytest.raises(ValueError, match="alias"):
            store.ingest(ambiguous)
        assert store.get_conversation(key) == before


def test_existing_duplicate_mirror_keeps_exact_primary_and_import_remains_usable(tmp_path):
    with Store(tmp_path / "db") as store:
        key = store.ingest(snapshot("event", "response"))
        before = store.get_conversation(key)["messages"]
        replay = snapshot("response")
        replay["messages"][0].update(
            sequence=2, text="updated canonical", native_aliases=["event", "record-response"]
        )
        store.ingest(replay)
        saved = store.get_conversation(key)["messages"]
        assert [m["id"] for m in saved] == [m["id"] for m in before]
        assert [m["text"] for m in saved] == ["event", "updated canonical"]
        store.ingest(snapshot("event"))
        assert [m["text"] for m in store.get_conversation(key)["messages"]] == [
            "event",
            "updated canonical",
        ]


def test_non_codex_messages_do_not_register_unproven_aliases(tmp_path):
    with Store(tmp_path / "db") as store:
        key = store.ingest(snapshot("original", platform="chatgpt"))
        other = snapshot("separate", platform="chatgpt")
        other["messages"][0].update(sequence=2, native_aliases=["original"])
        store.ingest(other)
        assert [m["native_id"] for m in store.get_conversation(key)["messages"]] == [
            "original",
            "separate",
        ]
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE key LIKE 'message-alias:%'"
            ).fetchone()[0]
            == 0
        )


def test_alias_cannot_claim_another_new_message_primary_in_one_snapshot(tmp_path):
    with Store(tmp_path / "db") as store:
        packet = snapshot("first", "second")
        packet["messages"][0]["native_aliases"] = ["second"]
        with pytest.raises(ValueError, match="alias"):
            store.ingest(packet)
        assert store.status()["messages"] == 0
        assert store.status()["conversations"] == 0


def test_status_exposes_only_safe_sync_preflight_categories(tmp_path):
    with Store(tmp_path / "db") as store:
        store.set_setting(
            "sync_preflight_errors",
            [
                {"reason": "auth_required"},
                {"reason": "synthetic private detail", "text": "must not be exposed"},
            ],
        )
        assert store.status()["sync_preflight_errors"] == [
            {"reason": "auth_required"},
            {"reason": "operation_failed"},
        ]
        store.set_setting("sync_preflight_errors", [])
        assert store.status()["sync_preflight_errors"] == []
