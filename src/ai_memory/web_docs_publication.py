"""Publish masked archive snapshots through Google Docs web sync."""

import json
from functools import partial
from uuid import uuid4

from . import source_names
from .diagnostics import PublicInputError, failure_code
from .privacy import MASKING_POLICY, snapshot_sha256
from .web_docs_background import BACKGROUND_SESSION
from .web_docs_journal import INDEX_RECEIPT_PREFIX, DocumentJournal, split_document
from .web_docs_transport import DocsWebError

DOCS_SETTING = "docs_web_session"


def enable_web_docs(store, session):
    """Stage the cutover while the existing NotebookLM sources remain available."""
    if session != BACKGROUND_SESSION and (type(session) is not int or session < 1):
        raise ValueError("Invalid legacy Docs setting")
    session = BACKGROUND_SESSION
    db = store.connection
    if not store.get_setting("notebook_id"):
        raise PublicInputError("Bind the existing NotebookLM notebook before enabling Google Docs")
    if store.get_setting(DOCS_SETTING):
        store.set_setting(DOCS_SETTING, session)
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("""SELECT 1 FROM sync_jobs
            WHERE stage IN ('registered','processing','ready','cleanup')
               OR (stage IN ('uploading','uncertain') AND new_source_id IS NOT NULL)
            LIMIT 1""").fetchone():
            raise PublicInputError("Finish in-progress Markdown jobs before switching publication")
        unresolved = db.execute("""SELECT j.id,j.segment_id,j.revision,j.title,r.snapshot,r.sha256
            FROM sync_jobs j LEFT JOIN outbound_revisions r USING(segment_id,revision)
            WHERE j.stage IN ('uploading','uncertain') AND j.new_source_id IS NULL""").fetchall()
        for row in unresolved:
            if (
                row["snapshot"] is None
                or snapshot_sha256(row["snapshot"]) != row["sha256"]
                or row["title"] != f"{row['segment_id']}-r{row['revision']}-{row['sha256'][:16]}.md"
            ):
                raise PublicInputError("An unfinished Markdown job has no verified local snapshot")
        # Its remote ownership cannot safely be inferred from a matching filename.
        # Keep the remote object and local evidence while publishing the same data anew.
        db.executemany(
            """UPDATE sync_jobs SET stage='superseded',error='UploadUncertain',next_attempt=0,
            failure_phase='google_docs_cutover' WHERE id=?""",
            ((row["id"],) for row in unresolved),
        )
        db.execute("""CREATE TABLE IF NOT EXISTS google_docs_parts (
            segment_id TEXT NOT NULL REFERENCES segments(id), part INTEGER NOT NULL,
            document_id TEXT UNIQUE, source_id TEXT UNIQUE,
            creating INTEGER NOT NULL DEFAULT 0, linking INTEGER NOT NULL DEFAULT 0,
            written_hash TEXT, indexed_hash TEXT,
            PRIMARY KEY(segment_id, part))""")
        db.execute("""CREATE TABLE IF NOT EXISTS google_docs_publications (
            segment_id TEXT PRIMARY KEY REFERENCES segments(id),
            ready_revision INTEGER, updating INTEGER NOT NULL DEFAULT 0,
            part_count INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""UPDATE segments SET revision=revision+1 WHERE state!='retired'
            AND (EXISTS (SELECT 1 FROM source_map sm WHERE sm.segment_id=segments.id)
                OR EXISTS (SELECT 1 FROM sync_jobs j WHERE j.segment_id=segments.id
                    AND j.revision=segments.revision AND j.failure_phase='google_docs_cutover'))
            AND NOT EXISTS (SELECT 1 FROM sync_jobs j WHERE j.segment_id=segments.id
                AND j.stage='missing')""")
        db.execute("INSERT INTO settings VALUES(?,?)", (DOCS_SETTING, json.dumps(session)))


def docs_status(store):
    db = store.connection
    counts = db.execute("""SELECT count(p.document_id),count(p.written_hash),
        count(CASE WHEN p.written_hash=p.indexed_hash THEN 1 END)
        FROM google_docs_parts p JOIN segments s ON s.id=p.segment_id
        WHERE s.state!='retired'""").fetchone()
    updating = db.execute(
        "SELECT count(*) FROM google_docs_publications WHERE updating=1"
    ).fetchone()[0]
    held = db.execute(
        "SELECT count(*) FROM sync_jobs WHERE failure_phase='google_docs_cutover'"
    ).fetchone()[0]
    return dict(
        zip(
            (
                "documents",
                "written_parts",
                "indexed_parts",
                "updating_segments",
                "unconfirmed_legacy_uploads",
            ),
            (*counts, updating, held),
            strict=True,
        )
    )


