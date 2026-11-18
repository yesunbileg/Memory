"""Public, offline consent and archive recovery contracts (synthetic data only)."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_store import conversation

from ai_memory.cli import main
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store


def cli(db, capsys, *args, code=0):
    assert main(["--db", str(db), *map(str, args)]) == code
    return json.loads(capsys.readouterr().out)


def test_atomic_approval_preview_revocation_and_private_fields(tmp_path, capsys):
    path = tmp_path / "archive.sqlite"
    secret_text = '  Монгол 🙂\r\n\rcode\npassword="""test\r\nkey"""  '
    with Store(path) as store:
        a = store.ingest(
            conversation(
                account="private-account",
                project="/private/project",
                source_url="https://example.invalid/private",
                external_id="private-native",
                messages=[dict(conversation()["messages"][0], text=secret_text)],
            )
        )
        b = store.ingest(conversation(account="separate"), consent=True)
    cli(path, capsys, "approve", a, "missing", code=1)
    with Store(path) as store:
        assert not store.get_conversation(a)["consent"]
        assert store.get_conversation(b)["consent"]
    assert [c["id"] for c in cli(path, capsys, "pending")["conversations"]] == [a]
    preview = cli(path, capsys, "preview", a)["segments"][0]
    assert not any(
        x in preview["text"]
        for x in [
            "private-account",
            "/private/project",
            "https://example.invalid/private",
            "private-native",
            "test\r\nkey",
        ]
    )
    assert 'password="""****\r\n***"""' in preview["text"]
    assert hashlib.sha256(preview["text"].encode()).hexdigest() == preview["sha256"]
    assert cli(path, capsys, "approve", a, a)["approved"] == [a]
    with Store(path) as store:
        builder = SegmentBuilder(store)
        sid = builder.build()[0]["id"]
        text = builder.render_outbound(sid)
        assert text == preview["text"]
        assert store.get_conversation(a)["messages"][0]["text"] == secret_text
        with store.connection:
            store.connection.execute(
                "INSERT INTO source_map VALUES(?,?,?)", (sid, "synthetic-owned", 1)
            )
    cli(path, capsys, "revoke", a)
    with Store(path) as store:
        assert (
            store.connection.execute("SELECT source_id FROM retained_sources").fetchone()[0]
            == "synthetic-owned"
        )
        assert (
            store.connection.execute("SELECT state FROM segments WHERE id=?", (sid,)).fetchone()[0]
            == "retired"
        )
        other = SegmentBuilder(store).build()[0]["id"]
        assert a not in SegmentBuilder(store).render_outbound(other)
        assert store.get_conversation(a)["messages"][0]["text"] == secret_text


def test_migration_v1_preserves_jobs_and_backup_restores_snapshot(tmp_path, capsys):
    from ai_memory.archive_files import workspace_for
    from ai_memory.store import SCHEMA, digest

    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as old:
        old.executescript(SCHEMA)
        old.execute(
            "INSERT INTO segments(id,state,revision,created_at) VALUES('SEG-001','open',1,'fixture')"
        )
        text = "  fixture\r\nтекст\r  "
        title = "SEG-001-r1-" + digest(text)[:16] + ".md"
        old.execute(
            "INSERT INTO sync_jobs(segment_id,revision,title,stage) VALUES('SEG-001',1,?,'uploading')",
            (title,),
        )
        old.execute("INSERT INTO source_map VALUES('SEG-001','old-source',1)")
    (tmp_path / "sync").mkdir()
    (tmp_path / "sync" / title).write_bytes(text.encode())
    with Store(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM source_receipts").fetchone()[0] == 0
    backup = tmp_path / "backup.sqlite"
    cli(path, capsys, "backup", backup)
    dest = tmp_path / "restored" / "new.sqlite"
    cli(dest, capsys, "restore", backup)
    assert (workspace_for(dest) / "sync" / title).read_bytes() == text.encode()
    with Store(dest) as restored:
        assert (
            restored.connection.execute("SELECT stage FROM sync_jobs").fetchone()[0] == "uploading"
        )
        assert (
            restored.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
            == "old-source"
        )
    cli(dest, capsys, "restore", backup, code=1)
    cli(path, capsys, "backup", backup, code=1)


def test_exports_are_scoped_and_never_overwrite_user_files(tmp_path, capsys):
    paths = [tmp_path / f"{name}.sqlite" for name in ("one", "two")]
    for path in paths:
        with Store(path) as store:
            store.ingest(conversation(), consent=True)
    out = [Path(cli(p, capsys, "build")["output"]) for p in paths]
    assert out[0] != out[1]
    (out[0] / "SEG-001.md").write_text("handwritten")
    cli(paths[0], capsys, "build", "--output", out[0], code=1)
    assert (out[0] / "SEG-001.md").read_text() == "handwritten"


def test_v1_job_can_reconstruct_original_bytes_without_external_files(tmp_path):
    from ai_memory.archive_files import backup_archive, restore_archive, workspace_for
    from ai_memory.store import SCHEMA

    path = tmp_path / "legacy.sqlite"
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "archive-v1-recovery.json").read_text(
            encoding="utf-8"
        )
    )
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        # Frozen rows and expected UTF-8 digest came from the v1 writer at b5b86a5.
        # Today's build/ingest/renderer must not rewrite the legacy input or oracle.
        for table in (
            "conversations",
            "messages",
            "segments",
            "segment_messages",
            "segment_metadata",
            "sync_jobs",
            "checkpoints",
        ):
            for row in fixture["rows"][table]:
                db.execute(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})",
                    tuple(row.values()),
                )
    with Store(path) as store:
        (old_copy,) = tmp_path.glob("*.pre-v2-*.sqlite")
        with sqlite3.connect(old_copy) as prior:
            assert prior.execute("PRAGMA user_version").fetchone()[0] == 1
        backup_archive(store, tmp_path / "portable.db")
    dest = tmp_path / "restore" / "archive.db"
    restore_archive(dest, tmp_path / "portable.db")
    actual = (workspace_for(dest) / "sync" / fixture["title"]).read_bytes()
    assert hashlib.sha256(actual).hexdigest() == fixture["snapshot_sha256"]
    with Store(dest) as store:
        restored = store.get_conversation(fixture["conversation_id"])
        assert restored["messages"][0]["text"] == fixture["text"]
        assert restored["consent"]


def test_unprovable_pending_backup_never_publishes_an_incomplete_file(tmp_path):
    from ai_memory.archive_files import backup_archive

    with Store(tmp_path / "archive.db") as store:
        store.ingest(conversation(), consent=True)
        row = SegmentBuilder(store).build()[0]
        with store.connection:
            store.connection.execute(
                "INSERT INTO sync_jobs(segment_id,revision,title,stage) VALUES(?,?,?,'uploading')",
                (row["id"], row["revision"], "SEG-001-r1-aaaaaaaaaaaaaaaa.md"),
            )
        path = tmp_path / "backup.db"
        with pytest.raises(ValueError, match="snapshot unavailable"):
            backup_archive(store, path)
        assert not path.exists()


def test_active_sync_cannot_race_successful_revoke(tmp_path):
    from ai_memory.archive_lock import archive_lock

    path = tmp_path / "archive.db"
    with Store(path) as store, Store(path) as other:
        cid = store.ingest(conversation(), consent=True)
        with archive_lock(other.path), pytest.raises(RuntimeError):
            store.revoke_many([cid])
        assert store.get_conversation(cid)["consent"]
        store.revoke_many([cid])
        assert not store.get_conversation(cid)["consent"]


@pytest.mark.parametrize("args", [["status"], ["connect"]])
def test_cli_never_echoes_untrusted_error_values(args, capsys, monkeypatch):
    from ai_memory import cli as command

    def fail(_):
        raise ValueError("synthetic private prompt, transcript, cookie, bearer and answer")

    monkeypatch.setattr(command, "execute", fail)
    assert command.main(args) == 1
    assert "synthetic private" not in capsys.readouterr().out


def test_missing_remote_hold_is_portable_without_a_fake_snapshot(tmp_path):
    from ai_memory.archive_files import backup_archive, restore_archive

    with Store(tmp_path / "original.db") as store:
        store.ingest(conversation(), consent=True)
        seg = SegmentBuilder(store).build()[0]
        with store.connection:
            store.connection.execute(
                "INSERT INTO sync_jobs(segment_id,revision,title,stage,error) VALUES(?,?,?,'missing','remote_missing')",
                (seg["id"], seg["revision"], f"{seg['id']}-r{seg['revision']}-missing.md"),
            )
        backup_archive(store, tmp_path / "copy.db")
    restore_archive(tmp_path / "new.db", tmp_path / "copy.db")
    with Store(tmp_path / "new.db") as store:
        assert store.connection.execute("SELECT stage FROM sync_jobs").fetchone()[0] == "missing"


def test_backup_rejects_wrong_outbound_filename_digest(tmp_path):
    from ai_memory.archive_files import backup_archive

    with Store(tmp_path / "original.db") as store:
        store.ingest(conversation(), consent=True)
        builder = SegmentBuilder(store)
        seg = builder.build()[0]
        with store.connection:
            store.save_outbound(seg["id"], seg["revision"], builder.render_outbound(seg["id"]))
            store.connection.execute(
                "INSERT INTO sync_jobs(segment_id,revision,title) VALUES(?,?,?)",
                (seg["id"], seg["revision"], f"{seg['id']}-r{seg['revision']}-aaaaaaaaaaaaaaaa.md"),
            )
        with pytest.raises(ValueError, match="checksum"):
            backup_archive(store, tmp_path / "copy.db")
        assert not (tmp_path / "copy.db").exists()


def test_cli_provider_error_uses_only_bounded_codes_and_messages(monkeypatch, capsys):
    from ai_memory import cli as command
    from ai_memory.notebook import NotebookLMError

    def fail(_):
        raise NotebookLMError("synthetic private transcript", "synthetic private prompt")

    monkeypatch.setattr(command, "execute", fail)
    assert command.main(["sync"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == {
        "code": "notebooklm_error",
        "message": "NotebookLM request failed. Check login and retry.",
    }


def test_unmaskable_derived_title_never_blocks_safely_masked_message(tmp_path):
    from ai_memory.privacy import find_sensitive

    title = "Сайн байна уу 🙂 ... password = '"
    packet = conversation(title=title)
    packet["messages"][0]["text"] = "Сайн байна уу 🙂\r\npassword = 'fixture-value'\r  "
    with Store(tmp_path / "archive.db") as store:
        cid = store.ingest(packet, consent=True)
        builder = SegmentBuilder(store)
        sid = builder.build()[0]["id"]
        outward = builder.render_outbound(sid)
        assert not find_sensitive(outward)
        assert "title:" not in outward
        assert "password = '*************'" in outward
        assert store.get_conversation(cid)["title"] == title
        assert store.get_conversation(cid)["messages"][0]["text"] == packet["messages"][0]["text"]


def test_malformed_cli_arguments_do_not_echo_values(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["ask", "synthetic question", "synthetic-private-cookie"])
    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "usage:" in stderr and "--help" in stderr
    assert "synthetic" not in stderr


def test_invalid_backup_does_not_publish_an_unusable_destination(tmp_path, capsys):
    from ai_memory.archive_files import workspace_for

    source = tmp_path / "original.sqlite"
    with Store(source) as store:
        store.ingest(conversation())
    copy = tmp_path / "backup.sqlite"
    cli(source, capsys, "backup", copy)
    with sqlite3.connect(copy) as corrupt:
        corrupt.execute("DROP TABLE source_receipts")
    destination = tmp_path / "restored.sqlite"
    cli(destination, capsys, "restore", copy, code=1)
    assert not destination.exists()
    assert not workspace_for(destination).exists()
