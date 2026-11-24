"""Serialize sync and notebook-binding changes for one archive."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class ArchiveBusyError(RuntimeError):
    pass


@contextmanager
def archive_lock(path: Path | str):
    """Hold the existing sync lock nonblockingly until the operation finishes.

    Callers must acquire this before reading notebook configuration. This lock
    is intentionally nonreentrant, including separate handles in one process.
    The stable lock file is never unlinked, so other workers share its inode.
    """
    lock_path = Path(str(Path(path).expanduser().resolve()) + ".sync.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveBusyError("Another archive operation or sync worker is active") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
