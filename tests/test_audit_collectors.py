"""Synthetic regressions for audited collector data loss and scale failures."""

import json
import sqlite3

import pytest

from ai_memory.collectors import collect
from ai_memory.importer import collect_into
from ai_memory.store import Store


def opencode_db(path, schema, count=1):
    db = sqlite3.connect(path / "opencode.db")
    db.execute("CREATE TABLE project(id TEXT, worktree TEXT)")
    db.execute("INSERT INTO project VALUES ('p', '/synthetic')")
    common = "id TEXT, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, title TEXT, version TEXT, summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER"
    if schema == 1:
        db.execute(f"CREATE TABLE session({common},time_created INTEGER,time_updated INTEGER)")
        db.execute(
            "CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)"
        )
        db.execute(
            "CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)"
        )
        db.executemany(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"s-{i}",
                    "p",
                    None,
                    "",
                    "/synthetic",
                    "Synthetic",
                    "1",
                    None,
                    None,
                    None,
                    1000 + i,
                    100000,
                )
                for i in range(count)
            ],
        )
        db.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("m", "s-0", 1000, 2000, json.dumps({"role": "assistant"})),
        )
        db.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            ("p-visible", "m", "s-0", 1000, 3000, json.dumps({"type": "text", "text": "new"})),
        )
        db.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (
                "p-hidden",
                "m",
                "s-0",
                1000,
                90000,
                json.dumps({"type": "reasoning", "text": "hidden"}),
            ),
        )
    else:
        db.execute(
            f"CREATE TABLE session_v2({common},cost REAL,tokens_input INTEGER,tokens_output INTEGER,tokens_reasoning INTEGER,model TEXT,agent TEXT,time_created INTEGER,time_updated INTEGER)"
        )
        db.execute(
            "CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT)"
        )
        db.executemany(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"s-{i}",
                    "p",
                    None,
                    "",
                    "/synthetic",
                    "Synthetic",
                    "2",
                    None,
                    None,
                    None,
                    0,
                    0,
                    0,
                    0,
                    None,
                    None,
                    1000 + i,
                    100000,
                )
                for i in range(count)
            ],
        )
        db.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (
                "m",
                "s-0",
                "assistant",
                1,
                1000,
                3000,
                json.dumps({"content": [{"type": "text", "text": "new"}]}),
            ),
        )
    db.commit()
    return db


@pytest.mark.parametrize("schema", [1, 2])
def test_native_opencode_message_versions_reject_older_snapshot(tmp_path, schema):
    db = opencode_db(tmp_path, schema)
    current = collect("opencode", tmp_path)[0]
    assert current["messages"][0].get("updated_at") == "1970-01-01T00:00:03.000Z"
    if schema == 1:
        db.execute(
            "UPDATE part SET time_updated=1500,data=? WHERE id=?",
            (json.dumps({"type": "text", "text": "old"}), "p-visible"),
        )
        db.execute("UPDATE message SET time_updated=1500")
    else:
        db.execute(
            "UPDATE session_message SET time_updated=1500,data=?",
            (json.dumps({"content": [{"type": "text", "text": "old"}]}),),
        )
    db.commit()
    older = collect("opencode", tmp_path)[0]
    db.close()
    with Store(tmp_path / "archive.sqlite") as store:
        cid = store.ingest(current)
        store.ingest(older)
        assert [m["text"] for m in store.get_conversation(cid)["messages"]] == ["new"]
        assert store.get_conversation(cid)["messages"][0]["native_id"] == "m"


@pytest.mark.parametrize("schema", [1, 2])
def test_large_opencode_archive_stays_below_sql_variable_limit(tmp_path, schema):
    db = opencode_db(tmp_path, schema, count=33000)
    db.close()
    conversations = collect("opencode", tmp_path)
    visible = [c for c in conversations if c["messages"]]
    assert [c["external_id"] for c in visible] == ["s-0"]
    assert visible[0]["messages"][0]["text"] == "new"


def write_rollout(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def event(text, timestamp, native_id=None):
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "agent_message",
            "message": text,
            **({"id": native_id} if native_id else {}),
        },
    }


def response(text, timestamp, native_id=None, role="assistant"):
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "output_text", "text": text}],
            **({"id": native_id} if native_id else {}),
        },
    }


@pytest.mark.parametrize("native_id", [None, "native-answer"])
def test_incremental_codex_pair_keeps_existing_event_without_collapsing_repeats(
    tmp_path, native_id
):
    path = tmp_path / "rollout.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "mirror-session"}},
        event("same", "2025-01-01T00:00:01Z"),
    ]
    write_rollout(path, records)
    with Store(tmp_path / "archive.sqlite") as store:
        first = collect_into(store, "codex", path)
        cid = first["conversation_ids"][0]
        original = store.get_conversation(cid)["messages"][0]
        records += [response("same", "2025-01-01T00:00:01.500Z", native_id)]
        write_rollout(path, records)
        result = collect_into(store, "codex", path)
        assert result["failed_files"] == 0
        messages = store.get_conversation(cid)["messages"]
        assert len(messages) == 1
        assert messages[0]["id"] == original["id"]
        records += [
            event("same", "2025-01-01T00:00:05Z"),
            response("same", "2025-01-01T00:00:05.500Z", "second-answer"),
        ]
        write_rollout(path, records)
        assert collect_into(store, "codex", path)["failed_files"] == 0
        assert len(store.get_conversation(cid)["messages"]) == 2


