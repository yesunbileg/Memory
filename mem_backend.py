"""Live archive access for mem.py. Reads never initialize or migrate an archive."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parent
PROVIDER_LABELS = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "codex": "Codex",
    "opencode": "OpenCode",
}
_BROWSER_FOLDERS = {
    "Brave": "BraveSoftware/Brave-Browser",
    "Chrome": "google-chrome",
    "Chromium": "chromium",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SYNC_OPERATIONS = {"sync", "auto-sync"}
_TIMEOUTS = {
    "ask": 420,
    "check": 90,
    "login": 135,
    "docs-connect": 90,
    "setup": 165,
    **dict.fromkeys(_SYNC_OPERATIONS, 1800),
}


class BackendError(RuntimeError):
    """Display-safe operation failure."""

    def __init__(self, message: str, *, code: str = "operation_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Citation:
    label: str
    source_id: str
    revision: int
    chat_id: str = ""
    message_id: str = ""
    number: int = 0
    stale: bool = False
    passage: str = ""


def display_text(value: str) -> str:
    return _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))


def _single_line(value: str) -> str:
    return " ".join(display_text(value).split())


def _epoch(value: str | None) -> float:
    if value:
        with suppress(ValueError, OSError, OverflowError):
            return datetime.fromisoformat(value).timestamp()
    return 0.0


def _use_repo(repo: Path) -> None:
    source = repo / "src"
    if not (source / "ai_memory" / "store.py").is_file():
        raise BackendError(
            "AI Memory-ийн хавтас олдсонгүй. AI_MEMORY_REPO-г шалгана уу.",
            code="repository_missing",
        )
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


class Backend:
    def __init__(self, db: Path | str | None = None, repo: Path | str | None = None) -> None:
        self.repo = (
            Path(repo or os.environ.get("AI_MEMORY_REPO") or DEFAULT_REPO).expanduser().resolve()
        )
        default = self.repo / "data" / "conversations.sqlite"
        self.db = Path(db or os.environ.get("AI_MEMORY_DB") or default).expanduser().resolve()
        self._coverage_cache = {}
        _use_repo(self.repo)

    @contextmanager
    def _archive(self, *, writable: bool = False):
        if not self.db.is_file():
            raise BackendError(
                "Архивын SQLite файл олдсонгүй. --db эсвэл AI_MEMORY_DB-г шалгана уу.",
                code="archive_missing",
            )
        try:
            mode = "rw" if writable else "ro"
            with closing(
                sqlite3.connect(f"{self.db.as_uri()}?mode={mode}", uri=True, timeout=2)
            ) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (1, 2, 3):
                    raise BackendError(
                        "Архивын SQLite хувилбар тохирохгүй байна.",
                        code="archive_incompatible",
                    )
                yield conn, version
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise BackendError(
                    "Архив завгүй байна. Түр хүлээгээд дахин оролдоно уу.",
                    code="archive_busy",
                ) from None
            raise BackendError(
                "Архивыг уншиж чадсангүй. SQLite файл болон эрхээ шалгана уу.",
                code="archive_unreadable",
            ) from None
        except (OSError, ValueError):
            raise BackendError(
                "Архивын тохиргоог уншиж чадсангүй.", code="archive_unreadable"
            ) from None

    @staticmethod
    def _view(conn):
        from ai_memory.store import Store

        # Store's query helpers only require this connection. Its constructor also
        # configures WAL and migrates databases, so reads use the read-only transaction.
        view = Store.__new__(Store)
        view.connection = conn
        return view

    def snapshot(self) -> dict:
        from ai_memory.publication import allowed_sources, published_message_counts
        from ai_memory.sync_activity import read_sync_activity

        with self._archive() as (conn, version):
            view = self._view(conn)
            sync_activity = read_sync_activity(self.db)
            if version >= 2:
                sources = allowed_sources(view)
                published = published_message_counts(
                    view, sources, comparison_cache=self._coverage_cache
                )
                status = view.status(sources=sources, published=published)
            else:
                status = {
                    "notebook_configured": bool(view.get_setting("notebook_id")),
                    "last_verified_remote": {
                        "managed_sources": [],
                        "eligible_sources": 0,
                    },
                    "sources": conn.execute("SELECT count(*) FROM source_map").fetchone()[0],
                    "schema_upgrade_required": True,
                }
                published = {}
            active_segment = sync_activity.get("segment_id") if sync_activity else None
            chats = self._chats(conn, published, active_segment)
            status["conversations"] = len(chats)
            status["messages"] = sum(chat["message_count"] for chat in chats)
            if version == 1:
                status["ready_conversations"] = 0
                status["pending_conversations"] = sum(chat["message_count"] > 0 for chat in chats)
                status["sync_status"] = (
                    "pending" if status["pending_conversations"] else "no_sources"
                )
            instance = view.get_setting("archive_instance_id")
            notebook_id = view.get_setting("notebook_id")
            auto_sync_preference = view.get_setting("tui_auto_sync", True) is True
            auto_sync_due = view.auto_sync_due()
            identity = view.get_setting("notebook_identity")
            if not isinstance(identity, dict) or identity.get("id") != notebook_id:
                identity = {}
        return {
            "chats": chats,
            "status": status,
            "connection": self._connection(instance),
            "notebook_id": notebook_id,
            "notebook_identity": identity,
            "sync_activity": sync_activity,
            "auto_sync": bool(notebook_id) and auto_sync_preference,
            "auto_sync_preference": auto_sync_preference,
            "auto_sync_due": auto_sync_due,
        }

    def set_auto_sync(self, enabled: bool) -> None:
        with self._archive(writable=True) as (conn, _):
            conn.execute("SELECT id FROM conversations LIMIT 0")
            self._view(conn).set_setting("tui_auto_sync", enabled)

    def background_sync_timer_active(self) -> bool:
        """Check the optional default-archive systemd timer."""
        launcher = Path.home() / ".local/bin/mem"
        if self.db != (self.repo / "data/conversations.sqlite").resolve():
            return False
        if launcher.resolve() != (self.repo / "scripts/mem").resolve():
            return False
        with suppress(OSError, subprocess.TimeoutExpired):
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "ai-memory-sync.timer"],
                capture_output=True,
                timeout=1,
            )
            return result.returncode == 0
        return False

    @staticmethod
    def _chats(conn, published: dict[str, int], active_segment: str | None = None) -> list[dict]:
        from ai_memory.diagnostics import SAFE_FAILURES

        query = """
            WITH message_totals AS (
                SELECT conversation_id, count(*) AS total FROM messages GROUP BY conversation_id
            ), errors AS (
                SELECT m.conversation_id, MIN(COALESCE(j.error,'remote_missing')) AS reason
                FROM sync_jobs j JOIN segments s ON s.id=j.segment_id
                JOIN segment_messages sm ON sm.segment_id=s.id
                JOIN messages m ON m.id=sm.message_id
                WHERE s.state!='retired' AND (
                    j.stage='missing' OR
                    (j.revision=s.revision AND j.error IS NOT NULL
                     AND j.stage NOT IN ('done','superseded'))
                ) GROUP BY m.conversation_id
            )
            SELECT c.id,c.platform,c.account,c.title,c.project,c.created_at,c.updated_at,
                c.revision,COALESCE(message_totals.total,0) AS total,errors.reason,
                EXISTS(
                    SELECT 1 FROM messages active_message
                    JOIN segment_messages active_assignment ON active_assignment.message_id=active_message.id
                    JOIN segments active_segment ON active_segment.id=active_assignment.segment_id
                    WHERE active_message.conversation_id=c.id AND active_assignment.segment_id=?
                    AND active_segment.state!='retired'
                ) AS running
            FROM conversations c LEFT JOIN message_totals ON message_totals.conversation_id=c.id
            LEFT JOIN errors ON errors.conversation_id=c.id
        """
        chats = []
        for row in conn.execute(query, (active_segment,)):
            total, covered = row["total"], published.get(row["id"], 0)
            reason = row["reason"] or ""
            if row["running"]:
                nb = "running"
            elif total and total == covered:
                nb = "done"
            elif reason:
                nb = "failed"
            elif covered:
                nb = "partial"
            else:
                nb = "pending"
            platform = row["platform"]
            chats.append(
                {
                    "id": row["id"],
                    "ai": PROVIDER_LABELS.get(platform, _single_line(platform)),
                    "title": _single_line(row["title"] or "") or row["id"],
                    "project": _single_line(row["project"] or ""),
                    "account": _single_line(row["account"]),
                    "platform": platform,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "ts": _epoch(row["updated_at"]) or _epoch(row["created_at"]),
                    "revision": row["revision"],
                    "local": True,
                    "nb": nb,
                    "parts": (covered, total),
                    "error": (reason if reason in SAFE_FAILURES else "operation_failed")
                    if nb == "failed"
                    else "",
                    "prog": covered / total if total else 0.0,
                    "message_count": total,
                }
            )
        return sorted(chats, key=lambda item: (-item["ts"], item["id"]))

    def messages(self, cid: str) -> list[tuple[str, str]]:
        with self._archive() as (conn, _):
            if not conn.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                raise BackendError("Чат олдсонгүй. Архиваа шинэчилнэ үү.", code="chat_missing")
            return self._messages(conn, cid)

    @staticmethod
    def _messages(conn, cid: str) -> list[tuple[str, str]]:
        return [
            ("Та" if row["role"] == "user" else "AI", display_text(row["text"]))
            for row in conn.execute(
                "SELECT role,text FROM messages WHERE conversation_id=? ORDER BY sequence", (cid,)
            )
        ]

    @staticmethod
    def _current_citation(mapping, revision: int) -> bool:
        return bool(
            mapping
            and mapping["content_hash"]
            and mapping["source_revision"] == mapping["revision"] == revision
        )

    def cited_messages(self, citation: Citation) -> tuple[list[tuple[str, str]], int]:
        from ai_memory.publication import allowed_sources

        with self._archive() as (conn, _):
            mapping = allowed_sources(self._view(conn)).get(citation.source_id)
            if self._current_citation(mapping, citation.revision):
                target = self._citation(
                    conn, citation.message_id, citation.chat_id, mapping["segment_id"]
                )
                if target:
                    return self._messages(conn, citation.chat_id), target[1]
        raise BackendError(
            "Эх мессеж өөрчлөгдсөн байна. Асуултаа дахин асууна уу.", code="stale_citation"
        )

    @staticmethod
    def _citation(conn, message_id: str, conversation: str, segment: str) -> tuple[str, int] | None:
        row = conn.execute(
            """SELECT c.title, (SELECT count(*) FROM messages preceding
               WHERE preceding.conversation_id=m.conversation_id AND preceding.sequence<=m.sequence)
               AS display_index FROM messages m JOIN conversations c ON c.id=m.conversation_id
               JOIN segment_messages sm ON sm.message_id=m.id
               WHERE m.id=? AND c.id=? AND sm.segment_id=?""",
            (message_id, conversation, segment),
        ).fetchone()
        if row:
            return (
                _single_line(row["title"] or "") or conversation,
                row["display_index"],
            )
        return None

    def connection(self, *, probe: bool = False) -> dict:
        with self._archive() as (conn, _):
            instance = self._view(conn).get_setting("archive_instance_id")
        return self._connection(instance, probe=probe)

    def _connection(self, instance, *, probe: bool = False) -> dict:
        from ai_memory.archive_files import workspace_for
        from ai_memory.connection import (
            _read_connection,
            _read_private,
            check_running_receiver,
        )

        workspace = workspace_for(self.db)
        result = {
            "transport": "unpaired",
            "state": "unpaired",
            "port": None,
            "connection_file": None,
            "browsers": [],
            "native_registered": False,
        }
        if isinstance(instance, str) and re.fullmatch(r"[0-9a-f]{32}", instance):
            host = "org.ai_memory.archive_" + instance
            root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            for browser, folder in _BROWSER_FOLDERS.items():
                path = root / folder / "NativeMessagingHosts" / f"{host}.json"
                try:
                    content = _read_private(path)
                    manifest = json.loads(content) if content else {}
                    if not isinstance(manifest, dict) or manifest.get("name") != host:
                        continue
                    origins = manifest.get("allowed_origins")
                    if not isinstance(origins, list):
                        continue
                    for origin in origins:
                        found = re.fullmatch(r"chrome-extension://([a-p]{32})/", str(origin))
                        if not found:
                            continue
                        directory = workspace / "native" / found[1]
                        executable = directory / "host"
                        descriptor = directory / "extension-connection.json"
                        if (
                            manifest.get("type") == "stdio"
                            and manifest.get("path") == str(executable)
                            and executable.is_file()
                            and os.access(executable, os.X_OK)
                        ):
                            data = _read_private(descriptor)
                            if data and json.loads(data) == {
                                "schema": 2,
                                "application": "ai-memory",
                                "transport": "native",
                                "native_host": host,
                                "archive_instance_id": instance,
                            }:
                                result["browsers"].append(browser)
                                result["connection_file"] = str(descriptor)
                                break
                except (OSError, ValueError):
                    continue
            if result["browsers"]:
                return {
                    **result,
                    "transport": "native",
                    "state": "registered",
                    "native_registered": True,
                }

        paths = [workspace / "extension-connection.json"]
        # Older installs stored the connection next to the repository archive.
        if self.db == self.repo / "data" / "conversations.sqlite":
            paths.append(self.db.parent / "extension-connection.json")
        for path in paths:
            try:
                config = _read_connection(path)
            except (OSError, ValueError):
                return {
                    **result,
                    "transport": "http",
                    "state": "invalid",
                    "connection_file": str(path),
                }
            if config:
                port = config["port"]
                status = check_running_receiver(port, config["token"], self.db) if probe else None
                state = {
                    "ok": "online",
                    "unauthorized": "invalid",
                    "different_archive": "invalid",
                    "incompatible": "offline",
                }.get(status, "configured")
                return {
                    **result,
                    "transport": "http",
                    "state": state,
                    "port": port,
                    "connection_file": str(path),
                }
        return result

    def _cloud_citations(self, sources: list[dict]) -> list[Citation]:
        from ai_memory.publication import allowed_sources

        if not sources:
            return []
        result = []
        with self._archive() as (conn, _):
            eligible = allowed_sources(self._view(conn))
            for source in sources:
                cid = source.get("conversation_id")
                segment, revision, sid = (
                    source["segment_id"],
                    source["revision"],
                    source["source_id"],
                )
                stale = source.get("stale") or not self._current_citation(
                    eligible.get(sid), revision
                )
                resolved = [
                    Citation(target[0], sid, revision, cid, mid, target[1])
                    for mid in source.get("message_ids", [])
                    if cid and not stale
                    if (target := self._citation(conn, mid, cid, segment)) is not None
                ]
                result.extend(
                    resolved
                    or [
                        Citation(
                            f"{_single_line(segment)} · r{revision}",
                            sid,
                            revision,
                            stale=bool(stale),
                            passage=display_text(source.get("cited_text") or ""),
                        )
                    ]
                )
        return list(dict.fromkeys(result))

    async def call(self, operation: str, question: str | dict = "", *, on_progress=None) -> dict:
        if operation not in _TIMEOUTS:
            raise BackendError("Үйлдлийг таньсангүй.", code="invalid_operation")
        payload = b""
        if operation == "ask":
            if not isinstance(question, str) or not question.strip() or len(question) > 4000:
                raise BackendError("Асуултаа 1–4000 тэмдэгтээр бичнэ үү.", code="invalid_question")
            payload = question.encode("utf-8")
        elif operation in {"login", "setup"}:
            fields = question if isinstance(question, dict) else {"profile": question}
            limits = {"profile": 256, "account": 256}
            if operation == "setup":
                limits.update(mode=6, value=512)
            if any(
                key not in limits
                or not isinstance(value, str)
                or len(value) > limits[key]
                or value != _single_line(value)
                for key, value in fields.items()
            ):
                raise BackendError(
                    "NotebookLM тохиргооны утгуудаа шалгана уу.", code="invalid_setup"
                )
            payload = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        python = Path(os.environ.get("AI_MEMORY_PYTHON") or self.repo / ".venv" / "bin" / "python")
        if not python.expanduser().is_file():
            raise BackendError(
                "AI Memory-ийн Python орчин олдсонгүй. AI_MEMORY_PYTHON-г шалгана уу.",
                code="backend_unavailable",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                str(python.expanduser().absolute()),
                str(Path(__file__).resolve()),
                str(self.repo),
                str(self.db),
                operation,
                cwd=self.repo,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                limit=1024 * 1024,
            )
        except OSError:
            raise BackendError(
                "AI Memory-ийн Python үйлдлийг эхлүүлж чадсангүй.",
                code="backend_unavailable",
            ) from None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TIMEOUTS[operation]
        try:
            async with asyncio.timeout_at(deadline):
                if operation in _SYNC_OPERATIONS:
                    output = await self._sync_output(proc, on_progress)
                else:
                    output, _ = await proc.communicate(payload)
        except (asyncio.CancelledError, TimeoutError) as error:
            # A sync may be finishing the upload and persisting its source ID.
            grace = max(2.0, deadline - loop.time()) if operation in _SYNC_OPERATIONS else 2.0
            await self._stop(proc, grace=grace)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BackendError("AI Memory-ийн үйлдлийн хугацаа хэтэрлээ.", code="timeout") from None
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise BackendError(
                "AI Memory-ээс хариу авч чадсангүй.", code="backend_unavailable"
            ) from None
        if not isinstance(result, dict):
            raise BackendError("AI Memory буруу хариу буцаалаа.", code="backend_unavailable")
        if proc.returncode and not any(key in result for key in ("error", "status", "notebook")):
            raise BackendError("AI Memory-ийн үйлдэл амжилтгүй боллоо.")
        if operation == "ask" and "answer" in result:
            result["answer"] = display_text(result["answer"])
            result["citations"] = await asyncio.to_thread(
                self._cloud_citations, result.get("sources", [])
            )
        return result

    @staticmethod
    async def _sync_output(proc, on_progress) -> bytes:
        proc.stdin.close()
        output = b""
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                output = line
                continue
            if isinstance(event, dict) and "progress" in event:
                if on_progress and isinstance(event["progress"], dict):
                    with suppress(Exception):
                        on_progress(event["progress"])
            else:
                output = line
        await proc.wait()
        return output

    @staticmethod
    async def _stop(proc, *, grace: float) -> None:
        if proc.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGINT)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=grace)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.communicate()


def _worker(repo: str, db: str, operation: str) -> int:
    _use_repo(Path(repo))
    from ai_memory.cli import main
    from ai_memory.gateway import MemoryApplication

    if operation not in _TIMEOUTS:
        return 2
    args = ["--db", db, "docs-web-enable" if operation == "docs-connect" else operation]
    if operation in _SYNC_OPERATIONS:
        args[-1] = "sync"
        if operation == "auto-sync":
            args.append("--automatic")

        def emit(event):
            print(json.dumps({"progress": event}, ensure_ascii=False), flush=True)

        return main(args, progress=emit) or 0
    if operation in {"check", "login", "setup"}:
        from ai_memory.notebook import (
            NotebookLMError,
            check_notebook_connection,
            login_from_brave,
            setup_notebook_from_brave,
        )

        try:
            if operation == "check":
                result = check_notebook_connection(Path(db))
            else:
                fields = json.loads(sys.stdin.buffer.read(8192))
                action = login_from_brave if operation == "login" else setup_notebook_from_brave
                result = action(Path(db), **fields)
        except NotebookLMError as error:
            message = str(error)
            if error.code == "auth_required":
                message = (
                    "Brave дээр NotebookLM-ээ нээдэг Google аккаунтаар нэвтэрч, "
                    "Холболт цонхоор дахин оролдоорой."
                )
            result = {"error": {"code": error.code, "message": message}}
        except (OSError, RuntimeError, ValueError, TypeError):
            result = {
                "error": {
                    "code": f"{operation}_failed",
                    "message": "NotebookLM үйлдэл амжилтгүй. Архив болон холболтоо шалгаарай.",
                }
            }
        except KeyboardInterrupt:
            return 130
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if operation != "ask":
        return main(args) or 0

    # Keep private questions out of process listings and command history.
    question = sys.stdin.buffer.read(16001).decode("utf-8")
    try:
        result = asyncio.run(MemoryApplication(Path(db)).chat(question))
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker(*sys.argv[1:]))
