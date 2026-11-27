"""Record a document write before sending its differential Google Docs payload."""

import json
import re
from uuid import uuid4

from .privacy import snapshot_sha256
from .web_docs_protocol import editor_commands
from .web_docs_transport import DocsWebError
from .web_text_delta import text_edits, utf16_length

DOCS_LIMIT = 1_000_000
INDEX_RECEIPT_PREFIX = "Memory publication receipt "
_WRITE_TEXT_BUDGET = 64_000
_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\ue000-\uf8ff]")


def docs_text(text):
    # Google Docs exports canonical newlines as LF; normalize before journaling.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Preserve controls as visible escapes when the editor would drop or fold them.
    return _UNSAFE_CONTROL.sub(lambda match: f"\\u{ord(match[0]):04x}", text)


def split_document(text):
    text = docs_text(text)
    limit = DOCS_LIMIT - len(INDEX_RECEIPT_PREFIX) - 25
    parts = []
    while utf16_length(text) > limit:
        stop = len(text.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore"))
        newline = text.rfind("\n", max(0, stop - 8192), stop)
        end = newline + 1 if newline >= 0 else stop
        parts.append(text[:end])
        text = text[end:]
    return [*parts, text]


def _next_target(before, after):
    """Bound seed writes and other large edits without resending their unchanged context."""
    edits = text_edits(before, after)
    if sum(len(edit.text) + 64 for edit in edits) <= _WRITE_TEXT_BUDGET:
        return after
    # Apply the largest prefix of the ordered edits that fits a provider write.
    result = before.encode("utf-16-le")
    budget = _WRITE_TEXT_BUDGET
    for edit in edits:
        budget -= 64
        if budget <= 0:
            break
        text = edit.text[:budget]
        result = result[: edit.start * 2] + text.encode("utf-16-le") + result[edit.end * 2 :]
        budget -= len(text)
    return result.decode("utf-16-le")


class DocumentJournal:
    def __init__(self, store, provider, document_id):
        self.db = store.connection
        self.provider = provider
        self.document_id = document_id
        self.key = f"docs-web:{document_id}"
        self.write_payload_bytes = 0
        self.repaired = False

    def _load(self):
        row = self.db.execute("SELECT value FROM checkpoints WHERE key=?", (self.key,)).fetchone()
        return json.loads(row[0]) if row else {"text": ""}

    def _save(self, record):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
                (self.key, json.dumps(record, ensure_ascii=False)),
            )

    async def _send(self, record, *, recovering):
        before = record["text"]
        pending = record["pending"]
        after = pending["text"]
        try:
            sent = await self.provider.write(
                self.document_id,
                pending["id"],
                snapshot_sha256(before),
                snapshot_sha256(after),
                editor_commands(text_edits(before, after)),
                recovering=recovering,
            )
        except DocsWebError as error:
            if not recovering and error.code == "docs_web_not_sent":
                self._save({"text": before})
            raise
        self.write_payload_bytes += sent
        self._save({"text": after})

    async def verify_remote(self, text):
        """Independently attest the committed model before linking or indexing it."""
        text = docs_text(text)
        record = self._load()
        if record != {"text": text}:
            raise DocsWebError("docs_web_uncertain")
        remote = await self.provider.call("check", id=self.document_id)
        if remote.get("hash") != snapshot_sha256(text):
            raise DocsWebError("docs_web_conflict")
        if type(remote.get("revision")) is not int or remote["revision"] < 0:
            raise DocsWebError("docs_web_uncertain")

    async def sync(self, text):
        self.write_payload_bytes = 0
        self.repaired = False
        text = docs_text(text)
        record = self._load()
        if "repair" in record:
            raise DocsWebError("docs_web_uncertain")
        if "pending" in record:
            await self._send(record, recovering=True)
            record = self._load()
        while record["text"] != text:
            # Opening the document first keeps a closed browser from creating an uncertain write.
            prepared = await self.provider.call(
                "prepare", id=self.document_id, known=snapshot_sha256(record["text"])
            )
            if prepared.get("status") != "ready":
                raise DocsWebError("docs_web_conflict")
            record["pending"] = {"id": uuid4().hex, "text": _next_target(record["text"], text)}
            self._save(record)
            await self._send(record, recovering=False)
            record = self._load()
        return self.write_payload_bytes

    async def matches_remote(self, text):
        known = snapshot_sha256(text)
        actual = await self.provider.call("inspect", id=self.document_id, known=known)
        received = actual.get("hash")
        if actual.get("status") == "ready" and received == known:
            return True
        if (
            actual.get("status") == "changed"
            and isinstance(received, str)
            and re.fullmatch(r"[0-9a-f]{64}", received)
            and received != known
        ):
            return False
        raise DocsWebError("docs_web_uncertain")

    async def sync_mirror(self, text, *, repair_target=None):
        """Restore a managed copy from the archive, including remote-only edits."""
        self.write_payload_bytes = 0
        self.repaired = False
        text = docs_text(text)
        record = self._load()
        if "repair" not in record:
            try:
                if record == {"text": text}:
                    if await self.matches_remote(text):
                        return 0
                else:
                    return await self.sync(text)
            except DocsWebError as error:
                if error.code != "docs_web_conflict":
                    raise
            # Any previous differential packet was computed against a different
            # model. Keep only trusted local text and verify the current remote
            # model again before planning a replacement packet.
            self._save({"text": self._load()["text"], "repair": True})

        self.repaired = True
        if repair_target is not None:
            text = docs_text(repair_target())
        previous_hash = None
        while True:
            actual = await self.provider.read_repair_base(self.document_id)
            before = actual.get("text")
            before_hash = actual.get("hash")
            if (
                not isinstance(before, str)
                or snapshot_sha256(before) != before_hash
                or type(actual.get("revision")) is not int
                or actual["revision"] < 0
            ):
                raise DocsWebError("docs_web_uncertain")
            if before == text:
                self._save({"text": text})
                return self.write_payload_bytes
            if before_hash == previous_hash:
                raise DocsWebError("docs_web_uncertain")
            previous_hash = before_hash
            after = _next_target(before, text)
            edits = editor_commands(text_edits(before, after))
            self.write_payload_bytes += await self.provider.write(
                self.document_id,
                uuid4().hex,
                before_hash,
                snapshot_sha256(after),
                edits,
                recovering=False,
            )
            if after == text:
                self._save({"text": text})
                return self.write_payload_bytes
