"""Offline regressions for archive CLI locking, export privacy and health."""

import asyncio
import fcntl
import json
import os

import pytest
from test_store import conversation
from test_sync import Remote

from ai_memory import cli, collectors
from ai_memory.importer import collect_into
from ai_memory.notebook import NotebookLMError
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine


@pytest.mark.parametrize("reason", ["auth_required", "network_error", "timeout"])
def test_sync_persists_jobless_failure_and_recovery(tmp_path, monkeypatch, reason):
    class OfflineNotebook(Remote):
        failure = None

        async def list_sources(self):
            if self.failure:
                raise NotebookLMError("PRIVATE-REMOTE-DETAIL", self.failure)
            return await super().list_sources()

        async def close(self):
            pass

    remote = OfflineNotebook()
    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        store.set_setting("notebook_id", "synthetic")
        store.ingest(conversation(), consent=True)
        asyncio.run(SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md").run_once())
        assert store.status()["sync_status"] == "up_to_date"
    remote.failure = reason
    monkeypatch.setattr("ai_memory.notebook.NotebookLMAdapter", lambda *_: remote)
    with Store(db) as store:
        failed = asyncio.run(cli.sync_archive(store))
        assert reason in json.dumps(failed["blocked"])
        assert "PRIVATE" not in json.dumps(failed)
        status = store.status()
        assert status["sync_preflight_errors"] == [{"reason": reason}]
        assert status["sync_status"] == "up_to_date"  # local bookkeeping, not remote health
        assert status["sync_errors"] == []
        remote.failure = None
        result = asyncio.run(cli.sync_archive(store))
        assert result["blocked"] == []
        assert store.status()["sync_preflight_errors"] == []


def test_missing_source_health_is_safe_source_specific_and_recovers(tmp_path):
    db = tmp_path / "archive.sqlite"
    first = tmp_path / "PRIVATE-FIRST-SOURCE"
    second = tmp_path / "PRIVATE-SECOND-SOURCE"
    with Store(db) as store:
        with pytest.raises(FileNotFoundError):
            collect_into(store, "codex", first)
        failures = store.status()["import_failures"]
        assert len(failures) == 1
        assert failures[0]["categories"] == ["source_missing"]
        assert failures[0]["scope"] == "source"
        first_id = failures[0]["source_id"]
        with pytest.raises(FileNotFoundError):
            collect_into(store, "codex", second)
        failures = store.status()["import_failures"]
        assert len(failures) == 2
        assert len({failure["source_id"] for failure in failures}) == 2
        assert "PRIVATE" not in json.dumps(failures)
        first.mkdir()
        collect_into(store, "codex", first)
        remaining = store.status()["import_failures"]
        assert len(remaining) == 1
        assert remaining[0]["source_id"] != first_id
        second.mkdir()
        collect_into(store, "codex", second)
        assert store.status()["import_failures"] == []


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (PermissionError("PRIVATE-PATH"), "source_permission"),
        (OSError("PRIVATE-PATH"), "source_read"),
    ],
)
def test_source_listing_error_is_persisted_without_paths(tmp_path, monkeypatch, error, category):
    source = tmp_path / "PRIVATE-SOURCE"
    source.mkdir()

    def fail_listing(*args, **kwargs):
        raise error

    monkeypatch.setattr(collectors, "codex_rollout_files", fail_listing)
    with Store(tmp_path / "archive.sqlite") as store:
        with pytest.raises(type(error)):
            collect_into(store, "codex", source)
        failures = store.status()["import_failures"]
        assert len(failures) == 1
        assert failures[0]["categories"] == [category]
        assert "PRIVATE" not in json.dumps(failures)


