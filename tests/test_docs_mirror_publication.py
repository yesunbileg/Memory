from test_sync import conversation
from test_web_docs_sync import Docs, Notebook

from ai_memory.notebook import NotebookLMError
from ai_memory.publication import allowed_sources
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine
from ai_memory.web_docs_publication import enable_web_docs


async def test_sync_repairs_managed_doc_for_unchanged_archive_and_reindexes(tmp_path):
    docs = Docs()
    manual_text = "Accidental editor content outside SQLite 🧭"

    class IndexedNotebook(Notebook):
        indexed_receipts = []
        not_yet_indexed = False

        async def refresh_document(self, source_id, receipt, **kwargs):
            self.indexed_receipts.append(receipt)
            if self.not_yet_indexed:
                assert receipt != self.indexed_receipts[0]
                assert self.sources[source_id]["ready"] is True
                raise NotebookLMError("Synthetic stale index", "not_ready")
            await super().refresh_document(source_id, receipt, **kwargs)

    archive = tmp_path / "archive.sqlite"
    notebook = IndexedNotebook()
    with Store(archive) as store:
        store.set_setting("notebook_id", "test-notebook")
        store.ingest(conversation())
        enable_web_docs(store, 3)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["completed"] == 1
        (doc,) = docs.documents
        (source_id,) = allowed_sources(store)
        initial = allowed_sources(store)[source_id]
        original_message = tuple(
            store.connection.execute("SELECT text,content_hash FROM messages").fetchone()
        )
        docs.documents[doc] += "\n" + manual_text
        notebook.not_yet_indexed = True

        result = await engine.run_once()
        assert result["completed"] == 0 and result["docs_web_write_payload_bytes"] > 0
        assert result["blocked"][0]["reason"] == "not_ready"
        revised_snapshot = store.connection.execute(
            "SELECT snapshot FROM outbound_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()[0]
        assert docs.documents[doc].split("\n", 1)[1] == bytes(revised_snapshot).decode()
        assert (
            tuple(store.connection.execute("SELECT text,content_hash FROM messages").fetchone())
            == original_message
        )
        assert not allowed_sources(store)
        assert set(docs.documents) == {doc} and set(notebook.sources) == {source_id}
        assert manual_text not in str(
            store.connection.execute("SELECT value FROM checkpoints").fetchall()
        )
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")

    with Store(archive, existing=True) as store:
        notebook.not_yet_indexed = False
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        result = await engine.run_once()
        assert result["completed"] == 1 and result["docs_web_write_payload_bytes"] == 0
        assert notebook.indexed_receipts[-1] == notebook.indexed_receipts[-2]
        assert notebook.indexed_receipts[-1] != notebook.indexed_receipts[0]
        repaired = allowed_sources(store)[source_id]
        assert repaired["source_revision"] > initial["source_revision"]
        clean = await engine.run_once()
        assert clean["completed"] == 0 and clean["docs_web_write_payload_bytes"] == 0
        assert len(docs.writes) == 2
