"""Real loopback HTTP requests and SQLite, using only synthetic transcripts."""

import http.client
import json
import subprocess
import threading
from pathlib import Path

import pytest

TOKEN = "synthetic-ingest-token"
ORIGIN = "chrome-extension://" + "a" * 32
RAW = "  Сайн уу\n```py\n  print('漢字 🦦')\n```\n\t"


def payload(text=RAW):
    return {
        "source": "chatgpt-web",
        "adapter": "chatgpt-web",
        "conversations": [
            {
                "externalId": "synthetic-web",
                "title": "Synthetic browser conversation",
                "createdAt": None,
                "updatedAt": None,
                "metadata": {
                    "accountId": "synthetic-account",
                    "url": "https://chatgpt.com/c/synthetic-web",
                },
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "createdAt": None,
                        "metadata": {"id": "user-native"},
                        "parts": [{"id": "p-1", "type": "text", "text": text}],
                    },
                    {
                        "role": "assistant",
                        "content": "DO NOT IMPORT",
                        "metadata": {"id": "hidden"},
                        "parts": [{"type": "thinking", "text": "DO NOT IMPORT"}],
                    },
                    {"role": "tool", "content": "DO NOT IMPORT", "metadata": {"id": "tool"}},
                    {
                        "role": "assistant",
                        "content": "",
                        "createdAt": None,
                        "metadata": {"id": "assistant-native"},
                        "parts": [
                            {
                                "type": "thinking",
                                "visibility": "ui",
                                "text": "  Visible summary.\n",
                            },
                            {"type": "text", "text": "  Final answer.\n"},
                        ],
                    },
                ],
            }
        ],
    }


@pytest.fixture
def running_server(tmp_path):
    from ai_memory.ingest import make_server

    database = tmp_path / "archive.sqlite3"
    server = make_server(database, TOKEN, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, database
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def request(server, method="POST", path="/ingest", data=None, headers=None):
    body = json.dumps(data if data is not None else payload(), ensure_ascii=False).encode()
    defaults = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    defaults.update(headers or {})
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request(method, path, body=body if method == "POST" else None, headers=defaults)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def archived(database):
    from ai_memory.store import Store

    with Store(database) as store:
        ids = [
            row[0] for row in store.connection.execute("SELECT id FROM conversations ORDER BY id")
        ]
        return [store.get_conversation(cid) for cid in ids]


def test_authenticated_ingest_preserves_raw_text_visibility_and_no_consent(running_server):
    server, database = running_server
    assert server.server_address[0] == "127.0.0.1"
    status, headers, body = request(server, headers={"Origin": ORIGIN})
    assert status == 200
    assert json.loads(body) == {"conversations": 1, "created": 1, "updated": 0}
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    conversation = archived(database)[0]
    assert conversation["platform"] == "chatgpt"
    assert conversation["account"] == "synthetic-account"
    assert conversation["consent"] is False
    assert conversation["created_at"] is None
    assert [(m["role"], m["kind"], m["text"]) for m in conversation["messages"]] == [
        ("user", "text", RAW),
        ("assistant", "thinking_summary", "  Visible summary.\n"),
        ("assistant", "text", "  Final answer.\n"),
    ]
    assert "DO NOT IMPORT" not in json.dumps(conversation)


def test_reimports_edit_without_duplicates_and_partial_snapshot_keeps_history(running_server):
    server, database = running_server
    assert request(server)[0] == 200
    updated = payload("  edited text\n")
    updated["conversations"][0]["messages"] = [updated["conversations"][0]["messages"][0]]
    status, _, body = request(server, data=updated)
    assert status == 200
    assert json.loads(body) == {"conversations": 1, "created": 0, "updated": 1}
    conversation = archived(database)[0]
    assert len(conversation["messages"]) == 3
    assert conversation["messages"][0]["text"] == "  edited text\n"
    assert request(server, data=updated)[0] == 200
    assert len(archived(database)[0]["messages"]) == 3


def test_account_isolation_uses_provider_native_account_not_short_source_id(running_server):
    server, database = running_server
    assert request(server)[0] == 200
    other = payload()
    other["conversations"][0]["metadata"]["accountId"] = "second-synthetic-account"
    assert request(server, data=other)[0] == 200
    assert len(archived(database)) == 2


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer wrong", "Basic synthetic-ingest-token", "Bearer synthetic-ingest-token-extra"],
)
def test_bad_authentication_cannot_write(running_server, authorization):
    server, database = running_server
    assert request(server, headers={"Authorization": authorization})[0] == 401
    assert archived(database) == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.evil"},
        {"Host": "evil.example"},
        {"Host": "127.0.0.1.evil.example"},
    ],
)
def test_untrusted_host_and_browser_origins_cannot_write(running_server, headers):
    server, database = running_server
    status, response_headers, _ = request(server, headers=headers)
    assert status == 403
    assert "Access-Control-Allow-Origin" not in response_headers
    assert archived(database) == []


