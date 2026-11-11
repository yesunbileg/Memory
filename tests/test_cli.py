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
    return subprocess.run([sys.executable, "-m", "ai_memory", *map(str, args)], cwd=ROOT,
                          text=True, capture_output=True, timeout=30)


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
    shutil.copyfile(Path(__file__).parent / "fixtures/codex-visible.jsonl", source / "healthy.jsonl")
    broken = source / "broken.jsonl"
    broken.write_text('{"type":"session_meta","payload":{"id":"broken"}}\n{"type":', encoding="utf-8")
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
            {"type": "session_meta", "payload": {"id": "stable-session", "timestamp": "2026-11-10T15:00:00Z"}},
            {"timestamp": "2026-11-10T15:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "stable-message",
                "content": [{"type": "output_text", "text": text}],
            }},
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
        conversation = store.get_conversation(store.connection.execute(
            "SELECT id FROM conversations WHERE external_id='stable-session'"
        ).fetchone()[0])
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


def test_cli_rejects_fast_watch_and_empty_server_token(tmp_path):
    db = tmp_path / "db"
    for args in [("watch", "--codex-path", tmp_path, "--interval", "0"), ("serve",)]:
        result = run_cli("--db", db, *args)
        assert result.returncode != 0
        assert json.loads(result.stdout)["error"]["code"] == "invalid_configuration"
        assert "Traceback" not in result.stderr


@pytest.mark.parametrize("pending,expected_calls", [(True, 3), (False, 1)])
def test_background_worker_revisits_pending_jobs_without_waiting_twelve_hours(tmp_path, monkeypatch, pending, expected_calls):
    from ai_memory import cli
    from ai_memory.demo import prepare_inputs
    from ai_memory.store import Store

    source, _ = prepare_inputs(tmp_path / "inputs")
    db = tmp_path / "db"
    with Store(db) as store:
        store.set_setting("notebook_id", "synthetic")
    calls, clock = [], [0]

    async def simulated_sync(store):
        calls.append(clock[0])
        return {"status": {"sync_status": "pending" if pending else "up_to_date"}}

    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 90:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sync_archive", simulated_sync)
    monkeypatch.setattr(cli, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "_sleep", advance)
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (0, 0, 0))
    args = cli.parser().parse_args(["--db", str(db), "watch", "--codex-path", str(source)])
    with pytest.raises(KeyboardInterrupt):
        cli._watch(args)
    assert len(calls) == expected_calls


def available_port():
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        return socket_.getsockname()[1]


def test_connect_writes_private_file_starts_receiver_and_reuses_running_setup(tmp_path):
    port = available_port()
    db = tmp_path / "archive.sqlite"
    command = [sys.executable, "-m", "ai_memory", "--db", str(db), "connect", "--port", str(port)]
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    file = tmp_path / "extension-connection.json"
    try:
        deadline = time.monotonic() + 7
        ready = False
        while time.monotonic() < deadline and process.poll() is None:
            if file.exists():
                settings = json.loads(file.read_text())
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
                try:
                    connection.request("POST", "/auth/check", body="{}", headers={
                        "Content-Type": "application/json", "Authorization": "Bearer " + settings["token"],
                    })
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
        result = run_cli("--db", tmp_path / "selected.sqlite", "connect", "--port", server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result.returncode == 1
    assert "port" in result.stdout.lower()
    assert "token" in result.stdout.lower() or "different connection" in result.stdout.lower()
    assert "other-synthetic-private-token" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "extension-connection.json").exists()


def test_connect_rejects_running_receiver_for_other_database_in_same_directory(tmp_path):
    from ai_memory.connection import prepare_connection
    from ai_memory.ingest import make_server

    first, second = tmp_path / 'first.sqlite', tmp_path / 'second.sqlite'
    files = prepare_connection(first, 41770, write_config=False)
    server = make_server(first, files.token, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run_cli('--db', second, 'connect', '--port', server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result.returncode == 1
    assert 'different archive' in result.stdout.lower()
    assert not (tmp_path / 'extension-connection.json').exists()
    assert not first.exists() and not second.exists()
