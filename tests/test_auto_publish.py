import os
from types import SimpleNamespace

from test_store import conversation

from ai_memory import auto_publish
from ai_memory.store import Store


def test_capture_starts_at_most_one_detached_publisher(tmp_path, monkeypatch):
    launched = []
    inherited = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        inherited.append(os.dup(kwargs["pass_fds"][0]))

    monkeypatch.setattr(auto_publish.subprocess, "Popen", popen)
    try:
        with Store(tmp_path / "archive.sqlite") as store:
            store.ingest(conversation())
            auto_publish.kick_sync(store)
            assert not launched
            store.set_setting("notebook_id", "synthetic-notebook")
            auto_publish.kick_sync(store)
            auto_publish.kick_sync(store)
            assert len(launched) == 1
            argv, options = launched[0]
            assert argv[1:3] == ["-m", "ai_memory.auto_publish"]
            assert options["start_new_session"] is True
            assert options["stdout"] == auto_publish.subprocess.DEVNULL
            os.close(inherited.pop())
            auto_publish.kick_sync(store)
            assert len(launched) == 2
    finally:
        for fd in inherited:
            os.close(fd)


async def test_worker_consumes_trailing_capture_after_the_debounce(tmp_path, monkeypatch):
    from ai_memory import cli

    clock = [1_000]
    changes = [0]
    sends = []

    async def sleep(seconds):
        clock[0] += seconds

    async def sync(_, *, automatic):
        assert automatic
        sends.append(clock[0])
        if len(sends) == 1:
            # An import lands while that first source is being verified.
            changes.append(0)
        changes.pop(0)
        store.set_setting(auto_publish.STARTED_AT, clock[0])
        return {"completed": 1}

    monkeypatch.setattr(auto_publish, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(auto_publish.asyncio, "sleep", sleep)
    monkeypatch.setattr(cli, "sync_archive", sync)
    with Store(tmp_path / "archive.sqlite") as store:
        monkeypatch.setattr(store, "auto_sync_due", lambda: changes[0] if changes else None)
        with auto_publish._lock(store.path, ".publish-worker.lock") as fd:
            await auto_publish._publish(store, os.dup(fd))
        assert sends == [1_000, 1_600]
        with auto_publish._lock(store.path, ".publish-worker.lock", nonblocking=True) as fd:
            assert fd is not None
