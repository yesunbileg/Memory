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
        with tempfile.TemporaryDirectory(prefix="ai-memory-docs-web-") as directory:
            file = Path(directory) / "operation.js"
            with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as run:
                run.write(f"const input = {json.dumps({'action': action, **payload})};\n")
                run.write(_DRIVER.read_text(encoding="utf-8"))
            try:
                output, status = await _start(
                    "-s", str(self.session), "--timeout", "48000", "-f", str(file)
                )
            except TimeoutError:
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
        return result

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
            return result["bytes"]
        if result.get("status") == "conflict":
            raise DocsWebError("docs_web_conflict")
        if result.get("status") == "not_sent":
            raise DocsWebError("docs_web_not_sent")
        if result.get("status") != "submitted":
            raise DocsWebError("docs_web_uncertain")
        previous, ack = result.get("previous"), result.get("ack", {})
        revision = confirmed_revision(ack, previous) if type(previous) is int else None
        if revision is None:
            raise DocsWebError("docs_web_uncertain")
        return result["bytes"]
