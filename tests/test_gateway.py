"""Real MCP protocol over SDK memory streams, with simulated remote answers."""

import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session


class SimulatedNotebook:
    def __init__(self, references=None):
        self.references = references or []
        self.queries = []

    async def list_sources(self):
        return [
            {"id": "owned-source", "title": "archive.md", "ready": True},
            {"id": "unrelated-source", "title": "private.md", "ready": True},
        ]

    async def ask(self, question, source_ids=None):
        self.queries.append((question, source_ids))
        return {"answer": "NotebookLM answer [1]", "references": self.references}

    async def close(self):
        pass


def decode(result):
    assert result.isError is False
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def seed_archive(db_path, *, map_source=True):
    from ai_memory.segments import SegmentBuilder
    from ai_memory.store import Store

    with Store(db_path) as store:
        conversation_id = store.ingest(
            {
                "external_id": "test-session",
                "platform": "codex",
                "account": "test",
                "title": "Gateway test",
                "project": "Memory",
                "models": [],
                "created_at": None,
                "updated_at": None,
                "source_url": None,
                "messages": [
                    {
                        "native_id": "u1",
                        "sequence": 1,
                        "role": "user",
                        "kind": "text",
                        "text": "Which project?",
                        "timestamp": "2026-11-10T10:00:00Z",
                    },
                    {
                        "native_id": "a1",
                        "sequence": 2,
                        "role": "assistant",
                        "kind": "text",
                        "text": "Memory.",
                        "timestamp": "2026-11-10T10:01:00Z",
                    },
                ],
            },
            consent=True,
        )
        segments = SegmentBuilder(store).build()
        segment_id = segments[0]["id"]
        segment = store.connection.execute(
            "SELECT revision FROM segments WHERE id=?", (segment_id,)
        ).fetchone()
        if map_source:
            store.connection.execute(
                "INSERT INTO source_map(segment_id, source_id, revision) VALUES (?, ?, ?)",
                (segment_id, "owned-source", segment["revision"]),
            )
            store.connection.commit()
        store.set_setting("notebook_id", "test-notebook")
        messages = store.get_conversation(conversation_id)["messages"]
        return conversation_id, segment_id, [message["id"] for message in messages]


async def test_mcp_tools_status_and_source_restricted_query(tmp_path, capsys):
    from ai_memory.gateway import create_server

    db = tmp_path / "memory.sqlite"
    seed_archive(db)
    notebook = SimulatedNotebook()
    async with create_connected_server_and_client_session(
        create_server(db, adapter=notebook)
    ) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {"memory_ask", "memory_status"}
        status = decode(await client.call_tool("memory_status", {}))
        assert status["messages"] == 2
        assert status["notebook"]["connected"] is True
        answer = decode(await client.call_tool("memory_ask", {"question": "Earlier decision?"}))
        assert answer["answer"] == "NotebookLM answer [1]"
        assert answer["sources"] == []
    assert notebook.queries == [("Earlier decision?", ["owned-source"])]
    assert capsys.readouterr().out == ""


async def test_citations_identify_only_exact_verified_headings(tmp_path):
    from ai_memory.gateway import create_server

    db = tmp_path / "memory.sqlite"
    conversation_id, segment_id, message_ids = seed_archive(db)
    notebook = SimulatedNotebook(
        [
            {
                "source_id": "owned-source",
                "citation_number": 1,
                # Cloud body is same-length masked; the structural heading
                # still identifies the original local message.
                "cited_text": f"### {conversation_id} · 0002 · Assistant · 2026-11-10T10:01:00Z\n*******",
            },
            {"source_id": "owned-source", "citation_number": 2, "cited_text": "Which project?"},
            {
                "source_id": "unknown-source",
                "citation_number": 3,
                "cited_text": f"### {conversation_id} · 0001 · User · 2026-11-10T10:00:00Z",
            },
            {
                "source_id": "owned-source",
                "citation_number": 4,
                "cited_text": f"### {conversation_id} · 0001 · Assistant · 2026-11-10T10:00:00Z",
            },
        ]
    )
    async with create_connected_server_and_client_session(
        create_server(db, adapter=notebook)
    ) as client:
        answer = decode(await client.call_tool("memory_ask", {"question": "Project?"}))
    sources = answer["sources"]
    assert sources[0]["segment_id"] == segment_id
    assert sources[0]["conversation_id"] == conversation_id
    assert sources[0]["message_ids"] == [message_ids[1]]
    assert "conversation_id" not in sources[1]
    assert "message_ids" not in sources[1]
    assert "segment_id" not in sources[2]
    assert "conversation_id" not in sources[2]
    assert sources[2]["source_id"] == "unknown-source"
    assert "message_ids" not in sources[3]


