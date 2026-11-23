"""Observe an active archive sync across processes without exposing chat content."""

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .private_io import write_private_text

_PHASES = {"prepare", "reconcile", "upload", "readiness", "cleanup"}
_SEGMENT = re.compile(r"SEG-[0-9]{3,}\Z")


def _path(db_path):
    return Path(str(Path(db_path).expanduser().resolve()) + ".sync-progress.json")


def _process_start(pid):
    try:
        # The command field can contain spaces or parentheses.
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def _locked(db_path):
    path = Path(str(Path(db_path).expanduser().resolve()) + ".sync.lock")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def read_sync_activity(db_path):
    """Return an active sync only while its exact process owns the archive lock."""
    try:
        data = json.loads(_path(db_path).read_text())
        pid = data["pid"]
        start = data["process_start"]
        phase = data["phase"]
        started = data["started_at"]
        updated = data["updated_at"]
        segment = data.get("segment_id")
        if (
            type(pid) is not int
            or pid <= 0
            or not isinstance(start, str)
            or not start
            or _process_start(pid) != start
            or phase not in _PHASES
            or type(started) not in (int, float)
            or type(updated) not in (int, float)
            or (
                segment is not None
                and (not isinstance(segment, str) or not _SEGMENT.fullmatch(segment))
            )
            or not _locked(db_path)
        ):
            return None
        result = {"phase": phase, "started_at": started, "updated_at": updated}
        if segment is not None:
            result["segment_id"] = segment
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


@contextmanager
def sync_activity(db_path):
    """Publish advisory phase updates while the caller holds the archive lock."""
    path = _path(db_path)
    pid = os.getpid()
    process_start = _process_start(pid)
    started = time.time()

    def publish(event):
        if not process_start:
            return
        payload = {
            **event,
            "pid": pid,
            "process_start": process_start,
            "started_at": started,
            "updated_at": time.time(),
        }
        try:
            write_private_text(path, json.dumps(payload))
        except OSError:
            pass

    try:
        publish({"phase": "prepare"})
        yield publish
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
