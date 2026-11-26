"""Pack inactive history into Markdown and switch sources only after indexing."""

import json
from collections import defaultdict

ENABLED = "cold_archive_enabled"
PLAN = "cold_archive_plan"
NEXT_CHECK = "cold_archive_next_check"
MAX_WORDS = 400_000
SOURCE_LIMIT = 50
IDLE_DAYS = 30
_AGE = "julianday(COALESCE(c.updated_at,c.created_at)) <= julianday('now', ?)"


def markdown_segment(store, segment_id):
    return (
        store.connection.execute(
            "SELECT 1 FROM checkpoints WHERE key=?", (f"cold-markdown:{segment_id}",)
        ).fetchone()
        is not None
    )


def _partition(builder, rows, *, markdown, maximum_segments):
    """Build ordered segments within both body and fully rendered word limits."""
    from .segments import CapacityError

    db = builder.store.connection
    limit = min(MAX_WORDS, builder.max_words) if markdown else builder.target_words
    segments = []

    def write(chunk):
        if len(segments) >= maximum_segments:
            raise CapacityError("Not enough spare archive segments for compaction")
        db.execute("SAVEPOINT cold_segment")
        try:
            sid = builder._new("sealed")
            if markdown:
                db.execute("INSERT INTO checkpoints VALUES(?, 'true')", (f"cold-markdown:{sid}",))
            db.executemany(
                "INSERT INTO segment_messages VALUES(?,?,?)",
                ((sid, row["id"], position) for position, row in enumerate(chunk)),
            )
            builder._refresh(sid)
        except CapacityError:
            db.execute("ROLLBACK TO cold_segment")
            db.execute("RELEASE cold_segment")
            if len(chunk) == 1:
                raise
            middle = len(chunk) // 2
            write(chunk[:middle])
            write(chunk[middle:])
        else:
            db.execute("RELEASE cold_segment")
            segments.append(sid)

    chunk, used, previous = [], 0, None
    heading_words = {}
    for row in rows:
        words = len(row["text"].split())
        chat_id = row["conversation_id"]
        if markdown and chat_id not in heading_words:
            metadata = builder.store.conversation_metadata(chat_id)
            heading_words[chat_id] = 14 + sum(
                len(str(metadata[field]).split())
                for field in ("title", "models", "created_at", "updated_at")
            )
        if markdown:
            words += 12  # Message heading and separator.
        chat_heading = heading_words[chat_id] if markdown else 0
        overhead = chat_heading if chat_id != previous else 0
        if chunk and used + words + overhead > limit:
            write(chunk)
            chunk, used = [], 0
            overhead = chat_heading
        chunk.append(row)
        used += words + overhead
        previous = chat_id
    if chunk:
        write(chunk)
    return segments


def stage_unassigned(builder):
    """Send newly captured old history directly to Markdown."""
    from .segments import CapacityError

    db = builder.store.connection
    with db:
        db.execute("BEGIN IMMEDIATE")
        free = (
            builder.max_active
            - db.execute("SELECT count(*) FROM segments WHERE state!='retired'").fetchone()[0]
        )
        if free <= 0:
            return
        rows = db.execute(
            f"""SELECT m.id,m.conversation_id,m.text FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE c.consent=1 AND {_AGE} AND NOT EXISTS (
                SELECT 1 FROM segment_messages sm JOIN segments s ON s.id=sm.segment_id
                WHERE sm.message_id=m.id AND s.state!='retired') ORDER BY m.rowid""",
            (f"-{IDLE_DAYS} days",),
        ).fetchall()
        try:
            _partition(builder, rows, markdown=True, maximum_segments=free)
        except CapacityError:
            # Leave this batch to the ordinary publisher if it cannot fit.
            db.rollback()


def _stage_group(builder, parents, replaced, free):
    from .segments import CapacityError
    from .web_docs_journal import split_document

    db = builder.store.connection
    db.execute("SAVEPOINT cold_plan")
    try:
        rows = db.execute(
            f"""SELECT DISTINCT m.rowid,m.id,m.conversation_id,m.text,({_AGE}) AS cold
            FROM segment_messages sm JOIN messages m ON sm.message_id=m.id
            JOIN conversations c ON c.id=m.conversation_id
            WHERE sm.segment_id IN (SELECT value FROM json_each(?)) ORDER BY m.rowid""",
            (f"-{IDLE_DAYS} days", json.dumps(parents)),
        ).fetchall()
        cold = _partition(
            builder,
            [row for row in rows if row["cold"]],
            markdown=True,
            maximum_segments=free,
        )
        hot = _partition(
            builder,
            [row for row in rows if not row["cold"]],
            markdown=False,
            maximum_segments=free - len(cold),
        )
        needed = len(cold) + sum(len(split_document(builder.render_outbound(sid))) for sid in hot)
        if needed > free or needed >= replaced:
            raise CapacityError(
                "Compaction cannot reduce the source count with the available slots"
            )
    except CapacityError:
        db.execute("ROLLBACK TO cold_plan")
        db.execute("RELEASE cold_plan")
        return None
    db.execute("RELEASE cold_plan")
    return cold + hot


