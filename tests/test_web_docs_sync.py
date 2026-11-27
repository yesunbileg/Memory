from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_sync import Remote, conversation

from ai_memory import web_docs_transport, web_text_delta
from ai_memory.notebook import NotebookLMAdapter, NotebookLMError
from ai_memory.privacy import snapshot_sha256
from ai_memory.publication import allowed_sources, observe_inventory
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine
from ai_memory.web_docs_journal import DocumentJournal, docs_text, split_document
from ai_memory.web_docs_protocol import confirmed_revision, editor_commands
from ai_memory.web_docs_publication import enable_web_docs
from ai_memory.web_docs_transport import DocsWebError
from ai_memory.web_text_delta import text_edits, utf16_length


def apply_editor_commands(text, commands):
    current = text.encode("utf-16-le")
    for group in commands:
        for operation in group["mts"]:
            if operation["ty"] == "is":
                start = (operation["ibi"] - 1) * 2
                current = current[:start] + operation["s"].encode("utf-16-le") + current[start:]
            elif operation["ty"] == "ds":
                start = (operation["si"] - 1) * 2
                current = current[:start] + current[operation["ei"] * 2 :]
    return current.decode("utf-16-le")


class Docs:
    def __init__(self):
        self.documents = {}
        self.titles = {}
        self.writes = []
        self.lose_ack = False

    async def create(self, title):
        document = f"document-{len(self.documents)}"
        self.documents[document] = ""
        return document

    async def confirm_title(self, doc, title):
        assert doc in self.documents
        self.titles[doc] = title

    async def call(self, action, *, id, known=None):
        if action == "check":
            return {"hash": snapshot_sha256(self.documents[id]), "revision": len(self.writes)}
        if action == "inspect":
            digest = snapshot_sha256(self.documents[id])
            return {"hash": digest, "status": "ready" if digest == known else "changed"}
        assert action == "prepare"
        return {"status": "ready" if snapshot_sha256(self.documents[id]) == known else "conflict"}

    async def read_repair_base(self, doc):
        return {"text": self.documents[doc], **await self.call("check", id=doc)}

    async def write(self, doc, transaction, before, after, commands, *, recovering):
        current = self.documents[doc]
        if recovering and snapshot_sha256(current) == after:
            return 0
        assert snapshot_sha256(current) == before
        text = apply_editor_commands(current, commands)
        assert snapshot_sha256(text) == after
        self.documents[doc] = text
        self.writes.append(commands)
        if self.lose_ack:
            self.lose_ack = False
            raise DocsWebError("docs_web_uncertain")
        return sum(len(op.get("s", "").encode()) for op in commands[0]["mts"])


class Notebook(Remote):
    async def list_sources(self):
        return [{"id": key, **value} for key, value in self.sources.items()]

    async def add_document(self, document_id, title):
        source_id = f"drive-{document_id}"
        self.sources[source_id] = {"title": title, "ready": False, "drive_document_id": document_id}
        return source_id

    async def refresh_document(self, source_id, receipt, *, refresh=True):
        await self.wait_ready(source_id)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("One\nMongol 🐎 text\nOld", "Lua\nMongol 🐎 salt\n"),
        ("a\nkeep\nold\nkeep too\nx", "b\nkeep\nnew\nkeep too\n"),
        ("New 🏳️‍🌈 flag", "New 🏳️‍⚧️ flag"),
        ("deletion 🐎", ""),
        ("", "insertion"),
    ],
)
def test_edits_match_editor_offsets(before, after):
    assert apply_editor_commands(before, editor_commands(text_edits(before, after))) == after


def test_large_repeated_line_diff_uses_bounded_fallback(monkeypatch):
    before = "same repeated line\n" * 800 + "TARGET\n" + "same repeated line\n" * 800
    after = before.replace("TARGET", "REPLACED", 1)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("SequenceMatcher exceeded the configured work budget")

    monkeypatch.setattr(web_text_delta, "_MAX_DIFF_WORK", 1)
    edits = text_edits(before, after)
    assert len(edits) == 1
    assert utf16_length(edits[0].text) <= 10
    assert apply_editor_commands(before, editor_commands(edits)) == after


async def test_transport_rejects_a_signed_out_brave_session(monkeypatch):
    async def signed_out(*_arguments):
        return ('AI_MEMORY_DOCS_RESULT={"status":"auth_required"}', 0)

    monkeypatch.setattr(web_docs_transport.WebDocsTransport, "_run_script", signed_out)
    with pytest.raises(DocsWebError) as error:
        await web_docs_transport.WebDocsTransport().call("ping")
    assert error.value.code == "docs_web_auth_required"