def test_sync_cli_cannot_read_binding_or_construct_adapter_before_archive_lock(
    tmp_path, monkeypatch
):
    db = tmp_path / "archive.sqlite"
    reads, adapters = [], []

    class OfflineNotebook(Remote):
        def __init__(self, *args):
            super().__init__()
            adapters.append(args)

        async def close(self):
            pass

    monkeypatch.setattr("ai_memory.notebook.NotebookLMAdapter", OfflineNotebook)
    with Store(db) as store:
        store.set_setting("notebook_id", "synthetic")
        real_get = store.get_setting

        def get_setting(key, *args):
            reads.append(key)
            return real_get(key, *args)

        monkeypatch.setattr(store, "get_setting", get_setting)
        descriptor = os.open(str(db) + ".sync.lock", os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="active"):
                asyncio.run(cli.sync_archive(store))
        assert reads == []
        assert adapters == []


def test_build_preserves_existing_symlink_and_publishes_complete_fresh_export(tmp_path):
    db = tmp_path / "archive.sqlite"
    output = tmp_path / "public-output"
    output.mkdir(mode=0o755)
    output.chmod(0o755)  # Fixture must be public even with a restrictive caller umask.
    outside = tmp_path / "existing-user-file"
    outside.write_text("must remain untouched")
    with Store(db) as store:
        store.ingest(conversation(), consent=True)
        builder = SegmentBuilder(store)
        builder.build()
        segment_id = store.connection.execute("SELECT id FROM segments").fetchone()[0]
        expected = builder.render(segment_id)
    destination = output / f"{segment_id}.md"
    destination.symlink_to(outside)
    with pytest.raises(ValueError, match="existing files were retained"):
        cli.execute(cli.parser().parse_args(["--db", str(db), "build", "--output", str(output)]))
    assert outside.read_text() == "must remain untouched"
    assert destination.is_symlink() and destination.resolve() == outside
    assert output.stat().st_mode & 0o777 == 0o755
    fresh = tmp_path / "new-export"
    result = cli.execute(
        cli.parser().parse_args(["--db", str(db), "build", "--output", str(fresh)])
    )
    assert result["segments"] == [segment_id]
    complete = fresh / f"{segment_id}.md"
    assert complete.read_bytes() == expected.encode("utf-8")
    assert not complete.is_symlink()
    assert complete.stat().st_mode & 0o777 == 0o600
    assert fresh.stat().st_mode & 0o777 == 0o700
    assert not list(tmp_path.glob(".partial-*"))


def test_busy_sync_does_not_overwrite_other_sync_diagnostic(tmp_path):
    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        store.set_setting("notebook_id", "synthetic")
        store.set_setting("sync_preflight_errors", [{"reason": "auth_required"}])
    descriptor = os.open(str(db) + ".sync.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with Store(db) as store, pytest.raises(RuntimeError, match="active"):
            asyncio.run(cli.sync_archive(store))
    with Store(db) as store:
        assert store.get_setting("sync_preflight_errors") == [{"reason": "auth_required"}]


def test_concurrent_source_diagnostics_do_not_overwrite_each_other(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "archive.sqlite"
    with Store(db):
        pass
    first_read = threading.Event()
    first_may_write = threading.Event()
    second_finished = threading.Event()
    real_get = Store.get_setting

    def get_setting(store, key, default=None):
        value = real_get(store, key, default)
        if key == "import_failures" and getattr(store, "test_source", None) == "first":
            first_read.set()
            assert first_may_write.wait(timeout=5)
        return value

    monkeypatch.setattr(Store, "get_setting", get_setting)

    def collect_missing(name):
        try:
            with Store(db) as store:
                store.test_source = name
                with pytest.raises(FileNotFoundError):
                    collect_into(store, "codex", tmp_path / name)
        finally:
            if name == "second":
                second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(collect_missing, "first")
        assert first_read.wait(timeout=5)
        second = pool.submit(collect_missing, "second")
        # Without a transaction, the second writer finishes while first holds a
        # stale snapshot. A correct writer blocks here until first commits.
        second_finished.wait(timeout=0.2)
        first_may_write.set()
        first.result(timeout=5)
        second.result(timeout=5)
    with Store(db) as store:
        failures = store.status()["import_failures"]
        assert len(failures) == 2
        assert len({failure["source_id"] for failure in failures}) == 2