class DocsPublisher:
    def __init__(self, store, notebook, client, progress):
        self.store = store
        self.db = store.connection
        self.notebook = notebook
        self.client = client
        self.progress = progress
        self.write_payload_bytes = 0

    def _part(self, segment_id, number):
        return self.db.execute(
            "SELECT * FROM google_docs_parts WHERE segment_id=? AND part=?",
            (segment_id, number),
        ).fetchone()

    def _receipt_state(self, document_id):
        row = self.db.execute(
            "SELECT value FROM checkpoints WHERE key=?", (f"docs-receipt:{document_id}",)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def _write_receipt_state(self, document_id, state):
        self.db.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
            (f"docs-receipt:{document_id}", json.dumps(state)),
        )

    def _receipt(self, document_id, part_hash):
        nonce = self._receipt_state(document_id).get("nonce")
        return (snapshot_sha256(f"{nonce}:{part_hash}") if nonce else part_hash)[:24]

    def _mark_repair(self, row):
        document_id = row["document_id"]
        if not self._receipt_state(document_id).get("pending"):
            self._write_receipt_state(document_id, {"nonce": uuid4().hex, "pending": True})
        self.db.execute(
            "UPDATE google_docs_parts SET indexed_hash=NULL WHERE document_id=?", (document_id,)
        )
        self.db.execute(
            "UPDATE google_docs_publications SET updating=1 WHERE segment_id=?",
            (row["segment_id"],),
        )

    def _repair_model(self, row, part, part_hash):
        with self.db:
            self._mark_repair(row)
        receipt = self._receipt(row["document_id"], part_hash)
        return f"{INDEX_RECEIPT_PREFIX}{receipt}\n{part}"

    async def reconcile_mirrors(self, inventory):
        """Queue repairs to current managed copies even if SQLite content is unchanged."""
        from .publication import allowed_sources

        allowed = allowed_sources(self.store)
        rows = self.db.execute("""SELECT p.* FROM google_docs_parts p
            JOIN google_docs_publications d USING(segment_id)
            WHERE d.updating=0 AND p.part<d.part_count AND p.document_id IS NOT NULL
                AND p.written_hash=p.indexed_hash ORDER BY p.segment_id,p.part""").fetchall()
        blocked = []
        for row in rows:
            source_id, document_id = row["source_id"], row["document_id"]
            mapping = allowed.get(source_id)
            if (
                not mapping
                or not mapping["content_hash"]
                or mapping["source_revision"] != mapping["revision"]
                or inventory.get(source_id, {}).get("drive_document_id") != document_id
            ):
                continue
            journal = DocumentJournal(self.store, self.client, document_id)
            prefix = f"{INDEX_RECEIPT_PREFIX}{self._receipt(document_id, row['written_hash'])}\n"
            recorded = journal._load()
            text = recorded["text"]
            try:
                if (
                    recorded.keys() != {"text"}
                    or not text.startswith(prefix)
                    or snapshot_sha256(text[len(prefix) :]) != row["written_hash"]
                ):
                    raise DocsWebError("docs_web_uncertain")
                if await journal.matches_remote(text):
                    continue
            except DocsWebError as error:
                blocked.append(
                    {"reason": failure_code(error), "document_segment": row["segment_id"]}
                )
                continue
            with self.db:
                self._mark_repair(row)
                self.db.execute(
                    "UPDATE segments SET revision=revision+1 WHERE id=? AND revision=?",
                    (row["segment_id"], mapping["revision"]),
                )
        return blocked

    async def _document(self, segment_id, number, title):
        row = self._part(segment_id, number)
        document_id = row["document_id"]
        if document_id and not row["creating"]:
            return document_id
        title = source_names.drive_document_name(
            self.store, segment_id, number, title, new=not document_id and not row["creating"]
        )
        if not document_id:
            if row["creating"]:
                try:
                    result = await self.client.call("find", title=title)
                except DocsWebError:
                    raise DocsWebError("docs_web_create_uncertain") from None
                candidates = result.get("ids", [])
                if len(candidates) != 1:
                    raise DocsWebError("docs_web_create_uncertain")
                document_id = candidates[0]
            else:
                with self.db:
                    self.db.execute(
                        "UPDATE google_docs_parts SET creating=1 WHERE segment_id=? AND part=?",
                        (segment_id, number),
                    )
                try:
                    document_id = await self.client.create(title)
                except DocsWebError as error:
                    if error.definitely_not_sent:
                        with self.db:
                            self.db.execute(
                                "UPDATE google_docs_parts SET creating=0 WHERE segment_id=? AND part=?",
                                (segment_id, number),
                            )
                    raise
            with self.db:
                self.db.execute(
                    "UPDATE google_docs_parts SET document_id=? WHERE segment_id=? AND part=?",
                    (document_id, segment_id, number),
                )
        # A lost rename response must leave a durable ID, so retry never allocates a second Doc.
        await self.client.confirm_title(document_id, title)
        with self.db:
            self.db.execute(
                "UPDATE google_docs_parts SET creating=0 WHERE segment_id=? AND part=?",
                (segment_id, number),
            )
        return document_id

    async def _source(self, row, document_id, title, inventory):
        matches = [item for item in inventory if item.get("drive_document_id") == document_id]
        if row["source_id"]:
            registered = next((item for item in inventory if item["id"] == row["source_id"]), None)
            if registered is None:
                raise DocsWebError("remote_missing")
            # Some inventory responses omit Drive metadata. An explicit mismatch,
            # however, is never evidence for our registered document.
            if registered.get("drive_document_id", document_id) != document_id:
                raise DocsWebError("remote_missing")
            return row["source_id"], False
        if len(matches) > 1:
            raise DocsWebError("docs_web_link_uncertain")
        if matches:
            source_id = matches[0]["id"]
        else:
            if row["linking"]:
                raise DocsWebError("docs_web_link_uncertain")
            with self.db:
                self.db.execute(
                    "UPDATE google_docs_parts SET linking=1 WHERE segment_id=? AND part=?",
                    (row["segment_id"], row["part"]),
                )
            try:
                source_id = await self.notebook.add_document(document_id, title)
            except Exception as error:
                if getattr(error, "definitely_not_sent", False):
                    with self.db:
                        self.db.execute(
                            "UPDATE google_docs_parts SET linking=0 WHERE segment_id=? AND part=?",
                            (row["segment_id"], row["part"]),
                        )
                    raise
                # The source may be registered after the request loses its response.
                linked = [
                    item
                    for item in await self.notebook.list_sources()
                    if item.get("drive_document_id") == document_id
                ]
                if len(linked) != 1:
                    raise
                source_id = linked[0]["id"]
            else:
                # Do not manufacture Drive identity from the add response alone.
                # An ambiguous result stays in linking state; retry only adopts
                # an independently observed, uniquely matching Drive source.
                linked = [
                    item
                    for item in await self.notebook.list_sources()
                    if item.get("drive_document_id") == document_id
                ]
                if len(linked) != 1 or linked[0]["id"] != source_id:
                    raise DocsWebError("docs_web_link_uncertain")
            if not any(item["id"] == source_id for item in inventory):
                inventory.append(linked[0])
        with self.db:
            self.db.execute(
                "UPDATE google_docs_parts SET source_id=?,linking=0 WHERE segment_id=? AND part=?",
                (source_id, row["segment_id"], row["part"]),
            )
            self.db.execute("INSERT OR IGNORE INTO registered_sources VALUES(?)", (source_id,))
        return source_id, True

    async def _verify_part(self, segment_id, number, journal, model):
        try:
            await journal.verify_remote(model)
        except DocsWebError as error:
            if error.code in {"docs_web_conflict", "docs_web_uncertain"}:
                # A previously indexed receipt cannot attest a now-mismatching
                # document. Force independent re-indexing after reconciliation.
                with self.db:
                    self.db.execute(
                        """UPDATE google_docs_parts SET written_hash=NULL,indexed_hash=NULL
                        WHERE segment_id=? AND part=?""",
                        (segment_id, number),
                    )
            raise

    async def recheck_index(self, part):
        if not part["indexed_hash"]:
            raise DocsWebError("docs_web_uncertain")
        receipt = self._receipt(part["document_id"], part["indexed_hash"])
        await self.notebook.refresh_document(part["source_id"], receipt)

    async def publish(self, segment_id, text, *, replaceable=()):
        parts = split_document(text)
        inventory = await self.notebook.list_sources()
        existing = self.db.execute(
            "SELECT * FROM google_docs_parts WHERE segment_id=? ORDER BY part", (segment_id,)
        ).fetchall()
        active = [row for row in existing if row["part"] < len(parts)]
        registered = {item["id"]: item for item in inventory}
        attached = {item.get("drive_document_id") for item in inventory}
        covered = set()
        for row in active:
            document_id, source_id = row["document_id"], row["source_id"]
            if source_id:
                item = registered.get(source_id)
                if item is None:
                    with self.db:
                        cleared = self.db.execute(
                            """UPDATE google_docs_parts SET source_id=NULL,linking=0,indexed_hash=NULL
                               WHERE source_id=? AND NOT EXISTS (
                                   SELECT 1 FROM source_receipts WHERE source_id=?
                                   UNION ALL SELECT 1 FROM source_map WHERE source_id=?)""",
                            (source_id, source_id, source_id),
                        ).rowcount
                    if not cleared:
                        raise DocsWebError("remote_missing")
                    source_id = None
                elif item.get("drive_document_id", document_id) != document_id:
                    raise DocsWebError("remote_missing")
            if source_id:
                covered.add(source_id)
            elif document_id and document_id in attached:
                matches = [
                    item["id"] for item in inventory if item.get("drive_document_id") == document_id
                ]
                if len(matches) != 1:
                    raise DocsWebError("docs_web_link_uncertain")
                covered.add(matches[0])
        new_slots = len(parts) - len(covered)
        total = len(inventory) + new_slots
        if total > 50:
            raise DocsWebError("SourceQuota")
        if new_slots and total == 50:
            if not set(replaceable).intersection(registered).difference(covered):
                raise DocsWebError("SpareSlotNeeded")
        archive_id = self.store.archive_instance_id()[:12]
        with self.db:
            self.db.execute(
                """INSERT INTO google_docs_publications(segment_id,updating) VALUES(?,1)
                ON CONFLICT(segment_id) DO UPDATE SET updating=1""",
                (segment_id,),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO google_docs_parts(segment_id,part) VALUES(?,?)",
                ((segment_id, number) for number in range(len(parts))),
            )
        sources = []
        verified_parts = []
        for number, part in enumerate(parts):
            title = f"AI Memory {archive_id} {segment_id} part {number + 1}"
            document_id = await self._document(segment_id, number, title)
            delta = DocumentJournal(self.store, self.client, document_id)
            wanted_hash = snapshot_sha256(part)
            row = self._part(segment_id, number)
            source_id = row["source_id"]
            owned = (
                source_id
                and registered.get(source_id, {}).get("drive_document_id") == document_id
                and self.db.execute(
                    "SELECT 1 FROM source_receipts WHERE source_id=? AND segment_id=? AND masking_policy=?",
                    (source_id, segment_id, MASKING_POLICY),
                ).fetchone()
            )
            self.progress("upload", segment_id)

            receipt = self._receipt(document_id, wanted_hash)
            model = f"{INDEX_RECEIPT_PREFIX}{receipt}\n{part}"
            try:
                if owned:
                    await delta.sync_mirror(
                        model, repair_target=partial(self._repair_model, row, part, wanted_hash)
                    )
                    receipt = self._receipt(document_id, wanted_hash)
                    model = f"{INDEX_RECEIPT_PREFIX}{receipt}\n{part}"
                else:
                    await delta.sync(model)
                # A save ACK is not independent evidence of the final document.
                # Also recheck when a prior attempt saved but failed before indexing.
                await self._verify_part(segment_id, number, delta, model)
            finally:
                self.write_payload_bytes += delta.write_payload_bytes
            with self.db:
                self.db.execute(
                    "UPDATE google_docs_parts SET written_hash=? WHERE segment_id=? AND part=?",
                    (wanted_hash, segment_id, number),
                )
            row = self._part(segment_id, number)
            display_title = (
                source_names.docs_name(segment_id, number)
                if self.store.get_setting(source_names.ENABLED)
                else title
            )
            source_id, newly_linked = await self._source(row, document_id, display_title, inventory)
            if newly_linked or row["indexed_hash"] != wanted_hash:
                self.progress("readiness", segment_id)
                await self.notebook.refresh_document(
                    source_id,
                    receipt,
                    refresh=not newly_linked,
                )
                with self.db:
                    self.db.execute(
                        "UPDATE google_docs_parts SET indexed_hash=? WHERE source_id=?",
                        (wanted_hash, source_id),
                    )
                    state = self._receipt_state(document_id)
                    if state.get("pending"):
                        state["pending"] = False
                        self._write_receipt_state(document_id, state)
            sources.append(source_id)
            verified_parts.append((number, delta, model))
        # Indexing can take a long time; do not publish a multipart revision
        # if an already processed part changed while another part was indexed.
        # This is a second observation, not a server-side atomic write lock.
        for number, delta, model in verified_parts:
            await self._verify_part(segment_id, number, delta, model)
        return sources
