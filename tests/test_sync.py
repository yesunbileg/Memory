import json
import time

import pytest
from test_store import conversation

from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
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
async def test_no_consent_or_secret_text_never_uploaded(tmp_path):
    with Store(tmp_path / "db") as store:
        store.ingest(conversation())
        remote = Remote()
        engine = SyncEngine(store, SegmentBuilder(store), remote, tmp_path / "md")
        await engine.run_once()
        assert remote.operations == []
        secret = conversation(
            external_id="synthetic-secret", title="password = secret" + "123456789"
        )
        store.ingest(secret, consent=True)
        result = await engine.run_once()
        assert remote.operations == []
        assert result["blocked"]
        assert "secret123" not in json.dumps(result)


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
async def test_privacy_blocked_job_does_not_starve_safe_jobs_or_leak(tmp_path, caplog):
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
        store.ingest(
            conversation(
                external_id="safe",
                title="Safe",
                messages=[dict(conversation()["messages"][0], text="safe message")],
            ),
            consent=True,
        )
        remote = Remote()
        engine = SyncEngine(store, SegmentBuilder(store, target_words=1), remote, tmp_path / "md")
        result = await engine.run_once()
        assert result["blocked"][0]["reason"] == "SensitiveData"
        assert result["completed"] == 1
        assert len(remote.sources) == 1
        assert "safe message" in next(iter(remote.sources.values()))["text"]
        job = store.connection.execute(
            "SELECT * FROM sync_jobs WHERE error='SensitiveData'"
        ).fetchone()
        assert job["stage"] == "blocked"
        assert job["next_attempt"] > time.time()
        assert (
            store.get_conversation(
                store.connection.execute(
                    "SELECT id FROM conversations WHERE external_id='secret'"
                ).fetchone()[0]
            )["messages"][0]["text"]
            == secret
        )
        await engine.run_once()
        assert len(caplog.records) == 1
        assert "SensitiveData" in caplog.text
        assert "retry" in caplog.text
        assert secret not in caplog.text + json.dumps(result)


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