async def test_docs_publication_checks_ready_source_and_indexed_receipt_without_chat():
    from notebooklm.exceptions import NetworkError

    receipt = "a" * 24
    sources = SimpleNamespace(
        refresh=AsyncMock(side_effect=NetworkError("refresh response lost")),
        wait_until_ready=AsyncMock(return_value=SimpleNamespace(is_ready=True)),
        get_fulltext=AsyncMock(
            return_value=SimpleNamespace(
                source_id="source", content=f"Memory publication receipt {receipt}\nMessage text"
            )
        ),
    )
    adapter = NotebookLMAdapter("notebook")
    adapter._client = SimpleNamespace(sources=sources, chat=SimpleNamespace(ask=AsyncMock()))
    await adapter.refresh_document("source", receipt)
    sources.refresh.assert_awaited_once_with("notebook", "source")
    sources.wait_until_ready.assert_awaited_once_with("notebook", "source")
    sources.get_fulltext.assert_awaited_once_with("notebook", "source")

    with pytest.raises(NotebookLMError) as rejected:
        await adapter.refresh_document("source", "b" * 24, refresh=False)
    assert rejected.value.code == "not_ready"
    sources.refresh.assert_awaited_once()

    sources.get_fulltext.reset_mock()
    sources.wait_until_ready.return_value = SimpleNamespace(is_ready=False)
    with pytest.raises(NotebookLMError) as rejected:
        await adapter.refresh_document("source", receipt, refresh=False)
    assert rejected.value.code == "not_ready"
    sources.get_fulltext.assert_not_awaited()
    adapter._client.chat.ask.assert_not_awaited()


@pytest.mark.parametrize(
    ("indexed_id", "content"),
    [
        ("other-source", f"Memory publication receipt {'a' * 24}\n"),
        ("source", f"Memory publication receipt {'a' * 24}extra\n"),
        ("source", f"{' ' * 1996}\nMemory publication receipt {'a' * 24}extra\n"),
    ],
)
async def test_docs_publication_rejects_unscoped_or_inexact_indexed_receipt(indexed_id, content):
    adapter = NotebookLMAdapter("notebook")
    adapter._client = SimpleNamespace(
        sources=SimpleNamespace(
            wait_until_ready=AsyncMock(return_value=SimpleNamespace(is_ready=True)),
            get_fulltext=AsyncMock(
                return_value=SimpleNamespace(source_id=indexed_id, content=content)
            ),
        )
    )
    with pytest.raises(NotebookLMError):
        await adapter.refresh_document("source", "a" * 24, refresh=False)


def test_large_sparse_edits_keep_unchanged_middle_out_of_payload():
    before = "".join(f"line {index:05}\n" for index in range(4000))
    after = before.replace("line 00010\n", "changed start\n").replace(
        "line 03990\n", "changed end\n"
    )
    edits = text_edits(before, after)
    assert len(edits) == 2
    assert sum(edit.end - edit.start + utf16_length(edit.text) for edit in edits) < 100
    assert apply_editor_commands(before, editor_commands(edits)) == after


def test_docs_text_normalizes_all_line_endings():
    assert docs_text("one\r\ntwo\rthree\n") == "one\ntwo\nthree\n"


def test_ten_words_only_in_payload_and_ack_requires_expected_revision():
    seed = "west " * 999 + "west"
    addition = "\nThe verified web sync compass code is ArcticMallow for sailors."
    assert len(seed.split()) == 1000 and len(addition.split()) == 10
    assert editor_commands(text_edits(seed, seed + addition)) == [
        {"ty": "mlti", "mts": [{"ty": "is", "ibi": utf16_length(seed) + 1, "s": addition}]}
    ]
    ack = {
        "metadata": {"needsTransformOnClient": False, "serverRevision": 3},
        "revisionRanges": [[4, 4]],
    }
    assert confirmed_revision(ack, 3) == 4
    assert confirmed_revision(ack, 2) is None
    ack["metadata"]["needsTransformOnClient"] = True
    assert confirmed_revision(ack, 3) is None


async def test_journal_normalizes_crlf_before_first_write_and_retry(tmp_path):
    docs = Docs()
    doc = await docs.create("line endings")
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, docs, doc)
        assert await journal.sync("one\r\ntwo\rthree") == len(b"one\ntwo\nthree")
        assert docs.documents[doc] == "one\ntwo\nthree"
        assert await DocumentJournal(store, docs, doc).sync("one\r\ntwo\rthree") == 0
        assert len(docs.writes) == 1


