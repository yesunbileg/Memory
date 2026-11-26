"""Local consent and cloud preview. This module never contacts a provider."""

import json
import tempfile
from pathlib import Path

from .cold_archive import cancel_for_revocation
from .diagnostics import PublicInputError
from .privacy import MASKING_POLICY, find_sensitive, snapshot_sha256


def pending(store):
    return {
        "conversations": [
            dict(row)
            for row in store.connection.execute(
                """SELECT c.id,c.title,c.platform,c.revision,COUNT(m.id) AS messages
        FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id WHERE c.consent=0
        GROUP BY c.id ORDER BY c.rowid"""
            )
        ]
    }


def update_consent(store, ids, *, approved):
    keys = list(dict.fromkeys(ids))
    db = store.connection
    with db:
        db.execute("BEGIN IMMEDIATE")
        for key in keys:
            if (
                db.execute(
                    "UPDATE conversations SET consent=? WHERE id=?", (int(approved), key)
                ).rowcount
                != 1
            ):
                raise PublicInputError("Conversation does not exist; no consent changes were saved")
            if approved:
                continue
            # A shared remote segment can no longer be uploaded as-is. Retire
            # its local assignment; still-approved co-members can be repacked.
            segments = [
                row[0]
                for row in db.execute(
                    """SELECT DISTINCT sm.segment_id FROM segment_messages sm JOIN messages m
                ON m.id=sm.message_id WHERE m.conversation_id=?""",
                    (key,),
                )
            ]
            for sid in cancel_for_revocation(store, segments):
                db.execute("INSERT OR IGNORE INTO retained_segments VALUES(?)", (sid,))
                ids_to_keep = {
                    r[0]
                    for r in db.execute(
                        "SELECT source_id FROM source_map WHERE segment_id=?", (sid,)
                    )
                }
                for job in db.execute(
                    "SELECT new_source_id,old_source_ids FROM sync_jobs WHERE segment_id=?", (sid,)
                ):
                    if job["new_source_id"]:
                        ids_to_keep.add(job["new_source_id"])
                    ids_to_keep.update(json.loads(job["old_source_ids"]))
                db.executemany(
                    "INSERT OR IGNORE INTO retained_sources VALUES(?)", [(i,) for i in ids_to_keep]
                )
                db.execute(
                    "UPDATE sync_jobs SET stage='superseded',error=NULL WHERE segment_id=? AND stage NOT IN ('done','superseded')",
                    (sid,),
                )
                db.execute("UPDATE segments SET state='retired' WHERE id=?", (sid,))
    return keys


def preview(store, key):
    """Simulate approval/build on a private SQLite copy; never grant permission."""
    from .segments import SegmentBuilder
    from .store import Store

    if not store.connection.execute("SELECT 1 FROM conversations WHERE id=?", (key,)).fetchone():
        raise PublicInputError("Conversation does not exist")
    with (
        tempfile.TemporaryDirectory(prefix="ai-memory-preview-") as temp,
        Store(Path(temp) / "preview.sqlite") as sim,
    ):
        store.connection.backup(sim.connection)
        sim.approve(key)
        builder = SegmentBuilder(sim)
        builder.build()
        segments = []
        for row in sim.connection.execute(
            """SELECT DISTINCT s.id,s.revision FROM segments s
            JOIN segment_messages sm ON sm.segment_id=s.id JOIN messages m ON m.id=sm.message_id
            WHERE s.state!='retired' AND m.conversation_id=? ORDER BY s.id""",
            (key,),
        ):
            text = builder.render_outbound(row["id"])
            # A residual credential must not appear in the preview/error output.
            blocked = bool(find_sensitive(text))
            segments.append(
                {
                    "segment_id": row["id"],
                    "revision": row["revision"],
                    "masking_policy": MASKING_POLICY,
                    "sha256": snapshot_sha256(text),
                    "text": None if blocked else text,
                    "blocked": blocked,
                }
            )
    return {
        "conversation_id": key,
        "segments": segments,
        "note": "Local only. A subsequent edit, new consent, or new import can change these bytes.",
    }
