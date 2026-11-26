"""Use a signed-in Brave editor session to send Google Docs web edits."""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from .web_docs_protocol import confirmed_revision

_DRIVER = Path(__file__).with_name("web_docs_driver.js")
_RESULT = re.compile(r"(?:^|\n)(?:\[log\] )?AI_MEMORY_DOCS_RESULT=(\{[^\r\n]*\})")


class DocsWebError(RuntimeError):
    def __init__(self, code):
        super().__init__("Google Docs web sync could not confirm the operation")
        self.code = code


async def _start(*arguments):
    try:
        process = await asyncio.create_subprocess_exec(
            "playwriter",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise DocsWebError("docs_web_unavailable") from None
    try:
        async with asyncio.timeout(52):
            output, _ = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return output.decode("utf-8", errors="replace"), process.returncode


async def new_brave_session():
    output, status = await _start("session", "new", "--tab-group", "memorysync")
    match = re.search(r"Session (\d+) created", output)
    if status or not match:
        raise DocsWebError("docs_web_unavailable")
    # A separate session handles only the temporary windows created for sync.
    sessions, _ = await _start("session", "list")
    if not re.search(rf"(?m)^\s*{match[1]}\s+Brave\s", sessions):
        raise DocsWebError("docs_web_brave_required")
    return int(match[1])


class WebDocsTransport:
    def __init__(self, session):
        if type(session) is not int or session < 1:
            raise ValueError("A Playwriter Brave session number is required")
        self.session = session

    async def call(self, action, **payload):
        result, _ = await self._operation(action, payload)
        return result

    async def release(self):
        return await self.call("release")

    async def read_repair_base(self, doc):
        result, content = await self._operation("read_repair_base", {"id": doc})
        return {**result, "text": content}

    async def _operation(self, action, payload):
        with tempfile.TemporaryDirectory(prefix="ai-memory-docs-web-") as directory:
            file = Path(directory) / "operation.js"
            content_file = Path(directory) / "repair.txt"
            if action == "read_repair_base":
                payload = {**payload, "output": str(content_file)}
            with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as run:
                run.write(f"const input = {json.dumps({'action': action, **payload})};\n")
                run.write(_DRIVER.read_text(encoding="utf-8"))
            try:
                output, status = await _start(
                    "-s", str(self.session), "--timeout", "48000", "-f", str(file)
                )
            except TimeoutError:
                raise DocsWebError("docs_web_uncertain") from None
            content = None
            if action == "read_repair_base" and content_file.is_file():
                try:
                    with content_file.open(encoding="utf-8", newline="") as source:
                        content = source.read()
                except (UnicodeError, OSError):
                    raise DocsWebError("docs_web_uncertain") from None
        matches = _RESULT.findall(output)
        if status or len(matches) != 1:
            if action == "write" and "extension is not connected" in output:
                raise DocsWebError("docs_web_not_sent")
            raise DocsWebError("docs_web_unavailable")
        try:
            result = json.loads(matches[0])
        except ValueError:
            raise DocsWebError("docs_web_uncertain") from None
        if result.get("status") == "unavailable":
            raise DocsWebError("docs_web_unavailable")
        if result.get("status") == "auth_required":
            raise DocsWebError("docs_web_auth_required")
        return result, content

    async def create(self, title):
        result = await self.call("create", title=title)
        doc = result.get("id")
        if not isinstance(doc, str) or not re.fullmatch(r"[\w-]{20,150}", doc):
            raise DocsWebError("docs_web_uncertain")
        return doc

    async def confirm_title(self, doc, title):
        result = await self.call("title", id=doc, title=title)
        if result.get("status") != "named":
            raise DocsWebError("docs_web_title_unconfirmed")

    async def write(self, doc, transaction, before, after, commands, *, recovering):
        result = await self.call(
            "write",
            id=doc,
            transaction=transaction,
            before=before,
            after=after,
            commands=commands,
            recover=recovering,
        )
        if result.get("status") == "committed":
            if type(result.get("bytes")) is not int or result["bytes"] != 0:
                raise DocsWebError("docs_web_uncertain")
            return 0
        if result.get("status") == "conflict":
            raise DocsWebError("docs_web_conflict")
        if result.get("status") == "not_sent":
            raise DocsWebError("docs_web_not_sent")
        if result.get("status") != "submitted":
            raise DocsWebError("docs_web_uncertain")
        previous, ack, count = result.get("previous"), result.get("ack"), result.get("bytes")
        if not (
            type(previous) is int
            and previous >= 0
            and type(count) is int
            and count > 0
            and isinstance(ack, dict)
            and type(ack.get("http")) is int
            and 200 <= ack["http"] < 300
        ):
            raise DocsWebError("docs_web_uncertain")
        metadata = ack.get("metadata")
        if isinstance(metadata, dict) and metadata.get("needsTransformOnClient") is True:
            # The server may have applied a transformed stale packet. Keep the
            # journal pending and show a conflict; never attest this response.
            raise DocsWebError("docs_web_conflict")
        if confirmed_revision(ack, previous) is None:
            raise DocsWebError("docs_web_uncertain")
        return count
