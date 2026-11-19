"""Synthetic cross-entry-point and read-boundary regressions."""

import json
import sqlite3

import pytest
from test_audit_collectors import opencode_db, response, write_rollout
from test_opencode_recovery import session_files
from test_store import conversation

from ai_memory.collectors import collect
from ai_memory.importer import collect_into
from ai_memory.store import Store


def turns(path, items, sid="test"):
    records = [{"type": "session_meta", "payload": {"id": sid}}]
    records.extend(
        response(txt, f"2026-01-01T00:00:{stamp:02d}Z", key, "user") for key, txt, stamp in items
    )
    write_rollout(path, records)


def test_exact_emoji_boundary_and_reserved_prefixes(tmp_path):
    text = "а" * 76 + "🌏 Монгол\r\n  ```\rмөр\r\n```  "
    other = "<environment_context> жинхэнэ асуулт\r "
    turns(tmp_path / "one.jsonl", [("u", text, 1), ("v", other, 2)])
    direct = collect("codex", tmp_path)[0]
    assert direct["title"] == "а" * 76 + "🌏..."
    with Store(tmp_path / "local.sqlite") as store:
        for _ in range(2):
            assert not collect_into(store, "codex", tmp_path)["failed_files"]
        rows = store.get_conversation(
            store.connection.execute("SELECT id FROM conversations").fetchone()[0]
        )
        assert [m["text"] for m in rows["messages"]] == [text, other]


def test_complete_wrapper_plus_visible_text_not_hidden(tmp_path):
    text = "<permissions instructions>x</permissions instructions>\nминий асуулт"
    turns(tmp_path / "one.jsonl", [("u", text, 1)])
    assert collect("codex", tmp_path)[0]["messages"][0]["text"] == text


def test_model_only_without_reparse_or_title(tmp_path, monkeypatch):
    src = tmp_path / "sessions"
    src.mkdir()
    turns(src / "one.jsonl", [("u", "hello", 1)])
    with sqlite3.connect(tmp_path / "state_5.sqlite") as idx:
        idx.execute("CREATE TABLE threads(id TEXT,title TEXT,model TEXT,archived INTEGER)")
        idx.execute("INSERT INTO threads VALUES ('test','','m1',0)")
    with Store(tmp_path / "db") as store:
        cid = collect_into(store, "codex", src)["conversation_ids"][0]
        assert "m1" in store.get_conversation(cid)["models"]
        with sqlite3.connect(tmp_path / "state_5.sqlite") as idx:
            idx.execute("UPDATE threads SET model='m2'")
        monkeypatch.setattr(
            "ai_memory.collectors.collect", lambda *a, **kw: pytest.fail("unrelated reparse")
        )
        assert not collect_into(store, "codex", src)["unchanged"]
        assert "m2" in store.get_conversation(cid)["models"]
        assert collect_into(store, "codex", src)["unchanged"]


@pytest.mark.parametrize("layout", ["old", "new"])
def test_direct_mixed_json_and_hidden_message(tmp_path, layout):
    session_files(tmp_path, "old", "ses_old", "байна")
    session_files(tmp_path, "new", "ses_new", "шинэ")
    _, msg, _ = session_files(tmp_path, layout, "ses_hidden", "not visible")
    msg.write_text(json.dumps({**json.loads(msg.read_bytes()), "synthetic": True}))
    seen = {
        c["external_id"]: [m["text"] for m in c["messages"]] for c in collect("opencode", tmp_path)
    }
    assert seen == {"ses_old": ["байна"], "ses_new": ["шинэ"], "ses_hidden": []}


@pytest.mark.parametrize("layout", ["old", "new"])
@pytest.mark.parametrize("level", ["session", "message", "part", "link"])
def test_json_parent_checks_at_read_boundary(tmp_path, layout, level):
    source = tmp_path / "source"
    info, msg, part = session_files(source, layout, "ses_demo", "visible")
    if level == "link":
        outside = tmp_path / "external.json"
        part.rename(outside)
        part.symlink_to(outside)
    else:
        file, key = {
            "session": (info, "id"),
            "message": (msg, "sessionID"),
            "part": (part, "messageID"),
        }[level]
        file.write_text(json.dumps({**json.loads(file.read_bytes()), key: "different"}))
    with pytest.raises((RuntimeError, ValueError)):
        collect("opencode", info)


