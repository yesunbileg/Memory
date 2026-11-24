import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ai_memory", *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_cli_init_status_and_missing_notebook(tmp_path):
    db = tmp_path / "archive.sqlite"
    result = run_cli("--db", db, "init")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["conversations"] == 0
    result = run_cli("--db", db, "status")
    assert result.returncode == 0
    assert json.loads(result.stdout)["notebook_configured"] is False
    result = run_cli("--db", db, "ask", "Өмнө ямар шийдвэр гаргасан бэ?")
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "not_configured"


@pytest.mark.parametrize(
    ("configured", "failure", "exit_code"),
    [
        (False, None, 1),
        (True, "auth_required", 1),
        (True, "network_error", 1),
        (True, None, 0),
    ],
)
def test_check_exit_reflects_notebook_connectivity(
    tmp_path, monkeypatch, capsys, configured, failure, exit_code
):
    from ai_memory.cli import main
    from ai_memory.notebook import NotebookLMError
    from ai_memory.store import Store

    class OfflineNotebook:
        def __init__(self, notebook_id, storage_path):
            assert notebook_id == "synthetic-notebook"

        async def list_sources(self):
            if failure:
                raise NotebookLMError("Safe failure message", failure)
            return []

        async def close(self):
            pass

    monkeypatch.setattr("ai_memory.gateway.NotebookLMAdapter", OfflineNotebook)
    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        if configured:
            store.set_setting("notebook_id", "synthetic-notebook")
    assert main(["--db", str(db), "status"]) == 0
    assert "notebook" not in json.loads(capsys.readouterr().out)

    actual_exit = main(["--db", str(db), "check"])
    notebook = json.loads(capsys.readouterr().out)["notebook"]
    assert notebook["configured"] is configured
    assert notebook["connected"] is (configured and not failure)
    if failure:
        assert notebook["error"]["code"] == failure
    assert actual_exit == exit_code


def test_cli_real_demo_is_repeatable_and_excludes_hidden_events(tmp_path):
    output = tmp_path / "demo"
    first = run_cli("demo", "--output", output)
    assert first.returncode == 0, first.stderr
    demo = json.loads(first.stdout)
    assert demo["status"]["conversations"] == 2
    assert demo["status"]["messages"] >= 5
    assert demo["live_notebooklm_test"] == "not_run"
    assert demo["stats"]["peak_rss_mib"] > 0
    segments = list((output / "segments").glob("*.md"))
    assert len(segments) == 1
    text = segments[0].read_text()
    assert "SQLite" in text and "Монгол" in text
    assert "DO NOT ARCHIVE" not in text
    repeated = run_cli("demo", "--output", output)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"]["messages"] == demo["status"]["messages"]


def test_demo_refuses_to_overwrite_existing_user_directory(tmp_path):
    (tmp_path / "notes.txt").write_text("user data")
    result = run_cli("demo", "--output", tmp_path)
    assert result.returncode != 0
    assert "empty output directory" in result.stdout
    assert (tmp_path / "notes.txt").read_text() == "user data"


def test_cli_collect_checkpoints_skip_unchanged_and_approve_explicitly(tmp_path):
    from ai_memory.demo import prepare_inputs

    codex, _ = prepare_inputs(tmp_path / "inputs")
    db = tmp_path / "db"
    first = run_cli("--db", db, "collect", "codex", codex)
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    assert result["imported_conversations"] == 1
    key = result["conversation_ids"][0]
    unchanged = run_cli("--db", db, "collect", "codex", codex)
    assert json.loads(unchanged.stdout)["unchanged"] is True
    build = run_cli("--db", db, "build")
    assert json.loads(build.stdout)["segments"] == []
    approved = run_cli("--db", db, "approve", key)
    assert approved.returncode == 0
    build = run_cli("--db", db, "build")
    assert json.loads(build.stdout)["segments"]
    shown = run_cli("--db", db, "show", key)
    assert json.loads(shown.stdout)["messages"][0]["text"].startswith("  ")