def _candidate_groups(candidates):
    yield [row["id"] for row in candidates]
    # A mixed group requires extra replacement slots for its active chats.
    yield [row["id"] for row in candidates if row["fully_cold"]]
    ranked = sorted(
        candidates, key=lambda row: (not row["fully_cold"], row["word_count"], row["id"])
    )
    for size in range(len(ranked) - 1, 1, -1):
        yield sorted(row["id"] for row in ranked[:size])
    for row in ranked:
        yield [row["id"]]


def stage_replacement(builder, inventory):
    """Create a resumable plan only when it actually reduces NotebookLM sources."""
    from .publication import allowed_sources

    store = builder.store
    db = store.connection
    with db:
        db.execute("BEGIN IMMEDIATE")
        if store.get_setting(PLAN):
            return
        ready = {source["id"] for source in inventory if source.get("ready") is True}
        mapped = defaultdict(set)
        for source in allowed_sources(store).values():
            if source["content_hash"] and source["revision"] == source["source_revision"]:
                mapped[source["segment_id"]].add(source["source_id"])
        candidates = [
            row
            for row in db.execute(
                f"""WITH candidate_messages AS (
                    SELECT s.id,s.word_count,COALESCE(({_AGE}),0) AS cold
                    FROM segments s JOIN segment_messages sm ON s.id=sm.segment_id
                    JOIN messages m ON m.id=sm.message_id JOIN conversations c ON c.id=m.conversation_id
                    WHERE s.state IN ('sealed','open') AND c.consent=1
                    AND NOT EXISTS (SELECT 1 FROM sync_jobs j WHERE j.segment_id=s.id
                        AND j.stage NOT IN ('done','superseded'))
                )
                SELECT id,word_count,MIN(cold) AS fully_cold FROM candidate_messages
                GROUP BY id HAVING MAX(cold)=1 ORDER BY id""",
                (f"-{IDLE_DAYS} days",),
            )
            if mapped[row["id"]] and mapped[row["id"]] <= ready
        ]
        free = min(
            SOURCE_LIMIT - len(inventory),
            builder.max_active
            - db.execute("SELECT count(*) FROM segments WHERE state!='retired'").fetchone()[0],
        )
        if free <= 0 or not candidates:
            return
        attempted = set()
        for parents in _candidate_groups(candidates):
            key = tuple(parents)
            if not parents or key in attempted:
                continue
            attempted.add(key)
            replaced = len(set().union(*(mapped[sid] for sid in parents)))
            if replaced <= 1:
                continue
            if children := _stage_group(builder, parents, replaced, free):
                db.executemany(
                    "UPDATE segments SET state='sealed' WHERE id=?", ((sid,) for sid in parents)
                )
                plan = {"parents": parents, "children": children}
                store.set_setting(PLAN, plan)
                return plan


def ready_replacements(store, plan):
    from .publication import allowed_sources

    children = set(plan["children"])
    mappings = [
        row
        for row in allowed_sources(store, include_staged=True).values()
        if row["segment_id"] in children
        and row["content_hash"]
        and row["revision"] == row["source_revision"]
    ]
    if {row["segment_id"] for row in mappings} == children:
        return {row["source_id"] for row in mappings}


def cancel_for_revocation(store, affected):
    """Cancel a regrouping that would otherwise resume after consent was removed."""
    plan = store.get_setting(PLAN)
    if not plan or not set(affected).intersection(plan["parents"] + plan["children"]):
        return affected
    db = store.connection
    db.execute("DELETE FROM settings WHERE key=?", (PLAN,))
    if cleanup := plan.get("cleanup"):
        # Do not continue deleting predecessors on behalf of an invalidated plan.
        db.execute(
            "UPDATE sync_jobs SET stage='done',old_source_ids='[]',error=NULL WHERE id=?",
            (cleanup,),
        )
        return affected
    # Parents still cover the original history. Retain any remote copies already
    # written by this interrupted plan and rebuild consented material as needed.
    return list(dict.fromkeys([*affected, *plan["children"]]))