@pytest.mark.parametrize("schema", [1, 2])
def test_db_message_visibility(tmp_path, schema):
    db = opencode_db(tmp_path, schema)
    table = "message" if schema == 1 else "session_message"
    data = json.loads(db.execute(f"SELECT data FROM {table}").fetchone()[0])
    db.execute(f"UPDATE {table} SET data=?", (json.dumps({**data, "synthetic": True}),))
    db.commit()
    db.close()
    assert not any(c["messages"] for c in collect("opencode", tmp_path))


def test_db_rejects_mismatched_part_parent(tmp_path):
    db = opencode_db(tmp_path, 1)
    db.execute("UPDATE part SET session_id='different'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError):
        collect("opencode", tmp_path)


def test_outer_ingest_transaction_rolls_back_valid_first_row(tmp_path):
    with Store(tmp_path / "db") as store:
        with pytest.raises(ValueError):
            with store.import_transaction():
                store.ingest(conversation())
                store.ingest({"external_id": 7})
        assert store.status()["conversations"] == 0


@pytest.mark.parametrize("reverse", [True, False])
def test_two_overlapping_snapshots_match_collect_across_scans(tmp_path, reverse):
    src = tmp_path / "sessions"
    src.mkdir()
    older = [("a", "old", 1), ("b", "middle", 2)]
    newer = [("a", "new", 3), ("c", "tail", 4)]
    first, second = (newer, older) if reverse else (older, newer)
    turns(src / "z.jsonl", first)
    with Store(tmp_path / "db") as store:
        cid = collect_into(store, "codex", src)["conversation_ids"][0]
        turns(src / "a.jsonl", second)
        assert not collect_into(store, "codex", src)["failed_files"]
        assert collect_into(store, "codex", src)["unchanged"]
        actual = [(m["native_id"], m["text"]) for m in store.get_conversation(cid)["messages"]]
        expected = [(m["native_id"], m["text"]) for m in collect("codex", src)[0]["messages"]]
        assert actual == expected == [("a", "new"), ("b", "middle"), ("c", "tail")]


def test_source_symlink_not_followed(tmp_path):
    outside = tmp_path / "outside.jsonl"
    turns(outside, [("u", "private", 1)])
    selected = tmp_path / "selected.jsonl"
    selected.symlink_to(outside)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        collect("codex", selected)


def test_opencode_read_snapshot_spans_session_message_and_part(tmp_path, monkeypatch):
    """Mutate between production SELECTs on a second SQLite connection in WAL mode."""
    db = opencode_db(tmp_path, 1)
    db.execute("PRAGMA journal_mode=WAL")
    db.close()
    hook = tmp_path / "mutate.mjs"
    hook.write_text("""
import {DatabaseSync} from 'node:sqlite';
const original=DatabaseSync.prototype.prepare;
let done=false;
DatabaseSync.prototype.prepare=function(sql){
  const stmt=original.call(this,sql);
  if(!done && /FROM session s/.test(sql)){
    const all=stmt.all.bind(stmt);
    stmt.all=(...args)=>{
      const rows=all(...args);
      if(!done){
        done=true;
        const writer=new DatabaseSync(process.env.SYNTHETIC_DB);
        writer.prepare("UPDATE part SET data=? WHERE id='p-visible'").run(JSON.stringify({type:'text',text:'later'}));
        writer.close();
      }
      return rows;
    };
  }
  return stmt;
};
""")
    monkeypatch.setenv("NODE_OPTIONS", f"--import={hook}")
    monkeypatch.setenv("SYNTHETIC_DB", str(tmp_path / "opencode.db"))
    assert collect("opencode", tmp_path)[0]["messages"][0]["text"] == "new"
    with sqlite3.connect(tmp_path / "opencode.db") as db:
        assert "later" in db.execute("SELECT data FROM part WHERE id='p-visible'").fetchone()[0]


def test_codex_source_filters_parts_and_unmarked_user_prose(tmp_path):
    data = response("хариу", "2026-01-01T00:00:01Z", "u", "user")
    for flag in ({"synthetic": True}, {"ignored": True}, {"visibility": "hidden"}):
        data["payload"]["content"].insert(0, {"type": "input_text", "text": "non-visible", **flag})
    hidden = response("non-visible", "2026-01-01T00:00:02Z", "hidden", "user")
    hidden["payload"]["ignored"] = True
    write_rollout(
        tmp_path / "visible.jsonl",
        [{"type": "session_meta", "payload": {"id": "type-flags"}}, data, hidden],
    )
    assert [m["text"] for m in collect("codex", tmp_path)[0]["messages"]] == ["хариу"]


def test_equal_clock_conflict_uses_same_winner_across_names_and_scans(tmp_path):
    digests = []
    for swap in (False, True):
        src = tmp_path / str(swap)
        src.mkdir()
        with Store(tmp_path / f"{swap}.db") as store:
            words = ["зүүн", "баруун"]
            if swap:
                words.reverse()
            for name, word in zip(("first.jsonl", "second.jsonl"), words, strict=True):
                turns(src / name, [("m", word, 1)])
                assert not collect_into(store, "codex", src)["failed_files"]
            cid = store.connection.execute("SELECT id FROM conversations").fetchone()[0]
            saved = store.get_conversation(cid)
            expected = collect("codex", src)[0]
            assert saved["messages"][0]["text"] == expected["messages"][0]["text"]
            assert saved["title"] == expected["title"]
            digests.append(
                (saved["title"], saved["messages"][0]["id"], saved["messages"][0]["text"])
            )
    assert digests[0] == digests[1]


def test_identical_mirrored_turn_has_stable_archive_id_across_entrypoints(tmp_path):
    from test_audit_collectors import event

    src = tmp_path / "selected"
    src.mkdir()
    records = [
        {"type": "session_meta", "payload": {"id": "pair"}},
        event("мөр", "2026-01-01T00:00:00Z"),
    ]
    path = src / "one.jsonl"
    write_rollout(path, records)
    with Store(tmp_path / "incremental.db") as incremental, Store(tmp_path / "direct.db") as direct:
        cid = collect_into(incremental, "codex", src)["conversation_ids"][0]
        records.append(response("мөр", "2026-01-01T00:00:00.500Z", "native-turn"))
        write_rollout(path, records)
        assert collect_into(incremental, "codex", src)["failed_files"] == 0
        assert direct.ingest(collect("codex", src)[0]) == cid
        assert (
            direct.get_conversation(cid)["messages"][0]["id"]
            == incremental.get_conversation(cid)["messages"][0]["id"]
        )


def test_index_model_and_title_clearing_survive_existing_rollout_model(tmp_path, monkeypatch):
    src = tmp_path / "sessions"
    src.mkdir()
    write_rollout(
        src / "r.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "turn_context", "payload": {"model": "native"}},
            response("  Эх нэр  ", "2026-01-01T00:00:00Z", "u", "user"),
        ],
    )
    idx = sqlite3.connect(tmp_path / "state_5.sqlite")
    idx.execute("CREATE TABLE threads(id TEXT,title TEXT,model TEXT,archived INTEGER)")
    idx.execute("INSERT INTO threads VALUES('s','Indexed','idx',0)")
    idx.commit()
    assert collect("codex", src)[0]["models"] == ["native", "idx"]
    with Store(tmp_path / "store") as store:
        cid = collect_into(store, "codex", src)["conversation_ids"][0]
        monkeypatch.setattr(
            "ai_memory.collectors.collect", lambda *a, **kw: pytest.fail("no reparse needed")
        )
        for name in ("idx2", "idx"):
            idx.execute("UPDATE threads SET title='', model=?", (name,))
            idx.commit()
            assert not collect_into(store, "codex", src)["unchanged"]
            assert store.get_conversation(cid)["title"] == "Эх нэр"
            assert collect_into(store, "codex", src)["unchanged"]
    idx.close()