async def test_journal_recovers_ack_with_no_duplicate_write(tmp_path):
    docs = Docs()
    doc = await docs.create("test")
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, docs, doc)
        await journal.sync("base")
        docs.lose_ack = True
        with pytest.raises(DocsWebError):
            await journal.sync("base plus 10 more")
        assert await DocumentJournal(store, docs, doc).sync("base plus 10 more") == 0
        assert len(docs.writes) == 2


async def test_journal_restarts_a_definitely_unsent_packet(tmp_path):
    class DisconnectedDocs(Docs):
        async def write(self, *args, recovering):
            assert not recovering
            if not self.writes and self.lose_ack:
                self.lose_ack = False
                raise DocsWebError("docs_web_not_sent")
            return await super().write(*args, recovering=recovering)

    docs = DisconnectedDocs()
    doc = await docs.create("test")
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, docs, doc)
        docs.lose_ack = True
        with pytest.raises(DocsWebError, match="could not confirm"):
            await journal.sync("new text")
        assert await journal.sync("new text") == 8
        assert len(docs.writes) == 1


async def test_large_seed_sends_each_added_character_once(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_memory.web_docs_journal._WRITE_TEXT_BUDGET", 100)
    docs = Docs()
    doc = await docs.create("test")
    content = "Монгол 🐎 test\n" * 14
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, docs, doc)
        assert await journal.sync(content) == len(content.encode())
        assert docs.documents[doc] == content
        assert len(docs.writes) > 1
        assert all(op["ty"] == "is" for group in docs.writes for op in group[0]["mts"])
        assert await journal.sync(content) == 0


async def test_title_failure_keeps_document_id_without_writing_or_linking(tmp_path):
    class InterruptedTitle(Docs):
        attempts = 0

        async def confirm_title(self, doc, title):
            self.attempts += 1
            if self.attempts == 1:
                raise DocsWebError("docs_web_title_unconfirmed")
            await super().confirm_title(doc, title)

    docs, notebook = InterruptedTitle(), Notebook()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        result = await engine.run_once()
        assert result["blocked"][0]["reason"] == "docs_web_title_unconfirmed"
        part = store.connection.execute(
            "SELECT document_id, creating, source_id FROM google_docs_parts"
        ).fetchone()
        assert tuple(part) == ("document-0", 1, None)
        assert not docs.writes and not notebook.sources

        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        assert (await engine.run_once())["completed"] == 1
        assert docs.attempts == 2
        assert set(docs.documents) == set(docs.titles) == {"document-0"}
        assert len(notebook.sources) == 1


async def test_create_response_loss_adopts_same_document_then_confirms_title(tmp_path):
    class InterruptedCreate(Docs):
        async def create(self, title):
            self.recovery_id = await super().create(title)
            raise DocsWebError("docs_web_uncertain")

        async def call(self, action, **kwargs):
            if action == "find":
                return {"ids": [self.recovery_id]}
            return await super().call(action, **kwargs)

    docs, notebook = InterruptedCreate(), Notebook()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["blocked"][0]["reason"] == "docs_web_uncertain"
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        assert (await engine.run_once())["completed"] == 1
        assert set(docs.documents) == set(docs.titles) == {"document-0"}
        assert len(notebook.sources) == 1


async def test_source_link_recovers_after_response_loss_without_second_add(tmp_path):
    class LostLinkAcknowledgement(Notebook):
        attempts = 0

        async def add_document(self, document_id, title):
            self.attempts += 1
            await super().add_document(document_id, title)
            raise ConnectionError("lost response")

    docs, notebook = Docs(), LostLinkAcknowledgement()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["completed"] == 1
        assert len(allowed_sources(store)) == 1
        assert notebook.attempts == 1


