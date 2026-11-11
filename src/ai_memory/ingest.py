"""Small authenticated, loopback-only receiver for the hstry extension.

Public health, authenticated connection checks, and hstry ``POST /ingest`` are exposed.
Capture archives locally with upload consent disabled; cloud sync is separate.
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .connection import archive_identity
from .normalization import normalize_conversation
from .store import Store

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_CONVERSATIONS = 100
_PLATFORMS = {
    "chatgpt-web": "chatgpt",
    "claude-web": "claude",
    "gemini": "gemini",
    "gemini-web": "gemini",
}
_EXTENSION_ORIGIN = re.compile(
    r"(?:chrome-extension://[a-p]{32}|moz-extension://[0-9a-fA-F-]{36})\Z"
)


def _normalize_packet(packet: object) -> list[dict]:
    if not isinstance(packet, dict):
        raise ValueError("invalid packet")
    adapter = packet.get("adapter")
    if not isinstance(adapter, str) or adapter not in _PLATFORMS:
        raise ValueError("unsupported browser adapter")
    source = packet.get("source")
    if not isinstance(source, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", source):
        raise ValueError("invalid source")
    conversations = packet.get("conversations")
    if not isinstance(conversations, list) or len(conversations) > MAX_CONVERSATIONS:
        raise ValueError("invalid conversation list")
    result = []
    for raw in conversations:
        if not isinstance(raw, dict):
            raise ValueError("invalid conversation")
        for field in ("title", "workspace", "externalId"):
            if raw.get(field) is not None and not isinstance(raw[field], str):
                raise ValueError("invalid conversation metadata")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("invalid conversation metadata")
        account = (
            metadata.get("accountId")
            or metadata.get("orgId")
            or raw.get("account")
            or packet.get("account")
            or source
        )
        if not isinstance(account, str) or not account or len(account) > 1024:
            raise ValueError("invalid account")
        raw_messages = raw.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("invalid messages")
        for message in raw_messages:
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError("invalid message content")
            if message.get("metadata") is not None and not isinstance(message["metadata"], dict):
                raise ValueError("invalid message metadata")
            parts = message.get("parts")
            if parts is not None and (
                not isinstance(parts, list) or any(not isinstance(part, dict) for part in parts)
            ):
                raise ValueError("invalid message parts")
            if parts and any(
                part.get("text") is not None and not isinstance(part["text"], str) for part in parts
            ):
                raise ValueError("invalid part text")
        conversation = normalize_conversation(raw, _PLATFORMS[adapter], account)
        # Validate the complete batch before Store.ingest can commit any row.
        # Escaped unpaired surrogates are JSON strings but cannot be stored as
        # lossless UTF-8 text.
        json.dumps(conversation, ensure_ascii=False).encode("utf-8")
        ids = [message["native_id"] for message in conversation["messages"]]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate message IDs")
        result.append(conversation)
    return result


def make_server(db_path: Path, token: str, port: int = 0) -> HTTPServer:
    """Return a server bound to 127.0.0.1; the caller owns serve/shutdown."""
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise ValueError("A nonempty ingest token is required")
    db_path = Path(db_path).expanduser().resolve()
    expected_auth = ("Bearer " + token).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AI-Memory"
        sys_version = ""
        # Closing each request also prevents unconsumed oversized bodies from
        # being interpreted as another request on a persistent connection.
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            # Paths, tokens, and source text must never reach default HTTP logs.
            pass

        def _allowed_origin(self, origin: str) -> bool:
            return bool(_EXTENSION_ORIGIN.fullmatch(origin)) or origin in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }

        def _boundary(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if len(hosts) != 1 or hosts[0] not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self._reply(403, {"error": "untrusted_host"}, cors=False)
                return False
            if len(origins) > 1 or (origins and not self._allowed_origin(origins[0])):
                self._reply(403, {"error": "untrusted_origin"}, cors=False)
                return False
            return True

        def _reply(
            self,
            status: int,
            data: dict | None = None,
            *,
            cors: bool = True,
            preflight: bool = False,
        ):
            body = (
                json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                if data is not None
                else b""
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            if cors and origin and self._allowed_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                if preflight:
                    self.send_header("Access-Control-Allow-Methods", "POST")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                    self.send_header("Access-Control-Max-Age", "600")
                    if self.headers.get("Access-Control-Request-Private-Network") == "true":
                        self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            if self._boundary():
                self._reply(200, {"status": "ok"}) if self.path == "/health" else self._reply(
                    404, {"error": "not_found"}
                )

        def do_OPTIONS(self):
            if not self._boundary():
                return
            requested = {
                header.strip().lower()
                for header in self.headers.get("Access-Control-Request-Headers", "").split(",")
                if header.strip()
            }
            if self.path not in {"/ingest", "/auth/check"}:
                self._reply(404, {"error": "not_found"})
            elif (
                not self.headers.get("Origin")
                or self.headers.get("Access-Control-Request-Method") != "POST"
                or not requested <= {"authorization", "content-type"}
            ):
                self._reply(403, {"error": "invalid_preflight"})
            else:
                self._reply(204, preflight=True)

        def do_POST(self):
            if not self._boundary():
                return
            if self.path not in {"/ingest", "/auth/check"}:
                self._reply(404, {"error": "not_found"})
                return
            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not hmac.compare_digest(
                authorization[0].encode("utf-8"), expected_auth
            ):
                self._reply(401, {"error": "unauthorized"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get("Transfer-Encoding")
                or len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdecimal()
            ):
                self._reply(400, {"error": "invalid_body_length"})
                return
            if len(lengths[0]) > 20:
                self._reply(413, {"error": "body_too_large"})
                return
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                self._reply(413, {"error": "body_too_large"})
                return
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                self._reply(415, {"error": "json_required"})
                return
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                packet = json.loads(raw.decode("utf-8")) if raw else {}
                if self.path == "/auth/check":
                    if not isinstance(packet, dict) or packet:
                        raise ValueError("connection check requires an empty object")
                    self._reply(
                        200,
                        {
                            "status": "ok",
                            "service": "ai-memory",
                            "schema": 1,
                            "archive_id": archive_identity(db_path),
                        },
                    )
                    return
                conversations = _normalize_packet(packet)
            except (ValueError, TypeError, UnicodeError, RecursionError, TimeoutError):
                self._reply(400, {"error": "invalid_payload"})
                return
            created = updated = 0
            try:
                with Store(db_path) as store:
                    for conversation in conversations:
                        previous = store.connection.execute(
                            "SELECT id FROM conversations WHERE platform=? AND account=? AND external_id=?",
                            (
                                conversation["platform"],
                                conversation["account"],
                                conversation["external_id"],
                            ),
                        ).fetchone()
                        if previous:
                            # A browser snapshot may omit deleted or older turns.
                            # Preserve archive identities and keep absent history.
                            known = dict(
                                store.connection.execute(
                                    "SELECT native_id,sequence FROM messages WHERE conversation_id=?",
                                    (previous[0],),
                                )
                            )
                            sequence = max(known.values(), default=0)
                            for message in conversation["messages"]:
                                if message["native_id"] in known:
                                    message["sequence"] = known[message["native_id"]]
                                else:
                                    sequence += 1
                                    message["sequence"] = sequence
                            updated += 1
                        else:
                            created += 1
                        store.ingest(conversation, consent=False)
            except ValueError:
                self._reply(400, {"error": "invalid_payload"})
                return
            except sqlite3.OperationalError:
                self._reply(503, {"error": "archive_unavailable"})
                return
            self._reply(
                200, {"conversations": len(conversations), "created": created, "updated": updated}
            )

    return HTTPServer(("127.0.0.1", port), Handler)
