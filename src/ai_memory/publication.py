"""Archive-local queries and inventory observations for NotebookLM publication state."""

import time

from .diagnostics import PublicInputError
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


def current_segments(sources):
    return {
        row["segment_id"]
        for row in sources.values()
        if row["content_hash"] and row["source_revision"] == row["revision"]
    }


def publication_status(store, *, sources=None):
    inventory = [
        dict(row)
        for row in store.connection.execute("""
        SELECT m.segment_id,m.revision,CASE WHEN EXISTS(SELECT 1 FROM sync_jobs h WHERE h.segment_id=m.segment_id AND h.stage='missing')
        THEN 'missing' ELSE COALESCE(r.remote_state,'unverified') END AS state,r.verified_at
        FROM source_map m LEFT JOIN source_receipts r USING(source_id) ORDER BY m.segment_id""")
    ]
    return {
        "managed_sources": inventory,
        "eligible_sources": len(allowed_sources(store) if sources is None else sources),
    }


async def cleanup_legacy_sources(store, adapter, approved_ids):
    """Preview old replacements and delete only explicitly selected source IDs."""
    sources = {source["id"]: source for source in await adapter.list_sources()}
    eligible = allowed_sources(store)
    recorded = store.connection.execute("""
        SELECT old.new_source_id AS source_id, old.segment_id, old.revision, old.title,
               replacement.new_source_id AS replacement_id,
               replacement.revision AS replacement_revision, replacement.title AS replacement_title
        FROM sync_jobs replacement
        JOIN source_map current ON current.source_id=replacement.new_source_id
             AND current.segment_id=replacement.segment_id AND current.revision=replacement.revision
        JOIN json_each(replacement.old_source_ids) predecessor
        JOIN sync_jobs old ON old.new_source_id=predecessor.value
             AND old.segment_id=replacement.segment_id AND old.revision<replacement.revision
        WHERE replacement.stage='done' AND old.stage='done' AND old.prior_source_ids IS NULL
          AND EXISTS (SELECT 1 FROM json_each(replacement.prior_source_ids)
                      WHERE value=old.new_source_id)
          AND NOT EXISTS (SELECT 1 FROM source_map WHERE source_id=old.new_source_id)
          AND NOT EXISTS (SELECT 1 FROM retained_sources WHERE source_id=old.new_source_id)
        ORDER BY old.segment_id, old.revision
    """)
    checked = {}
    for row in recorded:
        replacement_id = row["replacement_id"]
        mapped = eligible.get(replacement_id)
        remote = sources.get(replacement_id, {})
        if (
            not mapped
            or mapped["revision"] != row["replacement_revision"]
            or remote.get("ready") is not True
            or remote.get("title") != row["replacement_title"]
        ):
            continue
        source_id = row["source_id"]
        if source_id in sources and sources[source_id].get("title") != row["title"]:
            continue
        checked[source_id] = {
            key: row[key]
            for key in ("source_id", "segment_id", "revision", "title", "replacement_revision")
        }

    requested = list(dict.fromkeys(approved_ids))
    if any(source_id not in checked for source_id in requested):
        raise PublicInputError(
            "An ID does not match a legacy copy with a verified ready replacement. Run cleanup-legacy to review the candidates."
        )
    removals = [sid for sid in requested if sid in sources]
    already_absent = [sid for sid in requested if sid not in sources]
    for sid in removals:
        await adapter.delete(sid)
    remaining = set(sources)
    if removals:
        remaining = {source["id"] for source in await adapter.list_sources()}
        if remaining.intersection(removals):
            raise PublicInputError(
                "NotebookLM still lists a selected legacy source. Run cleanup-legacy to check what remains."
            )
    candidates = [item for sid, item in checked.items() if sid in remaining]
    return {"candidates": candidates, "deleted": removals, "already_absent": already_absent}


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