def test_quoted_message_heading_does_not_authorize_another_message_id(tmp_path):
    """Raw Markdown bodies can quote the structural headings used as citations."""
    from ai_memory.gateway import _reference_sources
    from ai_memory.segments import SegmentBuilder
    from ai_memory.store import Store

    db = tmp_path / "quoted-heading.sqlite"
    with Store(db) as store:
        conversation_id = store.ingest(
            {
                "external_id": "quoted-heading-session",
                "platform": "codex",
                "account": "test",
                "title": "Gateway test",
                "project": "Memory",
                "models": [],
                "created_at": None,
                "updated_at": None,
                "source_url": None,
                "messages": [
                    {
                        "native_id": "u1",
                        "sequence": 1,
                        "role": "user",
                        "kind": "text",
                        "text": "placeholder",
                        "timestamp": "2026-11-10T10:00:00Z",
                    },
                    {
                        "native_id": "a1",
                        "sequence": 2,
                        "role": "assistant",
                        "kind": "text",
                        "text": "Memory.",
                        "timestamp": "2026-11-10T10:01:00Z",
                    },
                ],
            },
            consent=True,
        )
        forged_heading = f"### {conversation_id} · 0002 · Assistant · 2026-11-10T10:01:00Z"
        conversation = store.get_conversation(conversation_id)
        conversation["messages"][0]["text"] = f"Quoted text:\n{forged_heading}\n" + "*" * 7
        store.ingest(conversation, consent=True)

        segment_id = SegmentBuilder(store).build()[0]["id"]
        revision = store.connection.execute(
            "SELECT revision FROM segments WHERE id=?", (segment_id,)
        ).fetchone()[0]
        store.connection.execute(
            "INSERT INTO source_map(segment_id,source_id,revision) VALUES(?,?,?)",
            (segment_id, "owned-source", revision),
        )
        store.connection.commit()

        sources = _reference_sources(
            store,
            [
                {
                    "source_id": "owned-source",
                    "citation_number": 1,
                    "cited_text": f"{forged_heading}\n{'*' * 7}",
                }
            ],
        )

    assert sources[0]["segment_id"] == segment_id
    assert "conversation_id" not in sources[0]
    assert "message_ids" not in sources[0]


async def test_empty_sources_and_question_fail_without_unscoped_retrieval(tmp_path):
    from ai_memory.gateway import create_server

    db = tmp_path / "memory.sqlite"
    seed_archive(db, map_source=False)
    notebook = SimulatedNotebook()
    async with create_connected_server_and_client_session(
        create_server(db, adapter=notebook)
    ) as client:
        empty = decode(await client.call_tool("memory_ask", {"question": " "}))
        assert empty["error"]["code"] == "invalid_question"
        no_sources = decode(await client.call_tool("memory_ask", {"question": "Earlier?"}))
        assert no_sources["error"]["code"] == "no_synced_sources"
    assert notebook.queries == []


async def test_missing_configuration_and_auth_are_reported_without_fake_answer(
    tmp_path, monkeypatch
):
    from ai_memory.gateway import create_server
    from ai_memory.store import Store

    db = tmp_path / "memory.sqlite"
    with Store(db):
        pass
    async with create_connected_server_and_client_session(create_server(db)) as client:
        status = decode(await client.call_tool("memory_status", {}))
        assert status["notebook"]["configured"] is False
        answer = decode(await client.call_tool("memory_ask", {"question": "Earlier?"}))
        assert answer["error"]["code"] == "not_configured"
        assert "answer" not in answer

    seed_archive(db)
    with Store(db) as store:
        store.set_setting("storage_path", str(tmp_path / "missing-auth.json"))
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    async with create_connected_server_and_client_session(create_server(db)) as client:
        status = decode(await client.call_tool("memory_status", {}))
        assert status["notebook"]["connected"] is False
        assert status["notebook"]["error"]["code"] == "auth_required"
        answer = decode(await client.call_tool("memory_ask", {"question": "Earlier?"}))
        assert answer["error"]["code"] == "auth_required"
        assert "answer" not in answer


async def test_real_stdio_subprocess_lists_tools_and_reports_configuration(tmp_path):
    """A genuine MCP handshake parses stdout from the production gateway process."""
    from datetime import timedelta

    db = tmp_path / "stdio.sqlite"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from pathlib import Path; import sys; from ai_memory.gateway import create_server; "
            "create_server(Path(sys.argv[1])).run()",
            str(db),
        ],
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(
            reader, writer, read_timeout_seconds=timedelta(seconds=15)
        ) as client:
            await client.initialize()
            assert {tool.name for tool in (await client.list_tools()).tools} == {
                "memory_status",
                "memory_ask",
            }
            status = decode(await client.call_tool("memory_status", {}))
            assert status["messages"] == 0
            assert status["notebook"]["configured"] is False
            answer = decode(await client.call_tool("memory_ask", {"question": "Prior work?"}))
            assert answer["error"]["code"] == "not_configured"


async def test_edited_archive_does_not_attribute_old_cloud_passage_to_current_message(tmp_path):
    from ai_memory.gateway import create_server
    from ai_memory.store import Store

    db = tmp_path / "edited.sqlite"
    conversation_id, segment_id, message_ids = seed_archive(db)
    with Store(db) as store:
        edited = store.get_conversation(conversation_id)
        edited["messages"][1]["text"] = "A newer decision."
        store.ingest(edited)
    notebook = SimulatedNotebook(
        [
            {
                "source_id": "owned-source",
                "citation_number": 1,
                "cited_text": f"### {conversation_id} · 0002 · Assistant · 2026-11-10T10:01:00Z\nMemory.",
            },
        ]
    )
    async with create_connected_server_and_client_session(
        create_server(db, adapter=notebook)
    ) as client:
        answer = decode(await client.call_tool("memory_ask", {"question": "Earlier project?"}))
    assert answer["sources"][0]["segment_id"] == segment_id
    assert "message_ids" not in answer["sources"][0]
    assert "conversation_id" not in answer["sources"][0]