def test_explicit_opencode_database_wal_edit(tmp_path):
    db = opencode_db(tmp_path, 1)
    db.execute("PRAGMA journal_mode=WAL")
    try:
        path = tmp_path / "opencode.db"
        assert collect("opencode", path)[0]["messages"][0]["text"] == "new"
        with Store(tmp_path / "archive.db") as store:
            cid = collect_into(store, "opencode", path)["conversation_ids"][0]
            db.execute(
                "UPDATE part SET data=?, time_updated=4000 WHERE id='p-visible'",
                (json.dumps({"type": "text", "text": "updated"}),),
            )
            db.commit()
            assert not collect_into(store, "opencode", path)["unchanged"]
            assert store.get_conversation(cid)["messages"][0]["text"] == "updated"
    finally:
        db.close()


def test_checkpoint_failure_rolls_back_archive_packet(tmp_path):
    path = tmp_path / "session.jsonl"
    turns(path, [("u", "синтетик", 1)])
    with Store(tmp_path / "archive") as store:
        store.connection.execute("""CREATE TRIGGER synthetic_checkpoint_failure
            BEFORE INSERT ON checkpoints WHEN NEW.key LIKE 'codex:%'
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        result = collect_into(store, "codex", path)
        assert result["failed_files"] == 1 and result["conversation_ids"] == []
        assert store.status()["conversations"] == 0


def test_newer_partial_message_preserves_known_source_model(tmp_path):
    with Store(tmp_path / "archive") as store:
        c = conversation()
        c["messages"][0]["model"] = "original"
        cid = store.ingest(c)
        del c["messages"][0]["model"]
        c["messages"][0]["text"] = "засвар"
        store.ingest(c)
        assert store.get_conversation(cid)["messages"][0]["model"] == "original"


def test_scope_separates_an_exported_rollout_from_unselected_sibling_index(tmp_path):
    path = tmp_path / "export.jsonl"
    turns(path, [("u", "visible", 1)])
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT,title TEXT,model TEXT)")
        db.execute("INSERT INTO threads VALUES('test','other index','index-model')")
    assert collect("codex", path)[0]["title"] == "visible"
    assert collect("codex", tmp_path)[0]["title"] == "other index"
    with Store(tmp_path / "archive") as store:
        cid = collect_into(store, "codex", tmp_path)["conversation_ids"][0]
        assert store.get_conversation(cid)["title"] == "other index"


def test_demo_and_collect_entrypoints_agree_on_ids_order_and_text(tmp_path):
    from ai_memory import cli
    from ai_memory.demo import prepare_inputs

    output = tmp_path / "demo"
    assert cli.main(["demo", "--output", str(output)]) == 0
    inputs = prepare_inputs(tmp_path / "same")
    imported = tmp_path / "collect.db"
    for platform, path in zip(("codex", "opencode"), inputs, strict=True):
        assert (
            cli.main(
                [
                    "--db",
                    str(imported),
                    "collect",
                    platform,
                    str(path),
                    "--account",
                    "synthetic-demo",
                ]
            )
            == 0
        )

    def visible(db):
        with sqlite3.connect(db) as conn:
            return conn.execute("""SELECT conversation_id,id,native_id,sequence,role,kind,text FROM messages
                                ORDER BY conversation_id,sequence""").fetchall()

    assert visible(imported) == visible(output / "conversations.sqlite")


def test_explicit_opencode_session_skips_an_unchanged_native_dependency_set(tmp_path, monkeypatch):
    info, _, _ = session_files(tmp_path / "selected", "new", "ses_once", "unchanged")
    with Store(tmp_path / "archive.sqlite") as store:
        assert collect_into(store, "opencode", info)["imported_conversations"] == 1

        def unexpected_reparse(*args, **kwargs):
            pytest.fail("unchanged session and dependencies must use their success checkpoint")

        monkeypatch.setattr("ai_memory.collectors.collect", unexpected_reparse)
        assert collect_into(store, "opencode", info)["unchanged"]


def test_unsupported_opencode_path_does_not_enumerate_guessed_dependencies(tmp_path, monkeypatch):
    from pathlib import Path

    info = tmp_path / "not-storage" / "misc" / "project" / "ses_unknown.json"
    info.parent.mkdir(parents=True)
    info.write_text("{}")
    unrelated = tmp_path / "not-storage" / "message" / info.stem
    unrelated.mkdir(parents=True)
    touched = []
    glob = Path.glob

    def track(directory, *args, **kwargs):
        touched.append(directory)
        return glob(directory, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", track)
    with Store(tmp_path / "archive.sqlite") as store:
        result = collect_into(store, "opencode", info)
        assert result["failed_files"] == 1
        assert result["conversation_ids"] == []
    assert unrelated not in touched


def test_collect_into_joins_existing_archive_transaction(tmp_path):
    source = tmp_path / "one.jsonl"
    turns(source, [("u", "same transaction", 1)])
    with Store(tmp_path / "archive.sqlite") as store:
        with (
            pytest.raises(RuntimeError, match="simulated caller failure"),
            store.import_transaction(),
        ):
            assert collect_into(store, "codex", source)["imported_conversations"] == 1
            raise RuntimeError("simulated caller failure")
        assert store.status()["conversations"] == 0
        assert not store.connection.execute("SELECT 1 FROM checkpoints").fetchone()
