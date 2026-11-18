"""Local, non-network view of committed NotebookLM revisions and consent."""

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
