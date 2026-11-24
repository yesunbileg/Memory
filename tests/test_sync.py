import time

import pytest
from test_store import conversation

from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store, digest
from ai_memory.sync import SyncEngine


def make_retries_due(store):
    """Simulate reaching persisted retry deadlines without sleeping in a test."""
    with store.connection:
        store.connection.execute("UPDATE sync_jobs SET next_attempt=0")


class Remote:
    """Explicit in-memory service simulator; not a NotebookLM retrieval engine."""

    def __init__(self):
        self.sources = {}
        self.operations = []
        self.fail_upload = False
        self.lose_response = False
        self.fail_ready = False
        self.fail_delete = False
        self.peak_sources = 0

    async def list_sources(self):
        return [dict(id=k, **v) for k, v in self.sources.items()]

    async def get_source_text(self, source_id):
        return self.sources[source_id]["text"]

    async def upload(self, path, title):
        self.operations.append(("upload", title))
        if self.fail_upload:
            exc = ConnectionError("synthetic network failure before registration")
            exc.definitely_not_sent = True
            raise exc
        key = f"source-{len(self.operations)}"
        self.sources[key] = {
            "title": title,
            "ready": False,
            "text": path.read_bytes().decode("utf-8"),
        }
        self.peak_sources = max(self.peak_sources, len(self.sources))
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost after remote created source")
        return key

    async def wait_ready(self, source_id):
        self.operations.append(("ready", source_id))
        if self.fail_ready:
            raise TimeoutError("processing not ready")
        self.sources[source_id]["ready"] = True

    async def delete(self, source_id):
        self.operations.append(("delete", source_id))
        if self.fail_delete:
            raise ConnectionError("synthetic deletion failure")
        self.sources.pop(source_id, None)


async def test_progress_reports_live_segment_phases_during_replacement(tmp_path):
    progress = []

    class ObservedRemote(Remote):
        async def upload(self, path, title):
            assert progress[-1] == {"phase": "upload", "segment_id": "SEG-001"}
            return await super().upload(path, title)

        async def wait_ready(self, source_id):
            assert progress[-1] == {"phase": "readiness", "segment_id": "SEG-001"}
            await super().wait_ready(source_id)

        async def delete(self, source_id):
            assert progress[-1] == {"phase": "cleanup", "segment_id": "SEG-001"}
            await super().delete(source_id)

    with Store(tmp_path / "db") as store:
        chat = conversation()
        store.ingest(chat, consent=True)
        remote = ObservedRemote()
        engine = SyncEngine(
            store, SegmentBuilder(store), remote, tmp_path / "md", progress=progress.append
        )
        assert (await engine.run_once())["completed"] == 1
        old_id = next(iter(remote.sources))
        progress.clear()
        chat["messages"][0]["text"] = "replacement revision"
        store.ingest(chat)
        result = await engine.run_once()

    assert result["completed"] == 1
    assert result["blocked"] == []
    assert progress[:2] == [{"phase": "prepare"}, {"phase": "reconcile"}]
    assert [event["phase"] for event in progress[-3:]] == ["upload", "readiness", "cleanup"]
    assert all(set(event) <= {"phase", "segment_id"} for event in progress)
    assert old_id not in remote.sources


@pytest.mark.asyncio
async def test_sync_rebuilds_a_pending_snapshot_changed_by_capture(tmp_path):
    path = tmp_path / "db"
    first = conversation(external_id="first")
    second = conversation(external_id="second")
    first["messages"][0]["text"] = "first snapshot"
    second["messages"][0]["text"] = "stale snapshot"

    class ConcurrentCapture(Remote):
        async def wait_ready(self, source_id):
            await super().wait_ready(source_id)
            if self.sources[source_id]["title"].startswith("SEG-001-"):
                second["messages"][0]["text"] = "fresh snapshot"
                with Store(path, existing=True) as writer:
                    writer.ingest(second)

    with Store(path) as store:
        store.ingest(first, consent=True)
        store.ingest(second, consent=True)
        remote = ConcurrentCapture()
        engine = SyncEngine(store, SegmentBuilder(store, target_words=3), remote, tmp_path / "md")
        result = await engine.run_once()
        assert result["blocked"] == []
        assert result["completed"] == 2
        assert result["status"]["sync_status"] == "up_to_date"
        assert len(remote.sources) == 2
        assert any("fresh snapshot" in row["text"] for row in remote.sources.values())
        assert all("stale snapshot" not in row["text"] for row in remote.sources.values())


@pytest.mark.asyncio
async def test_sync_rechecks_pending_snapshot_after_remote_inventory(tmp_path):
    path = tmp_path / "db"
    c = conversation()
    c["messages"][0]["text"] = "stale snapshot"

    class ConcurrentCapture(Remote):
        changed = False

        async def list_sources(self):
            if not self.changed:
                self.changed = True
                c["messages"][0]["text"] = "fresh snapshot"
                with Store(path, existing=True) as writer:
                    writer.ingest(c)
            return await super().list_sources()

    with Store(path) as store:
        store.ingest(c, consent=True)
        remote = ConcurrentCapture()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        result = await engine.run_once()
        assert result["blocked"] == []
        assert result["completed"] == 1
        assert result["status"]["sync_status"] == "up_to_date"
        [source] = remote.sources.values()
        assert "fresh snapshot" in source["text"]
        assert "stale snapshot" not in source["text"]
        assert sum(kind == "upload" for kind, _ in remote.operations) == 1


@pytest.mark.asyncio
async def test_sync_defers_a_segment_that_changes_again_during_the_retry(tmp_path):
    path = tmp_path / "db"
    c = conversation()

    class BusyCapture(Remote):
        checks = 0

        async def list_sources(self):
            self.checks += 1
            c["messages"][0]["text"] = f"capture edit {self.checks}"
            with Store(path, existing=True) as writer:
                writer.ingest(c)
            return await super().list_sources()

    with Store(path) as store:
        store.ingest(c, consent=True)
        remote = BusyCapture()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        result = await engine.run_once()
        assert result["blocked"] == []
        assert result["completed"] == 0
        assert result["deferred"] == 1
        assert result["status"]["sync_status"] == "pending"
        assert remote.checks == 2
        assert not remote.operations


