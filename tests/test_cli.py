import json
import subprocess
import sys
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
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli.time, "sleep", advance)
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (0, 0, 0))
    args = cli.parser().parse_args(["--db", str(db), "watch", "--codex-path", str(source)])
    with pytest.raises(KeyboardInterrupt):
        cli._watch(args)
    assert len(calls) == expected_calls