def test_preexisting_legacy_codex_event_id_is_an_alias_of_proven_pair(tmp_path):
    path = tmp_path / "rollout.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "legacy-mirror"}},
        event("answer", "2025-01-01T00:00:01Z"),
    ]
    write_rollout(path, records)
    current = collect("codex", path)[0]
    # Historical rows predate aliases, so explicitly strip the new hint.
    current["messages"][0].pop("native_aliases", None)
    with Store(tmp_path / "archive.sqlite") as store:
        cid = store.ingest(current)
        before = store.get_conversation(cid)["messages"][0]
        records += [response("answer", "2025-01-01T00:00:01.500Z", "canonical-answer")]
        write_rollout(path, records)
        assert collect_into(store, "codex", path)["failed_files"] == 0
        messages = store.get_conversation(cid)["messages"]
        assert len(messages) == 1
        assert messages[0]["id"] == before["id"]


def test_codex_title_only_edit_refreshes_archive_without_reparsing_rollouts(tmp_path, monkeypatch):
    from ai_memory import collectors

    source = tmp_path / "sessions"
    source.mkdir()
    rollout = source / "rollout.jsonl"
    write_rollout(
        rollout,
        [
            {"type": "session_meta", "payload": {"id": "title-only"}},
            response("question", "2025-01-01T00:00:01Z", "user", "user"),
        ],
    )
    db = sqlite3.connect(tmp_path / "state_5.sqlite")
    db.execute("CREATE TABLE threads(id TEXT,title TEXT,model TEXT,archived INTEGER)")
    db.execute("INSERT INTO threads VALUES (?,?,?,?)", ("title-only", "Old title", None, 0))
    db.commit()
    with Store(tmp_path / "archive.sqlite") as store:
        cid = collect_into(store, "codex", source)["conversation_ids"][0]
        before_messages = store.get_conversation(cid)["messages"]
        db.execute("UPDATE threads SET title='New title'")
        db.commit()

        def no_reparse(*args, **kwargs):
            pytest.fail("unchanged rollouts should not be reparsed for title-only changes")

        monkeypatch.setattr(collectors, "collect", no_reparse)
        result = collect_into(store, "codex", source)
        assert result["failed_files"] == 0
        assert store.get_conversation(cid)["title"] == "New title"
        assert store.get_conversation(cid)["messages"] == before_messages
        assert not result["unchanged"]
    db.close()


@pytest.mark.parametrize(
    "text",
    [
        "# AGENTS.md instructions\nPlease review this document.",
        "# Agent Configuration\nI wrote this documentation.",
        "Guidance for coding agents is the title I chose.",
    ],
)
def test_codex_keeps_user_text_with_instruction_like_prefix(tmp_path, text):
    path = tmp_path / "rollout.jsonl"
    write_rollout(
        path,
        [
            {"type": "session_meta", "payload": {"id": "visible-prefix"}},
            response(text, "2025-01-01T00:00:01Z", "user-text", "user"),
            response("reply", "2025-01-01T00:00:02Z", "reply"),
        ],
    )
    assert [m["text"] for m in collect("codex", path)[0]["messages"]] == [text, "reply"]


@pytest.mark.parametrize(
    "text",
    [
        "<environment_context>synthetic environment</environment_context>",
        "<permissions instructions>synthetic permissions</permissions instructions>",
        "<available_deferred_tools>synthetic tools</available_deferred_tools>",
        "# AGENTS.md instructions\n<INSTRUCTIONS>synthetic bootstrap</INSTRUCTIONS>",
    ],
)
def test_codex_retains_known_bootstrap_exclusion(tmp_path, text):
    path = tmp_path / "rollout.jsonl"
    write_rollout(
        path,
        [
            {"type": "session_meta", "payload": {"id": "bootstrap"}},
            response(text, "2025-01-01T00:00:01Z", "hidden", "user"),
            response("visible", "2025-01-01T00:00:02Z", "reply"),
        ],
    )
    assert [m["text"] for m in collect("codex", path)[0]["messages"]] == ["visible"]


def test_incremental_identical_codex_events_without_times_remain_distinct(tmp_path):
    path = tmp_path / "rollout.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "no-times"}}, event("same", None)]
    write_rollout(path, records)
    with Store(tmp_path / "archive.sqlite") as store:
        cid = collect_into(store, "codex", path)["conversation_ids"][0]
        records += [response("same", None, "canonical-one"), event("same", None)]
        write_rollout(path, records)
        assert collect_into(store, "codex", path)["failed_files"] == 0
        assert len(store.get_conversation(cid)["messages"]) == 2
        records += [response("same", None, "canonical-two")]
        write_rollout(path, records)
        assert collect_into(store, "codex", path)["failed_files"] == 0
        assert len(store.get_conversation(cid)["messages"]) == 2


def test_codex_snapshot_tree_pairs_older_event_with_newer_canonical_message(tmp_path):
    source = tmp_path / "sessions"
    source.mkdir()
    records = [
        {"type": "session_meta", "payload": {"id": "snapshot-mirror"}},
        event("same", "2025-01-01T00:00:01Z"),
    ]
    write_rollout(source / "older.jsonl", records)
    records += [response("same", "2025-01-01T00:00:01.500Z", "canonical-answer")]
    write_rollout(source / "newer.jsonl", records)
    conversation = collect("codex", source)[0]
    assert len(conversation["messages"]) == 1
    with Store(tmp_path / "archive.sqlite") as store:
        cid = store.ingest(conversation)
        assert len(store.get_conversation(cid)["messages"]) == 1
