"""Offline SDK contract tests. They do not establish live Google retrieval."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from notebooklm import AskResult, ChatReference, Source
from notebooklm.types import SourceStatus


def test_stored_client_allows_large_segment_uploads_without_short_write_timeout(monkeypatch):
    from notebooklm import NotebookLMClient

    from ai_memory.notebook import _stored_client

    seen = {}
    sentinel = object()

    def from_storage(**kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(NotebookLMClient, "from_storage", from_storage)
    assert _stored_client(Path("private-auth.json")) is sentinel
    assert seen["path"] == "private-auth.json"
    timeout = seen["config"].transfers.finalize_timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10, 300, 120, 10)


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


def offline_http(monkeypatch, handler):
    """Mock the transport and prevent ambient proxies from being constructed before it."""
    import httpx

    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)


async def test_real_sdk_post_answer_failure_is_content_safe(monkeypatch, caplog, capsys):
    import io
    import json
    import logging

    import httpx
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.options import ClientConfig, WebBackendConfig
    from notebooklm.rpc import RPCMethod

    from ai_memory.notebook import NotebookLMAdapter, NotebookLMError

    question = "Synthetic-Private-Question"
    answer = "Synthetic-Private-Answer"
    cookie = "Synthetic-Private-Cookie"
    csrf = "Synthetic-Private-Credential"
    transcript = "Synthetic-Private-Transcript"
    requested = []

    def simulated_google(request):
        if request.url.path.endswith("GenerateFreeFormStreamed"):
            requested.append("answer")
            assert question.encode() in request.content
            response = [[answer, None, ["fake-chat", 12], None, [[], None, None, [], 1]]]
            chunk = json.dumps([["wrb.fr", None, json.dumps(response)]])
        else:
            method = request.url.params.get("rpcids")
            assert method == RPCMethod.GET_LAST_CONVERSATION_ID.value
            requested.append("post_answer" if "answer" in requested else "pre_answer")
            chunk = json.dumps(["wrb.fr", method, json.dumps([]), None, None])
        # Emulate diagnostic output from the transport while handling the turn.
        logging.getLogger("httpcore.http11").debug("%s [%s, %s]", transcript, cookie, csrf)
        return httpx.Response(200, content=f")]}}'\n{len(chunk)}\n{chunk}\n".encode())

    offline_http(monkeypatch, simulated_google)
    auth = AuthTokens(cookies={"SID": cookie}, csrf_token=csrf, session_id="offline")
    adapter = NotebookLMAdapter(
        "synthetic-notebook",
        client_factory=lambda path: NotebookLMClient(
            auth, config=ClientConfig(backend=WebBackendConfig())
        ),
    )
    sdk_logger = logging.getLogger("notebooklm._chat.api")
    direct_output = io.StringIO()
    direct_handler = logging.StreamHandler(direct_output)
    sdk_logger.addHandler(direct_handler)
    try:
        with (
            caplog.at_level(logging.DEBUG),
            caplog.at_level(logging.DEBUG, logger="notebooklm"),
            pytest.raises(NotebookLMError) as caught,
        ):
            await adapter.ask(question, ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
        assert requested == ["pre_answer", "answer", "post_answer"]
        assert caught.value.code == "notebooklm_error"
    finally:
        sdk_logger.removeHandler(direct_handler)
        await adapter.close()

    logging.getLogger("ai_memory.synthetic").warning("Application logging still works")
    logging.getLogger("httpx").warning("Unrelated HTTP logging still works")
    assert "Application logging still works" in caplog.text
    assert "Unrelated HTTP logging still works" in caplog.text
    assert "NotebookLM SDK diagnostic withheld." in direct_output.getvalue()
    captured = capsys.readouterr()
    output = caplog.text + direct_output.getvalue() + captured.out + captured.err
    for private in (answer, question, transcript, cookie, csrf):
        assert private not in output


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


async def test_terminal_chat_uses_verbatim_prompt_and_explicit_sources():
    from ai_memory.notebook import NotebookLMAdapter

    sdk = SimulatedSDK()
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    try:
        result = await adapter.chat("hello", source_ids=["source-1"])
        assert result["answer"] == "A decision from the source. [1]"
        assert sdk.questions == [("notebook-1", "hello", ["source-1"])]
    finally:
        await adapter.close()


@pytest.mark.parametrize("access", [True, False])
def test_terminal_brave_login_saves_only_a_verified_session(tmp_path, monkeypatch, access):
    from types import SimpleNamespace

    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"
    credential = tmp_path / "session.json"
    credential.write_text("old session")
    with Store(db) as store:
        store.set_setting("notebook_id", "bound-notebook")
        store.set_setting("storage_path", str(credential))
        store.set_setting("sync_preflight_errors", [{"reason": "auth_required"}])

    staged = []

    def run(command, **kwargs):
        assert command[2:5] == ["notebooklm", "login", "--browser-cookies"]
        assert command[5] == "brave::Profile 1"
        path = Path(command[-1])
        assert path != credential
        assert Path(kwargs["env"]["NOTEBOOKLM_HOME"]) == path.parent
        path.write_text("fresh session")
        staged.append(path)
        return SimpleNamespace(returncode=0)

    async def get(notebook_id):
        assert notebook_id == "bound-notebook"
        return SimpleNamespace(id=notebook_id, title="Test notebook", role=None)

    async def sources(notebook_id):
        assert notebook_id == "bound-notebook"
        if not access:
            from notebooklm.exceptions import AuthError

            raise AuthError("this raw SDK detail must stay private")
        return [SimpleNamespace(id="source", is_ready=True)]

    async def account():
        return "signed-in@example.test"

    @asynccontextmanager
    async def verified(storage_path):
        assert storage_path == staged[0]
        yield SimpleNamespace(
            notebooks=SimpleNamespace(get=get),
            sources=SimpleNamespace(list=sources),
            get_account_email=account,
        )

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(notebook, "_stored_client", verified)
    if access:
        result = notebook.login_from_brave(db, "Profile 1")
        assert result["notebook"]["connected"] is True
        assert result["notebook"]["ready_sources"] == 1
        assert result["notebook"]["title"] == "Test notebook"
        assert result["notebook"]["account"] == "signed-in@example.test"
        assert result["notebook"]["can_write"] is None
        assert result["notebook"]["checked_at"].endswith("+00:00")
        assert credential.read_text() == "fresh session"
        assert credential.stat().st_mode & 0o777 == 0o600
        with Store(db, existing=True) as archive:
            assert archive.get_setting("sync_preflight_errors") == []
            assert archive.get_setting("notebook_identity") == {
                key: result["notebook"][key]
                for key in ("id", "title", "account", "role", "can_write", "checked_at")
            }
    else:
        with pytest.raises(notebook.NotebookLMAuthError):
            notebook.login_from_brave(db, "Profile 1")
        assert credential.read_text() == "old session"
        with Store(db, existing=True) as archive:
            assert archive.get_setting("sync_preflight_errors") == [{"reason": "auth_required"}]
    assert not staged[0].exists()


def test_brave_relogin_isolates_two_archives_that_used_default_sdk_storage(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from ai_memory import notebook
    from ai_memory.store import Store

    shared = tmp_path / "shared-sdk-session.json"
    shared.write_text("standalone notebooklm CLI session")
    monkeypatch.setattr("notebooklm.paths.get_storage_path", lambda: shared)

    @contextmanager
    def browser_session(profile, account):
        assert profile == "Default"
        staged = tmp_path / "staged-session.json"
        staged.write_text(account)
        yield staged

    async def verify(storage, notebook_id, *, account, bound):
        assert bound and storage.read_text() == account
        return {
            "id": notebook_id,
            "title": "Fixture",
            "account": account,
            "role": "owner",
            "can_write": True,
            "checked_at": "2026-01-01T00:00:00+00:00",
        }, []

    monkeypatch.setattr(notebook, "_brave_storage", browser_session)
    monkeypatch.setattr(notebook, "_inspect_notebook", verify)
    archives = {
        notebook_id: tmp_path / f"{notebook_id}.sqlite" for notebook_id in ("first", "second")
    }
    for notebook_id, db in archives.items():
        with Store(db) as store:
            store.set_setting("notebook_id", notebook_id)
        notebook.login_from_brave(db, "Default", f"{notebook_id}@example.test")

    auth_files = set()
    for notebook_id, db in archives.items():
        with Store(db, existing=True) as store:
            location = store.get_setting("storage_path")
            assert location, "this archive would still fall back to the SDK's shared session"
            path = Path(location)
            assert path.parent == store.workspace
            assert path.read_text() == f"{notebook_id}@example.test"
            assert path.stat().st_mode & 0o777 == 0o600
            auth_files.add(path)
    assert len(auth_files) == 2
    assert shared.read_text() == "standalone notebooklm CLI session"


def test_brave_relogin_uses_the_archive_account_and_profile_when_form_is_blank(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"
    credential = tmp_path / "session.json"
    credential.write_text("old session")
    with Store(db) as store:
        store.set_setting("notebook_id", "bound-notebook")
        store.set_setting("storage_path", str(credential))
        store.set_setting("notebook_identity", {"account": "selected@example.test"})
        store.set_setting("notebook_login_profile", "Profile 2")

    @contextmanager
    def browser_session(profile, account):
        assert profile == "Profile 2"
        assert account == "selected@example.test"
        staged = tmp_path / "staged-session.json"
        staged.write_text("fresh session")
        yield staged

    async def verify(storage, notebook_id, *, account, bound):
        assert bound and storage.read_text() == "fresh session"
        return {
            "id": notebook_id,
            "title": "Fixture",
            "account": account,
            "role": "owner",
            "can_write": True,
            "checked_at": "2026-01-01T00:00:00+00:00",
        }, []

    monkeypatch.setattr(notebook, "_brave_storage", browser_session)
    monkeypatch.setattr(notebook, "_inspect_notebook", verify)
    assert notebook.login_from_brave(db)["notebook"]["account"] == "selected@example.test"
    assert credential.read_text() == "fresh session"


def test_brave_notebook_setup_attaches_to_validated_id_and_initializes_archive(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from notebooklm.types import SharePermission

    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "brand-new" / "archive.sqlite"
    calls = []

    def run(command, **kwargs):
        assert command[5:8] == ["brave::Work", "--account", "owner@example.test"]
        Path(command[-1]).write_text("verified session")
        return SimpleNamespace(returncode=0)

    async def get(notebook_id):
        calls.append(("get", notebook_id))
        return SimpleNamespace(id=notebook_id, title="Work", role=SharePermission.EDITOR)

    async def sources(notebook_id):
        calls.append(("list", notebook_id))
        return []

    async def account():
        return "owner@example.test"

    @asynccontextmanager
    async def verified(storage_path):
        assert storage_path.read_text() == "verified session"
        yield SimpleNamespace(
            notebooks=SimpleNamespace(get=get),
            sources=SimpleNamespace(list=sources),
            get_account_email=account,
        )

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(notebook, "_stored_client", verified)
    assert not db.exists()
    result = notebook.setup_notebook_from_brave(
        db,
        "attach",
        "https://notebooklm.google.com/notebook/synthetic-id?authuser=1",
        "Work",
        "owner@example.test",
    )["notebook"]
    assert set(calls) == {("get", "synthetic-id"), ("list", "synthetic-id")}
    assert result["id"] == "synthetic-id"
    assert result["title"] == "Work"
    assert result["role"] == "editor"
    assert result["can_write"] is True
    assert notebook.check_notebook_connection(db)["notebook"]["title"] == "Work"
    with Store(db, existing=True) as archive:
        assert archive.get_setting("notebook_id") == "synthetic-id"
        assert archive.get_setting("notebook_identity")["account"] == "owner@example.test"
        auth_path = Path(archive.get_setting("storage_path"))
        assert auth_path.parent == archive.workspace
        assert auth_path.stat().st_mode & 0o777 == 0o600


def test_brave_notebook_setup_guards_invalid_url_and_existing_binding(tmp_path, monkeypatch):
    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"

    def forbidden_run(*args, **kwargs):
        pytest.fail("Invalid or already-bound setup must not import browser credentials")

    monkeypatch.setattr("subprocess.run", forbidden_run)
    with pytest.raises(notebook.NotebookLMError) as invalid:
        notebook.setup_notebook_from_brave(db, "attach", "https://example.test/notebook/id")
    assert invalid.value.code == "invalid_notebook"
    assert not db.exists()
    with Store(db) as archive:
        archive.set_setting("notebook_id", "original")
    with pytest.raises(notebook.NotebookLMError) as bound:
        notebook.setup_notebook_from_brave(db, "attach", "different")
    assert bound.value.code == "already_configured"


def test_brave_notebook_create_refuses_second_attempt_after_lost_response(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from notebooklm.exceptions import NetworkError

    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"
    creates = []

    def run(command, **kwargs):
        Path(command[-1]).write_text("temporary session")
        return SimpleNamespace(returncode=0)

    async def create(title):
        creates.append(title)
        raise NetworkError("response lost")

    async def account():
        return None

    @asynccontextmanager
    async def verified(storage_path):
        yield SimpleNamespace(notebooks=SimpleNamespace(create=create), get_account_email=account)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(notebook, "_stored_client", verified)
    for _ in range(2):
        with pytest.raises(notebook.NotebookLMError) as caught:
            notebook.setup_notebook_from_brave(db, "create", "My Notebook")
        assert caught.value.code == "notebook_create_unconfirmed"
    assert creates == ["My Notebook"]
    with Store(db, existing=True) as archive:
        assert archive.get_setting("notebook_id") is None
        assert archive.get_setting("storage_path") is None
        assert archive.get_setting("notebook_identity") is None


def test_brave_notebook_create_binds_only_the_returned_id_after_verification(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from notebooklm.types import SharePermission

    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "new.sqlite"
    visits = []

    def run(command, **kwargs):
        Path(command[-1]).write_text("verified session")
        return SimpleNamespace(returncode=0)

    async def create(title):
        visits.append(("create", title))
        return SimpleNamespace(id="new-notebook-id")

    async def get(notebook_id):
        visits.append(("get", notebook_id))
        return SimpleNamespace(id=notebook_id, title="My Notebook", role=SharePermission.OWNER)

    async def sources(notebook_id):
        visits.append(("list", notebook_id))
        return []

    async def account():
        return None

    @asynccontextmanager
    async def verified(storage_path):
        yield SimpleNamespace(
            notebooks=SimpleNamespace(create=create, get=get),
            sources=SimpleNamespace(list=sources),
            get_account_email=account,
        )

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(notebook, "_stored_client", verified)
    result = notebook.setup_notebook_from_brave(db, "create", "My Notebook")["notebook"]
    assert result["account"] is None
    assert result["role"] == "owner"
    assert visits == [
        ("create", "My Notebook"),
        ("list", "new-notebook-id"),
        ("get", "new-notebook-id"),
    ]
    with Store(db, existing=True) as archive:
        assert archive.get_setting("notebook_id") == result["id"] == "new-notebook-id"
        assert archive.get_setting("notebook_create_pending") is None


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

    offline_http(monkeypatch, simulated_google)
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


def test_successful_check_clears_only_resolved_preflight_auth_error(tmp_path, monkeypatch):
    from ai_memory import notebook
    from ai_memory.store import Store

    db = tmp_path / "archive.sqlite"
    with Store(db) as archive:
        archive.set_setting("notebook_id", "synthetic-notebook")
        archive.set_setting(
            "sync_preflight_errors",
            [{"reason": "auth_required"}, {"reason": "network_error"}],
        )

    async def inspect(*args, **kwargs):
        return (
            {
                "id": "synthetic-notebook",
                "title": "Verified",
                "account": None,
                "role": None,
                "can_write": None,
                "checked_at": "2026-01-01T00:00:00+00:00",
                "configured": True,
                "connected": True,
            },
            [],
        )

    monkeypatch.setattr(notebook, "_inspect_notebook", inspect)
    assert notebook.check_notebook_connection(db)["notebook"]["connected"]
    with Store(db, existing=True) as archive:
        assert archive.get_setting("sync_preflight_errors") == [{"reason": "network_error"}]


async def test_configure_custom_preserves_persona_verifies_and_sends_plain_question():
    from notebooklm.types import ChatGoal, ChatResponseLength, ChatSettings

    from ai_memory.notebook import CUSTOM_MARKER, NotebookLMAdapter

    class ConfigurableSDK(SimulatedSDK):
        settings = ChatSettings(ChatGoal.CUSTOM, ChatResponseLength.DEFAULT, "my memory manager")
        writes = 0

        async def get_settings(self, notebook_id):
            return self.settings

        async def configure(self, notebook_id, goal, response_length, custom_prompt):
            self.writes += 1
            self.settings = ChatSettings(goal, response_length, custom_prompt)

    sdk = ConfigurableSDK()
    adapter = NotebookLMAdapter("notebook-1", client_factory=lambda path: sdk)
    try:
        await adapter.configure_memory_prompt()
        await adapter.configure_memory_prompt()
        assert sdk.writes == 1
        assert sdk.settings.custom_prompt.startswith("my memory manager")
        assert sdk.settings.custom_prompt.count(CUSTOM_MARKER) == 1
        assert sdk.settings.response_length == ChatResponseLength.DEFAULT
        await adapter.ask("Earlier?", ["source-1"])
        assert sdk.questions == [("notebook-1", "Earlier?", ["source-1"])]
    finally:
        await adapter.close()