async def test_existing_markdown_migrates_and_docs_keep_stable_source_ids(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_memory.web_docs_journal.DOCS_LIMIT", 800)
    docs = Docs()
    notebook = Notebook()
    archive = tmp_path / "archive.sqlite"
    with Store(archive) as store:
        store.set_setting("notebook_id", "notebook")
        chat = conversation()
        chat["messages"][0]["text"] = "A readable archived message.\n" * 40
        assert len(split_document(chat["messages"][0]["text"])) == 2
        store.ingest(chat)
        builder = SegmentBuilder(store)
        out = tmp_path / "revisions"
        assert (await SyncEngine(store, builder, notebook, out).run_once())["completed"] == 1
        old = set(allowed_sources(store))
        enable_web_docs(store, 8)
        assert set(allowed_sources(store)) == old
        engine = SyncEngine(store, builder, notebook, out, docs_client=docs)
        assert (await engine.run_once())["completed"] == 1
        current = set(allowed_sources(store))
        assert len(current) >= 2 and not current & old
        chat["messages"][0]["text"] += "New compact update."
        store.ingest(chat)
        result = await engine.run_once()
        assert result["blocked"] == []
        assert 0 < result["docs_web_write_payload_bytes"] < 1024
        assert set(allowed_sources(store)) == current
        assert any("New compact update." in text for text in docs.documents.values())


async def test_sync_counts_confirmed_docs_payload_before_a_later_write_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_memory.web_docs_journal._WRITE_TEXT_BUDGET", 100)

    class InterruptedDocs(Docs):
        async def write(self, *args, recovering):
            if self.writes:
                raise DocsWebError("docs_web_not_sent")
            return await super().write(*args, recovering=recovering)

    docs = InterruptedDocs()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), Notebook(), tmp_path / "sync", docs_client=docs
        )
        result = await engine.run_once()
        assert result["blocked"]
        assert result["completed"] == 0
        assert result["docs_web_write_payload_bytes"] == len(docs.documents["document-0"].encode())
        assert result["docs_web_write_payload_bytes"] > 0


async def test_docs_grow_then_shrink_and_resume_source_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_memory.web_docs_journal.DOCS_LIMIT", 1170)
    docs, notebook = Docs(), Notebook()
    chat = conversation()
    chat["messages"][0]["text"] = "Short original message."
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(chat)
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["completed"] == 1
        (primary,) = allowed_sources(store)

        chat["messages"][0]["text"] = "Archived message with enough body text.\n" * 34
        store.ingest(chat)
        assert (await engine.run_once())["completed"] == 1
        (secondary,) = set(allowed_sources(store)) - {primary}
        notebook.sources[secondary]["ready"] = False
        observe_inventory(store, await notebook.list_sources())
        assert not allowed_sources(store)
        notebook.sources[secondary]["ready"] = True
        observe_inventory(store, await notebook.list_sources())

        chat["messages"][0]["text"] = "Shortened message."
        store.ingest(chat)
        notebook.fail_delete = True
        blocked = await engine.run_once()
        assert blocked["blocked"]
        assert set(allowed_sources(store)) == {primary}
        assert secondary in notebook.sources

        notebook.fail_delete = False
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0 WHERE stage='cleanup'")
        recovered = await engine.run_once()
        assert recovered["completed"] == 1
        assert recovered["docs_web_write_payload_bytes"] == 0
        assert set(notebook.sources) == {primary}

        chat["messages"][0]["text"] = "A follow-up after the archive shrank."
        store.ingest(chat)
        assert (await engine.run_once())["completed"] == 1
        assert set(allowed_sources(store)) == {primary}


