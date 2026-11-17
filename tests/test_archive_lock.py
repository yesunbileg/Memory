import fcntl
import os
import stat

import pytest


def test_archive_lock_uses_existing_sync_lock_and_releases_after_failure(tmp_path):
    from ai_memory.archive_lock import archive_lock

    db_path = tmp_path / "archive.sqlite"
    lock_path = tmp_path / "archive.sqlite.sync.lock"
    lock_path.touch(mode=0o644)
    original_inode = lock_path.stat().st_ino
    with pytest.raises(ValueError, match="synthetic"):
        with archive_lock(db_path):
            assert lock_path.stat().st_ino == original_inode
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
            with lock_path.open("a") as competing:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise ValueError("synthetic")
    with lock_path.open("a") as competing:
        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_archive_lock_is_private_at_creation_and_nonblocking(tmp_path):
    from ai_memory.archive_lock import archive_lock

    db_path = tmp_path / "archive.sqlite"
    mask = os.umask(0o022)
    try:
        with archive_lock(db_path):
            assert stat.S_IMODE((tmp_path / "archive.sqlite.sync.lock").stat().st_mode) == 0o600
            with pytest.raises(RuntimeError, match="active"):
                with archive_lock(db_path):
                    pytest.fail("A second archive operation acquired an occupied lock")
    finally:
        os.umask(mask)
