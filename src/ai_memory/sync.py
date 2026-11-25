"""Single-worker persistent upload → verify → map → delete state machine."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .archive_lock import archive_lock
from .diagnostics import failure_code
from .notebook import NotebookLMError
from .privacy import MASKING_POLICY, find_sensitive, redact_sensitive, snapshot_sha256
from .private_io import write_private_text
from .publication import observe_inventory
from .segments import CapacityError, SegmentBuilder, SourceBudgetError
from .store import Store, digest
from .sync_activity import sync_activity
from .web_docs_publication import DOCS_SETTING

logger = logging.getLogger(__name__)


class SourceQuota(RuntimeError):
    pass


class SpareSlotNeeded(RuntimeError):
    pass


class SnapshotError(RuntimeError):
    pass


class UploadUncertain(RuntimeError):
    pass


def _filename(segment_id: str, revision: int, text: str) -> str:
    return f"{segment_id}-r{revision}-{snapshot_sha256(text)[:16]}.md"


def republish(store, segment_id):
    """Local, explicit recovery authorization; never contacts the provider."""
    db = store.connection
    with archive_lock(store.path), db:
        row = db.execute(
            """SELECT sm.source_id FROM source_map sm
            JOIN segments s ON s.id=sm.segment_id
            WHERE sm.segment_id=? AND s.state!='retired' AND EXISTS (
            SELECT 1 FROM sync_jobs j WHERE j.segment_id=sm.segment_id AND j.stage='missing')""",
            (segment_id,),
        ).fetchone()
        if not row:
            raise ValueError("Select an active segment with a confirmed missing source")
        if store.get_setting(DOCS_SETTING):
            db.execute(
                """UPDATE google_docs_parts SET source_id=NULL,indexed_hash=NULL
                WHERE segment_id=? AND source_id IN
                    (SELECT source_id FROM source_receipts WHERE remote_state='missing')""",
                (segment_id,),
            )
        db.execute("DELETE FROM source_map WHERE segment_id=?", (segment_id,))
        db.execute("UPDATE segments SET revision=revision+1 WHERE id=?", (segment_id,))
        db.execute(
            "UPDATE sync_jobs SET stage='pending',new_source_id=NULL,next_attempt=0,error=NULL WHERE segment_id=? AND stage='missing'",
            (segment_id,),
        )
    return {
        "republish_authorized": segment_id,
        "action": "Run sync to publish the approved revision.",
    }


class SyncEngine:
    def __init__(
        self,
        store: Store,
        builder: SegmentBuilder,
        adapter,
        output_dir: Path,
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
        docs_client=None,
    ):
        self.store, self.builder, self.adapter = store, builder, adapter
        self.output_dir = Path(output_dir)
        self.progress = progress
        self._activity = None
        self.docs_client = docs_client
        self.docs_write_payload_bytes = 0

    def _emit_progress(self, phase: str, segment_id: str | None = None) -> None:
        event = {"phase": phase}
        if segment_id and re.fullmatch(r"SEG-[0-9]{3,}", segment_id):
            event["segment_id"] = segment_id
        if self._activity:
            self._activity(event)
        if self.progress:
            try:
                self.progress(event)
            except Exception:
                # Progress delivery is advisory. A closed consumer must not alter the queue.
                self.progress = None

    def _prepare_snapshot(self, segment_id: str, revision: int) -> tuple[str, str, str]:
        """Validate the local render, then make its upload-only redacted snapshot."""
        self._emit_progress("prepare", segment_id)
        segment = self.store.connection.execute(
            "SELECT revision,content_hash FROM segments WHERE id=?", (segment_id,)
        ).fetchone()
        if segment is None or segment["revision"] != revision:
            raise SnapshotError("Segment changed before its snapshot was validated")
        original = self.builder.render(segment_id)
        if digest(original) != segment["content_hash"]:
            raise SnapshotError("Segment changed before its snapshot was validated")
        text = self.builder.render_outbound(segment_id)
        if len(text.split()) > 500_000:
            raise SnapshotError("Redacted snapshot exceeds the source word ceiling")
        title = _filename(segment_id, revision, text)
        return original, text, title

    def _write_snapshot(self, title: str, text: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.output_dir / title
        write_private_text(path, text)
        return path

    def _migrate_pending_snapshot(self, job):
        """Replace a legacy unredacted pending snapshot after validating it exactly."""
        original, safe_text, safe_title = self._prepare_snapshot(job["segment_id"], job["revision"])
        old_title = job["title"]
        old_path = self.output_dir / old_title
        old_title_for_original = (
            f"{job['segment_id']}-r{job['revision']}-{digest(original)[:16]}.md"
        )
        if not old_path.exists():
            saved = self.store.connection.execute(
                "SELECT snapshot FROM legacy_job_snapshots WHERE job_id=? AND title=?",
                (job["id"], old_title),
            ).fetchone()
            if saved:
                self._write_snapshot(old_title, bytes(saved[0]).decode("utf-8"))
        self.store.save_outbound(job["segment_id"], job["revision"], safe_text)
        if old_path.exists():
            current_text = old_path.read_bytes().decode("utf-8")
            if current_text == safe_text:
                if old_title == safe_title:
                    return
                raise SnapshotError("Pending redacted snapshot title does not match its content")
            if current_text != original or old_title != old_title_for_original:
                raise SnapshotError("Pending snapshot differs from its validated revision")

        safe_path = self.output_dir / safe_title
        if safe_path.exists() and safe_path != old_path:
            if safe_path.read_bytes().decode("utf-8") != safe_text:
                raise SnapshotError("Redacted snapshot path contains different content")
        else:
            self._write_snapshot(safe_title, safe_text)

        if old_title != safe_title:
            with self.store.connection:
                self.store.connection.execute(
                    """UPDATE sync_jobs SET title=?,stage='pending',error=NULL,next_attempt=0,attempts=0
                    WHERE id=?""",
                    (safe_title, job["id"]),
                )
        if old_path != safe_path:
            old_path.unlink(missing_ok=True)

    def _carry_old_sources(self, segment_id, source_ids):
        """Attach cleanup IDs to the next live replacement in a segment lineage."""
        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            return True
        row = self.store.connection.execute(
            """WITH RECURSIVE descendants(id,depth) AS (
                   SELECT ?,0
                   UNION ALL
                   SELECT sp.segment_id,d.depth+1 FROM segment_parents sp
                   JOIN descendants d ON sp.parent_id=d.id
               )
               SELECT j.id,j.old_source_ids FROM sync_jobs j
               JOIN segments s ON s.id=j.segment_id
               JOIN descendants d ON d.id=j.segment_id
               WHERE s.state!='retired' AND j.revision=s.revision
                 AND j.stage NOT IN ('missing','superseded')
                 AND (j.stage!='done' OR EXISTS (
                       SELECT 1 FROM source_map sm JOIN source_receipts r USING(source_id)
                       WHERE sm.segment_id=j.segment_id AND sm.revision=j.revision
                       AND sm.source_id=j.new_source_id AND r.remote_state='ready'))
               ORDER BY (j.stage='done'),d.depth,j.id DESC LIMIT 1""",
            (segment_id,),
        ).fetchone()
        if row is None:
            return False
        old_ids = list(dict.fromkeys(json.loads(row["old_source_ids"]) + source_ids))
        with self.store.connection:
            self.store.connection.execute(
                """UPDATE sync_jobs SET old_source_ids=?,
                   stage=CASE WHEN stage='done' THEN 'cleanup' ELSE stage END
                   WHERE id=?""",
                (json.dumps(old_ids), row["id"]),
            )
        return True

    async def _migrate_inflight_snapshot(self, job):
        """Restart an unredacted in-flight upload as a safe replacement."""
        db = self.store.connection
        current = db.execute(
            "SELECT revision,state FROM segments WHERE id=?", (job["segment_id"],)
        ).fetchone()
        saved = db.execute(
            "SELECT snapshot FROM outbound_revisions WHERE segment_id=? AND revision=? AND masking_policy=?",
            (job["segment_id"], job["revision"], MASKING_POLICY),
        ).fetchone()
        if saved:
            # Current-format jobs already have immutable validated bytes independently of files.
            path = self.output_dir / job["title"]
            if not path.exists():
                self._write_snapshot(job["title"], bytes(saved[0]).decode("utf-8"))
            return
        old_ids = json.loads(job["old_source_ids"])
        if job["new_source_id"]:
            # A persisted registration ID, not a filename, proves ownership of old content.
            self._emit_progress("reconcile", job["segment_id"])
            remote = next(
                (s for s in await self.adapter.list_sources() if s["id"] == job["new_source_id"]),
                None,
            )
            if remote and remote["title"] == job["title"] and self._owns(job["new_source_id"]):
                old_ids.append(job["new_source_id"])
        elif job["stage"] in ("uploading", "uncertain"):
            # v1 has no pre-registration inventory or receipt. Quarantine the old
            # attempt but allow the new local revision to make progress.
            with db:
                if current["revision"] == job["revision"]:
                    db.execute(
                        "UPDATE segments SET revision=revision+1 WHERE id=?", (job["segment_id"],)
                    )
                db.execute("UPDATE sync_jobs SET stage='uncertain' WHERE id=?", (job["id"],))
            raise UploadUncertain("Legacy upload lacks ownership evidence")
        if current["revision"] != job["revision"] or current["state"] == "retired":
            if old_ids and not self._carry_old_sources(job["segment_id"], old_ids):
                raise SnapshotError("Stale upload has no active replacement")
            with db:
                db.execute(
                    "UPDATE sync_jobs SET stage='superseded',error=NULL WHERE id=?", (job["id"],)
                )
            job["stage"] = "superseded"
            return
        _, safe_text, safe_title = self._prepare_snapshot(job["segment_id"], job["revision"])
        self._write_snapshot(safe_title, safe_text)
        with db:
            self.store.save_outbound(job["segment_id"], job["revision"], safe_text)
            db.execute(
                """UPDATE sync_jobs SET title=?,stage='pending',new_source_id=NULL,
                 old_source_ids=?,error=NULL,next_attempt=0 WHERE id=?""",
                (safe_title, json.dumps(list(dict.fromkeys(old_ids))), job["id"]),
            )
        if job["title"] != safe_title:
            (self.output_dir / job["title"]).unlink(missing_ok=True)
        job.update(
            title=safe_title,
            stage="pending",
            new_source_id=None,
            old_source_ids=json.dumps(old_ids),
        )

    def _migrate_mapped_sources(self):
        """Schedule a replacement when an older archive mapped an unredacted source."""
        db = self.store.connection
        with db:
            db.execute(
                """UPDATE segments SET revision=revision+1
                WHERE state!='retired' AND NOT EXISTS(SELECT 1 FROM sync_jobs j WHERE j.segment_id=segments.id AND j.stage='missing') AND EXISTS (SELECT 1 FROM source_map sm
                  WHERE sm.segment_id=segments.id AND sm.revision=segments.revision
                    AND NOT EXISTS (SELECT 1 FROM source_receipts r WHERE r.source_id=sm.source_id
                    AND r.masking_policy=?))""",
                (MASKING_POLICY,),
            )

    async def _recover_terminal_upload_failure(self, job):
        """Retry a known failed upload body without adopting or deleting its partial source."""
        if (
            job["stage"] != "processing"
            or not job["new_source_id"]
            or job["failure_phase"] != "upload"
            or job["error"] not in {"network_error", "timeout"}
        ):
            return
        partial_id = job["new_source_id"]
        self._emit_progress("reconcile", job["segment_id"])
        sources = await self.adapter.list_sources()
        partial = next((source for source in sources if source["id"] == partial_id), None)
        if partial and partial["title"] != job["title"]:
            mapped = self.store.connection.execute(
                "SELECT 1 FROM source_map WHERE source_id=?", (partial_id,)
            ).fetchone()
            if mapped:
                raise SnapshotError("Registered source title mismatch; mapped source retained")
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE sync_jobs SET stage='pending',new_source_id=NULL WHERE id=?",
                    (job["id"],),
                )
            job.update(stage="pending", new_source_id=None)
            return
        if partial and partial.get("ready"):
            return

        old_ids = list(dict.fromkeys(json.loads(job["old_source_ids"]) + [partial_id]))
        with self.store.connection:
            self.store.connection.execute(
                """UPDATE sync_jobs SET stage='pending',new_source_id=NULL,old_source_ids=?
                WHERE id=?""",
                (json.dumps(old_ids), job["id"]),
            )
        job.update(
            stage="pending",
            new_source_id=None,
            old_source_ids=json.dumps(old_ids),
        )

    def _enqueue(self):
        db = self.store.connection
        self._migrate_mapped_sources()
        with db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("""SELECT s.* FROM segments s LEFT JOIN source_map sm ON s.id=sm.segment_id
                WHERE s.state!='retired' AND s.content_hash!='' AND NOT EXISTS(SELECT 1 FROM sync_jobs j WHERE j.segment_id=s.id AND j.stage='missing') AND (sm.source_id IS NULL OR sm.revision!=s.revision) ORDER BY s.rowid""").fetchall():
                existing = db.execute(
                    "SELECT * FROM sync_jobs WHERE segment_id=? AND revision=?",
                    (row["id"], row["revision"]),
                ).fetchone()
                if existing:
                    if (
                        existing["stage"] in ("pending", "blocked")
                        and not existing["new_source_id"]
                    ):
                        self._migrate_pending_snapshot(existing)
                    continue
                _, text, title = self._prepare_snapshot(row["id"], row["revision"])
                self.store.save_outbound(row["id"], row["revision"], text)
                db.execute(
                    "INSERT INTO sync_jobs(segment_id,revision,title) VALUES(?,?,?)",
                    (row["id"], row["revision"], title),
                )
                # Keep immutable redacted snapshots only while a revision needs sync/reconciliation.
                self._write_snapshot(title, text)

    def _owns(self, source_id):
        return bool(
            self.store.connection.execute(
                """SELECT 1 FROM registered_sources WHERE source_id=?
               UNION SELECT 1 FROM source_receipts WHERE source_id=?""",
                (source_id, source_id),
            ).fetchone()
        )

    def _old_sources(self, segment_id):
        sources = [
            r[0]
            for r in self.store.connection.execute(
                """WITH RECURSIVE ancestors(id) AS (
            SELECT ? UNION SELECT sp.parent_id FROM segment_parents sp JOIN ancestors a ON sp.segment_id=a.id)
            SELECT sm.source_id FROM source_map sm JOIN ancestors a ON a.id=sm.segment_id""",
                (segment_id,),
            )
        ]
        if self.docs_client is not None:
            sources.extend(
                r[0]
                for r in self.store.connection.execute(
                    """WITH RECURSIVE ancestors(id) AS (
                SELECT ? UNION SELECT sp.parent_id FROM segment_parents sp JOIN ancestors a ON sp.segment_id=a.id)
                SELECT p.source_id FROM google_docs_parts p JOIN ancestors a ON a.id=p.segment_id
                WHERE p.source_id IS NOT NULL""",
                    (segment_id,),
                )
            )
        return list(dict.fromkeys(sources))

    async def _publish_docs(self, job):
        from .web_docs_publication import DocsPublisher
        from .web_docs_transport import DocsWebError

        db = self.store.connection
        segment, revision = job["segment_id"], job["revision"]
        snapshot = db.execute(
            """SELECT snapshot,sha256,masking_policy FROM outbound_revisions
            WHERE segment_id=? AND revision=?""",
            (segment, revision),
        ).fetchone()
        if not snapshot or snapshot["masking_policy"] != MASKING_POLICY:
            raise SnapshotError("Missing approved snapshot for Google Docs")
        text = bytes(snapshot["snapshot"]).decode("utf-8")
        if (
            snapshot_sha256(text) != snapshot["sha256"]
            or _filename(segment, revision, text) != job["title"]
            or redact_sensitive(text) != text
            or find_sensitive(text)
        ):
            raise SnapshotError("Google Docs snapshot failed validation")
        old = list(dict.fromkeys(json.loads(job["old_source_ids"]) + self._old_sources(segment)))
        with db:
            db.execute("BEGIN IMMEDIATE")
            if self._pending_snapshot_changed(job["id"]):
                raise SnapshotError("Pending segment changed before Google Docs publication")
        publisher = DocsPublisher(self.store, self.adapter, self.docs_client, self._emit_progress)
        try:
            sources = await publisher.publish(segment, text)
        except DocsWebError as error:
            if error.code == "SourceQuota":
                raise SourceQuota(str(error)) from None
            raise
        finally:
            self.docs_write_payload_bytes += publisher.write_payload_bytes
        # Publication eligibility changes atomically with all parts of this revision.
        with db:
            db.execute("BEGIN IMMEDIATE")
            if self._pending_snapshot_changed(job["id"]):
                raise SnapshotError("Segment changed during Google Docs publication")
            for source in sources:
                self.store.attest_source(source, segment, revision)
            db.execute(
                "INSERT OR REPLACE INTO source_map VALUES(?,?,?)", (segment, sources[0], revision)
            )
            db.execute(
                """UPDATE google_docs_publications SET ready_revision=?,updating=0,part_count=?
                WHERE segment_id=?""",
                (revision, len(sources), segment),
            )
            db.execute(
                """UPDATE sync_jobs SET stage='cleanup',new_source_id=?,old_source_ids=?,
                error=NULL WHERE id=?""",
                (sources[0], json.dumps([sid for sid in old if sid not in sources]), job["id"]),
            )
        return dict(db.execute("SELECT * FROM sync_jobs WHERE id=?", (job["id"],)).fetchone())

    def _requeue_failed_mapping(self, mapping):
        """Requeue a confirmed processing failure without losing cleanup obligations."""
        db = self.store.connection
        with db:
            db.execute("DELETE FROM source_map WHERE source_id=?", (mapping["source_id"],))
            job = db.execute(
                "SELECT * FROM sync_jobs WHERE segment_id=? AND new_source_id=?",
                (mapping["segment_id"], mapping["source_id"]),
            ).fetchone()
            if job:
                db.execute(
                    """UPDATE sync_jobs SET stage='pending',new_source_id=NULL,
                    error=NULL,next_attempt=0,attempts=0 WHERE id=?""",
                    (job["id"],),
                )
        self._enqueue()
        # Keep the failed ID until its replacement is verified.
        self._carry_old_sources(mapping["segment_id"], [mapping["source_id"]])

    async def _reconcile_mapped_sources(self):
        mappings = self.store.connection.execute(
            "SELECT * FROM source_map ORDER BY segment_id"
        ).fetchall()
        if not mappings:
            return
        # An inventory exception is unknown state, never proof of absence.
        sources = {source["id"]: source for source in await self.adapter.list_sources()}
        observe_inventory(self.store, list(sources.values()))
        for mapping in mappings:
            source = sources.get(mapping["source_id"])
            if source is not None and source.get("ready") is not True:
                # Readiness retries belong to the persistent queue, including
                # their backoff. A finished mapping may need checking again even
                # though its upload-only snapshot was already removed.
                db = self.store.connection
                with db:
                    db.execute(
                        """INSERT OR IGNORE INTO sync_jobs
                        (segment_id,revision,title,stage,new_source_id)
                        VALUES(?,?,?,'cleanup',?)""",
                        (
                            mapping["segment_id"],
                            mapping["revision"],
                            f"{mapping['segment_id']}-r{mapping['revision']}-check.md",
                            mapping["source_id"],
                        ),
                    )
                    db.execute(
                        """UPDATE sync_jobs SET stage='cleanup',new_source_id=?,
                        error=NULL,attempts=0,next_attempt=0
                        WHERE segment_id=? AND revision=? AND stage='done'""",
                        (mapping["source_id"], mapping["segment_id"], mapping["revision"]),
                    )

    async def _prove_uncertain_registration(
        self, job, sources, text: str, excluded_ids: set[str]
    ) -> str:
        """A title selects candidates; only newly observed IDs with exact content prove a match."""
        matches = []
        if job["prior_source_ids"] is not None:
            before = set(json.loads(job["prior_source_ids"])) | excluded_ids
            for source in sources:
                source_id = source["id"]
                if source_id in before or source["title"] != job["title"]:
                    continue
                if await self.adapter.get_source_text(source_id) == text:
                    matches.append(source_id)
        if len(matches) != 1:
            raise UploadUncertain("Could not prove exactly one upload; no retry or deletion")
        return matches[0]

    def _record_registration(self, job, source_id, old_ids):
        db = self.store.connection
        job.update(new_source_id=source_id, old_source_ids=json.dumps(old_ids))
        with db:
            db.execute("INSERT OR IGNORE INTO registered_sources VALUES(?)", (source_id,))
            db.execute(
                """UPDATE sync_jobs SET stage='registered',new_source_id=?,old_source_ids=?,
                failure_phase=NULL WHERE id=?""",
                (source_id, job["old_source_ids"], job["id"]),
            )

    async def _upload_and_record(self, job, path, old_ids):
        db = self.store.connection
        try:
            source_id = await self.adapter.upload(path, job["title"])
        except Exception as exc:
            # notebooklm-py carries registration IDs on partial upload failures.
            partial = getattr(exc, "source_id", None)
            definitely_not_sent = getattr(exc, "definitely_not_sent", False)
            with db:
                if isinstance(partial, str) and partial:
                    db.execute("INSERT OR IGNORE INTO registered_sources VALUES(?)", (partial,))
                    db.execute(
                        """UPDATE sync_jobs SET new_source_id=?,stage='processing',failure_phase=?,error=?
                        WHERE id=?""",
                        (
                            partial,
                            "upload" if definitely_not_sent else "finalize_uncertain",
                            failure_code(exc),
                            job["id"],
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE sync_jobs SET stage=? WHERE id=?",
                        ("pending" if definitely_not_sent else "uncertain", job["id"]),
                    )
            raise
        self._record_registration(job, source_id, old_ids)

    async def _finish_registration_before_stopping(self, job, path, old_ids):
        # Keep the lock and adapter until the in-flight write has recorded its outcome.
        operation = asyncio.create_task(self._upload_and_record(job, path, old_ids))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(operation)
            raise

    def _pending_snapshot_changed(self, job_id: int) -> bool:
        """An unsent job can be rebuilt after capture changes its local segment."""
        return bool(
            self.store.connection.execute(
                """SELECT 1 FROM sync_jobs j JOIN segments s ON s.id=j.segment_id
                   WHERE j.id=? AND j.new_source_id IS NULL
                     AND j.stage IN ('pending','blocked','superseded')
                     AND (j.stage='superseded' OR s.state='retired'
                          OR s.revision!=j.revision OR s.content_hash='')""",
                (job_id,),
            ).fetchone()
        )

    async def _process(self, job) -> bool:
        db = self.store.connection
        job = dict(job)
        path = self.output_dir / job["title"]
        current = db.execute(
            "SELECT revision,state FROM segments WHERE id=?", (job["segment_id"],)
        ).fetchone()
        if (
            job["stage"] in ("pending", "blocked")
            and not job["new_source_id"]
            and (current["revision"] != job["revision"] or current["state"] == "retired")
        ):
            old_ids = json.loads(job["old_source_ids"])
            if old_ids and not self._carry_old_sources(job["segment_id"], old_ids):
                raise SnapshotError("Stale upload cleanup has no active replacement job")
            with db:
                db.execute("UPDATE sync_jobs SET stage='superseded' WHERE id=?", (job["id"],))
            path.unlink(missing_ok=True)
            return False
        if job["stage"] in ("uploading", "processing", "uncertain"):
            await self._migrate_inflight_snapshot(job)
            if job["stage"] == "superseded":
                return False
            path = self.output_dir / job["title"]
        await self._recover_terminal_upload_failure(job)
        if self.docs_client is not None and job["stage"] in ("pending", "blocked"):
            job = await self._publish_docs(job)
        if job["stage"] != "cleanup":
            if job["stage"] not in {"registered", "processing", "ready"}:
                # No local text leaves the machine until every byte (including metadata) passes.
                if not path.exists():
                    if current["revision"] != job["revision"]:
                        raise RuntimeError("Pending revision snapshot missing; archive retained")
                    self._migrate_pending_snapshot(job)
                    job["title"] = db.execute(
                        "SELECT title FROM sync_jobs WHERE id=?", (job["id"],)
                    ).fetchone()[0]
                    path = self.output_dir / job["title"]
                text = path.read_bytes().decode("utf-8")
                expected = _filename(job["segment_id"], job["revision"], text)
                if expected != job["title"] or len(text.split()) > 500_000:
                    raise SnapshotError("Pending snapshot differs from its validated revision")
                if not (
                    job["stage"] in ("uploading", "uncertain")
                    and current["revision"] != job["revision"]
                ):
                    _, expected_text, expected_title = self._prepare_snapshot(
                        job["segment_id"], job["revision"]
                    )
                    if text != expected_text or job["title"] != expected_title:
                        raise SnapshotError("Pending snapshot differs from its validated revision")
                if redact_sensitive(text) != text or find_sensitive(text):
                    raise SnapshotError("Pending snapshot failed its masking check")
                if not job["new_source_id"]:
                    self._emit_progress("reconcile", job["segment_id"])
                    sources = await self.adapter.list_sources()
                    excluded_ids = set(json.loads(job["old_source_ids"]))
                    old = list(
                        dict.fromkeys(
                            json.loads(job["old_source_ids"]) + self._old_sources(job["segment_id"])
                        )
                    )
                    if job["stage"] in {"uploading", "uncertain"}:
                        new_id = await self._prove_uncertain_registration(
                            job, sources, text, excluded_ids
                        )
                        self._record_registration(job, new_id, old)
                    else:
                        # Account for foreign sources too, and reserve the 50th slot for replacement.
                        if len(sources) >= 50 and job["error"] in {"network_error", "timeout"}:
                            terminal_partials = [
                                source
                                for source in sources
                                if source["id"] in excluded_ids
                                and self._owns(source["id"])
                                and source["title"] == job["title"]
                                and not source.get("ready")
                                and not db.execute(
                                    "SELECT 1 FROM source_map WHERE source_id=?",
                                    (source["id"],),
                                ).fetchone()
                            ]
                            if terminal_partials:
                                self._emit_progress("cleanup", job["segment_id"])
                            for partial in terminal_partials:
                                await self.adapter.delete(partial["id"])
                            if terminal_partials:
                                sources = await self.adapter.list_sources()
                        if len(sources) >= 50:
                            raise SourceQuota(
                                "No free upload slot; existing remote sources preserved"
                            )
                        if not old and len(sources) >= 49:
                            raise SpareSlotNeeded(
                                "Merge archive sources before growing into the spare slot"
                            )
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            if self._pending_snapshot_changed(job["id"]):
                                raise SnapshotError("Pending segment changed before upload")
                            db.execute(
                                "UPDATE sync_jobs SET stage='uploading',old_source_ids=?,prior_source_ids=?,failure_phase=NULL WHERE id=?",
                                (
                                    json.dumps(old),
                                    json.dumps([s["id"] for s in sources]),
                                    job["id"],
                                ),
                            )
                        self._emit_progress("upload", job["segment_id"])
                        await self._finish_registration_before_stopping(job, path, old)
            if not self._owns(job["new_source_id"]):
                raise SnapshotError("No ownership receipt for the registered source")
            if job["stage"] == "ready":
                self._emit_progress("readiness", job["segment_id"])
                inventory = await self.adapter.list_sources()
                observed = next(
                    (source for source in inventory if source["id"] == job["new_source_id"]), None
                )
                if observed is None:
                    raise SnapshotError("Previously ready source disappeared before mapping")
                ready = observed.get("ready") is True
            else:
                ready = False
            if not ready:
                with db:
                    db.execute(
                        "UPDATE sync_jobs SET stage='processing',failure_phase='readiness' WHERE id=?",
                        (job["id"],),
                    )
                self._emit_progress("readiness", job["segment_id"])
                await self.adapter.wait_ready(job["new_source_id"])
                with db:
                    db.execute(
                        "UPDATE sync_jobs SET stage='ready',error=NULL WHERE id=?", (job["id"],)
                    )
            # This transaction makes restart pick cleanup, never a second upload.
            with db:
                if current["revision"] != job["revision"] or current["state"] == "retired":
                    if not self._carry_old_sources(
                        job["segment_id"],
                        [job["new_source_id"], *json.loads(job["old_source_ids"])],
                    ):
                        raise SnapshotError("Replacement cleanup pending")
                    db.execute(
                        "UPDATE sync_jobs SET stage='superseded',error=NULL WHERE id=?",
                        (job["id"],),
                    )
                    return False
                self.store.attest_source(job["new_source_id"], job["segment_id"], job["revision"])
                db.execute(
                    "INSERT OR REPLACE INTO source_map(segment_id,source_id,revision) VALUES(?,?,?)",
                    (job["segment_id"], job["new_source_id"], job["revision"]),
                )
                db.execute(
                    "UPDATE sync_jobs SET stage='cleanup',error=NULL WHERE id=?", (job["id"],)
                )
        self._emit_progress("cleanup", job["segment_id"])
        old_ids = json.loads(job["old_source_ids"])
        sources = {s["id"]: s for s in await self.adapter.list_sources()}
        replacement_ids = [job["new_source_id"]]
        if self.docs_client is not None:
            replacement_ids.extend(
                row[0]
                for row in db.execute(
                    """SELECT p.source_id FROM google_docs_parts p
                    JOIN google_docs_publications d USING(segment_id)
                    WHERE p.segment_id=? AND d.ready_revision=? AND d.updating=0
                      AND p.part>0 AND p.part<d.part_count ORDER BY p.part""",
                    (job["segment_id"], job["revision"]),
                )
            )
        if any(source_id not in sources for source_id in replacement_ids):
            observe_inventory(self.store, list(sources.values()))
            raise SnapshotError(
                "Replacement missing; predecessors retained until explicit republish"
            )
        for source_id in replacement_ids:
            if sources[source_id].get("ready") is True:
                continue
            self._emit_progress("readiness", job["segment_id"])
            try:
                await self.adapter.wait_ready(source_id)
            except Exception as exc:
                if self.docs_client is None and failure_code(exc) == "processing_failed":
                    # A confirmed failed replacement is not a valid mapping.
                    # Retain it and every predecessor until a new source is ready.
                    self._requeue_failed_mapping(
                        {"segment_id": job["segment_id"], "source_id": job["new_source_id"]}
                    )
                raise
            self._emit_progress("cleanup", job["segment_id"])
        with db:
            db.executemany(
                """UPDATE source_receipts SET remote_state='ready',verified_at=?
                   WHERE source_id=? AND segment_id=? AND revision=? AND remote_state!='ready'""",
                [(time.time(), sid, job["segment_id"], job["revision"]) for sid in replacement_ids],
            )
        if old_ids:
            present = set(sources)
            for old_id in old_ids:
                if (
                    not self._owns(old_id)
                    or old_id == job["new_source_id"]
                    or db.execute(
                        "SELECT 1 FROM retained_sources WHERE source_id=?", (old_id,)
                    ).fetchone()
                ):
                    continue
                if old_id in present:
                    await self.adapter.delete(old_id)
                with db:
                    db.execute("DELETE FROM source_map WHERE source_id=?", (old_id,))
                    if self.docs_client is not None:
                        db.execute(
                            "UPDATE google_docs_parts SET source_id=NULL,indexed_hash=NULL WHERE source_id=?",
                            (old_id,),
                        )
        with db:
            db.execute(
                "UPDATE sync_jobs SET stage='done',error=NULL,next_attempt=0 WHERE id=?",
                (job["id"],),
            )
        path.unlink(missing_ok=True)
        return True

    async def _drain_queue(self, result, changed, *, segment_ids=None):
        db = self.store.connection
        # Reconciliation/cleanup for retired parents always finishes before later uploads.
        jobs = db.execute("""SELECT * FROM sync_jobs j WHERE stage NOT IN ('done','superseded','missing') AND NOT EXISTS (SELECT 1 FROM sync_jobs hold WHERE hold.segment_id=j.segment_id AND hold.stage='missing')
            ORDER BY CASE WHEN stage IN ('uploading','uncertain','registered','processing','ready','cleanup') THEN 0
            WHEN EXISTS(SELECT 1 FROM segment_parents p WHERE p.segment_id=j.segment_id) THEN 1 ELSE 2 END,id""").fetchall()
        for job in jobs:
            job = db.execute("SELECT * FROM sync_jobs WHERE id=?", (job["id"],)).fetchone()
            if job is None or job["stage"] in ("done", "superseded"):
                continue
            if segment_ids is not None and job["segment_id"] not in segment_ids:
                continue
            if job["next_attempt"] > time.time() and not self._pending_snapshot_changed(job["id"]):
                result["deferred"] += 1
                continue
            try:
                if await self._process(job):
                    result["completed"] += 1
            except SpareSlotNeeded:
                return "merge"
            except Exception as exc:
                if isinstance(exc, SnapshotError) and self._pending_snapshot_changed(job["id"]):
                    changed.add(job["segment_id"])
                    continue
                kind = failure_code(exc)
                if kind == "processing_failed":
                    latest = db.execute(
                        "SELECT new_source_id,stage FROM sync_jobs WHERE id=?", (job["id"],)
                    ).fetchone()
                    failed = latest["new_source_id"]
                    mapped = db.execute(
                        "SELECT 1 FROM source_map WHERE source_id=?", (failed,)
                    ).fetchone()
                    if (
                        failed
                        and self._owns(failed)
                        and not mapped
                        and latest["stage"] == "processing"
                    ):
                        try:
                            self._emit_progress("cleanup", job["segment_id"])
                            present = {s["id"] for s in await self.adapter.list_sources()}
                            if failed in present:
                                await self.adapter.delete(failed)
                            with db:
                                db.execute(
                                    "UPDATE sync_jobs SET stage='pending',new_source_id=NULL WHERE id=?",
                                    (job["id"],),
                                )
                        except Exception:
                            # Retry deletion next time; never discard a committed source.
                            kind = "failed_source_cleanup"
                next_attempt = time.time() + min(3600, 30 * 2 ** min(job["attempts"], 7))
                with db:
                    db.execute(
                        """UPDATE sync_jobs SET attempts=attempts+1,next_attempt=?,error=? WHERE id=?""",
                        (next_attempt, kind, job["id"]),
                    )
                failure = {
                    "segment_id": job["segment_id"],
                    "reason": kind,
                    "attempts": job["attempts"] + 1,
                    "next_attempt": next_attempt,
                }
                result["blocked"].append(failure)
                # The persisted deadline suppresses repeated scan messages, including restarts.
                logger.warning(
                    "Sync %s: %s; retry at %.0f (attempt %s)",
                    job["segment_id"],
                    kind,
                    next_attempt,
                    failure["attempts"],
                )
                if isinstance(exc, SourceQuota):
                    return "blocked"
        return (
            "blocked"
            if any(
                f.get("reason") not in {"UploadUncertain", "CapacityError"}
                for f in result["blocked"]
            )
            else "complete"
        )

    async def _drain(self, result):
        changed: set[str] = set()
        outcome = await self._drain_queue(result, changed)
        if not changed:
            return outcome
        if outcome == "merge":
            result["deferred"] += len(changed)
            return outcome

        # Capture can continue throughout the sync. Rebuild once and leave
        # further edits pending so a busy conversation cannot stall the run.
        self._build_segments(result)
        try:
            self._enqueue()
        except SnapshotError:
            result["blocked"].append({"reason": "SnapshotError"})
            return "blocked"
        still_changed: set[str] = set()
        outcome = await self._drain_queue(result, still_changed, segment_ids=changed)
        result["deferred"] += len(still_changed)
        return outcome

    def _build_segments(self, result):
        self._emit_progress("prepare")
        try:
            self.builder.build()
        except SourceBudgetError:
            pass
        except CapacityError as exc:
            result["blocked"].extend(
                getattr(self.builder, "blocked", [])
                or [{"reason": "CapacityError", "detail": str(exc)}]
            )
        else:
            result["blocked"].extend(getattr(self.builder, "blocked", []))

    async def run_once(self) -> dict:
        """Run one sync cycle with exclusive archive/notebook-binding ownership."""
        with archive_lock(self.store.path):
            return await self._run_once_locked()

    async def _run_once_locked(self, *, automatic=False) -> dict:
        """Run while the caller holds archive_lock(self.store.path).

        CLI callers acquire the lock before reading settings or constructing the
        adapter, then retain it across this await. Direct callers use run_once.
        """
        with sync_activity(self.store.path, automatic=automatic) as self._activity:
            try:
                return await self._run_with_activity()
            finally:
                self._activity = None

    async def _run_with_activity(self) -> dict:
        result = {"completed": 0, "deferred": 0, "blocked": []}
        self.docs_write_payload_bytes = 0
        self._build_segments(result)
        try:
            self._emit_progress("reconcile")
            await self._reconcile_mapped_sources()
            self._enqueue()
        except SnapshotError:
            result["blocked"].append({"reason": "SnapshotError"})
            result["status"] = self.store.status()
            return result
        except (NotebookLMError, ConnectionError, TimeoutError) as exc:
            result["blocked"].append({"reason": failure_code(exc)})
            result["status"] = self.store.status()
            result["status"]["sync_status"] = "pending"
            return result
        drained = await self._drain(result)
        if drained in {"complete", "merge"}:
            # One merge per invocation bounds network work; a daemon can continue next cycle.
            status = self.store.status()
            if (
                drained == "merge"
                or status["unassigned_messages"]
                and status["segments"] >= self.builder.max_active
            ):
                try:
                    self._emit_progress("prepare")
                    self.builder.merge_oldest(include_open=drained == "merge")
                    self._enqueue()
                    if await self._drain(result) == "complete":
                        self._emit_progress("prepare")
                        self.builder.build()
                        self._enqueue()
                        if await self._drain(result) == "merge":
                            result["blocked"].append(
                                {
                                    "reason": "SourceQuota",
                                    "detail": "Another merge is needed; retry sync",
                                }
                            )
                except CapacityError as exc:
                    result["blocked"].append({"reason": "CapacityError", "detail": str(exc)})
        result["status"] = self.store.status()
        if self.docs_client is not None:
            result["docs_web_write_payload_bytes"] = self.docs_write_payload_bytes
        return result
