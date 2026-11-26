"""Archive-specific files, immutable exports and portable SQLite backups."""

import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .diagnostics import PublicInputError
from .privacy import snapshot_sha256
from .private_io import write_private_text


def workspace_for(path):
    db = Path(path).expanduser().resolve()
    return db.with_name(db.name + ".files")


def _publish_database(connection, destination, *, inspect_copy=None):
    """Check a complete private copy before making its destination visible."""
    path = Path(destination).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".memory-db-", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        dest = sqlite3.connect(temporary)
        try:
            connection.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
            if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PublicInputError("Archive integrity check failed")
        finally:
            dest.close()
        result = inspect_copy(temporary) if inspect_copy else path
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise PublicInputError("Destination already exists; select a new path") from None
    finally:
        temporary.unlink(missing_ok=True)
    return result


def _valid_title(title):
    return isinstance(title, str) and re.fullmatch(r"SEG-\d+-r\d+-[a-f0-9]{16,64}\.md", title)


def prune_completed_snapshots(store, directory):
    """Remove exact redundant work files while the caller holds the archive lock."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return []
    removed = []
    for path in directory.iterdir():
        if not _valid_title(path.name) or path.is_symlink() or not path.is_file():
            continue
        rows = store.connection.execute(
            """SELECT j.stage,j.segment_id,j.revision,o.snapshot,o.sha256
               FROM sync_jobs j LEFT JOIN outbound_revisions o
               ON o.segment_id=j.segment_id AND o.revision=j.revision WHERE j.title=?""",
            (path.name,),
        ).fetchall()
        if len(rows) != 1:
            continue
        stage, segment, revision, snapshot, sha = rows[0]
        if (
            stage not in {"done", "superseded"}
            or snapshot is None
            or not sha
            or path.name != f"{segment}-r{revision}-{sha[:16]}.md"
        ):
            continue
        try:
            if (
                path.stat().st_size == len(snapshot)
                and path.read_bytes() == snapshot
                and snapshot_sha256(snapshot) == sha
            ):
                path.unlink()
                removed.append(path.name)
        except OSError:
            continue
    return removed


def legacy_bytes(store, job):
    """Read only a filename and digest proven by this old archive's job."""
    from .store import digest

    title = job["title"]
    if not _valid_title(title):
        return None
    for directory in (store.workspace / "sync", store.path.parent / "sync"):
        file = directory / title
        if directory.is_symlink() or file.is_symlink() or not file.is_file():
            continue
        data = file.read_bytes()
        try:
            expected = digest(data.decode("utf-8"))[:16]
        except UnicodeError:
            continue
        if title == f"{job['segment_id']}-r{job['revision']}-{expected}.md":
            return data
    # Reconstruct only when both the archived revision's original content hash
    # and the job's historical filename independently match the candidate.
    from .privacy import redact_sensitive
    from .segments import SegmentBuilder

    segment = store.connection.execute(
        "SELECT revision,content_hash FROM segments WHERE id=?", (job["segment_id"],)
    ).fetchone()
    if segment and segment["revision"] == job["revision"] and segment["content_hash"]:
        original = SegmentBuilder(store).render(job["segment_id"])
        if digest(original) == segment["content_hash"]:
            for candidate in (original, redact_sensitive(original)):
                if title == f"{job['segment_id']}-r{job['revision']}-{digest(candidate)[:16]}.md":
                    return candidate.encode("utf-8")
    return None


def _snapshots(db):
    for row in db.execute("""SELECT j.id,j.stage,j.segment_id,j.revision,j.title,
        o.snapshot,o.sha256,l.snapshot AS legacy,l.title AS legacy_title
        FROM sync_jobs j LEFT JOIN outbound_revisions o
        ON o.segment_id=j.segment_id AND o.revision=j.revision
        LEFT JOIN legacy_job_snapshots l ON l.job_id=j.id
        WHERE j.stage NOT IN ('done','superseded','cleanup','missing')"""):
        title = row["title"]
        data = row["snapshot"]
        if not _valid_title(title):
            raise PublicInputError("Invalid pending snapshot filename; archive retained")
        prefix = f"{row['segment_id']}-r{row['revision']}-"
        if not title.startswith(prefix):
            raise PublicInputError("Invalid pending snapshot revision; archive retained")
        if data is not None:
            if (
                snapshot_sha256(data) != row["sha256"]
                or title != prefix + row["sha256"][:16] + ".md"
            ):
                raise PublicInputError("Pending snapshot checksum mismatch; archive retained")
        elif row["legacy"] is not None and row["legacy_title"] == title:
            from .store import digest

            data = row["legacy"]
            if title != prefix + digest(data.decode("utf-8"))[:16] + ".md":
                raise PublicInputError("Legacy snapshot checksum mismatch; archive retained")
        else:
            raise PublicInputError(
                "Pending snapshot unavailable; no incomplete backup was published"
            )
        yield title, data


def backup_archive(store, destination):
    from .archive_lock import archive_lock

    with archive_lock(store.path):
        db = store.connection
        # Keep validation and sqlite3_backup pinned to the same WAL snapshot.
        with db:
            db.execute("BEGIN")
            for _ in _snapshots(db):
                pass
            result = _publish_database(db, destination)
    return {"backup": str(result), "schema": 2}


def restore_archive(destination, backup):
    from .store import Store

    def restored_status(candidate):
        # SQLite quick_check alone can't detect missing application tables.
        with Store(candidate, existing=True) as restored:
            return restored.status()

    path = Path(destination).expanduser().absolute()
    if path.exists() or path.is_symlink() or workspace_for(path).exists():
        raise PublicInputError("Restore requires a new archive path and workspace")
    source = Path(backup).expanduser().resolve()
    if not source.is_file():
        raise PublicInputError("Backup does not exist")
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    work = workspace_for(path)
    owned = False
    try:
        with db:
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise PublicInputError("Restore requires a version 2 portable backup")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            work.mkdir(mode=0o700)
            owned = True
            for title, data in _snapshots(db):
                sync_dir = work / "sync"
                sync_dir.mkdir(mode=0o700, exist_ok=True)
                write_private_text(sync_dir / title, data.decode("utf-8"))
            status = _publish_database(db, path, inspect_copy=restored_status)
    except BaseException:
        if owned:
            shutil.rmtree(work)
        raise
    finally:
        db.close()
    return {"restored": str(path), "status": status}


def export_segments(store, builder, destination=None):
    """Stage an entire immutable export on the destination filesystem."""
    parent = (
        store.workspace / "exports" if destination is None else Path(destination).absolute().parent
    )
    parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    stage = Path(tempfile.mkdtemp(prefix=".partial-", dir=parent))
    target = (
        parent / ("export-" + stage.name.removeprefix(".partial-"))
        if destination is None
        else Path(destination).absolute()
    )
    try:
        if (
            target.is_symlink()
            or target.exists()
            and (not target.is_dir() or any(target.iterdir()))
        ):
            raise PublicInputError(
                "Export destination must be new or empty; existing files were retained"
            )
        ids = []
        with store.connection as db:
            db.execute("BEGIN")
            for row in db.execute("SELECT id FROM segments WHERE state!='retired' ORDER BY rowid"):
                ids.append(row[0])
                write_private_text(stage / (row[0] + ".md"), builder.render(row[0]))
        stage.rename(target)
        return {"segments": ids, "output": str(target)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