async def test_mismatched_drive_document_cannot_attest_managed_source(tmp_path):
    docs, notebook = Docs(), Notebook()
    chat = conversation()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(chat)
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["completed"] == 1
        (source_id,) = allowed_sources(store)
        notebook.sources[source_id]["drive_document_id"] = "unrelated-document"
        inventory = await notebook.list_sources()
        observe_inventory(store, inventory)
        assert not allowed_sources(store)
        assert (
            store.connection.execute(
                "SELECT remote_state FROM source_receipts WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            == "missing"
        )
        assert (
            store.connection.execute(
                "SELECT count(*) FROM sync_jobs WHERE stage='missing'"
            ).fetchone()[0]
            == 1
        )
        # No remote object was deleted or rewritten merely because inventory disagreed.
        assert source_id in notebook.sources
        assert len(docs.writes) == 1


async def test_docs_publisher_rejects_registered_source_with_different_drive_id(tmp_path):
    from ai_memory.web_docs_publication import DocsPublisher

    with Store(tmp_path / "archive.sqlite") as store:
        publisher = DocsPublisher(store, Notebook(), Docs(), lambda *_: None)
        with pytest.raises(DocsWebError) as rejected:
            await publisher._source(
                {"source_id": "registered"},
                "expected-document",
                "title",
                [{"id": "registered", "drive_document_id": "unrelated-document"}],
            )
        assert rejected.value.code == "remote_missing"


def test_same_ten_word_append_has_bounded_payload_near_document_limit():
    import json
    from urllib.parse import urlencode

    from ai_memory.web_docs_journal import DOCS_LIMIT, INDEX_RECEIPT_PREFIX

    addition = "\nThe verified web sync compass code is ArcticMallow for sailors."
    assert len(addition.split()) == 10
    small = "west " * 999 + "west"
    near_limit = "west " * ((DOCS_LIMIT - 512) // 5)
    sizes = []
    for original in (small, near_limit):
        target = original + addition
        assert len(split_document(target)) == 1
        commands = editor_commands(text_edits(original, target))
        assert commands == [
            {"ty": "mlti", "mts": [{"ty": "is", "ibi": utf16_length(original) + 1, "s": addition}]}
        ]
        assert apply_editor_commands(original, commands) == target
        # Model the form shape, not measured network traffic: session identifiers are synthetic.
        body = urlencode(
            {
                "rev": "100",
                "bundles": json.dumps(
                    [{"commands": commands, "sid": "synthetic-session", "reqId": 20}],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ).encode()
        assert b"west" not in body
        sizes.append(len(body))
    assert max(sizes) <= 2048
    assert abs(sizes[0] - sizes[1]) <= 8

    reserve = len(INDEX_RECEIPT_PREFIX) + 25
    boundary = "x" * (DOCS_LIMIT - reserve - 1) + "🐎" + "tail"
    parts = split_document(boundary)
    assert "".join(parts) == boundary
    assert parts[1].startswith("🐎")
    assert all(utf16_length(part) + reserve <= DOCS_LIMIT for part in parts)


async def test_publication_requires_independent_remote_hash_after_ack(tmp_path):
    class ChangedAfterAck(Docs):
        expected = None

        async def write(self, doc, *args, recovering):
            count = await super().write(doc, *args, recovering=recovering)
            self.expected = self.documents[doc]
            self.documents[doc] += "Unexpected external text."
            return count

    docs, notebook = ChangedAfterAck(), Notebook()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        result = await engine.run_once()
        assert result["completed"] == 0
        assert result["blocked"][0]["reason"] == "docs_web_conflict"
        assert not notebook.sources and not allowed_sources(store)
        assert len(docs.writes) == 1
        (doc,) = docs.documents
        assert docs.documents[doc].endswith("Unexpected external text.")
        # An unresolved mismatch remains blocked after restart; do not overwrite it.
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        restarted = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await restarted.run_once())["blocked"][0]["reason"] == "docs_web_conflict"
        assert len(docs.writes) == 1 and not notebook.sources
        # Explicitly restoring the expected remote content permits a zero-write retry.
        docs.documents[doc] = docs.expected
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        result = await restarted.run_once()
        assert result["completed"] == 1
        assert result["docs_web_write_payload_bytes"] == 0
        assert len(docs.writes) == 1 and len(allowed_sources(store)) == 1


async def test_publication_with_missing_remote_revision_remains_unverified(tmp_path):
    class IncompleteRead(Docs):
        async def call(self, action, **kwargs):
            result = await super().call(action, **kwargs)
            if action == "check":
                result.pop("revision")
            return result

    docs, notebook = IncompleteRead(), Notebook()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        result = await SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        ).run_once()
        assert result["completed"] == 0
        assert result["blocked"][0]["reason"] == "docs_web_uncertain"
        assert not notebook.sources and not allowed_sources(store)


async def test_link_requires_independent_drive_identity_and_does_not_duplicate(tmp_path):
    class DelayedInventory(Notebook):
        hidden = True
        adds = 0

        async def add_document(self, document_id, title):
            self.adds += 1
            return await super().add_document(document_id, title)

        async def list_sources(self):
            items = await super().list_sources()
            if self.hidden:
                for item in items:
                    item.pop("drive_document_id", None)
            return items

    docs, notebook = Docs(), DelayedInventory()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        first = await engine.run_once()
        assert first["blocked"][0]["reason"] == "docs_web_link_uncertain"
        assert not allowed_sources(store)
        row = store.connection.execute("SELECT linking,source_id FROM google_docs_parts").fetchone()
        assert tuple(row) == (1, None)
        assert notebook.adds == 1 and len(docs.writes) == 1
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        assert (await engine.run_once())["blocked"][0]["reason"] == "docs_web_link_uncertain"
        assert notebook.adds == 1 and len(docs.writes) == 1
        notebook.hidden = False
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        result = await engine.run_once()
        assert result["completed"] == 1 and result["docs_web_write_payload_bytes"] == 0
        assert notebook.adds == 1 and len(allowed_sources(store)) == 1


async def test_registered_source_with_sparse_metadata_does_not_consume_new_quota_slot(tmp_path):
    docs, notebook = Docs(), Notebook()
    chat = conversation()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(chat)
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        assert (await engine.run_once())["completed"] == 1
        (source,) = allowed_sources(store)
        notebook.sources[source].pop("drive_document_id")
        for index in range(49):
            notebook.sources[f"foreign-{index}"] = {"title": "foreign", "ready": True}
        chat["messages"][0]["text"] += " One more archived message."
        store.ingest(chat)
        result = await engine.run_once()
        assert result["completed"] == 1 and not result["blocked"]
        assert set(allowed_sources(store)) == {source}
        assert len(notebook.sources) == 50 and len(docs.documents) == 1


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ({"status": "committed", "bytes": -1}, "docs_web_uncertain"),
        ({"status": "committed", "bytes": False}, "docs_web_uncertain"),
        ({"status": "submitted", "previous": 7, "bytes": 258, "ack": []}, "docs_web_uncertain"),
        (
            {
                "status": "submitted",
                "previous": 7,
                "bytes": 258,
                "ack": {"http": 200, "metadata": {"needsTransformOnClient": True}},
            },
            "docs_web_conflict",
        ),
        (
            {
                "status": "submitted",
                "previous": 7,
                "bytes": 258,
                "ack": {
                    "http": 500,
                    "metadata": {"needsTransformOnClient": False, "serverRevision": 7},
                    "revisionRanges": [[8, 8]],
                },
            },
            "docs_web_uncertain",
        ),
    ],
)
async def test_transport_rejects_invalid_or_transformed_save_response(monkeypatch, result, reason):
    transport = web_docs_transport.WebDocsTransport()
    monkeypatch.setattr(transport, "call", AsyncMock(return_value=result))
    with pytest.raises(DocsWebError) as rejected:
        await transport.write("synthetic", "tx", "before", "after", [], recovering=False)
    assert rejected.value.code == reason


