"""Run the Docs web driver in a private, headless Brave process for one sync."""

import asyncio
import shutil
from contextlib import suppress
from importlib.util import find_spec
from pathlib import Path

from .web_docs_transport import DocsWebError, WebDocsTransport

BACKGROUND_SESSION = "background"
_WORKER = Path(__file__).with_name("web_docs_background.cjs")


class BackgroundDocsTransport(WebDocsTransport):
    def __init__(self, storage):
        if not storage:
            from notebooklm.paths import get_storage_path

            storage = get_storage_path()
        self.storage = Path(storage).expanduser()
        self._process = None
        self._lock = asyncio.Lock()

    async def _start_worker(self):
        if not self.storage.is_file():
            raise DocsWebError("docs_web_auth_required", definitely_not_sent=True)
        playwright = find_spec("playwright")
        brave = shutil.which("brave") or shutil.which("brave-browser")
        if not playwright or not playwright.origin or not brave:
            raise DocsWebError("docs_web_unavailable", definitely_not_sent=True)
        driver = Path(playwright.origin).parent / "driver"
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(driver / "node"),
                str(_WORKER),
                str(driver / "package"),
                brave,
                str(self.storage),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(20):
                ready = await self._process.stdout.readline()
        except (OSError, TimeoutError):
            ready = b""
        if ready != b"ready\n":
            await self._close_worker()
            code = (
                "docs_web_auth_required" if ready == b"auth_required\n" else "docs_web_unavailable"
            )
            raise DocsWebError(code, definitely_not_sent=True)

    async def _run_script(self, file):
        async with self._lock:
            try:
                if self._process is None or self._process.returncode is not None:
                    await self._start_worker()
                self._process.stdin.write(f"{file}\n".encode())
                async with asyncio.timeout(75):
                    await self._process.stdin.drain()
                    output = await self._process.stdout.readline()
                if not output:
                    await self._close_worker()
                    raise DocsWebError("docs_web_uncertain")
                return output.decode("utf-8", errors="replace"), 0
            except OSError:
                await self._close_worker()
                raise DocsWebError("docs_web_uncertain") from None
            except (TimeoutError, asyncio.CancelledError):
                await self._close_worker()
                raise

    async def _close_worker(self):
        process, self._process = self._process, None
        if process is None:
            return
        process.stdin.close()
        try:
            async with asyncio.timeout(4):
                await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                async with asyncio.timeout(4):
                    await process.wait()
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.release()

    async def release(self):
        async with self._lock:
            await self._close_worker()
