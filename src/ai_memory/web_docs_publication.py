"""Publish approved archive snapshots through Google Docs web sync."""

from .diagnostics import PublicInputError
from .privacy import snapshot_sha256
from .web_docs_journal import INDEX_RECEIPT_PREFIX, DocumentJournal, split_document
from .web_docs_transport import DocsWebError, WebDocsTransport

DOCS_SETTING = "docs_web_session"


def enable_web_docs(store, session):
    """Stage the cutover while the existing NotebookLM sources remain available."""
    WebDocsTransport(session)
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
        db.execute("INSERT INTO settings VALUES(?,?)", (DOCS_SETTING, str(session)))


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

    async def _document(self, segment_id, number, title):
        row = self._part(segment_id, number)
        document_id = row["document_id"]
        if document_id and not row["creating"]:
            return document_id
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
                document_id = await self.client.create(title)
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
            if not any(item["id"] == source_id for item in inventory):
                inventory.append({"id": source_id, "drive_document_id": document_id})
        with self.db:
            self.db.execute(
                "UPDATE google_docs_parts SET source_id=?,linking=0 WHERE segment_id=? AND part=?",
                (source_id, row["segment_id"], row["part"]),
            )
            self.db.execute("INSERT OR IGNORE INTO registered_sources VALUES(?)", (source_id,))
        return source_id, True

    async def publish(self, segment_id, text):
        parts = split_document(text)
        inventory = await self.notebook.list_sources()
        existing = self.db.execute(
            "SELECT * FROM google_docs_parts WHERE segment_id=? ORDER BY part", (segment_id,)
        ).fetchall()
        documents = {row["document_id"] for row in existing if row["part"] < len(parts)}
        attached = {item.get("drive_document_id") for item in inventory}
        new_slots = len(parts) - len((documents - {None}) & attached)
        if len(inventory) + new_slots > 50:
            raise DocsWebError("SourceQuota")
        publication = self.db.execute(
            "SELECT ready_revision FROM google_docs_publications WHERE segment_id=?", (segment_id,)
        ).fetchone()
        first_publication = publication is None or publication["ready_revision"] is None
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
        for number, part in enumerate(parts):
            title = f"AI Memory {archive_id} {segment_id} part {number + 1}"
            document_id = await self._document(segment_id, number, title)
            delta = DocumentJournal(self.store, self.client, document_id)
            wanted_hash = snapshot_sha256(part)
            receipt = wanted_hash[:24]
            self.progress("upload", segment_id)
            try:
                await delta.sync(f"{INDEX_RECEIPT_PREFIX}{receipt}\n{part}")
            finally:
                self.write_payload_bytes += delta.write_payload_bytes
            with self.db:
                self.db.execute(
                    "UPDATE google_docs_parts SET written_hash=? WHERE segment_id=? AND part=?",
                    (wanted_hash, segment_id, number),
                )
            row = self._part(segment_id, number)
            source_id, newly_linked = await self._source(row, document_id, title, inventory)
            if newly_linked or row["indexed_hash"] != wanted_hash:
                self.progress("readiness", segment_id)
                await self.notebook.refresh_document(
                    source_id,
                    receipt,
                    refresh=not newly_linked,
                    allow_indexed_read=first_publication,
                )
                with self.db:
                    self.db.execute(
                        "UPDATE google_docs_parts SET indexed_hash=? WHERE source_id=?",
                        (wanted_hash, source_id),
                    )
            sources.append(source_id)
        return sources
