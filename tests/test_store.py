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
        assert archived["consent"] is False
        assert archived["revision"] == 1
        changed = copy.deepcopy(c)
        changed["messages"][0]["text"] += "edited"
        assert store.ingest(changed) == key
        assert store.get_conversation(key)["revision"] == 2
        store.ingest(conversation(account="other"))
        assert store.status()["conversations"] == 2
        assert store.status()["messages"] == 2


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
        s = SegmentBuilder(store).build()[0]
        with store.connection:
            store.connection.execute(
                "INSERT INTO source_map VALUES(?,?,?)", (s["id"], "source", s["revision"])
            )
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