@pytest.mark.asyncio
async def test_replace_waits_for_ready_and_unchanged_sync_is_noop(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        b = SegmentBuilder(store)
        remote = Remote()
        engine = SyncEngine(store, b, remote, tmp_path / "segments")
        await engine.run_once()
        old_id = next(iter(remote.sources))
        before = len(remote.operations)
        await engine.run_once()
        assert len(remote.operations) == before
        c["messages"][0]["text"] = "new revision"
        store.ingest(c)
        remote.fail_ready = True
        await engine.run_once()
        assert old_id in remote.sources
        assert store.connection.execute("SELECT source_id FROM source_map").fetchone()[0] == old_id
        remote.fail_ready = False
        make_retries_due(store)
        await engine.run_once()
        assert old_id not in remote.sources
        assert len(remote.sources) == 1
        assert remote.operations[-2][0] == "ready"
        assert remote.operations[-1] == ("delete", old_id)
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_ambiguous_upload_reconciles_after_restart_without_duplicate(tmp_path):
    remote = Remote()
    remote.lose_response = True
    path = tmp_path / "db"
    with Store(path) as store:
        store.ingest(conversation(), consent=True)
        await SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md").run_once()
        job = store.connection.execute("SELECT * FROM sync_jobs").fetchone()
        assert job["attempts"] == 1
        assert job["next_attempt"] > time.time()
        assert len(remote.sources) == 1
    with Store(path) as store:
        make_retries_due(store)
        await SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md").run_once()
        assert len([o for o in remote.operations if o[0] == "upload"]) == 1
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_offline_retry_and_cleanup_persist(tmp_path):
    remote = Remote()
    remote.fail_upload = True
    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        assert store.status()["pending_jobs"] == 1
        attempts = len(remote.operations)
        await engine.run_once()
        assert len(remote.operations) == attempts
        make_retries_due(store)
        remote.fail_upload = False
        await engine.run_once()
        assert store.status()["sync_status"] == "up_to_date"
        c["messages"][0]["text"] = "edited message"
        store.ingest(c)
        remote.fail_delete = True
        await engine.run_once()
        assert len(remote.sources) == 2
        assert store.status()["pending_jobs"] == 1
        uploads = len([o for o in remote.operations if o[0] == "upload"])
        make_retries_due(store)
        remote.fail_delete = False
        await engine.run_once()
        assert len(remote.sources) == 1
        assert len([o for o in remote.operations if o[0] == "upload"]) == uploads


@pytest.mark.asyncio
async def test_real_49_source_overflow_uses_only_one_spare_slot(tmp_path):
    with Store(tmp_path / "db") as store:
        for n in range(50):
            c = conversation(external_id=f"conversation-{n}")
            c["messages"][0]["text"] = f"message {n}"
            store.ingest(c, consent=True)
        b = SegmentBuilder(store, target_words=2, max_words=10, max_active=49)
        remote = Remote()
        engine = SyncEngine(store, b, remote, tmp_path / "md")
        await engine.run_once()
        assert remote.peak_sources == 50
        assert len(remote.sources) <= 49
        assert store.status()["messages"] == 50
        assert store.status()["unassigned_messages"] == 0
        assert store.status()["sync_status"] == "up_to_date"
        uploaded = "\n".join(s["text"] for s in remote.sources.values())
        for n in range(50):
            assert f"message {n}\n" in uploaded


@pytest.mark.asyncio
async def test_upload_masks_credentials_and_preserves_the_local_original(tmp_path):
    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=False)
        remote = Remote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        assert remote.operations == []
        secret_value = "SyntheticPass987654!"
        secret_text = f"password = '{secret_value}'"
        secret = conversation(
            external_id="synthetic-secret",
            title="Credentials test",
            messages=[dict(conversation()["messages"][0], text=secret_text)],
        )
        store.ingest(secret, consent=True)
        result = await engine.run_once()
        assert not result["blocked"]
        assert len(remote.sources) == 1
        uploaded = next(iter(remote.sources.values()))["text"]
        expected_redacted = f"password = '{'*' * len(secret_value)}'"
        assert expected_redacted in uploaded
        assert secret_value not in uploaded
        conversation_id = store.connection.execute(
            "SELECT id FROM conversations WHERE external_id='synthetic-secret'"
        ).fetchone()[0]
        assert store.get_conversation(conversation_id)["messages"][0]["text"] == secret_text
        assert (
            store.connection.execute(
                "SELECT consent FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()[0]
            == 1
        )
        status = store.status()
        assert status["sync_status"] == "pending"
        assert status["ready_conversations"] == status["pending_conversations"] == 1
        assert remote.operations[0][0] == "upload"
        snapshot = next((tmp_path / "md").glob("*.md"), None)
        assert snapshot is None  # completed sync removes its immutable local snapshot


@pytest.mark.asyncio
async def test_existing_foreign_sources_respected_at_quota(tmp_path):
    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = Remote()
        remote.sources = {f"foreign-{n}": {"title": "foreign", "ready": True} for n in range(50)}
        result = await SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md").run_once()
        assert len(remote.sources) == 50
        assert remote.operations == []
        assert result["blocked"]


@pytest.mark.asyncio
async def test_sync_lock_blocks_second_worker(tmp_path):
    import fcntl

    with Store(tmp_path / "db") as store:
        with open(str(store.path) + ".sync.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="worker"):
                await SyncEngine(store, SegmentBuilder(store), Remote(), tmp_path / "md").run_once()


@pytest.mark.asyncio
async def test_edit_during_failed_merge_invalidates_retired_parent_citation(tmp_path):
    from ai_memory.gateway import _reference_sources

    with Store(tmp_path / "db") as store:
        remote = Remote()
        builder = SegmentBuilder(store, target_words=2, max_words=10, max_active=3)
        for n in range(3):
            c = conversation(external_id=f"c{n}")
            c["messages"][0]["text"] = f"message {n}"
            store.ingest(c, consent=True)
        engine = SyncEngine(store, builder, remote, tmp_path / "md")
        await engine.run_once()
        parent = store.connection.execute(
            "SELECT source_id FROM source_map WHERE segment_id='SEG-001'"
        ).fetchone()[0]
        cited = remote.sources[parent]["text"]
        c = conversation(external_id="c3")
        c["messages"][0]["text"] = "message 3"
        store.ingest(c, consent=True)
        remote.fail_ready = True
        await engine.run_once()
        assert (
            store.connection.execute("SELECT state FROM segments WHERE id='SEG-001'").fetchone()[0]
            == "retired"
        )
        c = conversation(external_id="c0")
        c["messages"][0]["text"] = "edited zero"
        store.ingest(c)
        refs = _reference_sources(store, [{"source_id": parent, "cited_text": cited}])
        assert all("message_ids" not in r for r in refs)


@pytest.mark.asyncio
async def test_invalid_edited_segment_is_never_enqueued_or_uploaded(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "small"
        store.ingest(c, consent=True)
        builder = SegmentBuilder(store, target_words=10, max_words=20)
        builder.build()
        c["messages"][0]["text"] = "huge " * 21
        store.ingest(c)
        remote = Remote()
        result = await SyncEngine(store, builder, remote, tmp_path / "md").run_once()
        assert result["blocked"]
        assert remote.operations == []
        assert store.connection.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_terminal_partial_upload_can_recover_without_deleting_old_source(tmp_path):
    from ai_memory.notebook import NotebookLMError

    class PartialRemote(Remote):
        partial_id = None

        async def upload(self, path, title):
            sid = await super().upload(path, title)
            if self.partial_id is None:
                self.partial_id = sid
                error = NotebookLMError("partial upload failed")
                error.source_id = sid
                raise error
            return sid

        async def wait_ready(self, source_id):
            if source_id == self.partial_id:
                raise NotebookLMError("terminal processing failed", code="processing_failed")
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = PartialRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        for _ in range(2):
            make_retries_due(store)
            await engine.run_once()
        assert remote.partial_id not in remote.sources
        assert len(remote.sources) == 1
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_known_body_upload_failure_reuploads_partial_and_keeps_mapping_until_ready(tmp_path):
    from ai_memory.notebook import NotebookLMError

    class BodyFailureRemote(Remote):
        fail_finalize = False
        fail_ready_once = None

        async def upload(self, path, title):
            sid = await super().upload(path, title)
            if self.fail_finalize:
                self.fail_finalize = False
                error = NotebookLMError("upload body failed", code="network_error")
                error.definitely_not_sent = True  # Explicit rejection before body transfer.
                error.source_id = sid
                raise error
            return sid

        async def wait_ready(self, source_id):
            if source_id == self.fail_ready_once:
                self.fail_ready_once = None
                self.operations.append(("ready", source_id))
                raise NotebookLMError("source is not ready", code="not_ready")
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        remote = BodyFailureRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        original_source = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]

        c["messages"][0]["text"] = "edited content that requires a safe replacement"
        store.ingest(c)
        remote.fail_finalize = True
        await engine.run_once()
        upload_job = store.connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC").fetchone()
        failed_partial = upload_job["new_source_id"]
        assert upload_job["stage"] == "processing"
        assert upload_job["error"] == "network_error"
        assert (
            store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
            == original_source
        )

        make_retries_due(store)
        remote.fail_ready_once = "source-4"
        await engine.run_once()
        retry_job = store.connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC").fetchone()
        assert retry_job["stage"] == "processing"
        assert retry_job["new_source_id"] == "source-4"
        assert retry_job["error"] == "not_ready"
        assert failed_partial in retry_job["old_source_ids"]
        assert failed_partial in remote.sources
        assert (
            store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
            == original_source
        )

        uploads = [op for op in remote.operations if op[0] == "upload"]
        assert len(uploads) == 3  # original source, failed body, safe retry
        make_retries_due(store)
        await engine.run_once()
        assert len([op for op in remote.operations if op[0] == "upload"]) == 3
        assert (
            store.connection.execute("SELECT source_id FROM source_map").fetchone()[0] == "source-4"
        )
        assert set(remote.sources) == {"source-4"}
        ready_index = remote.operations.index(("ready", "source-4"))
        assert ready_index < remote.operations.index(("delete", failed_partial))
        assert ready_index < remote.operations.index(("delete", original_source))
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_known_upload_failure_reconciles_partial_that_is_already_ready(tmp_path):
    from ai_memory.notebook import NotebookLMError

    class ReadyPartialRemote(Remote):
        fail_finalize = False

        async def upload(self, path, title):
            sid = await super().upload(path, title)
            if self.fail_finalize:
                self.fail_finalize = False
                self.sources[sid]["ready"] = True
                error = NotebookLMError("finalize response failed", code="network_error")
                error.source_id = sid
                raise error
            return sid

    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        remote = ReadyPartialRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        old_source = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]

        c["messages"][0]["text"] = "edited content"
        store.ingest(c)
        remote.fail_finalize = True
        await engine.run_once()
        failed_job = store.connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC").fetchone()
        partial_id = failed_job["new_source_id"]
        assert failed_job["error"] == "network_error"
        assert remote.sources[partial_id]["ready"]

        make_retries_due(store)
        await engine.run_once()
        assert len([op for op in remote.operations if op[0] == "upload"]) == 2
        assert (
            store.connection.execute("SELECT source_id FROM source_map").fetchone()[0] == partial_id
        )
        assert set(remote.sources) == {partial_id}
        assert remote.operations.index(("ready", partial_id)) < remote.operations.index(
            ("delete", old_source)
        )


@pytest.mark.asyncio
async def test_terminal_error_never_adopts_ready_source_with_a_different_title(tmp_path):
    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        builder = SegmentBuilder(store)
        builder.build()
        segment = store.connection.execute("SELECT * FROM segments").fetchone()
        original, safe_text, safe_title = SyncEngine(
            store, builder, Remote(), tmp_path / "md"
        )._prepare_snapshot(segment["id"], segment["revision"])
        assert safe_text != original and "archive_receipt: " in safe_text
        md = tmp_path / "md"
        md.mkdir()
        (md / safe_title).write_text(safe_text, encoding="utf-8")
        with store.connection:
            store.connection.execute(
                """INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id,error)
                VALUES(?,?,?,'processing','foreign-ready','network_error')""",
                (segment["id"], segment["revision"], safe_title),
            )
        remote = Remote()
        remote.sources["foreign-ready"] = {
            "title": "foreign-source.md",
            "ready": True,
            "text": "unrelated source",
        }

        await SyncEngine(store, builder, remote, md).run_once()
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped != "foreign-ready"
        assert "foreign-ready" in remote.sources
        assert len([op for op in remote.operations if op[0] == "upload"]) == 1


@pytest.mark.asyncio
async def test_failed_partial_at_notebook_quota_yields_only_its_uncommitted_slot(tmp_path):
    from ai_memory.notebook import NotebookLMError

    class QuotaRemote(Remote):
        fail_finalize = False

        async def upload(self, path, title):
            sid = await super().upload(path, title)
            if self.fail_finalize:
                self.fail_finalize = False
                error = NotebookLMError("upload body failed", code="network_error")
                error.definitely_not_sent = True  # Explicit rejection before body transfer.
                error.source_id = sid
                raise error
            return sid

    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        remote = QuotaRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        old_source = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        foreign_ids = {f"foreign-{n}" for n in range(48)}
        remote.sources.update(
            {
                source_id: {"title": source_id, "ready": True, "text": "foreign"}
                for source_id in foreign_ids
            }
        )

        c["messages"][0]["text"] = "edited quota-bound message"
        store.ingest(c)
        remote.fail_finalize = True
        await engine.run_once()
        job = store.connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC").fetchone()
        partial_id = job["new_source_id"]
        assert job["error"] == "network_error"
        assert len(remote.sources) == 50
        assert (
            store.connection.execute("SELECT source_id FROM source_map").fetchone()[0] == old_source
        )

        make_retries_due(store)
        await engine.run_once()
        new_source = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert new_source != old_source
        assert new_source != partial_id
        assert set(remote.sources) == foreign_ids | {new_source}
        retry_upload_index = max(
            index
            for index, operation in enumerate(remote.operations)
            if operation == ("upload", job["title"])
        )
        assert remote.operations.index(("delete", partial_id)) < retry_upload_index
        assert remote.operations.index(("ready", new_source)) < remote.operations.index(
            ("delete", old_source)
        )
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_foreign_source_budget_preserves_a_spare_and_merges_before_growth(tmp_path):
    with Store(tmp_path / "db") as store:
        remote = Remote()
        remote.sources["foreign"] = {"title": "Foreign", "ready": True, "text": "Foreign source"}
        builder = SegmentBuilder(store, target_words=2, max_words=10, max_active=49)
        for n in range(49):
            c = conversation(external_id=f"foreign-budget-{n}")
            c["messages"][0]["text"] = f"message {n}"
            store.ingest(c, consent=True)
        result = await SyncEngine(store, builder, remote, tmp_path / "md").run_once()
        assert result["status"]["sync_status"] == "up_to_date"
        assert len(remote.sources) <= 49
        assert remote.peak_sources <= 50
        assert "foreign" in remote.sources


@pytest.mark.asyncio
async def test_tampered_pending_snapshot_is_never_uploaded(tmp_path):
    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = Remote()
        remote.fail_upload = True
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        snapshot = next((tmp_path / "md").glob("*.md"))
        snapshot.write_text("unvalidated altered source")
        make_retries_due(store)
        remote.fail_upload = False
        result = await engine.run_once()
        assert result["blocked"]
        assert not remote.sources


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,error", [("pending", None), ("blocked", "SensitiveData")])
@pytest.mark.parametrize("snapshot_exists", [True, False])
async def test_legacy_sensitive_pending_snapshot_is_redacted_before_resume(
    tmp_path, stage, error, snapshot_exists
):
    secret = "password = 'SyntheticPass987654!'"
    with Store(tmp_path / "db") as store:
        store.ingest(
            conversation(
                external_id="secret",
                title="Secret",
                messages=[dict(conversation()["messages"][0], text=secret)],
            ),
            consent=True,
        )
        builder = SegmentBuilder(store, target_words=1)
        builder.build()
        segment = store.connection.execute("SELECT * FROM segments").fetchone()
        old_snapshot = builder.render(segment["id"])
        old_title = f"{segment['id']}-r{segment['revision']}-{digest(old_snapshot)[:16]}.md"
        md = tmp_path / "md"
        md.mkdir()
        old_path = md / old_title
        if snapshot_exists:
            old_path.write_text(old_snapshot, encoding="utf-8")
        with store.connection:
            cursor = store.connection.execute(
                "INSERT INTO sync_jobs(segment_id,revision,title,stage,error,attempts) VALUES(?,?,?,?,?,1)",
                (segment["id"], segment["revision"], old_title, stage, error),
            )
        job_id = cursor.lastrowid
        remote = Remote()
        engine = SyncEngine(store, builder, remote, md)
        result = await engine.run_once()
        assert not result["blocked"]
        assert len(remote.sources) == 1
        uploaded = next(iter(remote.sources.values()))["text"]
        assert "SyntheticPass987654!" not in uploaded
        assert "*" * len("SyntheticPass987654!") in uploaded
        job = store.connection.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
        assert job["stage"] == "done"
        assert job["title"] != old_title
        from hashlib import sha256

        assert (
            job["title"]
            == f"{segment['id']}-r{segment['revision']}-{sha256(uploaded.encode()).hexdigest()[:16]}.md"
        )
        assert not old_path.exists()
        assert (
            store.get_conversation(
                store.connection.execute(
                    "SELECT id FROM conversations WHERE external_id='secret'"
                ).fetchone()[0]
            )["messages"][0]["text"]
            == secret
        )


def add_legacy_source(engine_store, builder, remote, segment_id="SEG-001"):
    segment = engine_store.connection.execute(
        "SELECT * FROM segments WHERE id=?", (segment_id,)
    ).fetchone()
    original = builder.render(segment_id)
    title = f"{segment_id}-r{segment['revision']}-{digest(original)[:16]}.md"
    with engine_store.connection:
        engine_store.connection.execute(
            "INSERT INTO source_map(segment_id,source_id,revision) VALUES(?,?,?)",
            (segment_id, "legacy-raw", segment["revision"]),
        )
    remote.sources["legacy-raw"] = {"title": title, "ready": True, "text": original}
    return title, original


@pytest.mark.asyncio
async def test_legacy_mapped_raw_source_replaced_but_unproven_copy_never_deleted(tmp_path):
    secret = "password = 'SyntheticPass987654!'"
    with Store(tmp_path / "db") as store:
        store.ingest(
            conversation(
                external_id="mapped-legacy",
                messages=[dict(conversation()["messages"][0], text=secret)],
            ),
            consent=True,
        )
        builder = SegmentBuilder(store)
        builder.build()
        remote = Remote()
        old_title, original = add_legacy_source(store, builder, remote)
        result = await SyncEngine(store, builder, remote, tmp_path / "md").run_once()
        assert not result["blocked"]
        assert "SyntheticPass987654!" in original
        assert remote.sources["legacy-raw"]["text"] == original
        published = [row for sid, row in remote.sources.items() if sid != "legacy-raw"]
        assert len(published) == 1 and "SyntheticPass987654!" not in published[0]["text"]
        assert not [op for op in remote.operations if op[0] == "delete"]
        assert store.connection.execute("SELECT revision FROM segments").fetchone()[0] == 2
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped in remote.sources
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_explicit_legacy_cleanup_retries_completed_replacement(tmp_path):
    from ai_memory.diagnostics import PublicInputError
    from ai_memory.publication import cleanup_legacy_sources

    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        builder = SegmentBuilder(store)
        builder.build()
        remote = Remote()
        title, old_text = add_legacy_source(store, builder, remote)
        with store.connection:
            store.connection.execute(
                """INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id)
                VALUES('SEG-001',1,?,'done','legacy-raw')""",
                (title,),
            )
        remote.sources["same-title"] = {"title": title, "ready": True, "text": old_text}
        await SyncEngine(store, builder, remote, tmp_path / "md").run_once()
        (current,) = store.connection.execute("SELECT source_id FROM source_map").fetchone()
        assert set(remote.sources) == {"legacy-raw", "same-title", current}
        before = remote.operations.copy()

        preview = await cleanup_legacy_sources(store, remote, [])
        assert [row["source_id"] for row in preview["candidates"]] == ["legacy-raw"]
        assert remote.operations == before
        for source_id in ("same-title", current):
            with pytest.raises(PublicInputError):
                await cleanup_legacy_sources(store, remote, ["legacy-raw", source_id])

        remote.sources[current]["ready"] = False
        with pytest.raises(PublicInputError):
            await cleanup_legacy_sources(store, remote, ["legacy-raw"])
        remote.sources[current]["ready"] = True
        with store.connection:
            store.connection.execute("INSERT INTO retained_sources VALUES('legacy-raw')")
        with pytest.raises(PublicInputError):
            await cleanup_legacy_sources(store, remote, ["legacy-raw"])
        with store.connection:
            store.connection.execute("DELETE FROM retained_sources")
        assert remote.operations == before

        result = await cleanup_legacy_sources(store, remote, ["legacy-raw"])
        assert result["deleted"] == ["legacy-raw"]
        assert result["candidates"] == []
        assert set(remote.sources) == {"same-title", current}
        assert remote.operations[len(before) :] == [("delete", "legacy-raw")]
        assert (await cleanup_legacy_sources(store, remote, ["legacy-raw"]))["already_absent"] == [
            "legacy-raw"
        ]
        assert (await cleanup_legacy_sources(store, remote, []))["candidates"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["uploading", "processing"])
async def test_legacy_inflight_unproven_source_never_claimed_or_deleted(tmp_path, stage):
    secret = "password = 'SyntheticPass987654!'"
    with Store(tmp_path / "db") as store:
        store.ingest(
            conversation(
                external_id=f"inflight-{stage}",
                messages=[dict(conversation()["messages"][0], text=secret)],
            ),
            consent=True,
        )
        builder = SegmentBuilder(store)
        builder.build()
        segment = store.connection.execute("SELECT * FROM segments").fetchone()
        original = builder.render(segment["id"])
        old_title = f"{segment['id']}-r{segment['revision']}-{digest(original)[:16]}.md"
        snapshot_dir = tmp_path / "md"
        snapshot_dir.mkdir()
        (snapshot_dir / old_title).write_text(original, encoding="utf-8")
        remote = Remote()
        remote.sources["legacy-raw"] = {"title": old_title, "ready": False, "text": original}
        with store.connection:
            cursor = store.connection.execute(
                """INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id)
                VALUES(?,?,?,?,?)""",
                (
                    segment["id"],
                    segment["revision"],
                    old_title,
                    stage,
                    "legacy-raw" if stage == "processing" else None,
                ),
            )
        if stage == "processing":
            with store.connection:
                store.connection.execute(
                    "UPDATE sync_jobs SET old_source_ids='[]' WHERE id=?", (cursor.lastrowid,)
                )
        result = await SyncEngine(store, builder, remote, snapshot_dir).run_once()
        if stage == "uploading":
            assert result["blocked"][0]["reason"] == "UploadUncertain"
            await SyncEngine(store, builder, remote, snapshot_dir).run_once()
        assert remote.sources["legacy-raw"]["text"] == original
        assert not [op for op in remote.operations if op[0] == "delete"]
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped != "legacy-raw"
        assert "SyntheticPass987654!" not in remote.sources[mapped]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pending", "processing"])
async def test_stale_raw_source_cleanup_moves_to_current_revision_job(tmp_path, stage):
    secret = "password = 'SyntheticPass987654!';"
    with Store(tmp_path / "db") as store:
        store.ingest(
            conversation(
                external_id="stale-inflight",
                messages=[dict(conversation()["messages"][0], text=secret)],
            ),
            consent=True,
        )
        builder = SegmentBuilder(store)
        builder.build()
        segment = store.connection.execute("SELECT * FROM segments").fetchone()
        original = builder.render(segment["id"])
        old_title = f"{segment['id']}-r{segment['revision']}-{digest(original)[:16]}.md"
        snapshot_dir = tmp_path / "md"
        snapshot_dir.mkdir()
        old_path = snapshot_dir / old_title
        old_path.write_text(original, encoding="utf-8")
        remote = Remote()
        remote.sources["legacy-raw"] = {
            "title": old_title,
            "ready": False,
            "text": original,
        }
        with store.connection:
            store.connection.execute(
                """INSERT INTO sync_jobs(segment_id,revision,title,stage,new_source_id,old_source_ids)
                VALUES(?,?,?,?,?,?)""",
                (
                    segment["id"],
                    segment["revision"],
                    old_title,
                    stage,
                    "legacy-raw" if stage == "processing" else None,
                    "[]" if stage == "processing" else '["legacy-raw"]',
                ),
            )

        current = store.get_conversation(
            store.connection.execute("SELECT id FROM conversations").fetchone()[0]
        )
        current["messages"][0]["text"] = "A newer local decision."
        store.ingest(current, consent=True)

        result = await SyncEngine(store, builder, remote, snapshot_dir).run_once()

        assert not result["blocked"]
        assert "legacy-raw" in remote.sources and remote.sources["legacy-raw"]["text"] == original
        assert not [op for op in remote.operations if op[0] == "delete"]
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert (
            mapped != "legacy-raw" and "SyntheticPass987654!" not in remote.sources[mapped]["text"]
        )
        jobs = store.connection.execute("SELECT stage FROM sync_jobs ORDER BY id").fetchall()
        assert [r[0] for r in jobs] == ["superseded", "done"]


@pytest.mark.asyncio
async def test_auth_failure_has_safe_status_retry_and_rate_limited_log(tmp_path, caplog):
    from ai_memory.notebook import NotebookLMAuthError

    class AuthRemote(Remote):
        async def list_sources(self):
            raise NotebookLMAuthError()

    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        engine = SyncEngine(store, SegmentBuilder(store), AuthRemote(), tmp_path / "md")
        result = await engine.run_once()
        failure = result["blocked"][0]
        assert failure["reason"] == "auth_required"
        assert failure["next_attempt"] > time.time()
        assert failure["attempts"] == 1
        assert result["status"]["sync_errors"][0]["reason"] == "auth_required"
        await engine.run_once()
        assert len(caplog.records) == 1
        assert "auth_required" in caplog.text and "retry" in caplog.text
        assert "Эх текст" not in caplog.text


@pytest.mark.asyncio
async def test_failed_unregistered_upload_then_edit_recovers_without_losing_revision(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        c["messages"][0]["text"] = "old exact content"
        store.ingest(c, consent=True)
        remote = Remote()
        remote.fail_upload = True
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        first = store.connection.execute("SELECT * FROM sync_jobs").fetchone()
        assert first["stage"] == "pending" and not remote.sources
        c["messages"][0]["text"] = "new exact content"
        store.ingest(c)
        remote.fail_upload = False
        make_retries_due(store)
        result = await engine.run_once()
        assert not result["blocked"]
        assert result["status"]["sync_status"] == "up_to_date"
        assert len(remote.sources) == 1
        assert "new exact content\n" in next(iter(remote.sources.values()))["text"]
        assert [
            r[0] for r in store.connection.execute("SELECT stage FROM sync_jobs ORDER BY id")
        ] == ["superseded", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_state", ["missing", "failed", "unknown"])
async def test_resumed_cleanup_preserves_predecessor_until_replacement_verified(
    tmp_path, replacement_state
):
    from ai_memory.notebook import NotebookLMError

    class UnreadyRemote(Remote):
        rejected_id = None

        async def wait_ready(self, source_id):
            if source_id == self.rejected_id:
                code = "processing_failed" if replacement_state == "failed" else "not_ready"
                raise NotebookLMError("Synthetic readiness failure", code)
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        remote = UnreadyRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        predecessor = next(iter(remote.sources))
        c["messages"][0]["text"] = "replacement exact text"
        store.ingest(c)
        remote.fail_delete = True
        await engine.run_once()
        job = store.connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC").fetchone()
        replacement = job["new_source_id"]
        assert job["stage"] == "cleanup"
        if replacement_state == "missing":
            del remote.sources[replacement]
        else:
            remote.sources[replacement]["ready"] = False
            remote.rejected_id = replacement
        remote.fail_delete = False
        before = len(remote.operations)
        make_retries_due(store)
        result = await engine.run_once()
        if replacement_state == "missing" and predecessor not in remote.sources:
            # A repaired replacement may authorize cleanup, but only after ready.
            repaired = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
            assert repaired in remote.sources and remote.sources[repaired]["ready"]
            operations = remote.operations[before:]
            assert operations.index(("ready", repaired)) < operations.index(("delete", predecessor))
        else:
            assert predecessor in remote.sources
            assert result["status"]["sync_status"] == "pending"
            if replacement_state == "failed":
                make_retries_due(store)
                recovered = await engine.run_once()
                assert not recovered["blocked"]
                assert recovered["status"]["sync_status"] == "up_to_date"
                repaired = store.connection.execute("SELECT source_id FROM source_map").fetchone()[
                    0
                ]
                assert repaired not in {predecessor, replacement}
                assert set(remote.sources) == {repaired}
                operations = remote.operations[before:]
                assert operations.index(("ready", repaired)) < operations.index(
                    ("delete", predecessor)
                )


@pytest.mark.asyncio
async def test_missing_current_managed_source_requires_republish_by_identity(tmp_path):
    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = Remote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        lost = next(iter(remote.sources))
        del remote.sources[lost]
        remote.sources["foreign"] = {"title": "Foreign", "ready": True, "text": "untouched"}
        result = await engine.run_once()
        assert not result["blocked"]
        assert result["status"]["sync_status"] == "pending"
        assert set(remote.sources) == {"foreign"}
        assert store.connection.execute("SELECT stage FROM sync_jobs").fetchone()[0] == "missing"
        from ai_memory.sync import republish

        assert republish(store, "SEG-001")["republish_authorized"] == "SEG-001"
        result = await engine.run_once()
        assert not result["blocked"]
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped != lost and set(remote.sources) == {"foreign", mapped}
        assert result["status"]["sync_status"] == "up_to_date"


@pytest.mark.asyncio
async def test_inventory_failure_is_unknown_and_preserves_current_mapping(tmp_path):
    class InventoryRemote(Remote):
        offline = False

        async def list_sources(self):
            if self.offline:
                raise ConnectionError("Synthetic inventory failure")
            return await super().list_sources()

    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = InventoryRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        remote.offline = True
        result = await engine.run_once()
        assert result["blocked"]
        assert result["status"]["sync_status"] == "pending"
        assert store.connection.execute("SELECT source_id FROM source_map").fetchone()[0] == mapped
        assert mapped in remote.sources


@pytest.mark.asyncio
async def test_foreign_capacity_compacts_open_tail_and_preserves_exact_text(tmp_path):
    with Store(tmp_path / "db") as store:
        for n in range(2):
            c = conversation(external_id=f"tiny-{n}")
            c["messages"][0]["text"] = f"exact text {n}"
            store.ingest(c, consent=True)
        remote = Remote()
        foreign = {f"foreign-{n}" for n in range(48)}
        remote.sources.update({sid: {"title": sid, "ready": True, "text": sid} for sid in foreign})
        builder = SegmentBuilder(store, target_words=3, max_words=20)
        engine = SyncEngine(store, builder, remote, tmp_path / "md")
        result = await engine.run_once()
        assert not result["blocked"]
        assert result["status"]["sync_status"] == "up_to_date"
        assert len(remote.sources) == 49 and remote.peak_sources <= 50
        assert foreign <= set(remote.sources)
        managed = [source for sid, source in remote.sources.items() if sid not in foreign]
        assert len(managed) == 1
        assert "exact text 0\n" in managed[0]["text"]
        assert "exact text 1\n" in managed[0]["text"]


@pytest.mark.asyncio
async def test_missing_cleanup_replacement_after_another_edit_transfers_obligations(tmp_path):
    with Store(tmp_path / "db") as store:
        c = conversation()
        store.ingest(c, consent=True)
        remote = Remote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        predecessor = next(iter(remote.sources))
        c["messages"][0]["text"] = "first replacement"
        store.ingest(c)
        remote.fail_delete = True
        await engine.run_once()
        replacement = store.connection.execute(
            "SELECT new_source_id FROM sync_jobs WHERE stage='cleanup'"
        ).fetchone()[0]
        del remote.sources[replacement]
        c["messages"][0]["text"] = "newest replacement exact text"
        store.ingest(c)
        remote.fail_delete = False
        make_retries_due(store)
        result = await engine.run_once()
        assert not result["blocked"]
        assert result["status"]["sync_status"] == "pending"
        assert set(remote.sources) == {predecessor}
        from ai_memory.sync import republish

        republish(store, "SEG-001")
        result = await engine.run_once()
        assert not result["blocked"]
        assert result["status"]["sync_status"] == "up_to_date"
        assert len(remote.sources) == 1
        assert predecessor not in remote.sources
        assert "newest replacement exact text\n" in next(iter(remote.sources.values()))["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["processing_failed", "not_ready"])
async def test_unready_current_mapping_uses_queue_backoff_and_can_recover(tmp_path, code):
    from ai_memory.notebook import NotebookLMError

    class ReadinessRemote(Remote):
        rejected_id = None
        checks = 0

        async def wait_ready(self, source_id):
            if source_id == self.rejected_id:
                self.checks += 1
                raise NotebookLMError("Synthetic source not ready", code)
            await super().wait_ready(source_id)

    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = ReadinessRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        source_id = next(iter(remote.sources))
        remote.rejected_id = source_id
        remote.sources[source_id]["ready"] = False
        result = await engine.run_once()
        assert result["blocked"]
        job = store.connection.execute("SELECT * FROM sync_jobs").fetchone()
        assert job["stage"] != "done" and job["next_attempt"] > time.time()
        await engine.run_once()
        assert remote.checks == 1
        assert source_id in remote.sources
        make_retries_due(store)
        if code == "not_ready":
            remote.rejected_id = None
        result = await engine.run_once()
        assert not result["blocked"]
        assert result["status"]["sync_status"] == "up_to_date"
        assert result["status"]["last_verified_remote"]["eligible_sources"] == 1
        assert len(remote.sources) == 1
        if code == "processing_failed":
            assert source_id not in remote.sources


@pytest.mark.asyncio
@pytest.mark.parametrize("max_active", [2, 49])
async def test_full_budget_failed_upload_then_edit_and_new_conversation_recovers(
    tmp_path, max_active
):
    with Store(tmp_path / "db") as store:
        first = conversation(external_id="conversation-0")
        first["messages"][0]["text"] = "first original"
        store.ingest(first, consent=True)
        for n in range(1, max_active):
            item = conversation(external_id=f"conversation-{n}")
            item["messages"][0]["text"] = f"message {n}"
            store.ingest(item, consent=True)
        remote = Remote()
        builder = SegmentBuilder(store, target_words=2, max_words=10, max_active=max_active)
        engine = SyncEngine(store, builder, remote, tmp_path / "md")
        await engine.run_once()
        original_source = store.connection.execute(
            "SELECT source_id FROM source_map WHERE segment_id='SEG-001'"
        ).fetchone()[0]

        first["messages"][0]["text"] = "first interim"
        store.ingest(first)
        remote.fail_upload = True
        failed = await engine.run_once()
        assert failed["blocked"][0]["reason"] == "ConnectionError"
        first["messages"][0]["text"] = "first corrected"
        store.ingest(first)
        extra = conversation(external_id="overflow")
        extra["messages"][0]["text"] = "overflow message"
        store.ingest(extra, consent=True)
        make_retries_due(store)

        # Capacity pressure cannot invalidate the in-flight revision's hash or
        # delete the committed predecessor while the network is still down.
        retry = await engine.run_once()
        assert retry["blocked"][0]["reason"] == "ConnectionError"
        assert original_source in remote.sources
        assert not any(op == ("delete", original_source) for op in remote.operations)

        remote.fail_upload = False
        make_retries_due(store)
        recovered = await engine.run_once()
        assert not recovered["blocked"]
        assert recovered["status"]["sync_status"] == "up_to_date"
        assert recovered["status"]["unassigned_messages"] == 0
        assert recovered["status"]["messages"] == max_active + 1
        uploaded = "\n".join(source["text"] for source in remote.sources.values())
        assert "first corrected\n" in uploaded
        assert "first interim\n" not in uploaded
        assert "overflow message\n" in uploaded
        assert len(remote.sources) <= max_active
        assert remote.peak_sources <= max_active + 1


@pytest.mark.asyncio
async def test_already_locked_sync_keeps_shared_lock_until_operation_finishes(tmp_path):
    from ai_memory.archive_lock import archive_lock

    class LockCheckingRemote(Remote):
        async def upload(self, path, title):
            with pytest.raises(RuntimeError, match="active"):
                with archive_lock(store.path):
                    pytest.fail("Archive lock released before upload")
            return await super().upload(path, title)

    with Store(tmp_path / "db") as store:
        store.ingest(conversation(), consent=True)
        remote = LockCheckingRemote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        with archive_lock(store.path):
            result = await engine._run_once_locked()
        assert result["status"]["sync_status"] == "up_to_date"
        assert len(remote.sources) == 1


def test_snapshot_is_private_before_fsync_in_existing_public_directory(tmp_path, monkeypatch):
    import os
    import stat

    output = tmp_path / "public"
    output.mkdir(mode=0o755)
    observed_modes = []
    real_fsync = os.fsync

    def inspect_before_fsync(fd):
        observed_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        assert [file.read_text() for file in output.iterdir()] == ["synthetic snapshot"]
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", inspect_before_fsync)
    mask = os.umask(0o022)
    try:
        with Store(tmp_path / "db") as store:
            engine = SyncEngine(store, SegmentBuilder(store), Remote(), output)
            path = engine._write_snapshot("segment.md", "synthetic snapshot")
        assert observed_modes == [0o600]
        assert path.read_text() == "synthetic snapshot"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(output.stat().st_mode) == 0o755
        assert list(output.iterdir()) == [path]
    finally:
        os.umask(mask)


@pytest.mark.asyncio
async def test_first_sync_retains_notebook_binding_during_concurrent_rebind(tmp_path):
    class RebindingRemote(Remote):
        async def upload(self, path, title):
            with Store(db_path) as competing_store:
                with pytest.raises(RuntimeError, match="active"):
                    competing_store.set_setting("notebook_id", "notebook-B")
            return await super().upload(path, title)

    db_path = tmp_path / "archive.sqlite"
    with Store(db_path) as store:
        store.set_setting("notebook_id", "notebook-A")
        store.ingest(conversation(), consent=True)
        remote = RebindingRemote()
        result = await SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md").run_once()
        assert result["blocked"] == []
        assert result["status"]["sync_status"] == "up_to_date"
        assert store.get_setting("notebook_id") == "notebook-A"
        (mapping,) = store.connection.execute("SELECT source_id FROM source_map").fetchall()
        assert mapping["source_id"] in remote.sources
        with pytest.raises(ValueError, match="separate archive"):
            store.set_setting("notebook_id", "notebook-B")


def test_notebook_rebind_guard_cannot_overlap_first_sync(tmp_path):
    import asyncio
    import sqlite3

    db_path = tmp_path / "archive.sqlite"
    with Store(db_path) as settings, Store(db_path) as syncing:
        settings.set_setting("notebook_id", "notebook-A")
        settings.ingest(conversation(), consent=True)
        remote = Remote()
        engine = SyncEngine(syncing, SegmentBuilder(syncing), remote, tmp_path / "md")
        attempted = blocked = False

        def interleave(cursor, raw):
            nonlocal attempted, blocked
            row = sqlite3.Row(cursor, raw)
            if not attempted and cursor.description[0][0] == "COUNT(*)":
                # Pause the real setting setter after reading its empty mapping
                # guard, at the exact boundary where the audit raced first sync.
                attempted = True
                try:
                    asyncio.run(engine.run_once())
                except RuntimeError as error:
                    assert "active" in str(error)
                    blocked = True
            return row

        settings.connection.row_factory = interleave
        settings.set_setting("notebook_id", "notebook-B")
        assert attempted and blocked
        assert settings.get_setting("notebook_id") == "notebook-B"
        assert remote.sources == {}
        assert settings.connection.execute("SELECT COUNT(*) FROM source_map").fetchone()[0] == 0


async def test_sync_activity_is_visible_across_readers_and_cleared_on_cancel(tmp_path):
    import asyncio
    import json
    import os
    from pathlib import Path

    from ai_memory.sync_activity import read_sync_activity

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowRemote(Remote):
        async def upload(self, path, title):
            started.set()
            await release.wait()
            return await super().upload(path, title)

    path = tmp_path / "db"
    with Store(path) as store:
        store.ingest(conversation(), consent=True)
        task = asyncio.create_task(
            SyncEngine(store, SegmentBuilder(store), SlowRemote(), tmp_path / "md").run_once()
        )
        await asyncio.wait_for(started.wait(), 5)
        state = read_sync_activity(path)
        assert state is not None
        assert state["phase"] == "upload"
        assert state["segment_id"] == "SEG-001"
        assert state["started_at"] <= state["updated_at"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert read_sync_activity(path) is None

        # A stale record left by a crash never claims that a stopped worker runs.
        record = {
            "phase": "upload",
            "segment_id": "SEG-001",
            "started_at": 1,
            "updated_at": 2,
            "pid": os.getpid(),
            "process_start": "no-such-process-generation",
        }
        Path(str(path) + ".sync-progress.json").write_text(json.dumps(record))
        assert read_sync_activity(path) is None


def test_sync_activity_reader_handles_other_process_death(tmp_path):
    import subprocess
    import sys

    from ai_memory.sync_activity import read_sync_activity

    path = tmp_path / "archive.sqlite"
    source = """
import sys,time
from ai_memory.archive_lock import archive_lock
from ai_memory.sync_activity import sync_activity
with archive_lock(sys.argv[1]), sync_activity(sys.argv[1]) as publish:
    publish({"phase":"upload", "segment_id":"SEG-101"})
    print("ready", flush=True)
    time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", source, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        progress = read_sync_activity(path)
        assert progress and progress["phase"] == "upload"
        assert progress["segment_id"] == "SEG-101"
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
    assert read_sync_activity(path) is None
