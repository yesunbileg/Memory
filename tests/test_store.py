import copy
import sqlite3

import pytest

from ai_memory.store import Store


def conversation(**overrides):
    value = {
        "platform": "codex", "account": "local", "external_id": "session-1",
        "title": "Монгол demo", "project": "Memory", "models": ["test-model"],
        "created_at": "2026-11-10T15:00:00+08:00", "updated_at": None,
        "source_url": None,
        "messages": [{"native_id": "m1", "sequence": 1, "role": "user", "kind": "text",
                      "text": "  Эх текст\n\n```python\nx = 1\n```\n ",
                      "timestamp": "2026-11-10T15:00:00+08:00"}],
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
            c["messages"][0]["text"], "Continuation"
        ]
        assert store.status()["messages"] == 2


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
            store.connection.execute("INSERT INTO source_map VALUES(?,?,?)", (s["id"], "source", s["revision"]))
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
            store.connection.execute("INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id) VALUES(?,?,?,'processing',?)", (s["id"], s["revision"], "pending.md", "source-in-A"))
        with pytest.raises(ValueError, match="notebook"):
            store.set_setting("notebook_id", "notebook-B")
        assert store.get_setting("notebook_id") == "notebook-A"
