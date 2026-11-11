"""Synthetic inputs exercise the vendored hstry parsers through Node, not mocks."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
RAW_USER = " \tСайн уу — café\n```python\nprint('漢字 🦦')\n```\n  "
RAW_FINAL = "  Done.\n\n```js\n  console.log('✓');\n```\n\t"


def collect(*args, **kwargs):
    from ai_memory.collectors import collect as real_collect

    return real_collect(*args, **kwargs)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_codex_exact_text_mirrors_visibility_and_native_ids(tmp_path):
    source = tmp_path / "sessions"
    source.mkdir()
    shutil.copyfile(FIXTURES / "codex-visible.jsonl", source / "rollout-demo.jsonl")
    conversations = collect("Codex", source, account="synthetic")
    assert len(conversations) == 1
    conversation = conversations[0]
    assert {
        k: conversation[k]
        for k in ("platform", "account", "external_id", "project", "models", "source_url")
    } == {
        "platform": "codex",
        "account": "synthetic",
        "external_id": "synthetic-codex",
        "project": "/synthetic/project",
        "models": ["gpt-synthetic"],
        "source_url": None,
    }
    assert [
        (m["native_id"], m["role"], m["kind"], m["text"]) for m in conversation["messages"]
    ] == [
        ("u-1", "user", "text", RAW_USER),
        ("a-progress", "assistant", "progress", "  Checking fixtures…\n"),
        ("a-final", "assistant", "text", RAW_FINAL),
        ("a-repeat", "assistant", "text", RAW_FINAL),
        ("visible-summary", "assistant", "thinking_summary", "  Visible summary only.\n"),
    ]
    assert [m["sequence"] for m in conversation["messages"]] == [1, 2, 3, 4, 5]
    assert all(m["model"] == "gpt-synthetic" for m in conversation["messages"])
    assert "DO NOT IMPORT" not in json.dumps(conversation)


def test_codex_fallback_ids_survive_appended_records_and_unknown_metadata(tmp_path):
    source = tmp_path / "rollout-no-times.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "unknown-metadata"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": " \n?\t"}],
            },
        },
    ]
    source.write_text("\n".join(json.dumps(v) for v in records) + "\n")
    before = collect("codex", source)[0]
    assert before["created_at"] is None
    assert before["updated_at"] is None
    assert before["models"] == []
    assert before["messages"][0]["timestamp"] is None
    assert "model" not in before["messages"][0]
    with source.open("a") as file:
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "answer",
                        "content": [{"type": "output_text", "text": "42"}],
                    },
                }
            )
            + "\n"
        )
    after = collect("codex", source)[0]
    assert after["messages"][0] == before["messages"][0]
    assert after["messages"][1]["native_id"] == "answer"


def test_derived_message_ids_do_not_depend_on_message_order():
    from ai_memory.collectors import normalize_conversation

    messages = [
        {"role": "user", "content": "question", "createdAt": 1000},
        {"role": "assistant", "content": "answer", "createdAt": 2000},
    ]
    def normalized(items):
        return normalize_conversation({"externalId": "stable", "messages": items}, "codex", "local")

    forward = {m["text"]: m["native_id"] for m in normalized(messages)["messages"]}
    reverse = {m["text"]: m["native_id"] for m in normalized(list(reversed(messages)))["messages"]}
    assert forward == reverse

    with_part_ids = {"externalId": "stable", "messages": [{
        "role": "assistant", "createdAt": 2000,
        "parts": [{"id": "counter-1", "type": "text", "text": "answer"}],
    }]}
    with_different_part_ids = {"externalId": "stable", "messages": [{
        "role": "assistant", "createdAt": 2000,
        "parts": [{"id": "counter-99", "type": "text", "text": "answer"}],
    }]}
    first = normalize_conversation(with_part_ids, "codex", "local")["messages"][0]["native_id"]
    second = normalize_conversation(with_different_part_ids, "codex", "local")["messages"][0]["native_id"]
    assert first == second


def test_codex_mirror_counts_do_not_drop_repeated_real_events(tmp_path):
    source = tmp_path / "rollout-mirrors.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "mirrors"}}]
    for _ in range(2):
        records.extend(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "same reply"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "same reply"}],
                    },
                },
            ]
        )
    source.write_text("\n".join(json.dumps(v) for v in records))
    messages = collect("codex", source)[0]["messages"]
    assert [m["text"] for m in messages] == ["same reply", "same reply"]
    assert len({m["native_id"] for m in messages}) == 2


def make_opencode_json(base):
    write_json(
        base / "storage/session/project-demo/ses_demo.json",
        {
            "id": "ses_demo",
            "title": "Synthetic OpenCode",
            "directory": "/synthetic/project",
            "projectID": "project-demo",
            "time": {"created": 1000, "updated": 2000},
        },
    )
    for message_id, role, timestamp in [
        ("msg_user", "user", 1000),
        ("msg_assistant", "assistant", 2000),
    ]:
        write_json(
            base / f"storage/message/ses_demo/{message_id}.json",
            {
                "id": message_id,
                "sessionID": "ses_demo",
                "role": role,
                "time": {"created": timestamp},
                **({"modelID": "opencode-synthetic"} if role == "assistant" else {}),
            },
        )
    parts = [
        ("msg_user", "prt_01", "text", RAW_USER[:15]),
        ("msg_user", "prt_02", "text", RAW_USER[15:]),
        ("msg_assistant", "prt_01", "reasoning", "DO NOT IMPORT hidden reasoning"),
        ("msg_assistant", "prt_02", "text", RAW_FINAL),
    ]
    for message_id, part_id, kind, text in parts:
        write_json(
            base / f"storage/part/{message_id}/{part_id}.json",
            {
                "id": part_id,
                "messageID": message_id,
                "sessionID": "ses_demo",
                "type": kind,
                "text": text,
            },
        )
    write_json(
        base / "storage/part/msg_assistant/prt_03.json",
        {
            "id": "prt_03",
            "type": "tool",
            "tool": "shell",
            "state": {"input": "DO NOT IMPORT command", "output": "DO NOT IMPORT output"},
        },
    )


def test_opencode_json_exact_multipart_and_hidden_reasoning_omitted(tmp_path):
    make_opencode_json(tmp_path)
    conversation = collect("opencode", tmp_path)[0]
    assert conversation["external_id"] == "ses_demo"
    assert conversation["models"] == ["opencode-synthetic"]
    assert [
        (m["native_id"], m["role"], m["text"], m["timestamp"]) for m in conversation["messages"]
    ] == [
        ("msg_user", "user", RAW_USER, "1970-01-01T00:00:01.000Z"),
        ("msg_assistant", "assistant", RAW_FINAL, "1970-01-01T00:00:02.000Z"),
    ]
    assert "model" not in conversation["messages"][0]
    assert "DO NOT IMPORT" not in json.dumps(conversation)


def test_opencode_sqlite_v1_executes_real_parser(tmp_path):
    database = sqlite3.connect(tmp_path / "opencode.db")
    database.executescript("""
        CREATE TABLE project(id TEXT, worktree TEXT);
        CREATE TABLE session(id TEXT, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, title TEXT,
          version TEXT, summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
          time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        INSERT INTO project VALUES ('p', '/synthetic/project');
        INSERT INTO session VALUES ('ses_db', 'p', NULL, '', '/synthetic/project', 'SQLite', '1', NULL,NULL,NULL,1000,2000);
    """)
    database.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        (
            "native-a",
            "ses_db",
            1000,
            2000,
            json.dumps({"role": "assistant", "modelID": "synthetic-model"}),
        ),
    )
    for part_id, kind, text in [
        ("prt_1", "text", RAW_FINAL[:10]),
        ("prt_2", "text", RAW_FINAL[10:]),
        ("prt_3", "reasoning", "DO NOT IMPORT"),
    ]:
        database.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (part_id, "native-a", "ses_db", 1000, 2000, json.dumps({"type": kind, "text": text})),
        )
    database.commit()
    database.close()
    conversation = collect("opencode", tmp_path)[0]
    assert [(m["native_id"], m["text"]) for m in conversation["messages"]] == [
        ("native-a", RAW_FINAL)
    ]
    assert conversation["models"] == ["synthetic-model"]


def test_opencode_sqlite_v2_preserves_native_sequence_and_text(tmp_path):
    database = sqlite3.connect(tmp_path / "opencode.db")
    database.executescript("""
        CREATE TABLE project(id TEXT, worktree TEXT);
        CREATE TABLE session_v2(id TEXT, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, title TEXT,
          version TEXT, summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
          cost REAL, tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
          model TEXT, agent TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message(id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
        INSERT INTO project VALUES ('p', '/synthetic/project');
        INSERT INTO session_v2 VALUES ('ses_v2','p',NULL,'','/synthetic/project','v2','2',NULL,NULL,NULL,0,0,0,0,NULL,NULL,1000,2000);
    """)
    records = [
        ("native-u", "user", 1, 2000, {"text": RAW_USER}),
        (
            "native-a",
            "assistant",
            2,
            1000,
            {
                "content": [
                    {"type": "reasoning", "text": "DO NOT IMPORT"},
                    {"type": "text", "visibility": "hidden", "text": "DO NOT IMPORT"},
                    {"type": "text", "synthetic": True, "text": "DO NOT IMPORT"},
                    {"type": "text", "text": RAW_FINAL[:10]},
                    {"type": "text", "text": RAW_FINAL[10:]},
                ]
            },
        ),
        ("native-system", "synthetic", 3, 3000, {"text": "DO NOT IMPORT"}),
    ]
    for mid, kind, sequence, timestamp, data in records:
        database.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (mid, "ses_v2", kind, sequence, timestamp, timestamp, json.dumps(data)),
        )
    database.commit()
    database.close()
    messages = collect("opencode", tmp_path)[0]["messages"]
    assert [(m["native_id"], m["sequence"], m["text"]) for m in messages] == [
        ("native-u", 1, RAW_USER),
        ("native-a", 2, RAW_FINAL),
    ]


def test_unsupported_platform_and_missing_path_fail_explicitly(tmp_path):
    with pytest.raises(ValueError, match="platform"):
        collect("unknown", tmp_path)
    with pytest.raises(FileNotFoundError):
        collect("codex", tmp_path / "missing")


def test_invalid_unicode_is_reported_instead_of_rewritten(tmp_path):
    path = tmp_path / "rollout-invalid-unicode.jsonl"
    path.write_text(
        '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"\\ud800"}]}}\n'
    )
    with pytest.raises(RuntimeError, match="parser failed"):
        collect("codex", path)


def test_extension_starts_with_local_opt_in_and_token_required():
    script = Path(__file__).parents[1] / "vendor/hstry/extension/test/local-config.js"
    result = subprocess.run(["node", str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_codex_keeps_real_user_discussion_of_agent_instructions(tmp_path):
    path = tmp_path / "rollout-user-query.jsonl"
    text = "Please explain the instructions in AGENTS.md.\n  Keep this exact text.\n"
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "real-user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
    )
    assert collect("codex", path)[0]["messages"][0]["text"] == text


def test_codex_subagent_rollout_is_not_a_user_conversation(tmp_path):
    path = tmp_path / "rollout-subagent.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "subagent",
                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "DO NOT IMPORT hidden task"}],
                    },
                },
            ]
        )
    )
    assert collect("codex", path) == []


def test_opencode_synthetic_and_hidden_text_are_not_user_messages(tmp_path):
    make_opencode_json(tmp_path)
    for index, flags in enumerate(
        [{"synthetic": True}, {"ignored": True}, {"visibility": "hidden"}], 3
    ):
        write_json(
            tmp_path / f"storage/part/msg_user/prt_{index:02d}.json",
            {
                "id": f"prt_{index:02d}",
                "type": "text",
                "text": "DO NOT IMPORT",
                **flags,
            },
        )
    messages = collect("opencode", tmp_path)[0]["messages"]
    assert messages[0]["text"] == RAW_USER


def test_invalid_utf8_fails_instead_of_replacement_characters(tmp_path):
    path = tmp_path / "rollout-invalid-utf8.jsonl"
    path.write_bytes(
        b'{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"bad\xff"}]}}\n'
    )
    with pytest.raises(RuntimeError, match="parser failed"):
        collect("codex", path)


def test_codex_explicit_source_does_not_read_default_personal_index(tmp_path, monkeypatch):
    other_home = tmp_path / "unrelated-codex-home"
    other_home.mkdir()
    with sqlite3.connect(other_home / "state_5.sqlite") as database:
        database.execute("CREATE TABLE threads(id TEXT,title TEXT,model TEXT,archived INTEGER)")
        database.execute(
            "INSERT INTO threads VALUES(?,?,?,?)",
            ("synthetic-id", "DO NOT IMPORT", "DO NOT IMPORT", 0),
        )
    monkeypatch.setenv("CODEX_HOME", str(other_home))
    source = tmp_path / "selected-rollout.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"type": "session_meta", "payload": {"id": "synthetic-id"}},
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Visible user text"}],
                    },
                },
            ]
        )
    )
    conversation = collect("codex", source)[0]
    assert conversation["models"] == []
    assert conversation["title"] == "Visible user text"
    assert conversation["created_at"] is None
    assert conversation["messages"][0]["timestamp"] == "2026-01-01T00:00:00.000Z"


def test_opencode_does_not_silently_drop_corrupt_source_parts(tmp_path):
    make_opencode_json(tmp_path)
    (tmp_path / "storage/part/msg_user/prt_03.json").write_text('{"text":')
    with pytest.raises(RuntimeError, match="parser failed"):
        collect("opencode", tmp_path)
