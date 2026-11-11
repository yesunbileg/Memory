"""Single-worker persistent upload → verify → map → delete state machine."""

import fcntl
import json
import logging
import os
import time
from pathlib import Path

from .diagnostics import failure_code
from .privacy import find_sensitive
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

    def _enqueue(self):
        db = self.store.connection
        with db:
            for row in db.execute("""SELECT s.* FROM segments s LEFT JOIN source_map sm ON s.id=sm.segment_id
                WHERE s.state!='retired' AND s.content_hash!='' AND (sm.source_id IS NULL OR sm.revision!=s.revision) ORDER BY s.rowid""").fetchall():
                existing = db.execute(
                    "SELECT 1 FROM sync_jobs WHERE segment_id=? AND revision=?",
                    (row["id"], row["revision"]),
                ).fetchone()
                if existing:
                    continue
                text = self.builder.render(row["id"])
                if digest(text) != row["content_hash"] or len(text.split()) > 500_000:
                    raise SnapshotError("Segment changed before its snapshot was validated")
                title = f"{row['id']}-r{row['revision']}-{row['content_hash'][:16]}.md"
                db.execute(
                    "INSERT INTO sync_jobs(segment_id,revision,title) VALUES(?,?,?)",
                    (row["id"], row["revision"], title),
                )
                # Keep immutable snapshots only while a revision needs sync/reconciliation.
                self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.output_dir / title
                temporary = path.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8", newline="") as file:
                    file.write(text)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.chmod(0o600)
                temporary.replace(path)

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
            with db:
                db.execute("UPDATE sync_jobs SET stage='superseded' WHERE id=?", (job["id"],))
            path.unlink(missing_ok=True)
            return
        if job["stage"] != "cleanup":
            # No local text leaves the machine until every byte (including metadata) passes.
            if not path.exists():
                if current["revision"] != job["revision"]:
                    raise RuntimeError("Pending revision snapshot missing; archive retained")
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_text(
                    self.builder.render(job["segment_id"]), encoding="utf-8", newline=""
                )
                path.chmod(0o600)
            text = path.read_text(encoding="utf-8")
            expected = f"{job['segment_id']}-r{job['revision']}-{digest(text)[:16]}.md"
            if expected != job["title"] or len(text.split()) > 500_000:
                raise SnapshotError("Pending snapshot differs from its validated revision")
            if find_sensitive(text):
                raise SensitiveData("Sensitive text detected; local archive retained")
            if not job["new_source_id"]:
                sources = await self.adapter.list_sources()
                candidates = [s["id"] for s in sources if s["title"] == job["title"]]
                if len(candidates) > 1:
                    raise RuntimeError(
                        "Multiple sources have this revision title; reconcile manually"
                    )
                old = self._old_sources(job["segment_id"])
                if candidates:
                    new_id = candidates[0]
                else:
                    # Account for foreign sources too, and reserve the 50th slot for replacement.
                    if len(sources) >= 50:
                        raise SourceQuota("No free upload slot; existing remote sources preserved")
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
