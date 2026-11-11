"""Single-worker persistent upload → verify → map → delete state machine."""

import fcntl
import json
import logging
import os
import time
from pathlib import Path

from .diagnostics import failure_code
from .privacy import find_sensitive, redact_sensitive
from .segments import CapacityError, SegmentBuilder, SourceBudgetError
from .store import Store, digest

logger = logging.getLogger(__name__)


class SensitiveData(RuntimeError):
    pass


class SourceQuota(RuntimeError):
    pass


class SpareSlotNeeded(RuntimeError):
    pass


class SnapshotError(RuntimeError):
    pass


class SyncEngine:
    def __init__(self, store: Store, builder: SegmentBuilder, adapter, output_dir: Path):
        self.store, self.builder, self.adapter = store, builder, adapter
        self.output_dir = Path(output_dir)

    def _prepare_snapshot(self, segment_id, revision):
        """Validate the local render, then make its upload-only redacted snapshot."""
        segment = self.store.connection.execute(
            "SELECT revision,content_hash FROM segments WHERE id=?", (segment_id,)
        ).fetchone()
        if segment is None or segment["revision"] != revision:
            raise SnapshotError("Segment changed before its snapshot was validated")
        original = self.builder.render(segment_id)
        if digest(original) != segment["content_hash"] or len(original.split()) > 500_000:
            raise SnapshotError("Segment changed before its snapshot was validated")
        text = redact_sensitive(original)
        if len(text.split()) > 500_000:
            raise SnapshotError("Redacted snapshot exceeds the source word ceiling")
        title = f"{segment_id}-r{revision}-{digest(text)[:16]}.md"
        return original, text, title

    def _write_snapshot(self, title, text):
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.output_dir / title
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        return path

    def _migrate_pending_snapshot(self, job):
        """Replace a legacy unredacted pending snapshot after validating it exactly."""
        original, safe_text, safe_title = self._prepare_snapshot(job["segment_id"], job["revision"])
        old_title = job["title"]
        old_path = self.output_dir / old_title
        old_title_for_original = (
            f"{job['segment_id']}-r{job['revision']}-{digest(original)[:16]}.md"
        )
        if old_title == safe_title and old_path.exists():
            current_text = old_path.read_text(encoding="utf-8")
            if current_text == safe_text:
                return
        if old_path.exists():
            current_text = old_path.read_text(encoding="utf-8")
            current_digest_title = (
                f"{job['segment_id']}-r{job['revision']}-{digest(current_text)[:16]}.md"
            )
            if current_text == safe_text:
                if old_title != safe_title:
                    raise SnapshotError(
                        "Pending redacted snapshot title does not match its content"
                    )
            elif current_text == original and old_title == old_title_for_original:
                pass
            else:
                raise SnapshotError("Pending snapshot differs from its validated revision")
            if current_digest_title != old_title:
                raise SnapshotError("Pending snapshot title does not match its content")

        safe_path = self.output_dir / safe_title
        if safe_path.exists() and safe_path != old_path:
            if safe_path.read_text(encoding="utf-8") != safe_text:
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
                 AND j.stage NOT IN ('done','superseded')
               ORDER BY d.depth,j.id DESC LIMIT 1""",
            (segment_id,),
        ).fetchone()
        if row is None:
            return False
        old_ids = list(dict.fromkeys(json.loads(row["old_source_ids"]) + source_ids))
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE sync_jobs SET old_source_ids=? WHERE id=?",
                (json.dumps(old_ids), row["id"]),
            )
        return True

    async def _migrate_inflight_snapshot(self, job):
        """Restart an unredacted in-flight upload as a safe replacement."""
        old_path = self.output_dir / job["title"]
        if not old_path.exists():
            raise SnapshotError("In-flight snapshot missing; archive retained")
        original = old_path.read_text(encoding="utf-8")
        expected_old_title = f"{job['segment_id']}-r{job['revision']}-{digest(original)[:16]}.md"
        if expected_old_title != job["title"] or len(original.split()) > 500_000:
            raise SnapshotError("In-flight snapshot differs from its validated revision")

        current = self.store.connection.execute(
            "SELECT revision,state FROM segments WHERE id=?", (job["segment_id"],)
        ).fetchone()
        old_ids = list(dict.fromkeys(json.loads(job["old_source_ids"])))
        unresolved_uploading = False
        if job["stage"] == "processing" and job["new_source_id"]:
            old_ids.append(job["new_source_id"])
        elif job["stage"] == "uploading":
            candidates = [
                source["id"]
                for source in await self.adapter.list_sources()
                if source["title"] == job["title"]
            ]
            if not candidates:
                unresolved_uploading = True
            else:
                old_ids.extend(candidates)
        old_ids = list(dict.fromkeys(old_ids))

        if current and current["revision"] != job["revision"]:
            if unresolved_uploading:
                raise SnapshotError("Stale in-flight upload cannot be reconciled yet")
            if not self._carry_old_sources(job["segment_id"], old_ids):
                raise SnapshotError("Stale upload has no active replacement for source cleanup")
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE sync_jobs SET stage='superseded',error=NULL WHERE id=?",
                    (job["id"],),
                )
            old_path.unlink(missing_ok=True)
            job["stage"] = "superseded"
            return False

        if current and current["revision"] == job["revision"]:
            rendered, safe_text, safe_title = self._prepare_snapshot(
                job["segment_id"], job["revision"]
            )
            if original != rendered and original != safe_text:
                raise SnapshotError("In-flight snapshot differs from its validated revision")
        else:
            safe_text = redact_sensitive(original)
            safe_title = f"{job['segment_id']}-r{job['revision']}-{digest(safe_text)[:16]}.md"
        if unresolved_uploading and safe_title != job["title"]:
            raise SnapshotError("In-flight upload cannot be reconciled yet")
        if safe_title == job["title"]:
            return False

        safe_path = self._write_snapshot(safe_title, safe_text)
        with self.store.connection:
            self.store.connection.execute(
                """UPDATE sync_jobs SET title=?,stage='pending',new_source_id=NULL,
                old_source_ids=?,error=NULL,attempts=0,next_attempt=0 WHERE id=?""",
                (safe_title, json.dumps(old_ids), job["id"]),
            )
        old_path.unlink(missing_ok=True)
        job.update(
            title=safe_title,
            stage="pending",
            new_source_id=None,
            old_source_ids=json.dumps(old_ids),
        )
        return safe_path

    def _migrate_mapped_sources(self):
        """Schedule a replacement when an older archive mapped an unredacted source."""
        key = "sync_redaction_version"
        if self.store.get_setting(key) == 1:
            return
        db = self.store.connection
        with db:
            rows = db.execute(
                """SELECT s.id,s.revision FROM segments s JOIN source_map sm
                ON sm.segment_id=s.id AND sm.revision=s.revision
                WHERE s.state!='retired' ORDER BY s.rowid"""
            ).fetchall()
            for row in rows:
                original, safe_text, _ = self._prepare_snapshot(row["id"], row["revision"])
                if original != safe_text:
                    db.execute("UPDATE segments SET revision=revision+1 WHERE id=?", (row["id"],))
            db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                (key, json.dumps(1)),
            )

    async def _recover_terminal_upload_failure(self, job):
        """Retry a known failed upload body without adopting or deleting its partial source."""
        if (
            job["stage"] != "processing"
            or not job["new_source_id"]
            or job["error"] not in {"network_error", "timeout"}
        ):
            return
        partial_id = job["new_source_id"]
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
            for row in db.execute("""SELECT s.* FROM segments s LEFT JOIN source_map sm ON s.id=sm.segment_id
                WHERE s.state!='retired' AND s.content_hash!='' AND (sm.source_id IS NULL OR sm.revision!=s.revision) ORDER BY s.rowid""").fetchall():
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
                db.execute(
                    "INSERT INTO sync_jobs(segment_id,revision,title) VALUES(?,?,?)",
                    (row["id"], row["revision"], title),
                )
                # Keep immutable redacted snapshots only while a revision needs sync/reconciliation.
                self._write_snapshot(title, text)

    def _old_sources(self, segment_id):
        return [
            r[0]
            for r in self.store.connection.execute(
                """WITH RECURSIVE ancestors(id) AS (
            SELECT ? UNION SELECT sp.parent_id FROM segment_parents sp JOIN ancestors a ON sp.segment_id=a.id)
            SELECT sm.source_id FROM source_map sm JOIN ancestors a ON a.id=sm.segment_id""",
                (segment_id,),
            )
        ]

    async def _process(self, job):
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
            return
        if job["stage"] in ("uploading", "processing"):
            migrated = await self._migrate_inflight_snapshot(job)
            if job["stage"] == "superseded":
                return
            if migrated:
                path = self.output_dir / job["title"]
        await self._recover_terminal_upload_failure(job)
        if job["stage"] != "cleanup":
            if job["stage"] != "processing":
                # No local text leaves the machine until every byte (including metadata) passes.
                if not path.exists():
                    if current["revision"] != job["revision"]:
                        raise RuntimeError("Pending revision snapshot missing; archive retained")
                    self._migrate_pending_snapshot(job)
                    job["title"] = db.execute(
                        "SELECT title FROM sync_jobs WHERE id=?", (job["id"],)
                    ).fetchone()[0]
                    path = self.output_dir / job["title"]
                text = path.read_text(encoding="utf-8")
                expected = f"{job['segment_id']}-r{job['revision']}-{digest(text)[:16]}.md"
                if expected != job["title"] or len(text.split()) > 500_000:
                    raise SnapshotError("Pending snapshot differs from its validated revision")
                _, expected_text, expected_title = self._prepare_snapshot(
                    job["segment_id"], job["revision"]
                )
                if text != expected_text or job["title"] != expected_title:
                    raise SnapshotError("Pending snapshot differs from its validated revision")
                if redact_sensitive(text) != text:
                    raise SnapshotError("Pending snapshot is not redacted")
                if find_sensitive(text):
                    raise SensitiveData("Sensitive text detected; local archive retained")
                if not job["new_source_id"]:
                    sources = await self.adapter.list_sources()
                    excluded_ids = set(json.loads(job["old_source_ids"]))
                    candidates = [
                        s["id"]
                        for s in sources
                        if s["title"] == job["title"] and s["id"] not in excluded_ids
                    ]
                    if len(candidates) > 1:
                        raise RuntimeError(
                            "Multiple sources have this revision title; reconcile manually"
                        )
                    old = list(
                        dict.fromkeys(
                            json.loads(job["old_source_ids"]) + self._old_sources(job["segment_id"])
                        )
                    )
                    if candidates:
                        new_id = candidates[0]
                    else:
                        # Account for foreign sources too, and reserve the 50th slot for replacement.
                        if len(sources) >= 50 and job["error"] in {"network_error", "timeout"}:
                            terminal_partials = [
                                source
                                for source in sources
                                if source["id"] in excluded_ids
                                and source["title"] == job["title"]
                                and not source.get("ready")
                                and not db.execute(
                                    "SELECT 1 FROM source_map WHERE source_id=?",
                                    (source["id"],),
                                ).fetchone()
                            ]
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
                            db.execute(
                                "UPDATE sync_jobs SET stage='uploading',old_source_ids=? WHERE id=?",
                                (json.dumps(old), job["id"]),
                            )
                        try:
                            new_id = await self.adapter.upload(path, job["title"])
                        except Exception as exc:
                            # notebooklm-py carries registration IDs on partial upload failures.
                            partial = getattr(exc, "source_id", None)
                            if isinstance(partial, str) and partial:
                                with db:
                                    db.execute(
                                        "UPDATE sync_jobs SET new_source_id=?,stage='processing' WHERE id=?",
                                        (partial, job["id"]),
                                    )
                            raise
                    job["new_source_id"], job["old_source_ids"] = new_id, json.dumps(old)
                    with db:
                        db.execute(
                            "UPDATE sync_jobs SET stage='processing',new_source_id=?,old_source_ids=? WHERE id=?",
                            (new_id, json.dumps(old), job["id"]),
                        )
            await self.adapter.wait_ready(job["new_source_id"])
            # This transaction makes restart pick cleanup, never a second upload.
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO source_map(segment_id,source_id,revision) VALUES(?,?,?)",
                    (job["segment_id"], job["new_source_id"], job["revision"]),
                )
                db.execute(
                    "UPDATE sync_jobs SET stage='cleanup',error=NULL WHERE id=?", (job["id"],)
                )
        old_ids = json.loads(job["old_source_ids"])
        if old_ids:
            present = {s["id"] for s in await self.adapter.list_sources()}
            for old_id in old_ids:
                if old_id == job["new_source_id"]:
                    continue
                if old_id in present:
                    await self.adapter.delete(old_id)
                with db:
                    db.execute("DELETE FROM source_map WHERE source_id=?", (old_id,))
        with db:
            db.execute(
                "UPDATE sync_jobs SET stage='done',error=NULL,next_attempt=0 WHERE id=?",
                (job["id"],),
            )
        path.unlink(missing_ok=True)

    async def _drain(self, result):
        db = self.store.connection
        # Reconciliation/cleanup for retired parents always finishes before later uploads.
        jobs = db.execute("""SELECT * FROM sync_jobs j WHERE stage NOT IN ('done','superseded')
            ORDER BY CASE WHEN stage IN ('uploading','processing','cleanup') THEN 0
            WHEN EXISTS(SELECT 1 FROM segment_parents p WHERE p.segment_id=j.segment_id) THEN 1 ELSE 2 END,id""").fetchall()
        for job in jobs:
            job = db.execute("SELECT * FROM sync_jobs WHERE id=?", (job["id"],)).fetchone()
            if job is None or job["stage"] in ("done", "superseded"):
                continue
            if job["next_attempt"] > time.time():
                result["deferred"] += 1
                if job["stage"] == "blocked" and not job["new_source_id"]:
                    continue
                # Never overtake an unresolved upload: the notebook may be using its spare slot.
                return "blocked"
            try:
                await self._process(job)
                result["completed"] += 1
            except SpareSlotNeeded:
                return "merge"
            except Exception as exc:
                kind = failure_code(exc)
                if kind == "processing_failed":
                    latest = db.execute(
                        "SELECT new_source_id,stage FROM sync_jobs WHERE id=?", (job["id"],)
                    ).fetchone()
                    failed = latest["new_source_id"]
                    mapped = db.execute(
                        "SELECT 1 FROM source_map WHERE source_id=?", (failed,)
                    ).fetchone()
                    if failed and not mapped and latest["stage"] == "processing":
                        try:
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
                    if isinstance(exc, SensitiveData):
                        db.execute("UPDATE sync_jobs SET stage='blocked' WHERE id=?", (job["id"],))
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
                if isinstance(exc, SensitiveData) and not job["new_source_id"]:
                    continue
                return "blocked"
        return "complete"

    async def run_once(self) -> dict:
        lock_path = Path(str(self.store.path) + ".sync.lock")
        with lock_path.open("a") as lock:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another sync worker is active") from exc
            result = {"completed": 0, "deferred": 0, "blocked": []}
            try:
                self.builder.build()
            except SourceBudgetError:
                pass
            except CapacityError as exc:
                result["blocked"].append({"reason": "CapacityError", "detail": str(exc)})
                result["status"] = self.store.status()
                return result
            try:
                self._enqueue()
            except SnapshotError:
                result["blocked"].append({"reason": "SnapshotError"})
                result["status"] = self.store.status()
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
                        self.builder.merge_oldest()
                        self._enqueue()
                        if await self._drain(result) == "complete":
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
            return result
