"""Archive-local queries and inventory observations for NotebookLM publication state."""

import time

from .privacy import MASKING_POLICY


def allowed_sources(store):
    """Freeze managed, attested revisions only. Never infer ownership from filenames."""
    return {
        row["source_id"]: dict(row)
        for row in store.connection.execute(
            """
       SELECT m.source_id,m.segment_id,m.revision AS source_revision,s.revision,s.content_hash,r.sha256
       FROM source_map m JOIN segments s ON s.id=m.segment_id
       JOIN source_receipts r ON r.source_id=m.source_id AND r.segment_id=m.segment_id AND r.revision=m.revision
       JOIN outbound_revisions o ON o.segment_id=m.segment_id AND o.revision=m.revision
          AND o.masking_policy=r.masking_policy AND o.sha256=r.sha256
       WHERE s.state!='retired' AND r.masking_policy=? AND r.remote_state='ready'
         AND NOT EXISTS(SELECT 1 FROM sync_jobs h WHERE h.segment_id=s.id AND h.stage='missing')
         AND NOT EXISTS(SELECT 1 FROM retained_segments rs WHERE rs.segment_id=m.segment_id)
         AND NOT EXISTS(SELECT 1 FROM segment_messages sm JOIN messages msg ON msg.id=sm.message_id
           JOIN conversations c ON c.id=msg.conversation_id
           WHERE sm.segment_id=m.segment_id AND c.consent!=1)
       ORDER BY m.segment_id""",
            (MASKING_POLICY,),
        )
    }


def publication_status(store):
    inventory = [
        dict(row)
        for row in store.connection.execute("""
        SELECT m.segment_id,m.revision,CASE WHEN EXISTS(SELECT 1 FROM sync_jobs h WHERE h.segment_id=m.segment_id AND h.stage='missing')
        THEN 'missing' ELSE COALESCE(r.remote_state,'unverified') END AS state,r.verified_at
        FROM source_map m LEFT JOIN source_receipts r USING(source_id) ORDER BY m.segment_id""")
    ]
    return {"managed_sources": inventory, "eligible_sources": len(allowed_sources(store))}


def _missing(db, mapping):
    """Hold all revisions of the segment, not just the one absent from the provider."""
    db.execute(
        """INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id,error)
        VALUES(?,?,?,'missing',?,'remote_missing') ON CONFLICT(segment_id,revision)
        DO UPDATE SET stage='missing',error='remote_missing',next_attempt=0""",
        (
            mapping["segment_id"],
            mapping["revision"],
            f"{mapping['segment_id']}-r{mapping['revision']}-missing.md",
            mapping["source_id"],
        ),
    )
    db.execute(
        "UPDATE segments SET state='sealed' WHERE id=? AND state='open'", (mapping["segment_id"],)
    )


def observe_inventory(store, sources):
    """Record only provider-confirmed inventory observations, never on network failure."""
    db = store.connection
    now = time.time()
    available = {s["id"]: bool(s.get("ready")) for s in sources}
    with db:
        for mapping in db.execute(
            "SELECT sm.* FROM source_map sm JOIN segments s ON s.id=sm.segment_id WHERE s.state!='retired'"
        ).fetchall():
            source_id = mapping["source_id"]
            state = (
                ("ready" if available[source_id] else "unready")
                if source_id in available
                else "missing"
            )
            db.execute(
                "UPDATE source_receipts SET remote_state=?,verified_at=? WHERE source_id=?",
                (state, now, source_id),
            )
            if state == "missing":
                _missing(db, mapping)
