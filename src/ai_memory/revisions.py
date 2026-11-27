"""Archive migrations and immutable outbound revision metadata."""

import hashlib
import hmac
import os
import sqlite3
import time

from .archive_files import _publish_database, legacy_bytes
from .diagnostics import PublicInputError
from .privacy import MASKING_POLICY, snapshot_sha256

SCHEMA_VERSION = 3

_REVISION_SCHEMA = """
CREATE TABLE outbound_revisions(segment_id TEXT NOT NULL REFERENCES segments(id),
 revision INTEGER NOT NULL, masking_policy INTEGER NOT NULL, sha256 TEXT NOT NULL,
 snapshot BLOB NOT NULL, PRIMARY KEY(segment_id,revision));
CREATE TABLE source_receipts(source_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL,revision INTEGER NOT NULL,
 masking_policy INTEGER NOT NULL,sha256 TEXT NOT NULL,verified_at REAL NOT NULL,
 remote_state TEXT NOT NULL DEFAULT 'ready');
CREATE TABLE legacy_job_snapshots(job_id INTEGER PRIMARY KEY REFERENCES sync_jobs(id),title TEXT NOT NULL,snapshot BLOB NOT NULL);
CREATE TABLE registered_sources(source_id TEXT PRIMARY KEY);
ALTER TABLE sync_jobs ADD COLUMN prior_source_ids TEXT;
ALTER TABLE sync_jobs ADD COLUMN failure_phase TEXT;
INSERT INTO settings(key,value) VALUES('receipt_key',json_quote(lower(hex(randomblob(32)))));
"""


def initialize(store, original_schema):
    """Serialize first open and migrations; back up an existing v1 before DDL."""
    db = store.connection
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise PublicInputError("Archive schema is newer than this application")
    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise PublicInputError("Unrecognized archive schema; original database retained")
            for statement in original_schema.split(";"):
                if statement.strip():
                    db.execute(statement)
        elif version == 1:
            # Another read connection sees a consistent pre-migration image
            # while this writer prevents any concurrent source modifications.
            prior = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
            try:
                dest = store.path.with_name(
                    store.path.name + ".pre-v2-" + os.urandom(4).hex() + ".sqlite"
                )
                _publish_database(prior, dest)
            finally:
                prior.close()
        elif version != 2:
            raise PublicInputError("Unsupported archive schema")
        if version in (0, 1):
            for statement in _REVISION_SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            for job in db.execute(
                "SELECT * FROM sync_jobs WHERE stage NOT IN ('done','superseded','cleanup')"
            ):
                data = legacy_bytes(store, job)
                if data is not None:
                    db.execute(
                        "INSERT INTO legacy_job_snapshots VALUES(?,?,?)",
                        (job["id"], job["title"], data),
                    )
        if version in (1, 2):
            # The retired assignments stay as history. The normal builder will
            # assign messages that no longer belong to any active segment.
            if any(row[1] == "consent" for row in db.execute("PRAGMA table_info(conversations)")):
                db.execute("ALTER TABLE conversations DROP COLUMN consent")
            db.execute("DROP TABLE IF EXISTS retained_segments")
            db.execute("DROP TABLE IF EXISTS retained_sources")
            db.execute("DELETE FROM settings WHERE key='default_publication_v3'")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def receipt_marker(store, segment_id, revision):
    key = bytes.fromhex(store.get_setting("receipt_key"))
    message = f"ai-memory-outbound\n{MASKING_POLICY}\n{segment_id}\n{revision}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def save_outbound(store, segment_id, revision, text):
    raw = text.encode("utf-8") if isinstance(text, str) else text
    sha = snapshot_sha256(raw)
    db = store.connection
    db.execute(
        "INSERT OR IGNORE INTO outbound_revisions VALUES(?,?,?,?,?)",
        (segment_id, revision, MASKING_POLICY, sha, raw),
    )
    saved = db.execute(
        "SELECT masking_policy,sha256,snapshot FROM outbound_revisions WHERE segment_id=? AND revision=?",
        (segment_id, revision),
    ).fetchone()
    if tuple(saved) != (MASKING_POLICY, sha, raw):
        raise PublicInputError("Outbound revision already has different bytes or policy")
    return sha


def attest_source(store, source_id, segment_id, revision):
    db = store.connection
    row = db.execute(
        "SELECT sha256 FROM outbound_revisions WHERE segment_id=? AND revision=? AND masking_policy=?",
        (segment_id, revision, MASKING_POLICY),
    ).fetchone()
    if row is None:
        raise PublicInputError("No validated snapshot for this revision")
    db.execute(
        """INSERT OR REPLACE INTO source_receipts(source_id,segment_id,revision,masking_policy,sha256,verified_at,remote_state)
        VALUES(?,?,?,?,?,?,'ready')""",
        (source_id, segment_id, revision, MASKING_POLICY, row[0], time.time()),
    )
