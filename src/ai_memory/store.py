"""Transactional source archive. This module performs no AI retrieval."""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .diagnostics import SAFE_FAILURES


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _time_order(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    try:
        a = datetime.fromisoformat(left.replace("Z", "+00:00"))
        b = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return (left > right) - (left < right)
    if a.tzinfo is None and b.tzinfo is not None:
        a = a.replace(tzinfo=b.tzinfo)
    elif b.tzinfo is None and a.tzinfo is not None:
        b = b.replace(tzinfo=a.tzinfo)
    return (a > b) - (a < b)


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, external_id TEXT NOT NULL, platform TEXT NOT NULL,
 account TEXT NOT NULL, title TEXT, project TEXT, models TEXT NOT NULL,
 created_at TEXT, updated_at TEXT, source_url TEXT, consent INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, UNIQUE(platform,account,external_id)
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 native_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>0),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')),
 kind TEXT NOT NULL CHECK(kind IN ('text','progress','thinking_summary')),
 text TEXT NOT NULL, timestamp TEXT, model TEXT, content_hash TEXT NOT NULL,
 UNIQUE(conversation_id,native_id), UNIQUE(conversation_id,sequence)
);
CREATE TABLE IF NOT EXISTS segments (
 id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('open','sealed','retired')),
 revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 content_hash TEXT NOT NULL DEFAULT '', word_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_segment ON segments(state) WHERE state='open';