def test_collect_isolates_bad_jsonl_and_retries_it_after_repair(tmp_path):
    import shutil

    from ai_memory.cli import collect_into
    from ai_memory.store import Store

    source = tmp_path / "sessions"
    source.mkdir()
    shutil.copyfile(
        Path(__file__).parent / "fixtures/codex-visible.jsonl", source / "healthy.jsonl"
    )
    broken = source / "broken.jsonl"
    broken.write_text(
        '{"type":"session_meta","payload":{"id":"broken"}}\n{"type":', encoding="utf-8"
    )
    with Store(tmp_path / "archive.sqlite") as store:
        first = collect_into(store, "codex", source)
        assert first["imported_conversations"] == 1
        assert first["failed_files"] == 1
        assert first["unchanged"] is False
        assert store.status()["conversations"] == 1
        assert store.status()["import_failures"][0]["failed_files"] == 1
        assert store.status()["import_failures"][0]["categories"] == ["parse"]

        repaired = (Path(__file__).parent / "fixtures/codex-visible.jsonl").read_text()
        broken.write_text(repaired.replace("synthetic-codex", "repaired-codex"), encoding="utf-8")
        second = collect_into(store, "codex", source)
        assert second["imported_conversations"] == 1
        assert second["failed_files"] == 0
        assert second["unchanged"] is False
        assert store.status()["conversations"] == 2
        assert store.status()["import_failures"] == []


@pytest.mark.parametrize("import_order", [("old.jsonl", "new.jsonl"), ("new.jsonl", "old.jsonl")])
def test_collect_latest_edit_wins_independent_of_file_import_order(tmp_path, import_order):
    from ai_memory.cli import collect_into
    from ai_memory.store import Store

    source = tmp_path / "sessions"
    source.mkdir()

    def rollout(text, updated):
        records = [
            {
                "type": "session_meta",
                "payload": {"id": "stable-session", "timestamp": "2026-11-10T15:00:00Z"},
            },
            {
                "timestamp": "2026-11-10T15:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": "stable-message",
                    "content": [{"type": "output_text", "text": text}],
                },
            },
            {"timestamp": updated, "type": "turn_context", "payload": {"model": "synthetic"}},
        ]
        return "\n".join(json.dumps(record) for record in records)

    snapshots = {
        "old.jsonl": rollout("older", "2026-11-10T15:30:00Z"),
        "new.jsonl": rollout("latest", "2026-11-10T16:00:00Z"),
    }
    with Store(tmp_path / "archive.sqlite") as store:
        for name in import_order:
            (source / name).write_text(snapshots[name], encoding="utf-8")
            result = collect_into(store, "codex", source)
            assert result["failed_files"] == 0
        conversation = store.get_conversation(
            store.connection.execute(
                "SELECT id FROM conversations WHERE external_id='stable-session'"
            ).fetchone()[0]
        )
        assert len(conversation["messages"]) == 1
        assert conversation["messages"][0]["text"] == "latest"
        assert conversation["messages"][0]["timestamp"] == "2026-11-10T15:00:00.000Z"
        assert conversation["updated_at"] == "2026-11-10T16:00:00.000Z"


def test_upload_approval_is_honored_on_unchanged_import(tmp_path):
    from ai_memory.demo import prepare_inputs
    from ai_memory.store import Store

    source, _ = prepare_inputs(tmp_path / "inputs")
    db = tmp_path / "db"
    first = run_cli("--db", db, "collect", "codex", source)
    key = json.loads(first.stdout)["conversation_ids"][0]
    result = run_cli("--db", db, "collect", "codex", source, "--approve-upload")
    assert result.returncode == 0
    with Store(db) as store:
        assert store.get_conversation(key)["consent"] is True


def test_cli_rejects_empty_server_token(tmp_path):
    result = run_cli("--db", tmp_path / "db", "serve")
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "invalid_configuration"
    assert "Traceback" not in result.stderr


