import time

import pytest
from test_store import conversation

from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store, digest
from ai_memory.sync import SyncEngine


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

    async def upload(self, path, title):
        self.operations.append(("upload", title))
        if self.fail_upload:
            raise ConnectionError("synthetic network failure")
        key = f"source-{len(self.operations)}"
        self.sources[key] = {"title": title, "ready": False, "text": path.read_text()}
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
        store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        store.connection.commit()
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
        store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        store.connection.commit()
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
        store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        store.connection.commit()
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
        store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
        store.connection.commit()
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
        store.ingest(conversation())
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
        assert store.status()["sync_status"] == "up_to_date"
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
                raise NotebookLMError("partial upload failed", source_id=sid)
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
            with store.connection:
                store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
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

        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
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
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
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

        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
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
        assert safe_text == original
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

        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
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
        with store.connection:
            store.connection.execute("UPDATE sync_jobs SET next_attempt=0")
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
        assert job["title"] == f"{segment['id']}-r{segment['revision']}-{digest(uploaded)[:16]}.md"
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
async def test_legacy_mapped_raw_source_is_replaced_before_it_is_deleted(tmp_path):
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
        assert "SyntheticPass987654!" not in remote.sources[next(iter(remote.sources))]["text"]
        assert len(remote.sources) == 1
        assert old_title not in [source["title"] for source in remote.sources.values()]
        operations = [op[0] for op in remote.operations]
        assert operations.index("ready") < operations.index("delete")
        assert store.connection.execute("SELECT revision FROM segments").fetchone()[0] == 2
        mapped = store.connection.execute("SELECT source_id FROM source_map").fetchone()[0]
        assert mapped in remote.sources
        assert store.status()["sync_status"] == "up_to_date"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["uploading", "processing"])
async def test_legacy_inflight_raw_source_is_replaced_not_reconciled_as_safe(tmp_path, stage):
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
        assert not result["blocked"]
        assert len(remote.sources) == 1
        safe_source = next(iter(remote.sources.values()))
        assert safe_source["text"] != original
        assert "SyntheticPass987654!" not in safe_source["text"]
        assert all(source["title"] != old_title for source in remote.sources.values())
        assert remote.operations.index(("ready", "source-1")) < remote.operations.index(
            ("delete", "legacy-raw")
        )
        job = store.connection.execute("SELECT * FROM sync_jobs").fetchone()
        assert job["stage"] == "done"
        assert (
            store.connection.execute("SELECT source_id FROM source_map").fetchone()[0] == "source-1"
        )


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
        assert len(remote.sources) == 1
        safe_source = next(iter(remote.sources.values()))
        assert "SyntheticPass987654!" not in safe_source["text"]
        assert "legacy-raw" not in remote.sources
        assert remote.operations.index(("ready", "source-1")) < remote.operations.index(
            ("delete", "legacy-raw")
        )
        jobs = store.connection.execute(
            "SELECT revision,stage,old_source_ids FROM sync_jobs ORDER BY id"
        ).fetchall()
        assert jobs[0]["stage"] == "superseded"
        assert jobs[1]["stage"] == "done"
        assert '"legacy-raw"' in jobs[1]["old_source_ids"]


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
