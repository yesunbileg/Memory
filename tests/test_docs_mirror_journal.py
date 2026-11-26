import pytest
from test_web_docs_sync import apply_editor_commands

from ai_memory.privacy import snapshot_sha256
from ai_memory.store import Store
from ai_memory.web_docs_journal import DocumentJournal
from ai_memory.web_docs_transport import DocsWebError


class Mirror:
    def __init__(self, text):
        self.text = text
        self.writes = []
        self.lose_reply = False

    async def call(self, action, *, id, known=None):
        digest = snapshot_sha256(self.text)
        if action == "inspect":
            return {"status": "ready" if digest == known else "changed", "hash": digest}
        if action == "prepare":
            return {"status": "ready" if digest == known else "conflict"}
        assert action == "check"
        return {"hash": digest, "revision": len(self.writes)}

    async def read_repair_base(self, doc):
        return {"text": self.text, **await self.call("check", id=doc)}

    async def write(self, doc, transaction, before, after, commands, *, recovering):
        assert before == snapshot_sha256(self.text)
        self.text = apply_editor_commands(self.text, commands)
        assert after == snapshot_sha256(self.text)
        self.writes.append(commands)
        if self.lose_reply:
            self.lose_reply = False
            raise DocsWebError("docs_web_uncertain")
        return len(str(commands))


async def test_unchanged_mirror_repairs_only_remote_difference(tmp_path):
    base = "Монгол 🧭\nUNCHANGED SENTINEL\nOriginal field\n"
    remote = Mirror(base)
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, remote, "document")
        journal._save({"text": base})
        assert await journal.sync_mirror(base) == 0
        remote.text = base.replace("Original field", "Accidental edit")
        assert await journal.sync_mirror(base) > 0
        assert journal.repaired
        assert "UNCHANGED SENTINEL" not in str(remote.writes)
        assert remote.text == base
        await journal.verify_remote(base)
        assert await journal.sync_mirror(base) == 0
        assert not journal.repaired
        assert len(remote.writes) == 1


async def test_mirror_recovers_lost_ack_after_restart_without_persisting_manual_text(tmp_path):
    archive = tmp_path / "archive.sqlite"
    trusted = "authoritative 🦦 text\n"
    remote = Mirror("manual data outside archive 🧭")
    with Store(archive) as store:
        journal = DocumentJournal(store, remote, "document")
        journal._save({"text": trusted})
        remote.lose_reply = True
        with pytest.raises(DocsWebError, match="could not confirm"):
            await journal.sync_mirror(trusted)
        assert journal._load() == {"text": trusted, "repair": True}
        assert remote.text == trusted
    with Store(archive, existing=True) as store:
        journal = DocumentJournal(store, remote, "document")
        assert await journal.sync_mirror(trusted) == 0
        assert journal.repaired
        assert len(remote.writes) == 1
        assert journal._load() == {"text": trusted}


async def test_mirror_restores_from_latest_sqlite_while_recovering(tmp_path):
    remote = Mirror("manual change")
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, remote, "document")
        journal._save({"text": "previous SQLite text"})
        assert await journal.sync_mirror("latest SQLite text") > 0
        assert journal.repaired
        assert remote.text == "latest SQLite text"


async def test_mirror_refuses_a_base_with_unverified_identity(tmp_path):
    remote = Mirror("manual change")

    async def inconsistent_read(_doc):
        return {"text": remote.text, "hash": snapshot_sha256("different text"), "revision": 1}

    remote.read_repair_base = inconsistent_read
    with Store(tmp_path / "archive.sqlite") as store:
        journal = DocumentJournal(store, remote, "document")
        journal._save({"text": "SQLite text"})
        with pytest.raises(DocsWebError) as error:
            await journal.sync_mirror("SQLite text")
        assert error.value.code == "docs_web_uncertain"
        assert remote.writes == []
