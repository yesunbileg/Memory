from test_store import conversation
from test_sync import Remote, make_retries_due
from test_web_docs_sync import Docs, Notebook

from ai_memory.cold_archive import markdown_segment
from ai_memory.segments import SegmentBuilder
from ai_memory.source_names import ENABLED, apply_source_names, matches_source_title
from ai_memory.store import Store
from ai_memory.sync import SyncEngine
from ai_memory.web_docs_publication import enable_web_docs
from ai_memory.web_docs_transport import DocsWebError


async def test_display_names_are_verified_without_changing_upload_recovery_titles(tmp_path):
    class Notebook(Remote):
        async def rename_source(self, source_id, title):
            self.sources[source_id]["title"] = title
            raise TimeoutError("Simulate a lost rename response")

    with Store(tmp_path / "db") as store:
        store.ingest(conversation())
        remote = Notebook()
        assert (
            await SyncEngine(
                store, SegmentBuilder(store), remote, tmp_path / "snapshots"
            ).run_once()
        )["completed"] == 1
        job = store.connection.execute("SELECT * FROM sync_jobs").fetchone()
        sid, original = job["new_source_id"], job["title"]
        with store.connection:
            store.connection.execute(
                "INSERT INTO checkpoints VALUES('cold-markdown:SEG-001','true')"
            )
        assert markdown_segment(store, "SEG-001")
        remote.sources["foreign"] = {"title": original, "ready": True}
        assert await apply_source_names(store, remote) == {
            "renamed": 1,
            "confirmed": 1,
            "failed": [],
        }
        new_title = remote.sources[sid]["title"]
        assert new_title == "Архив 001 · 30+ хоног"
        assert matches_source_title(store, sid, new_title, original)
        assert not matches_source_title(store, "foreign", new_title, original)
        assert remote.sources["foreign"]["title"] == original
        assert store.connection.execute("SELECT title FROM sync_jobs").fetchone()[0] == original
        assert await apply_source_names(store, remote) == {
            "renamed": 0,
            "confirmed": 1,
            "failed": [],
        }


async def test_new_markdown_is_named_at_submission_and_recovers_lost_response(tmp_path):
    with Store(tmp_path / "db") as store:
        store.set_setting(ENABLED, True)
        store.ingest(conversation())
        remote = Remote()
        remote.lose_response = True
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "snapshots")
        assert (await engine.run_once())["blocked"]
        sid, source = next(iter(remote.sources.items()))
        assert source["title"] == "Чатын багц 001.md"
        canonical = store.connection.execute("SELECT title FROM sync_jobs").fetchone()[0]
        assert canonical.startswith("SEG-001-r1-")

        make_retries_due(store)
        # The next cycle matches the name submitted by the previous cycle.
        store.set_setting(ENABLED, False)
        assert (await engine.run_once())["completed"] == 1
        assert list(remote.sources) == [sid]
        assert sum(op[0] == "upload" for op in remote.operations) == 1
        assert matches_source_title(store, sid, source["title"], canonical)


async def test_new_docs_and_linked_sources_start_with_readable_titles(tmp_path):
    class LostCreateResponse(Docs):
        async def create(self, title):
            document_id = await super().create(title)
            self.titles[document_id] = title
            raise DocsWebError("docs_web_create_uncertain")

        async def call(self, action, **kwargs):
            if action == "find":
                return {
                    "ids": [key for key, value in self.titles.items() if value == kwargs["title"]]
                }
            return await super().call(action, **kwargs)

    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "synthetic")
        enable_web_docs(store, 1)
        store.set_setting(ENABLED, True)
        store.ingest(conversation())
        docs, remote = LostCreateResponse(), Notebook()
        engine = SyncEngine(
            store, SegmentBuilder(store), remote, tmp_path / "snapshots", docs_client=docs
        )
        assert (await engine.run_once())["blocked"]
        assert next(iter(docs.titles.values())).startswith("Идэвхтэй чат 001 · хэсэг 1")
        make_retries_due(store)
        assert (await engine.run_once())["blocked"] == []
        assert len(docs.documents) == 1
        assert next(iter(remote.sources.values()))["title"] == "Идэвхтэй чат 001"


async def test_docs_title_stays_valid_when_more_parts_are_added(tmp_path, monkeypatch):
    with Store(tmp_path / "db") as store:
        store.set_setting("notebook_id", "synthetic")
        enable_web_docs(store, 1)
        store.set_setting(ENABLED, True)
        chat = conversation()
        store.ingest(chat)
        docs, remote = Docs(), Notebook()
        engine = SyncEngine(
            store, SegmentBuilder(store), remote, tmp_path / "snapshots", docs_client=docs
        )
        assert (await engine.run_once())["blocked"] == []
        first = next(iter(remote.sources))
        chat["messages"][0]["text"] = "Updated synthetic body"
        store.ingest(chat)
        monkeypatch.setattr(
            "ai_memory.web_docs_publication.split_document",
            lambda text: [text[: len(text) // 2], text[len(text) // 2 :]],
        )
        assert (await engine.run_once())["blocked"] == []
        assert remote.sources[first]["title"] == "Идэвхтэй чат 001"
        assert {s["title"] for s in remote.sources.values()} == {
            "Идэвхтэй чат 001",
            "Идэвхтэй чат 001 · үргэлжлэл 2",
        }