def test_collect_partial_failure_returns_unsuccessful_cli_status(tmp_path, monkeypatch, capsys):
    from ai_memory import cli

    monkeypatch.setattr(
        cli,
        "collect_into",
        lambda *a, **kw: {
            "imported_conversations": 1,
            "failed_files": 1,
            "file_failures": [{"file_id": "opaque-id", "category": "parse"}],
        },
    )
    assert cli.main(["--db", str(tmp_path / "db"), "collect", "codex", str(tmp_path)]) == 1
    assert '"failed_files": 1' in capsys.readouterr().out


def available_port():
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        return socket_.getsockname()[1]


def test_connect_writes_private_file_starts_receiver_and_reuses_running_setup(tmp_path):
    port = available_port()
    db = tmp_path / "archive.sqlite"
    command = [sys.executable, "-m", "ai_memory", "--db", str(db), "connect", "--port", str(port)]
    process = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    file = tmp_path / "archive.sqlite.files" / "extension-connection.json"
    try:
        deadline = time.monotonic() + 7
        ready = False
        while time.monotonic() < deadline and process.poll() is None:
            if file.exists():
                settings = json.loads(file.read_text())
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
                try:
                    connection.request(
                        "POST",
                        "/auth/check",
                        body="{}",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer " + settings["token"],
                        },
                    )
                    response = connection.getresponse()
                    ready = response.status == 200
                    response.read()
                    if ready:
                        break
                except OSError:
                    pass
                finally:
                    connection.close()
            time.sleep(0.03)
        assert ready
        assert file.stat().st_mode & 0o777 == 0o600
        repeated = run_cli("--db", db, "connect", "--port", port)
        assert repeated.returncode == 0
        assert "already running" in repeated.stdout.lower()
        assert str(file) in repeated.stdout
        token_exposed = settings["token"] in repeated.stdout + repeated.stderr
        assert not token_exposed
    finally:
        process.terminate()
        output, errors = process.communicate(timeout=5)
    assert "terminal" in output.lower()
    assert str(file) in output
    assert "Traceback" not in errors
    if file.exists():
        token_exposed = json.loads(file.read_text())["token"] in output + errors
        assert not token_exposed


