"""Native completion events import exactly one synthetic session, locally."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_audit_collectors import opencode_db

from ai_memory.store import Store


def hook(archive, platform, payload, *args):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_memory",
            "--db",
            str(archive),
            "hook",
            platform,
            *map(str, args),
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


def stop(source, session_id="synthetic-codex"):
    return {
        "hook_event_name": "Stop",
        "transcript_path": str(source),
        "session_id": session_id,
        "last_assistant_message": "DO NOT IMPORT payload text",
    }


def test_codex_stop_replay_then_continuation_preserves_ids_and_consent(tmp_path):
    source = tmp_path / "rollout-selected.jsonl"
    shutil.copyfile(Path(__file__).parent / "fixtures/codex-visible.jsonl", source)
    (tmp_path / "rollout-unselected.jsonl").write_text(
        source.read_text().replace("synthetic-codex", "unselected")
    )
    archive = tmp_path / "archive.sqlite"
    with Store(archive):
        pass
    for _ in range(2):
        result = hook(archive, "codex", stop(source), "--account", "work")
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    with Store(archive) as store:
        (row,) = store.connection.execute(
            "SELECT id,external_id,account,revision,consent FROM conversations"
        ).fetchall()
        cid = row[0]
        assert tuple(row[1:]) == ("synthetic-codex", "work", 1, 0)
        messages = store.get_conversation(cid)["messages"]
        assert len(messages) == 5 and "DO NOT IMPORT" not in json.dumps(messages)
        store.approve_many([cid])
    with source.open("a") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "u-continue",
                        "content": [{"type": "input_text", "text": "one new turn"}],
                    },
                }
            )
            + "\n"
        )
    assert hook(archive, "codex", stop(source), "--account", "work").returncode == 0
    with Store(archive) as store:
        continued = store.get_conversation(cid)
        assert continued["consent"] is True
        assert [m["id"] for m in continued["messages"][:-1]] == [m["id"] for m in messages]
        assert continued["messages"][-1]["text"] == "one new turn"
        assert store.connection.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("schema", [1, 2])
def test_opencode_idle_queries_only_selected_native_session(tmp_path, schema):
    db = opencode_db(tmp_path, schema, count=2)
    if schema == 1:
        db.execute("INSERT INTO message VALUES ('other','s-1',1,1,'PRIVATE-invalid-json')")
    else:
        db.execute(
            "INSERT INTO session_message VALUES ('other','s-1','assistant',1,1,1,'PRIVATE-invalid-json')"
        )
    db.commit()
    db.close()
    source = tmp_path / "opencode.db"
    archive = tmp_path / "archive.db"
    with Store(archive):
        pass
    for _ in range(2):
        result = hook(
            archive, "opencode", {"event": "session.idle", "session_id": "s-0"}, "--source", source
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    with sqlite3.connect(archive) as db:
        assert db.execute("SELECT external_id,revision,consent FROM conversations").fetchall() == [
            ("s-0", 1, 0)
        ]
        assert db.execute("SELECT native_id,text FROM messages").fetchall() == [("m", "new")]


def test_codex_stop_rejects_unrelated_or_unbounded_sources_privately(tmp_path):
    archive = tmp_path / "archive.db"
    source = tmp_path / "PRIVATE-rollout.jsonl"
    shutil.copyfile(Path(__file__).parent / "fixtures/codex-visible.jsonl", source)
    symlink = tmp_path / "PRIVATE-link.jsonl"
    symlink.symlink_to(source)
    with Store(archive):
        pass
    for payload in (
        stop(source, "PRIVATE-wrong-session"),
        stop(tmp_path),
        stop(symlink),
        stop("PRIVATE-relative.jsonl"),
    ):
        result = hook(archive, "codex", payload)
        assert result.returncode == 1 and result.stderr
        assert result.stdout == "" and "PRIVATE" not in result.stderr
    with sqlite3.connect(archive) as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_hook_never_initializes_missing_database(tmp_path):
    archive = tmp_path / "new/archive.sqlite"
    result = hook(archive, "codex", stop(tmp_path / "PRIVATE.jsonl"))
    assert result.returncode == 1 and not archive.parent.exists()
    assert result.stdout == "" and "PRIVATE" not in result.stderr


def test_ephemeral_codex_stop_skips_absent_transcript_without_archiving_event_text(tmp_path):
    archive = tmp_path / "archive.db"
    with Store(archive):
        pass
    result = hook(archive, "codex", {**stop("unused"), "transcript_path": None})
    assert result.returncode == 0 and not (result.stdout or result.stderr)
    with sqlite3.connect(archive) as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