def test_preflight_is_limited_to_authenticated_json_ingest(running_server):
    server, _ = running_server
    status, headers, _ = request(
        server,
        method="OPTIONS",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert headers["Access-Control-Allow-Methods"] == "POST"
    assert headers["Access-Control-Allow-Private-Network"] == "true"
    assert (
        request(
            server,
            method="OPTIONS",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": "DELETE"},
        )[0]
        == 403
    )


def test_body_bound_and_input_validation_leave_archive_empty(running_server):
    from ai_memory.ingest import MAX_BODY_BYTES

    server, database = running_server
    assert request(server, headers={"Content-Length": str(MAX_BODY_BYTES + 1)})[0] == 413
    assert request(server, headers={"Content-Type": "text/plain"})[0] == 415
    assert (
        request(
            server,
            data={
                "source": "chatgpt-web",
                "adapter": "chatgpt-web",
                "conversations": [{"messages": []}],
            },
        )[0]
        == 400
    )
    batch = payload()
    batch["conversations"].append({"externalId": "invalid", "messages": "not a list"})
    assert request(server, data=batch)[0] == 400
    assert archived(database) == []


def test_health_reveals_only_status_and_unknown_routes_are_absent(running_server):
    server, _ = running_server
    status, _, body = request(server, method="GET", path="/health", headers={"Authorization": ""})
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert request(server, method="GET", path="/search")[0] == 404
    assert request(server, path="/sources")[0] == 404


def test_missing_server_token_is_rejected(tmp_path):
    from ai_memory.ingest import make_server

    with pytest.raises(ValueError, match="token"):
        make_server(tmp_path / "archive.db", "")


def test_real_extension_providers_roundtrip_to_archive(running_server):
    server, database = running_server
    script = Path(__file__).parents[1] / "vendor/hstry/extension/test/lossless.js"
    process = subprocess.run(
        ["node", str(script), "--json"], capture_output=True, text=True, check=True
    )
    packets = json.loads(process.stdout)
    assert len(packets) == 3
    for packet in packets:
        assert request(server, data=packet)[0] == 200
    conversations = archived(database)
    assert {c["platform"] for c in conversations} == {"chatgpt", "claude", "gemini"}
    for conversation in conversations:
        assert [m["text"] for m in conversation["messages"]] == [RAW, "  Visible answer.\n"]
        assert conversation["created_at"] is None
        assert all(m["timestamp"] is None for m in conversation["messages"])
        assert all(
            m["native_id"] and not m["native_id"].startswith("record:")
            for m in conversation["messages"]
        )
        assert conversation["consent"] is False


def test_visible_summary_changes_keep_the_native_final_text_identity(running_server):
    server, database = running_server
    assert request(server)[0] == 200
    before = archived(database)[0]
    final_id = next(
        message["native_id"]
        for message in before["messages"]
        if message["text"] == "  Final answer.\n"
    )
    next_packet = payload()
    next_packet["conversations"][0]["messages"][-1]["parts"] = [
        {"type": "text", "text": "  Edited final answer.\n"}
    ]
    assert request(server, data=next_packet)[0] == 200
    after = archived(database)[0]
    assert (
        next(message["text"] for message in after["messages"] if message["native_id"] == final_id)
        == "  Edited final answer.\n"
    )
    assert len(after["messages"]) == 3


def test_hidden_text_parts_are_not_archived(running_server):
    server, database = running_server
    data = payload()
    data["conversations"][0]["messages"][0]["parts"].append(
        {"type": "text", "visibility": "hidden", "text": "DO NOT IMPORT"}
    )
    assert request(server, data=data)[0] == 200
    assert "DO NOT IMPORT" not in json.dumps(archived(database))


def test_invalid_unicode_batch_is_rejected_before_any_conversation_is_written(running_server):
    server, database = running_server
    data = payload()
    invalid = payload("invalid \ud800")["conversations"][0]
    invalid["externalId"] = "invalid-unicode"
    data["conversations"].append(invalid)
    body = json.dumps(data, ensure_ascii=True).encode()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request(
        "POST",
        "/ingest",
        body,
        {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    response = connection.getresponse()
    assert response.status == 400
    response.read()
    connection.close()
    assert archived(database) == []


def test_extreme_body_length_header_is_rejected_without_parsing_large_integer(running_server):
    server, database = running_server
    assert request(server, headers={"Content-Length": "9" * 5000})[0] == 413
    assert archived(database) == []


def test_authenticated_connection_probe_does_not_create_or_write_archive(running_server):
    server, database = running_server
    status, headers, body = request(server, path="/auth/check", data={}, headers={"Origin": ORIGIN})
    assert status == 200
    assert json.loads(body) == {"status": "ok", "service": "ai-memory", "schema": 1}
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert not database.exists()
    assert request(server)[0] == 200
    before = archived(database)
    assert request(server, path="/auth/check", data={})[0] == 200
    assert archived(database) == before


@pytest.mark.parametrize("authorization", ["", "Bearer wrong", "Basic synthetic-ingest-token"])
def test_connection_probe_requires_the_same_bearer_token(running_server, authorization):
    server, database = running_server
    assert request(server, path="/auth/check", data={}, headers={"Authorization": authorization})[0] == 401
    assert not database.exists()


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}])
def test_connection_probe_rejects_untrusted_host_or_origin(running_server, headers):
    server, database = running_server
    status, response_headers, _ = request(server, path="/auth/check", data={}, headers=headers)
    assert status == 403
    assert "Access-Control-Allow-Origin" not in response_headers
    assert not database.exists()