CREATE TABLE IF NOT EXISTS segment_messages (
 segment_id TEXT NOT NULL REFERENCES segments(id), message_id TEXT NOT NULL REFERENCES messages(id),
 position INTEGER NOT NULL, PRIMARY KEY(segment_id,message_id), UNIQUE(segment_id,position)
);
CREATE INDEX IF NOT EXISTS segment_message_lookup ON segment_messages(message_id);
CREATE TABLE IF NOT EXISTS segment_metadata (
 segment_id TEXT NOT NULL REFERENCES segments(id), conversation_id TEXT NOT NULL REFERENCES conversations(id),
 metadata TEXT NOT NULL, PRIMARY KEY(segment_id,conversation_id)
);
CREATE TABLE IF NOT EXISTS segment_parents (
 segment_id TEXT NOT NULL REFERENCES segments(id), parent_id TEXT NOT NULL REFERENCES segments(id),
 PRIMARY KEY(segment_id,parent_id)
);
CREATE TABLE IF NOT EXISTS source_map (
 segment_id TEXT PRIMARY KEY REFERENCES segments(id), source_id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_jobs (
 id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES segments(id), revision INTEGER NOT NULL,
 title TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'pending', new_source_id TEXT,
 old_source_ids TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, error TEXT, UNIQUE(segment_id,revision)
);
CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.close()
            raise ValueError("Archive schema is newer than this application")
        if version == 0:
            self.connection.executescript(SCHEMA)
        elif version != 1:
            self.close()
            raise ValueError("Unsupported archive schema")

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def ingest(self, conversation: dict, consent: bool = False) -> str:
        c = dict(conversation)
        platform = str(c.get("platform", "")).lower()
        external = c.get("external_id")
        account = c.get("account", "local")
        if not platform or not isinstance(external, str) or not external or not isinstance(account, str):
            raise ValueError("platform, account, and native conversation ID are required")
        if not isinstance(c.get("models", []), list) or any(not isinstance(m, str) for m in c.get("models", [])):
            raise ValueError("models must be a list of source-provided strings")
        for field in ("title", "project", "created_at", "updated_at", "source_url"):
            if c.get(field) is not None and not isinstance(c[field], str):
                raise ValueError(f"{field} must be source text or null")
        messages = c.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        seen_ids, seen_sequences = set(), set()
        for m in messages:
            if m.get("role") not in {"user", "assistant"} or m.get("kind", "text") not in {"text", "progress", "thinking_summary"}:
                raise ValueError("Only user-visible conversation text is accepted")
            if not isinstance(m.get("text"), str) or not isinstance(m.get("native_id"), str) or not m["native_id"]:
                raise ValueError("A stable native_id and exact text are required")
            seq = m.get("sequence")
            if type(seq) is not int or seq < 1 or seq in seen_sequences or m["native_id"] in seen_ids:
                raise ValueError("Message identities and positive sequence numbers must be unique")
            for field in ("timestamp", "updated_at", "model"):
                if m.get(field) is not None and not isinstance(m[field], str):
                    raise ValueError(f"message {field} must be source text or null")
            seen_ids.add(m["native_id"])
            seen_sequences.add(seq)
        prefix = {"codex": "CX", "opencode": "OC", "chatgpt": "CG", "claude": "CL", "gemini": "GM"}.get(platform, "CV")
        key = prefix + "-" + digest([platform, account, external])[:16].upper()
        db = self.connection
        with db:
            old = db.execute("SELECT * FROM conversations WHERE id=?", (key,)).fetchone()
            incoming_update = c.get("updated_at")
            old_update = old["updated_at"] if old else None
            metadata_stale = bool(old and old_update and (
                not incoming_update or _time_order(incoming_update, old_update) == -1
            ))
            models = list(dict.fromkeys((json.loads(old["models"]) if old else []) + c.get("models", [])))
            fields = [
                old[f] if old and (metadata_stale or (f == "updated_at" and not c.get(f)))
                else c.get(f) if c.get(f) is not None else (old[f] if old else None)
                for f in ("title", "project", "created_at", "updated_at", "source_url")
            ]
            metadata = [*fields, json.dumps(models, ensure_ascii=False)]
            changed = old is None or metadata != [old[f] for f in ("title", "project", "created_at", "updated_at", "source_url")] + [old["models"]]
            db.execute("""INSERT INTO conversations(id,external_id,platform,account,title,project,
                created_at,updated_at,source_url,models,consent) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,project=excluded.project,
                created_at=excluded.created_at,updated_at=excluded.updated_at,source_url=excluded.source_url,
                models=excluded.models,consent=MAX(conversations.consent,excluded.consent)""",
                       (key, external, platform, account, *fields, metadata[-1], int(consent)))
            for m in messages:
                mid = digest([key, m["native_id"]])
                values = [m["sequence"], m["role"], m.get("kind", "text"), m["text"], m.get("timestamp"), m.get("model")]
                content_hash = digest(values)
                previous = db.execute("SELECT content_hash,timestamp FROM messages WHERE id=?", (mid,)).fetchone()
                version_key = f"message-version:{mid}"
                saved_version = db.execute("SELECT value FROM checkpoints WHERE key=?", (version_key,)).fetchone()
                incoming_version = m.get("updated_at") or c.get("updated_at") or m.get("timestamp")
                existing_version = saved_version[0] if saved_version else (
                    old["updated_at"] if old and old["updated_at"] else
                    previous["timestamp"] if previous else None
                )
                if previous is not None and existing_version and (
                        not incoming_version or _time_order(incoming_version, existing_version) == -1):
                    continue
                if previous is None or previous[0] != content_hash:
                    changed = True
                    db.execute("""INSERT INTO messages(id,conversation_id,native_id,sequence,role,kind,
                        text,timestamp,model,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence,role=excluded.role,
                        kind=excluded.kind,text=excluded.text,timestamp=excluded.timestamp,
                        model=excluded.model,content_hash=excluded.content_hash""",
                               (mid, key, m["native_id"], *values, content_hash))
                    if previous is not None:
                        db.execute("""UPDATE segments SET content_hash='' WHERE id IN (
                            SELECT segment_id FROM segment_messages WHERE message_id=?)""", (mid,))
                if incoming_version and (previous is None or not existing_version or
                                         _time_order(incoming_version, existing_version) != -1):
                    db.execute("INSERT OR REPLACE INTO checkpoints(key,value) VALUES(?,?)",
                               (version_key, incoming_version))
            if changed and old:
                db.execute("UPDATE conversations SET revision=revision+1 WHERE id=?", (key,))
                db.execute("""UPDATE segments SET content_hash='' WHERE state='open' AND id IN (
                    SELECT sm.segment_id FROM segment_messages sm JOIN messages m ON m.id=sm.message_id
                    WHERE m.conversation_id=?)""", (key,))
        return key

    def approve(self, conversation_id: str):
        with self.connection:
            if self.connection.execute("UPDATE conversations SET consent=1 WHERE id=?", (conversation_id,)).rowcount != 1:
                raise ValueError("Conversation does not exist")

    def get_conversation(self, conversation_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise ValueError("Conversation does not exist")
        result = dict(row)
        result["models"] = json.loads(result["models"])
        result["consent"] = bool(result["consent"])
        result["messages"] = [dict(r) for r in self.connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence", (conversation_id,))]
        return result

    def get_setting(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        if key == "notebook_id" and self.get_setting(key) not in (None, value):
            mapped = self.connection.execute("SELECT COUNT(*) FROM source_map").fetchone()[0]
            unresolved = self.connection.execute("SELECT COUNT(*) FROM sync_jobs WHERE stage NOT IN ('done','superseded')").fetchone()[0]
            if mapped or unresolved:
                raise ValueError("Use a separate archive for a different notebook; sources or unresolved uploads already exist")
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))

    def status(self) -> dict:
        db = self.connection

        def count(query):
            return db.execute(query).fetchone()[0]
        pending = count("""SELECT COUNT(*) FROM segments s LEFT JOIN source_map sm ON s.id=sm.segment_id
            WHERE s.state!='retired' AND (sm.source_id IS NULL OR sm.revision!=s.revision OR s.content_hash='')""")
        unassigned = count("""SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE c.consent=1 AND NOT EXISTS(SELECT 1 FROM segment_messages sm JOIN segments s ON s.id=sm.segment_id
            WHERE sm.message_id=m.id AND s.state!='retired')""")
        cleanup = count("SELECT COUNT(*) FROM sync_jobs WHERE stage NOT IN ('done','superseded')")
        sync_errors = [
            {"segment_id": row["segment_id"],
             "reason": row["error"] if row["error"] in SAFE_FAILURES else "operation_failed",
             "attempts": row["attempts"], "next_attempt": row["next_attempt"],
             "stage": row["stage"]}
            for row in db.execute("""SELECT segment_id,error,attempts,next_attempt,stage
                FROM sync_jobs WHERE error IS NOT NULL AND stage NOT IN ('done','superseded')
                ORDER BY next_attempt,segment_id""")
        ]
        import_failures = self.get_setting("import_failures", [])
        return {"conversations": count("SELECT COUNT(*) FROM conversations"),
                "messages": count("SELECT COUNT(*) FROM messages"),
                "unapproved_conversations": count("SELECT COUNT(*) FROM conversations WHERE consent=0"),
                "segments": count("SELECT COUNT(*) FROM segments WHERE state!='retired'"),
                "sources": count("SELECT COUNT(*) FROM source_map"),
                "pending_segments": pending, "unassigned_messages": unassigned,
                "pending_jobs": cleanup,
                "sync_errors": sync_errors,
                "import_failures": import_failures,
                "sync_status": "pending" if pending or unassigned or cleanup else "up_to_date",
                "notebook_configured": bool(self.get_setting("notebook_id"))}
