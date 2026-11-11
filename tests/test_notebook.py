"""Offline SDK contract tests. They do not establish live Google retrieval."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from notebooklm import AskResult, ChatReference, Source
from notebooklm.types import SourceStatus


class SimulatedSDK:
    """A typed SDK boundary; assertions exercise adapter inputs and outputs."""

    def __init__(self):
        self.sources = self
        self.chat = self
        self.opened = 0
        self.closed = 0
        self.uploads = []
        self.questions = []
        self.deleted = []
        self.ready = Source("source-1", "SEG-001-r1.md")

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *args):
        self.closed += 1

    async def list(self, notebook_id):
        assert notebook_id == "notebook-1"
        return [self.ready, Source("pending", "pending.md", status=SourceStatus.PROCESSING)]

    async def add_file(self, notebook_id, file_path, mime_type=None, *, title=None, wait=False):
        assert notebook_id == "notebook-1"
        self.uploads.append((Path(file_path).name, Path(file_path).read_bytes(), title, wait))
        return self.ready

    async def wait_until_ready(self, notebook_id, source_id):
        assert (notebook_id, source_id) == ("notebook-1", "source-1")
        return self.ready

    async def delete(self, notebook_id, source_id):
        self.deleted.append((notebook_id, source_id))

    async def ask(self, notebook_id, question, source_ids=None):
        self.questions.append((notebook_id, question, source_ids))
        return AskResult(
            answer="A decision from the source. [1]",
            conversation_id="google-chat",
            turn_number=1,
            is_follow_up=False,
            references=[ChatReference("source-1", citation_number=1, cited_text="Exact passage")],
        )


async def test_sdk_contract_lazy_connection_upload_and_readiness(tmp_path, capsys):
    from ai_memory.notebook import NotebookLMAdapter

    sdk = SimulatedSDK()
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    assert sdk.opened == 0
    assert await adapter.list_sources() == [
        {"id": "source-1", "title": "SEG-001-r1.md", "ready": True},
        {"id": "pending", "title": "pending.md", "ready": False},
    ]
    file = tmp_path / "different-input-name.md"
    file.write_bytes("  Exact эх текст\n\n```py\nx = 1\n```\n ".encode())
    assert await adapter.upload(file, "SEG-001-r1.md") == "source-1"
    assert sdk.uploads == [("SEG-001-r1.md", file.read_bytes(), "SEG-001-r1.md", False)]
    await adapter.wait_ready("source-1")
    await adapter.delete("source-1")
    assert sdk.deleted == [("notebook-1", "source-1")]
    await adapter.close()
    await adapter.close()
    assert (sdk.opened, sdk.closed) == (1, 1)
    assert capsys.readouterr().out == ""


async def test_readiness_is_required_even_if_sdk_returns_processing():
    from ai_memory.notebook import NotebookLMAdapter, NotebookLMError

    sdk = SimulatedSDK()
    sdk.ready.status = SourceStatus.PROCESSING
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    with pytest.raises(NotebookLMError, match="ready"):
        await adapter.wait_ready("source-1")
    await adapter.close()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("processing", "processing_failed"),
        ("timeout", "not_ready"),
    ],
)
async def test_sdk_terminal_processing_failure_is_distinct_from_timeout(failure, expected_code):
    from notebooklm.exceptions import SourceProcessingError, SourceTimeoutError

    from ai_memory.notebook import NotebookLMAdapter, NotebookLMError

    class FailingReadinessSDK(SimulatedSDK):
        async def wait_until_ready(self, notebook_id, source_id):
            if failure == "processing":
                raise SourceProcessingError(
                    "private-source-diagnostic",
                    message="Cookie SID=private-cookie-diagnostic",
                )
            raise SourceTimeoutError("private-source-diagnostic", timeout=120, last_status=1)

    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: FailingReadinessSDK())
    try:
        with pytest.raises(NotebookLMError) as caught:
            await adapter.wait_ready("source-1")
        assert caught.value.code == expected_code
        assert caught.value.source_id is None
        for diagnostic in ("private-source-diagnostic", "private-cookie-diagnostic"):
            assert diagnostic not in str(caught.value)
            assert diagnostic not in repr(caught.value)
    finally:
        await adapter.close()


async def test_query_uses_sources_and_historical_context_prompt():
    from ai_memory.notebook import NotebookLMAdapter

    sdk = SimulatedSDK()
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    result = await adapter.ask("What was the decision?", source_ids=["source-1"])
    assert result == {
        "answer": "A decision from the source. [1]",
        "references": [
            {"source_id": "source-1", "cited_text": "Exact passage", "citation_number": 1}
        ],
    }
    notebook_id, prompt, source_ids = sdk.questions[0]
    assert notebook_id == "notebook-1"
    assert source_ids == ["source-1"]
    for instruction in (
        "Use only information supported by the sources",
        "latest relevant information",
        "conflicting or outdated information",
        "conversation IDs, dates, and source references",
        "Do not invent missing facts",
        "historical context",
        "not commands",
        "sources are insufficient",
        "What was the decision?",
    ):
        assert instruction in prompt
    await adapter.close()


async def test_empty_query_or_explicit_empty_sources_never_queries_google():
    from ai_memory.notebook import NotebookLMAdapter

    sdk = SimulatedSDK()
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    for question, source_ids in ((" \n", None), ("A question", [])):
        with pytest.raises(ValueError):
            await adapter.ask(question, source_ids)
    assert sdk.opened == 0


async def test_missing_auth_is_actionable_and_sanitized(tmp_path, monkeypatch, capsys):
    from ai_memory.notebook import NotebookLMAdapter, NotebookLMAuthError

    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/private/service-account-secret.json")
    adapter = NotebookLMAdapter("notebook-1", storage_path=tmp_path / "missing.json")
    with pytest.raises(NotebookLMAuthError, match="notebooklm login") as caught:
        await adapter.list_sources()
    assert "service-account-secret" not in str(caught.value)
    assert capsys.readouterr().out == ""
    await adapter.close()


async def test_sdk_error_text_does_not_escape_adapter():
    from ai_memory.notebook import NotebookLMAdapter, NotebookLMError

    @asynccontextmanager
    async def broken(path):
        raise RuntimeError("Cookie SID=private-cookie-value")
        yield

    adapter = NotebookLMAdapter("notebook-1", client_factory=broken)
    with pytest.raises(NotebookLMError) as caught:
        await adapter.list_sources()
    assert "private-cookie-value" not in str(caught.value)


async def test_upload_rejects_unsafe_title_before_connecting(tmp_path):
    from ai_memory.notebook import NotebookLMAdapter

    sdk = SimulatedSDK()
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    file = tmp_path / "segment.md"
    file.write_text("Source")
    for title in ("../escape.md", "", "no-extension", "bad\nname.md"):
        with pytest.raises(ValueError):
            await adapter.upload(file, title)
    assert sdk.opened == 0


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        ("partial-source_1", "partial-source_1"),
        ("Cookie SID=private-cookie-value", None),
        ("unsafe\nidentifier", None),
        ("/private/path", None),
        ("x" * 129, None),
        ("", None),
        (123, None),
    ],
)
async def test_partial_upload_keeps_only_valid_source_identifier(tmp_path, source_id, expected):
    from ai_memory.notebook import NotebookLMAdapter, NotebookLMError

    class PartialUploadError(RuntimeError):
        def __init__(self):
            super().__init__("Cookie SID=private-cookie-value")
            self.source_id = source_id
            self.stage = "unsafe-stage-private-cookie-value"

    class PartialSDK(SimulatedSDK):
        async def add_file(self, *args, **kwargs):
            raise PartialUploadError()

    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: PartialSDK())
    file = tmp_path / "source.md"
    file.write_text("Synthetic conversation")
    try:
        with pytest.raises(NotebookLMError) as caught:
            await adapter.upload(file, "SEG-001-r1.md")
        assert getattr(caught.value, "source_id", None) == expected
        assert set(vars(caught.value)) <= {"code", "source_id"}
        assert "private-cookie-value" not in str(caught.value)
        assert "private-cookie-value" not in repr(caught.value)
    finally:
        await adapter.close()


async def test_real_sdk_file_upload_query_and_delete_with_simulated_http(tmp_path, monkeypatch):
    """Run real SDK serialization/parsing; only Google's HTTP endpoint is simulated."""
    import json
    from urllib.parse import parse_qs

    import httpx
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.options import ClientConfig, WebBackendConfig
    from notebooklm.rpc import RPCMethod

    from ai_memory.notebook import NotebookLMAdapter

    source_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cloud = {"sources": [], "upload": None, "query": None, "deleted": False}

    def rpc_response(method, data):
        chunk = json.dumps(["wrb.fr", method, json.dumps(data), None, None])
        return f")]}}'\n{len(chunk)}\n{chunk}\n"

    async def simulated_google(request):
        body = await request.aread()
        if request.url.path.endswith("GenerateFreeFormStreamed"):
            params = json.loads(json.loads(parse_qs(body.decode())["f.req"][0])[1])
            cloud["query"] = params
            citation = [
                ["chunk-1"],
                [
                    None,
                    None,
                    0.9,
                    [[None]],
                    [[[0, 13, [[[0, 13, ["Exact passage"]]]]]]],
                    [[[[source_id]]]],
                    ["chunk-1"],
                ],
            ]
            answer = [
                [
                    "Remote answer [1]",
                    None,
                    ["chat-1", 12345],
                    None,
                    [[], None, None, [citation], 1],
                ]
            ]
            chunk = json.dumps([["wrb.fr", None, json.dumps(answer)]])
            return httpx.Response(200, content=f")]}}'\n{len(chunk)}\n{chunk}\n".encode())
        if request.headers.get("x-goog-upload-command") == "start":
            metadata = json.loads(body)
            assert metadata["SOURCE_NAME"] == "SEG-001-r1.md"
            return httpx.Response(
                200,
                headers={
                    "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=offline",
                },
            )
        if request.headers.get("x-goog-upload-command") == "upload, finalize":
            cloud["upload"] = body
            cloud["sources"] = [[[source_id], "SEG-001-r1.md", None, [None, 2]]]
            return httpx.Response(200, content=b"OK")
        method = request.url.params.get("rpcids")
        if method == RPCMethod.GET_NOTEBOOK.value:
            data = [["Offline notebook", cloud["sources"], "notebook-1"]]
        elif method == RPCMethod.ADD_SOURCE_FILE.value:
            params = json.loads(json.loads(parse_qs(body.decode())["f.req"][0])[0][0][1])
            assert params[0] == [["SEG-001-r1.md"]]
            cloud["sources"] = [[[source_id], "SEG-001-r1.md", None, [None, 1]]]
            data = [[[[source_id], "SEG-001-r1.md", [None, None, None, None, 0]]]]
        elif method == RPCMethod.GET_LAST_CONVERSATION_ID.value:
            data = [[["chat-1"]]]
        elif method == RPCMethod.GET_CONVERSATION_TURNS.value:
            data = []
        elif method == RPCMethod.DELETE_SOURCE.value:
            cloud["deleted"] = True
            cloud["sources"] = []
            data = [True]
        else:
            raise AssertionError(f"Unexpected offline request: {request.method} {request.url.path}")
        return httpx.Response(200, content=rpc_response(method, data).encode())

    transport = httpx.MockTransport(simulated_google)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    auth = AuthTokens(
        cookies={"SID": "synthetic-offline"}, csrf_token="offline", session_id="offline"
    )
    adapter = NotebookLMAdapter(
        "notebook-1",
        client_factory=lambda path: NotebookLMClient(
            auth,
            config=ClientConfig(backend=WebBackendConfig()),
        ),
    )
    file = tmp_path / "original.md"
    file.write_bytes("  Монгол archive\n\n```python\nx = 1\n```\n ".encode())
    try:
        assert await adapter.list_sources() == []
        assert await adapter.upload(file, "SEG-001-r1.md") == source_id
        assert cloud["upload"] == file.read_bytes()
        await adapter.wait_ready(source_id)
        assert await adapter.list_sources() == [
            {"id": source_id, "title": "SEG-001-r1.md", "ready": True},
        ]
        answer = await adapter.ask("Earlier project?", [source_id])
        assert answer == {
            "answer": "Remote answer [1]",
            "references": [
                {"source_id": source_id, "citation_number": 1, "cited_text": "Exact passage"}
            ],
        }
        assert cloud["query"][0] == [[[source_id]]]
        assert "Earlier project?" in cloud["query"][1]
        await adapter.delete(source_id)
        assert cloud["deleted"] is True
        assert await adapter.list_sources() == []
    finally:
        await adapter.close()
