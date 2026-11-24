"""Wake a bounded NotebookLM publisher after a local capture commits."""

import asyncio
import fcntl
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from .archive_lock import ArchiveBusyError
from .diagnostics import failure_code

STARTED_AT = "automatic_sync_started_at"
DEBOUNCE = 600


@contextmanager
def _lock(path: Path, suffix: str, *, nonblocking=False):
    fd = os.open(str(path) + suffix, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            yield None
        else:
            yield fd
    finally:
        os.close(fd)


def kick_sync(store) -> None:
    """Wake one detached worker. Scheduler errors never undo local capture."""
    with suppress(OSError, RuntimeError, ValueError, sqlite3.Error):
        if (
            not store.get_setting("notebook_id")
            or store.get_setting("tui_auto_sync", True) is not True
        ):
            return
        with _lock(store.path, ".publish-request.lock"):
            if store.auto_sync_due() is None:
                return
            with _lock(store.path, ".publish-worker.lock", nonblocking=True) as owner:
                if owner is None:
                    return
                subprocess.Popen(
                    [sys.executable, "-m", "ai_memory.auto_publish", str(store.path), str(owner)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(owner,),
                )


async def _publish(store, owner: int) -> None:
    from .cli import sync_archive

    try:
        while True:
            with _lock(store.path, ".publish-request.lock"):
                due = store.auto_sync_due()
                if due is None:
                    # Serialize the idle decision with capture's next wake-up.
                    os.close(owner)
                    owner = -1
                    return
                started = store.get_setting(STARTED_AT, 0)
                earliest = max(due, started + DEBOUNCE) if type(started) in (int, float) else due
            delay = earliest - time.time()
            if delay > 0:
                await asyncio.sleep(min(delay, 60))
                continue
            try:
                outcome = await sync_archive(store, automatic=True)
            except ArchiveBusyError:
                await asyncio.sleep(5)
                continue
            except Exception as exc:
                if failure_code(exc) == "auth_required":
                    return
                await asyncio.sleep(5)
            else:
                if any(
                    item.get("reason") == "auth_required" for item in outcome.get("blocked", ())
                ):
                    return
    finally:
        if owner >= 0:
            os.close(owner)


def main(path: str, owner: str) -> None:
    from .store import Store

    with Store(path, existing=True) as store:
        asyncio.run(_publish(store, int(owner)))


if __name__ == "__main__":
    main(*sys.argv[1:])
