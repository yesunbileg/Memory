import asyncio
import json

import httpx
import pytest
from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.options import ClientConfig, WebBackendConfig
from notebooklm.rpc import RPCMethod
from test_notebook import SimulatedSDK, offline_http

from ai_memory.notebook import NotebookLMAdapter, NotebookLMError

SOURCE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TITLE = "test.md"


def synthetic_sdk(monkeypatch, events, *, pause_start=None):
    async def simulated_google(request):
        command = request.headers.get("x-goog-upload-command")
        if command == "start":
            events.append("start")
            if pause_start:
                await pause_start()
            return httpx.Response(
                200,
                headers={
                    "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=test"
                },
            )
        if command == "upload, finalize":
            events.append(await request.aread())
            return httpx.Response(200, content=b"OK")
        method = request.url.params.get("rpcids")
        if method == RPCMethod.GET_NOTEBOOK.value:
            data = [["Synthetic notebook", [], "notebook-1"]]
        elif method == RPCMethod.ADD_SOURCE_FILE.value:
            events.append("register")
            data = [[[[SOURCE_ID], TITLE, [None, None, None, None, 0]]]]
        else:
            raise AssertionError(f"Unexpected synthetic request: {request.url.path}")
        chunk = json.dumps(["wrb.fr", method, json.dumps(data), None, None])
        return httpx.Response(200, content=f")]}}'\n{len(chunk)}\n{chunk}\n".encode())

    offline_http(monkeypatch, simulated_google)
    auth = AuthTokens(cookies={"SID": "synthetic"}, csrf_token="offline", session_id="offline")
    sdk = NotebookLMClient(auth, config=ClientConfig(backend=WebBackendConfig()))
    return NotebookLMAdapter("notebook-1", client_factory=lambda _path: sdk), sdk


async def test_tracked_upload_records_exact_id_before_session_or_file_stream(tmp_path, monkeypatch):
    path = tmp_path / TITLE
    path.write_bytes(b"synthetic content")
    events = []
    adapter, sdk = synthetic_sdk(monkeypatch, events)
    try:
        assert await adapter.upload_tracked(path, TITLE, events.append) == SOURCE_ID
        assert events == ["register", SOURCE_ID, "start", path.read_bytes()]
        assert "register_file_source" not in vars(sdk.sources._uploader)
    finally:
        await adapter.close()


async def test_tracked_upload_stops_if_recorder_fails(tmp_path, monkeypatch):
    path = tmp_path / TITLE
    path.write_bytes(b"synthetic content")
    events = []
    adapter, sdk = synthetic_sdk(monkeypatch, events)

    def fail(source_id):
        events.append(source_id)
        raise RuntimeError("private local failure")

    try:
        with pytest.raises(NotebookLMError) as exc:
            await adapter.upload_tracked(path, TITLE, fail)
        assert exc.value.code == "upload_registration_failed"
        assert exc.value.source_id == SOURCE_ID
        assert exc.value.definitely_not_sent
        assert "private local failure" not in str(exc.value)
        assert events == ["register", SOURCE_ID]
        assert "register_file_source" not in vars(sdk.sources._uploader)
    finally:
        await adapter.close()


async def test_tracked_upload_restores_seam_on_cancel_and_serializes_ordinary_upload(
    tmp_path, monkeypatch
):
    path = tmp_path / TITLE
    path.write_bytes(b"synthetic content")
    events = []
    started = asyncio.Event()

    async def pause_first_start():
        if not started.is_set():
            started.set()
            await asyncio.Event().wait()

    adapter, sdk = synthetic_sdk(monkeypatch, events, pause_start=pause_first_start)
    task = asyncio.create_task(adapter.upload_tracked(path, TITLE, events.append))
    ordinary = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        ordinary = asyncio.create_task(adapter.upload(path, TITLE))
        await asyncio.sleep(0)
        assert events == ["register", SOURCE_ID, "start"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(ordinary, timeout=2) == SOURCE_ID
        assert events == ["register", SOURCE_ID, "start", "register", "start", path.read_bytes()]
        assert "register_file_source" not in vars(sdk.sources._uploader)
    finally:
        tasks = [task] if ordinary is None else [task, ordinary]
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.close()


async def test_tracked_upload_rejects_an_incompatible_pipeline_before_sending(tmp_path):
    path = tmp_path / TITLE
    path.write_bytes(b"synthetic content")
    sdk = SimulatedSDK()
    events = []
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda _path: sdk)
    try:
        with pytest.raises(NotebookLMError) as exc:
            await adapter.upload_tracked(path, TITLE, events.append)
        assert exc.value.code == "upload_registration_unavailable"
        assert exc.value.definitely_not_sent
        assert not events
        assert not sdk.uploads
    finally:
        await adapter.close()
