"""The background worker uses temporary archives and stubbed external boundaries."""

import logging
import time

import pytest

from ai_memory import cli
from ai_memory.store import Store


def test_watch_does_not_patch_shared_sleep(monkeypatch):
    original = time.sleep
    assert hasattr(cli, "_sleep")
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    assert time.sleep is original


def test_worker_survives_import_and_auth_failures_with_safe_limited_logs(tmp_path, monkeypatch, caplog):
    from ai_memory.notebook import NotebookLMAuthError

    db = tmp_path / "db"
    with Store(db) as store:
        store.set_setting("notebook_id", "synthetic")
    clock, scans, syncs = [0], [], []

    def collect(store, platform, path, account):
        scans.append((platform, clock[0]))
        if platform == "codex":
            raise ValueError("password = 'PRIVATE-CONVERSATION-DO-NOT-LOG'")
        return {"failed_files": 1}

    async def sync(store):
        syncs.append(clock[0])
        if len(syncs) <= 2:
            raise NotebookLMAuthError()
        return {"status": {"sync_status": "up_to_date"}}

    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 90:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "collect_into", collect)
    monkeypatch.setattr(cli, "sync_archive", sync)
    monkeypatch.setattr(cli, "_monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(cli, "_sleep", advance, raising=False)
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (0, 0, 0))
    args = cli.parser().parse_args(["--db", str(db), "watch", "--codex-path", str(tmp_path / "broken"),
                                  "--opencode-path", str(tmp_path / "healthy")])
    with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt):
        cli._watch(args)
    assert len(scans) == 6 and len(syncs) == 3
    assert len(caplog.records) == 3  # one per import platform and auth category
    assert "import_failed" in caplog.text and "auth_required" in caplog.text
    assert "retry" in caplog.text
    assert "PRIVATE-CONVERSATION" not in caplog.text


def test_collect_partial_failure_returns_unsuccessful_cli_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "collect_into", lambda *a, **kw: {
        "imported_conversations": 1, "failed_files": 1,
        "file_failures": [{"file_id": "opaque-id", "category": "parse"}],
    })
    assert cli.main(["--db", str(tmp_path / "db"), "collect", "codex", str(tmp_path)]) == 1
    assert '"failed_files": 1' in capsys.readouterr().out


@pytest.mark.parametrize("reason", ["CapacityError", "SnapshotError", "SourceQuota"])
def test_worker_reports_preflight_blockers_without_private_details_or_log_spam(tmp_path, monkeypatch, caplog, reason):
    db = tmp_path / "archive.sqlite"
    source = tmp_path / "sessions"
    source.mkdir()
    with Store(db) as store:
        store.set_setting("notebook_id", "synthetic")
    clock, syncs = [0], []

    async def blocked_sync(store):
        syncs.append(clock[0])
        return {"blocked": [{"reason": reason, "detail": "PRIVATE-ARCHIVE-VALUE"}],
                "status": {"sync_status": "pending"}}

    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 90:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sync_archive", blocked_sync)
    monkeypatch.setattr(cli, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "_sleep", advance)
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (0, 0, 0))
    args = cli.parser().parse_args(["--db", str(db), "watch", "--codex-path", str(source)])
    with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt):
        cli._watch(args)
    assert syncs == [0, 30, 60]
    assert len(caplog.records) == 1
    assert reason in caplog.text and "retry" in caplog.text
    assert "PRIVATE-ARCHIVE-VALUE" not in caplog.text