def test_connect_explains_occupied_receiver_with_different_token(tmp_path):
    from ai_memory.ingest import make_server

    server = make_server(tmp_path / "other.sqlite", "other-synthetic-private-token", port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run_cli(
            "--db", tmp_path / "selected.sqlite", "connect", "--port", server.server_port
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result.returncode == 1
    assert "port" in result.stdout.lower()
    assert "token" in result.stdout.lower() or "different connection" in result.stdout.lower()
    assert "other-synthetic-private-token" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "selected.sqlite.files" / "extension-connection.json").exists()


def test_connect_rejects_running_receiver_for_other_database_in_same_directory(tmp_path):
    from ai_memory.connection import prepare_connection
    from ai_memory.ingest import make_server

    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    files = prepare_connection(first, 41770, write_config=False)
    server = make_server(first, files.token, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run_cli("--db", second, "connect", "--port", server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result.returncode == 1
    assert "different connection token" in result.stdout.lower()
    assert not (tmp_path / "second.sqlite.files" / "extension-connection.json").exists()
    # The authenticated probe persists only the logical receiver archive identity.
    assert first.exists() and not second.exists()


@pytest.mark.parametrize("operation", [("build",), ("sync",), ("show", "missing")])
def test_existing_archive_commands_do_not_create_an_archive_on_a_typo(tmp_path, operation):
    db = tmp_path / "typo" / "absent.sqlite"
    result = run_cli("--db", db, *operation)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "archive_not_found"
    assert not db.parent.exists()


def test_all_chats_sync_explicitly_includes_all_archived_items(tmp_path, monkeypatch, capsys):
    from test_store import conversation
    from test_sync import Remote

    from ai_memory.cli import main
    from ai_memory.store import Store

    remote = Remote()

    class Notebook:
        def __init__(self, notebook_id, storage_path):
            assert notebook_id == "bound-notebook"

        async def list_sources(self):
            return await remote.list_sources()

        async def get_source_text(self, source_id):
            return await remote.get_source_text(source_id)

        async def upload(self, path, title):
            return await remote.upload(path, title)

        async def wait_ready(self, source_id):
            await remote.wait_ready(source_id)

        async def delete(self, source_id):
            await remote.delete(source_id)

        async def close(self):
            pass

    monkeypatch.setattr("ai_memory.notebook.NotebookLMAdapter", Notebook)
    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        first = conversation(external_id="first")
        first["messages"][0]["text"] = "first body"
        second = conversation(external_id="second")
        second["messages"][0]["text"] = "second body password = 'SyntheticPass987654!'"
        first_id = store.ingest(first, consent=True)
        second_id = store.ingest(second)
        store.set_setting("notebook_id", "bound-notebook")

    # Default sync retains its existing behavior and leaves the second chat out.
    assert main(["--db", str(db), "sync"]) == 0
    assert json.loads(capsys.readouterr().out)["completed"] == 1
    assert "first body" in next(iter(remote.sources.values()))["text"]
    assert "second body" not in next(iter(remote.sources.values()))["text"]
    with Store(db, existing=True) as store:
        assert store.get_conversation(first_id)["consent"]
        assert not store.get_conversation(second_id)["consent"]

    assert main(["--db", str(db), "sync", "--all-chats"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"]["sync_status"] == "up_to_date"
    with Store(db, existing=True) as store:
        assert store.get_conversation(first_id)["consent"]
        assert store.get_conversation(second_id)["consent"]
        assert "SyntheticPass987654!" in store.get_conversation(second_id)["messages"][0]["text"]
    assert any("second body" in entry["text"] for entry in remote.sources.values())
    assert all("SyntheticPass987654!" not in entry["text"] for entry in remote.sources.values())

    # Repeat uses existing sources and grants nothing twice.
    before = len(remote.operations)
    assert main(["--db", str(db), "sync", "--all-chats"]) == 0
    capsys.readouterr()
    assert len(remote.operations) == before


def test_all_chats_sync_does_not_change_archive_without_notebook_binding(tmp_path, capsys):
    from test_store import conversation

    from ai_memory.cli import main
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        chat = store.ingest(conversation())
    assert main(["--db", str(db), "sync", "--all-chats"]) == 1
    capsys.readouterr()
    with Store(db, existing=True) as store:
        assert not store.get_conversation(chat)["consent"]


def test_automatic_sync_leaves_work_for_the_next_cycle_without_failing(tmp_path, monkeypatch, capsys):
    from ai_memory.cli import main
    from ai_memory.store import Store

    async def sync(store, *, progress, all_chats, automatic):
        assert automatic and not all_chats
        return {
            "completed": 1,
            "status": {"sync_status": "pending", "unassigned_messages": 1},
        }

    monkeypatch.setattr("ai_memory.cli.sync_archive", sync)
    db = tmp_path / "archive.sqlite"
    with Store(db):
        pass
    assert main(["--db", str(db), "sync", "--automatic"]) == 0
    assert json.loads(capsys.readouterr().out)["status"]["sync_status"] == "pending"


def test_all_chats_sync_does_not_change_archive_if_another_sync_holds_lock(tmp_path, capsys):
    from test_store import conversation

    from ai_memory.archive_lock import archive_lock
    from ai_memory.cli import main
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        chat = store.ingest(conversation())
        store.set_setting("notebook_id", "bound-notebook")

    with archive_lock(db):
        assert main(["--db", str(db), "sync", "--all-chats"]) == 1
        capsys.readouterr()
        with Store(db, existing=True) as store:
            assert not store.get_conversation(chat)["consent"]