def test_connection_preflight_is_only_allowed_on_recognized_routes(running_server):
    server, _ = running_server
    headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "authorization, content-type"}
    for path in ("/auth/check", "/ingest"):
        assert request(server, method="OPTIONS", path=path, headers=headers)[0] == 204
    for path in ("/auth/check?query=1", "/unknown", "/health"):
        assert request(server, method="OPTIONS", path=path, headers=headers)[0] == 404


def test_connection_probe_rejects_unexpected_payload_without_archiving(running_server):
    server, database = running_server
    assert request(server, path="/auth/check", data=payload())[0] == 400
    assert request(server, path="/auth/check", data=[])[0] == 400
    assert not database.exists()


def test_real_worker_auth_helper_connects_to_loopback_receiver_without_ingesting(running_server):
    server, database = running_server
    script = """
import { checkConnection } from './vendor/hstry/extension/worker-auth.js';
const settings = {port: Number(process.argv[1]), token: 'synthetic-ingest-token'};
const accepted = await checkConnection(settings);
const rejected = await checkConnection({...settings, token: 'wrong-private-token'});
process.stdout.write(JSON.stringify({accepted, rejected}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(server.server_port)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True, timeout=10,
    )
    data = json.loads(result.stdout)
    assert data["accepted"]["ok"] is True
    assert data["rejected"]["ok"] is False
    assert data["rejected"]["code"] == "unauthorized"
    assert TOKEN not in result.stdout + result.stderr
    assert not database.exists()
