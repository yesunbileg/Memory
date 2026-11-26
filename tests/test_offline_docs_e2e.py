"""Real hook, archive, publication and retrieval logic; remote services are simulated."""

from test_web_docs_sync import Docs, Notebook

from ai_memory.archive_files import backup_archive, restore_archive
from ai_memory.gateway import MemoryApplication
from ai_memory.privacy import snapshot_sha256
from ai_memory.publication import allowed_sources
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine
from ai_memory.web_docs_publication import enable_web_docs
from scripts.audit_docs import capture


async def test_codex_hook_docs_memory_citation_and_backup(tmp_path):
    base = "Өвлийн тэмдэглэл 🧭\r\npassword=ONLY_SYNTHETIC_SECRET_912"
    answer = "The winter observatory stores its spare compass in the amber cedar drawer."
    question = "Where is the winter observatory's spare compass?"
    db, origin = tmp_path / "archive.sqlite", tmp_path / "synthetic.jsonl"
    docs = Docs()

    class IndexedNotebook(Notebook):
        async def refresh_document(self, source_id, receipt, **kwargs):
            doc = self.sources[source_id]["drive_document_id"]
            body = docs.documents[doc]
            assert body.startswith(f"Memory publication receipt {receipt}\n")
            self.sources[source_id]["text"] = body
            await super().refresh_document(source_id, receipt, **kwargs)

        async def ask(self, question_text, source_ids=None):
            assert question_text == question and answer not in question_text
            assert source_ids and len(source_ids) == 1
            source = self.sources[source_ids[0]]
            assert source["text"].count(answer) == 1
            return {
                "answer": answer,
                "references": [{"source_id": source_ids[0], "cited_text": answer}],
            }

    notebook = IndexedNotebook()
    with Store(db) as store:
        store.set_setting("tui_auto_sync", False)
        store.set_setting("notebook_id", "synthetic-isolated-notebook")
        enable_web_docs(store, 3)
    capture(db, origin, "synthetic-session", base)
    with Store(db) as store:
        chat_id = store.connection.execute("SELECT id FROM conversations").fetchone()[0]
        store.approve(chat_id)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["completed"] == 1
        (source_id,) = allowed_sources(store)
        (doc_id,) = docs.documents
        assert "ONLY_SYNTHETIC_SECRET_912" not in docs.documents[doc_id]
    capture(db, origin, "synthetic-session", base, answer)
    capture(db, origin, "synthetic-session", base, answer)
    with Store(db) as store:
        messages = store.get_conversation(chat_id)["messages"]
        assert [m["text"] for m in messages] == [base, answer]
        message_id = messages[1]["id"]
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        result = await engine.run_once()
        assert result["completed"] == 1 and 0 < result["docs_web_write_payload_bytes"] < 2048
        assert set(allowed_sources(store)) == {source_id} and set(docs.documents) == {doc_id}
        assert docs.documents[doc_id].count(answer) == 1
        count = len(docs.writes)
        assert (await engine.run_once())["docs_web_write_payload_bytes"] == 0
        assert len(docs.writes) == count
        backup = tmp_path / "backup.sqlite"
        backup_archive(store, backup)
    reply = await MemoryApplication(db, notebook).call("memory_ask", {"question": question})
    assert reply["answer"] == answer
    (ref,) = reply["sources"]
    assert ref["source_id"] == source_id and ref["conversation_id"] == chat_id
    assert ref["message_ids"] == [message_id] and ref["stale"] is False
    restored = tmp_path / "restored.sqlite"
    restore_archive(restored, backup)
    with Store(restored, existing=True) as store:
        assert store.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert store.status()["pending_jobs"] == 0
        saved = store.get_conversation(chat_id)["messages"]
        assert [snapshot_sha256(m["text"]) for m in saved] == [
            snapshot_sha256(base),
            snapshot_sha256(answer),
        ]
        assert [m["id"] for m in saved] == [m["id"] for m in messages]
        assert set(allowed_sources(store)) == {source_id}
