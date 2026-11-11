import json
import sqlite3
from pathlib import Path

import pytest

from ai_memory.cli import collect_into
from ai_memory.collectors import collect
from ai_memory.store import Store


def session_files(root: Path, layout: str, session_id: str, text: str):
    message_id = f"msg_{session_id[4:]}"
    if layout == "new":
        storage = root / "storage"
        info = storage / f"session/project-demo/{session_id}.json"
        message = storage / f"message/{session_id}/{message_id}.json"
        part = storage / f"part/{message_id}/prt_text.json"
    else:
        storage = root / "project/project-demo/storage/session"
        info = storage / f"info/{session_id}.json"
        message = storage / f"message/{session_id}/{message_id}.json"
        part = storage / f"part/{session_id}/{message_id}/prt_text.json"
    values = [
        {
            "id": session_id,
            "title": "Visible session",
            "directory": "/synthetic/project",
            "projectID": "project-demo",
            "time": {"created": 1000, "updated": 2000},
        },
        {"id": message_id, "sessionID": session_id, "role": "user", "time": {"created": 1000}},
        {
            "id": "prt_text",
            "messageID": message_id,
            "sessionID": session_id,
            "type": "text",
            "text": text,
        },
    ]
    for path, value in zip((info, message, part), values, strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return info, message, part


@pytest.mark.parametrize("layout", ["new", "old"])
def test_explicit_opencode_session_preserves_exact_visible_text(tmp_path, layout):
    text = "  Монгол текст\n```python\nx = 1\n```\n "
    info, _, _ = session_files(tmp_path, layout, "ses_visible", text)
    conversations = collect("opencode", info)
    assert len(conversations) == 1
    assert conversations[0]["external_id"] == "ses_visible"
    assert conversations[0]["messages"][0]["text"] == text


@pytest.mark.parametrize("layout", ["new", "old"])
@pytest.mark.parametrize("broken_index", [0, 1, 2], ids=["session", "message", "part"])
def test_corrupt_opencode_session_is_isolated_and_retried_after_repair(
    tmp_path, layout, broken_index
):
    source = tmp_path / "source"
    session_files(source, layout, "ses_healthy", "Healthy visible text")
    damaged = session_files(source, layout, "ses_broken", "Repaired visible text")[broken_index]
    original = damaged.read_bytes()
    damaged.write_text('{"id":', encoding="utf-8")
    with Store(tmp_path / "archive.sqlite") as store:
        first = collect_into(store, "opencode", source)
        assert first["imported_conversations"] == 1
        assert first["failed_files"] == 1
        assert store.status()["conversations"] == 1
        assert store.status()["messages"] == 1
        assert store.status()["unapproved_conversations"] == 1
        assert "Healthy visible text" not in json.dumps(first)
        retry = collect_into(store, "opencode", source)
        assert retry["imported_conversations"] == 0
        assert retry["failed_files"] == 1
        damaged.write_bytes(original)
        repaired = collect_into(store, "opencode", source)
        assert repaired["failed_files"] == 0
        assert store.status()["conversations"] == 2
        assert store.status()["messages"] == 2
        assert store.status()["import_failures"] == []
        assert collect_into(store, "opencode", source)["unchanged"] is True


@pytest.mark.parametrize("layout", ["new", "old"])
def test_opencode_part_edit_invalidates_unchanged_session_checkpoint(tmp_path, layout):
    source = tmp_path / "source"
    info, _, part = session_files(source, layout, "ses_edit", "Original text")
    info_stat = info.stat()
    with Store(tmp_path / "archive.sqlite") as store:
        first = collect_into(store, "opencode", source)
        key = first["conversation_ids"][0]
        value = json.loads(part.read_text())
        value["text"] = "New visible text, without changing session JSON"
        part.write_text(json.dumps(value), encoding="utf-8")
        result = collect_into(store, "opencode", source)
        assert result["failed_files"] == 0
        assert info.stat().st_mtime_ns == info_stat.st_mtime_ns
        assert store.get_conversation(key)["messages"][0]["text"] == value["text"]
        assert store.status()["messages"] == 1


@pytest.mark.parametrize("layout", ["new", "old"])
def test_explicit_session_import_observes_edits_to_its_part_files(tmp_path, layout):
    info, _, part = session_files(tmp_path / "source", layout, "ses_explicit", "Original")
    with Store(tmp_path / "archive.sqlite") as store:
        first = collect_into(store, "opencode", info)
        assert first["imported_conversations"] == 1
        value = json.loads(part.read_text())
        value["text"] = "Edited text from the referenced part file"
        part.write_text(json.dumps(value), encoding="utf-8")
        changed = collect_into(store, "opencode", info)
        assert changed["imported_conversations"] == 1
        key = first["conversation_ids"][0]
        assert store.get_conversation(key)["messages"][0]["text"] == value["text"]


@pytest.mark.parametrize("layout", ["new", "old"])
def test_invalid_utf8_in_explicit_session_is_rejected(tmp_path, layout):
    info, _, _ = session_files(tmp_path, layout, "ses_invalid", "Visible text")
    info.write_bytes(info.read_bytes().replace(b"Visible session", b"Invalid \xff title"))
    with pytest.raises(RuntimeError):
        collect("opencode", info)


def test_mixed_json_layouts_keep_old_only_sessions_and_prefer_new_duplicates(tmp_path):
    source = tmp_path / "source"
    session_files(source, "new", "ses_shared", "Current text")
    session_files(source, "old", "ses_shared", "Stale legacy text")
    session_files(source, "old", "ses_legacy", "Legacy-only visible text")
    with Store(tmp_path / "archive.sqlite") as store:
        result = collect_into(store, "opencode", source)
        assert result["failed_files"] == 0
        assert store.status()["conversations"] == 2
        archived = {
            store.get_conversation(key)["external_id"]: store.get_conversation(key)["messages"][0][
                "text"
            ]
            for key in result["conversation_ids"]
        }
        assert archived == {"ses_shared": "Current text", "ses_legacy": "Legacy-only visible text"}


def test_sqlite_still_takes_priority_over_broken_legacy_json(tmp_path):
    source = tmp_path / "source"
    info, _, _ = session_files(source, "new", "ses_legacy", "Legacy text")
    info.write_text('{"id":', encoding="utf-8")
    with sqlite3.connect(source / "opencode.db") as database:
        database.executescript("""
            CREATE TABLE project(id TEXT, worktree TEXT);
            CREATE TABLE session(id TEXT, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT,
                title TEXT, version TEXT, summary_additions INTEGER, summary_deletions INTEGER,
                summary_files INTEGER, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            INSERT INTO project VALUES('p', '/synthetic');
            INSERT INTO session VALUES('ses_database','p',NULL,'','/synthetic','Database','1',NULL,NULL,NULL,1000,2000);
        """)
        database.execute(
            "INSERT INTO message VALUES(?,?,?,?,?)",
            ("msg_db", "ses_database", 1000, 2000, json.dumps({"role": "user"})),
        )
        database.execute(
            "INSERT INTO part VALUES(?,?,?,?,?,?)",
            (
                "prt_db",
                "msg_db",
                "ses_database",
                1000,
                2000,
                json.dumps({"type": "text", "text": "Current SQLite text"}),
            ),
        )
    with Store(tmp_path / "archive.sqlite") as store:
        result = collect_into(store, "opencode", source)
        assert result["failed_files"] == 0
        assert result["imported_conversations"] == 1
        archived = store.get_conversation(result["conversation_ids"][0])
        assert archived["external_id"] == "ses_database"
        assert archived["messages"][0]["text"] == "Current SQLite text"