async def test_transport_accepts_exact_save_ack_or_zero_byte_reconciliation(monkeypatch):
    transport = web_docs_transport.WebDocsTransport()
    result = {
        "status": "submitted",
        "previous": 7,
        "bytes": 258,
        "ack": {
            "http": 200,
            "metadata": {"needsTransformOnClient": False, "serverRevision": 7},
            "revisionRanges": [[8, 8]],
        },
    }
    call = AsyncMock(side_effect=[result, {"status": "committed", "bytes": 0}])
    monkeypatch.setattr(transport, "call", call)
    assert await transport.write("synthetic", "tx", "before", "after", [], recovering=False) == 258
    assert await transport.write("synthetic", "tx", "before", "after", [], recovering=True) == 0


async def test_change_during_notebook_indexing_requires_reindex_before_publication(tmp_path):
    docs = Docs()

    class ChangedDuringIndex(Notebook):
        original = None
        refreshes = 0

        async def refresh_document(self, source_id, receipt, **kwargs):
            await super().refresh_document(source_id, receipt, **kwargs)
            self.refreshes += 1
            if self.refreshes == 1:
                doc = self.sources[source_id]["drive_document_id"]
                self.original = docs.documents[doc]
                # Leave the receipt untouched: the receipt alone is insufficient.
                docs.documents[doc] += " Unexpected editor change during indexing."

    notebook = ChangedDuringIndex()
    with Store(tmp_path / "archive.sqlite") as store:
        store.set_setting("notebook_id", "notebook")
        store.ingest(conversation())
        enable_web_docs(store, 8)
        engine = SyncEngine(
            store, SegmentBuilder(store), notebook, tmp_path / "sync", docs_client=docs
        )
        first = await engine.run_once()
        assert first["blocked"][0]["reason"] == "docs_web_conflict"
        assert first["completed"] == 0 and not allowed_sources(store)
        row = store.connection.execute(
            "SELECT source_id,written_hash,indexed_hash FROM google_docs_parts"
        ).fetchone()
        assert row["source_id"] and row["written_hash"] is None and row["indexed_hash"] is None
        assert len(docs.writes) == notebook.refreshes == 1
        (doc,) = docs.documents
        docs.documents[doc] = notebook.original
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        result = await engine.run_once()
        assert result["completed"] == 1 and result["docs_web_write_payload_bytes"] == 0
        assert len(docs.writes) == 1 and notebook.refreshes == 2
        assert set(allowed_sources(store)) == {row["source_id"]}
