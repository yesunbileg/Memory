"""Deterministic Markdown sources, regenerated from the SQLite archive."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Store, digest


class CapacityError(RuntimeError):
    pass


class SourceBudgetError(CapacityError):
    """Validated messages need a spare source slot; merging may make room."""


def yaml_value(value) -> str:
    # JSON scalar/array syntax is valid YAML 1.2 and safely escapes user metadata.
    return json.dumps(value, ensure_ascii=False)


class SegmentBuilder:
    def __init__(self, store: Store, target_words=150_000, max_words=450_000, max_active=49):
        if not 0 < target_words <= max_words or not 2 <= max_active <= 49:
            raise ValueError("Require 0 < target_words <= max_words and 2 <= max_active <= 49")
        self.store = store
        self.target_words = target_words
        self.max_words = max_words
        self.max_active = max_active

    def _new(self, state="open") -> str:
        db = self.store.connection
        number = db.execute("SELECT COUNT(*)+1 FROM segments").fetchone()[0]
        segment_id = f"SEG-{number:03d}"
        created_at = datetime.now(ZoneInfo("Asia/Ulaanbaatar")).isoformat()
        db.execute(
            "INSERT INTO segments(id,state,created_at) VALUES(?,?,?)",
            (segment_id, state, created_at),
        )
        return segment_id

    def render(self, segment_id: str) -> str:
        db = self.store.connection
        segment = db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if segment is None:
            raise ValueError("Segment does not exist")
        lines = [
            "---",
            "schema: 1",
            f"segment_id: {segment_id}",
            f"created_at: {yaml_value(segment['created_at'])}",
            "timezone: Asia/Ulaanbaatar",
            "---",
            "",
            f"# {segment_id}",
            "",
        ]
        rows = db.execute(
            """SELECT m.* FROM segment_messages sm JOIN messages m ON m.id=sm.message_id
            WHERE sm.segment_id=? ORDER BY sm.position""",
            (segment_id,),
        ).fetchall()
        previous = None
        for m in rows:
            if previous != m["conversation_id"]:
                metadata = db.execute(
                    "SELECT metadata FROM segment_metadata WHERE segment_id=? AND conversation_id=?",
                    (segment_id, m["conversation_id"]),
                ).fetchone()
                c = (
                    json.loads(metadata[0])
                    if metadata
                    else self.store.get_conversation(m["conversation_id"])
                )
                # Title remains fully represented in quoted YAML; heading is structural only.
                lines.extend([f"## {c['id']}", "", "```yaml"])
                for field in (
                    "id",
                    "external_id",
                    "platform",
                    "account",
                    "title",
                    "models",
                    "project",
                    "created_at",
                    "updated_at",
                    "source_url",
                ):
                    name = "conversation_id" if field == "id" else field
                    lines.append(f"{name}: {yaml_value(c[field])}")
                lines.extend(["```", ""])
                previous = m["conversation_id"]
            kind = {"progress": " · Progress", "thinking_summary": " · Thinking Summary"}.get(
                m["kind"], ""
            )
            heading = f"### {m['conversation_id']} · {m['sequence']:04d} · {m['role'].title()}{kind} · {m['timestamp'] or 'Unknown'}"
            lines.extend([heading, "", m["text"], "", "---", ""])
        return "\n".join(lines)

    def _refresh(self, segment_id: str) -> bool:
        db = self.store.connection
        state = db.execute(
            "SELECT state,content_hash FROM segments WHERE id=?", (segment_id,)
        ).fetchone()
        if state["state"] == "open" or state["content_hash"] == "":
            for row in db.execute(
                "SELECT DISTINCT m.conversation_id FROM messages m JOIN segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=?",
                (segment_id,),
            ).fetchall():
                c = self.store.get_conversation(row[0])
                del c["messages"]
                db.execute(
                    "INSERT OR REPLACE INTO segment_metadata VALUES(?,?,?)",
                    (segment_id, row[0], json.dumps(c, ensure_ascii=False)),
                )
        text = self.render(segment_id)
        if len(text.split()) > 500_000:
            raise CapacityError(
                "Rendered segment exceeds the 500000-word source ceiling; archive retained"
            )
        value = digest(text)
        old = db.execute("SELECT content_hash FROM segments WHERE id=?", (segment_id,)).fetchone()[
            0
        ]
        if value == old:
            return False
        words = db.execute(
            """SELECT m.text FROM segment_messages sm JOIN messages m ON m.id=sm.message_id
            WHERE sm.segment_id=?""",
            (segment_id,),
        ).fetchall()
        word_count = sum(len(r[0].split()) for r in words)
        if word_count > self.max_words:
            raise CapacityError(
                "Segment exceeds maximum after an edit; archive retained; split policy requires review"
            )
        db.execute(
            "UPDATE segments SET revision=revision+1,content_hash=?,word_count=? WHERE id=?",
            (value, word_count, segment_id),
        )
        return True

    def build(self) -> list[dict]:
        db = self.store.connection
        changed = []
        with db:
            pending = db.execute("""SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE c.consent=1 AND NOT EXISTS(SELECT 1 FROM segment_messages sm JOIN segments s ON s.id=sm.segment_id
                WHERE sm.message_id=m.id AND s.state!='retired') ORDER BY m.rowid""").fetchall()
            # Validate before assigning anything: an oversized message is always retained locally.
            if any(len(m["text"].split()) > self.max_words for m in pending):
                raise CapacityError(
                    "A message exceeds the maximum segment size; archive retained without truncation"
                )
            assigned = 0
            row = db.execute("SELECT * FROM segments WHERE state='open'").fetchone()
            if row:
                used = sum(
                    len(r[0].split())
                    for r in db.execute(
                        """SELECT text FROM messages m JOIN
                    segment_messages sm ON m.id=sm.message_id WHERE sm.segment_id=?""",
                        (row["id"],),
                    )
                )
                position = db.execute(
                    "SELECT COUNT(*) FROM segment_messages WHERE segment_id=?", (row["id"],)
                ).fetchone()[0]
            else:
                used = position = 0
            for m in pending:
                words = len(m["text"].split())
                if row:
                    if used and used + words > self.target_words:
                        db.execute("UPDATE segments SET state='sealed' WHERE id=?", (row["id"],))
                        row = None
                if row is None:
                    count = db.execute(
                        "SELECT COUNT(*) FROM segments WHERE state!='retired'"
                    ).fetchone()[0]
                    if count >= self.max_active:
                        # Sync must upload/verify a merge using the spare slot before assignment resumes.
                        if assigned:
                            break
                        raise SourceBudgetError(
                            "Active source budget reached; sync a merge before building more segments"
                        )
                    sid = self._new()
                    row = db.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
                    used = position = 0
                else:
                    sid = row["id"]
                db.execute("INSERT INTO segment_messages VALUES(?,?,?)", (sid, m["id"], position))
                position += 1
                used += words
                assigned += 1
            for row in db.execute(
                "SELECT id FROM segments WHERE state!='retired' ORDER BY id"
            ).fetchall():
                if self._refresh(row["id"]):
                    changed.append(
                        dict(
                            db.execute("SELECT * FROM segments WHERE id=?", (row["id"],)).fetchone()
                        )
                    )
        return changed

    def merge_oldest(self) -> str:
        db = self.store.connection
        with db:
            # A newly created merge occupies its parents' chronological position.
            candidates = db.execute("""SELECT s.*,MIN(m.rowid) AS first_position FROM segments s
                JOIN segment_messages sm ON s.id=sm.segment_id JOIN messages m ON m.id=sm.message_id
                WHERE s.state='sealed' GROUP BY s.id ORDER BY first_position""").fetchall()
            for start in range(len(candidates)):
                window, total = [], 0
                for c in candidates[start : start + 3]:
                    if total + c["word_count"] > self.max_words:
                        break
                    window.append(c)
                    total += c["word_count"]
                for size in range(len(window), 1, -1):
                    chosen = window[:size]
                    # Rendered metadata can exceed the source ceiling even when
                    # body words fit. Discard that candidate before trying a pair
                    # or a later adjacent window, preserving all parent state.
                    db.execute("SAVEPOINT merge_candidate")
                    try:
                        sid = self._new("sealed")
                        position = 0
                        for parent in chosen:
                            db.execute(
                                "INSERT INTO segment_parents VALUES(?,?)", (sid, parent["id"])
                            )
                            for m in db.execute(
                                "SELECT message_id FROM segment_messages WHERE segment_id=? ORDER BY position",
                                (parent["id"],),
                            ).fetchall():
                                db.execute(
                                    "INSERT INTO segment_messages VALUES(?,?,?)",
                                    (sid, m[0], position),
                                )
                                position += 1
                        self._refresh(sid)
                    except CapacityError:
                        db.execute("ROLLBACK TO merge_candidate")
                        db.execute("RELEASE merge_candidate")
                        continue
                    for parent in chosen:
                        db.execute(
                            "UPDATE segments SET state='retired' WHERE id=?", (parent["id"],)
                        )
                    db.execute("RELEASE merge_candidate")
                    return sid
            raise CapacityError(
                "No adjacent sealed segments fit the body and rendered source ceilings; archive retained; summarization disabled"
            )
