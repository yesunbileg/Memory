"""Deterministic Markdown sources, regenerated from the SQLite archive."""

import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .privacy import MASKING_POLICY, find_sensitive, redact_sensitive
from .revisions import receipt_marker
from .store import Store, digest


class CapacityError(RuntimeError):
    pass


class SourceBudgetError(CapacityError):
    """Validated messages need a spare source slot; merging may make room."""


def yaml_value(value) -> str:
    # JSON scalar/array syntax is valid YAML 1.2 and safely escapes user metadata.
    return json.dumps(value, ensure_ascii=False)


def message_heading(row) -> str:
    """Exact structural line shared by the renderer and citation verifier."""
    kind = {"progress": " · Progress", "thinking_summary": " · Thinking Summary"}.get(
        row["kind"], ""
    )
    return f"### {row['conversation_id']} · {row['sequence']:04d} · {row['role'].title()}{kind} · {row['timestamp'] or 'Unknown'}"


def mask_for_upload(text: str) -> str:
    """Contain a masking failure to the current field or message."""
    try:
        masked = redact_sensitive(text)
        if not find_sensitive(masked):
            return masked
        masked = "".join(
            "".join(ch if ch in "\r\n" else "*" for ch in line) if find_sensitive(line) else line
            for line in masked.splitlines(keepends=True)
        )
        if not find_sensitive(masked):
            return masked
    except Exception:
        # A detection failure must never send the original text.
        pass
    return "***"


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
        return self._render(segment_id, outbound=False)

    def render_outbound(self, segment_id: str) -> str:
        """Serialize only reviewed cloud fields; local exports retain full originals."""
        return self._render(segment_id, outbound=True)

    def _render(self, segment_id: str, *, outbound: bool) -> str:
        db = self.store.connection
        segment = db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if segment is None:
            raise ValueError("Segment does not exist")
        if outbound and (
            segment["state"] == "retired"
            or db.execute(
                """SELECT 1 FROM segment_messages sm JOIN messages m ON m.id=sm.message_id
                JOIN conversations c ON c.id=m.conversation_id WHERE sm.segment_id=? AND c.consent=0 LIMIT 1""",
                (segment_id,),
            ).fetchone()
        ):
            raise ValueError("Segment is not approved for upload")
        metadata_lines = (
            [
                f"masking_policy: {MASKING_POLICY}",
                f"archive_receipt: {receipt_marker(self.store, segment_id, segment['revision'])}",
            ]
            if outbound
            else [f"created_at: {yaml_value(segment['created_at'])}"]
        )
        lines = [
            "---",
            "schema: 1",
            f"segment_id: {segment_id}",
            *metadata_lines,
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
        # A backfilled earlier message can occupy a later arrival slot. Use
        # canonical sequence within each conversation's existing slots without
        # moving other conversations or repacking sealed segment membership.
        conversation_rows = defaultdict(list)
        for row in rows:
            conversation_rows[row["conversation_id"]].append(row)
        ordered = {
            key: iter(sorted(messages, key=lambda message: message["sequence"]))
            for key, messages in conversation_rows.items()
        }
        previous = None
        for row in rows:
            m = next(ordered[row["conversation_id"]])
            if previous != m["conversation_id"]:
                metadata = db.execute(
                    "SELECT metadata FROM segment_metadata WHERE segment_id=? AND conversation_id=?",
                    (segment_id, m["conversation_id"]),
                ).fetchone()
                c = (
                    json.loads(metadata[0])
                    if metadata
                    else self.store.conversation_metadata(m["conversation_id"])
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
                    if outbound and field not in {
                        "id",
                        "platform",
                        "title",
                        "models",
                        "created_at",
                        "updated_at",
                    }:
                        continue
                    name = "conversation_id" if field == "id" else field
                    value = c[field]
                    if outbound and isinstance(value, str):
                        value = mask_for_upload(value)
                    elif outbound and field == "models" and isinstance(value, list):
                        value = [mask_for_upload(model) for model in value]
                    lines.append(f"{name}: {yaml_value(value)}")
                lines.extend(["```", ""])
                previous = m["conversation_id"]
            heading, text = message_heading(m), m["text"]
            if outbound:
                heading, text = mask_for_upload(heading), mask_for_upload(text)
            lines.extend([heading, "", text, "", "---", ""])
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
                c = self.store.conversation_metadata(row[0])
                db.execute(
                    "INSERT OR REPLACE INTO segment_metadata VALUES(?,?,?)",
                    (segment_id, row[0], json.dumps(c, ensure_ascii=False)),
                )
        text = self.render(segment_id)
        if len(self.render_outbound(segment_id).split()) > 500_000:
            raise CapacityError(
                "Rendered segment exceeds the 500000-word source ceiling; archive retained"
            )
        value = digest(text)
        if value == state["content_hash"]:
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
        """Fast batch path; on capacity failure isolate each unsendable unit."""
        self.blocked = []
        try:
            return self._build()
        except SourceBudgetError:
            raise
        except CapacityError:
            return self._isolate_capacity()

    def _isolate_capacity(self):
        db = self.store.connection
        changed = []
        # First isolate dirty existing revisions, so an edited oversized open
        # segment cannot absorb and block unrelated conversations.
        for row in db.execute(
            "SELECT id FROM segments WHERE state!='retired' AND content_hash='' ORDER BY rowid"
        ).fetchall():
            try:
                with db:
                    if self._refresh(row[0]):
                        changed.append(
                            dict(
                                db.execute(
                                    "SELECT * FROM segments WHERE id=?", (row[0],)
                                ).fetchone()
                            )
                        )
            except CapacityError:
                with db:
                    db.execute(
                        "UPDATE segments SET state='sealed' WHERE id=? AND state='open'", (row[0],)
                    )
                self.blocked.append({"reason": "CapacityError", "segment_id": row[0]})
        pending = db.execute("""SELECT m.id,m.conversation_id FROM messages m JOIN conversations c
            ON c.id=m.conversation_id WHERE c.consent=1 AND NOT EXISTS(
            SELECT 1 FROM segment_messages sm JOIN segments s ON s.id=sm.segment_id
            WHERE sm.message_id=m.id AND s.state!='retired') ORDER BY m.rowid""").fetchall()
        for row in pending:
            try:
                changed.extend(self._build(row["id"]))
            except SourceBudgetError:
                break
            except CapacityError:
                # Retry in a fresh segment when combined metadata, not this unit, overflowed.
                with db:
                    db.execute("UPDATE segments SET state='sealed' WHERE state='open'")
                try:
                    changed.extend(self._build(row["id"]))
                    continue
                except SourceBudgetError:
                    break
                except CapacityError:
                    self.blocked.append(
                        {
                            "reason": "CapacityError",
                            "conversation_id": row["conversation_id"],
                            "message_id": row["id"],
                        }
                    )
        if self.blocked and not changed:
            raise CapacityError(
                "An approved unit exceeds the rendered or body limit; originals retained"
            )
        return changed

    def _build(self, only_message_id=None) -> list[dict]:
        db = self.store.connection
        changed = []
        budget_exhausted = False
        with db:
            pending = db.execute(
                """SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE c.consent=1 AND (? IS NULL OR m.id=?) AND NOT EXISTS(SELECT 1 FROM segment_messages sm JOIN segments s ON s.id=sm.segment_id
                WHERE sm.message_id=m.id AND s.state!='retired') ORDER BY m.rowid""",
                (only_message_id, only_message_id),
            ).fetchall()
            # Validate before assigning anything: an oversized message is always retained locally.
            if any(len(m["text"].split()) > self.max_words for m in pending):
                raise CapacityError(
                    "A message exceeds the maximum segment size; archive retained without truncation"
                )
            assigned = 0
            touched = set()
            row = db.execute("SELECT * FROM segments WHERE state='open'").fetchone()
            if row and pending:
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
                if row and used and used + words > self.target_words:
                    db.execute("UPDATE segments SET state='sealed' WHERE id=?", (row["id"],))
                    row = None
                if row is None:
                    count = db.execute(
                        "SELECT COUNT(*) FROM segments WHERE state!='retired'"
                    ).fetchone()[0]
                    if count >= self.max_active:
                        # Sync must upload/verify a merge using the spare slot before assignment resumes.
                        # Still refresh edited active segments below. Raising here
                        # would roll their revision/hash updates back and strand
                        # immutable pending snapshots behind a stale revision.
                        budget_exhausted = True
                        break
                    sid = self._new()
                    row = db.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
                    used = position = 0
                else:
                    sid = row["id"]
                db.execute("INSERT INTO segment_messages VALUES(?,?,?)", (sid, m["id"], position))
                touched.add(sid)
                position += 1
                used += words
                assigned += 1
            for row in db.execute(
                "SELECT id,content_hash FROM segments WHERE state!='retired' ORDER BY id"
            ).fetchall():
                if row["id"] not in touched and (
                    row["content_hash"] or only_message_id is not None
                ):
                    continue
                if self._refresh(row["id"]):
                    changed.append(
                        dict(
                            db.execute("SELECT * FROM segments WHERE id=?", (row["id"],)).fetchone()
                        )
                    )
        if budget_exhausted and not assigned:
            raise SourceBudgetError(
                "Active source budget reached; sync a merge before building more segments"
            )
        return changed

    def merge_oldest(self, *, include_open=False) -> str:
        db = self.store.connection
        with db:
            # A newly created merge occupies its parents' chronological position.
            candidates = db.execute(
                """SELECT s.*,MIN(m.rowid) AS first_position FROM segments s
                JOIN segment_messages sm ON s.id=sm.segment_id JOIN messages m ON m.id=sm.message_id
                WHERE (s.state='sealed' OR (? AND s.state='open')) AND s.content_hash!=''
                AND NOT EXISTS(SELECT 1 FROM sync_jobs j WHERE j.segment_id=s.id AND j.stage='missing')
                GROUP BY s.id ORDER BY first_position""",
                (include_open,),
            ).fetchall()
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
