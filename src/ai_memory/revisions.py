"""Version 2 archive metadata. Existing source mappings start unverified."""

import hashlib
import hmac
import os
import sqlite3
import time

from .archive_files import _publish_database, legacy_bytes
from .diagnostics import PublicInputError
from .privacy import MASKING_POLICY, snapshot_sha256

MIGRATION = """
CREATE TABLE outbound_revisions(segment_id TEXT NOT NULL REFERENCES segments(id),
 revision INTEGER NOT NULL, masking_policy INTEGER NOT NULL, sha256 TEXT NOT NULL,
 snapshot BLOB NOT NULL, PRIMARY KEY(segment_id,revision));
CREATE TABLE source_receipts(source_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL,revision INTEGER NOT NULL,
 masking_policy INTEGER NOT NULL,sha256 TEXT NOT NULL,verified_at REAL NOT NULL,
 remote_state TEXT NOT NULL DEFAULT 'ready');
CREATE TABLE legacy_job_snapshots(job_id INTEGER PRIMARY KEY REFERENCES sync_jobs(id),title TEXT NOT NULL,snapshot BLOB NOT NULL);
CREATE TABLE retained_sources(source_id TEXT PRIMARY KEY);
CREATE TABLE registered_sources(source_id TEXT PRIMARY KEY);
CREATE TABLE retained_segments(segment_id TEXT PRIMARY KEY);
ALTER TABLE sync_jobs ADD COLUMN prior_source_ids TEXT;
ALTER TABLE sync_jobs ADD COLUMN failure_phase TEXT;
INSERT INTO settings(key,value) VALUES('receipt_key',json_quote(lower(hex(randomblob(32)))));
PRAGMA user_version=2;
"""


def initialize(store, original_schema):
    """Serialize first open and migrations; back up an existing v1 before DDL."""
    db = store.connection
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 2:
        return
    if version > 2:
        raise PublicInputError("Archive schema is newer than this application")
    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
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
        else:
            raise PublicInputError("Unsupported archive schema")
        for statement in MIGRATION.split(";"):
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
