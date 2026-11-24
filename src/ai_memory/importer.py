"""Incremental local import, per-file checkpoints, and isolated failures."""

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from .store import Store


def _opencode_sessions(root: Path) -> list[Path]:
    """Enumerate both JSON layouts without listing inside unselected symlinks."""
    from .collectors import selected_source

    def entries(directory):
        if not directory.exists() and not directory.is_symlink():
            return []
        return sorted(selected_source(directory).iterdir())

    found = {}
    for legacy, projects in ((False, root / "storage/session"), (True, root / "project")):
        for project in entries(projects):
            if project.is_symlink() or not project.is_dir():
                continue
            for item in entries(project / "storage/session/info" if legacy else project):
                if item.match("ses_*.json"):
                    found.setdefault(item.name, item)
    return list(found.values())


def _fingerprint(platform, path, file, relative, json_layout):
    """Only stat native dependencies that could change this unit's visible data."""
    from .collectors import selected_source

    def stamp(p):
        st = selected_source(p).stat()
        return [str(p), st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]

    def children(directory, pattern):
        if not directory.exists() and not directory.is_symlink():
            return []
        return sorted(selected_source(directory).glob(pattern))

    if json_layout:
        info = selected_source(file)
        session = info.stem
        # Match the adapter's native path shapes before enumerating dependencies.
        legacy = info.match("project/*/storage/session/info/ses_*.json")
        if not legacy and not info.match("storage/session/*/ses_*.json"):
            raise ValueError("Unsupported OpenCode session JSON path")
        base = info.parents[1] if legacy else info.parents[2]
        files = [info]
        parts = base / "part" / session if legacy else base / "part"
        for message in children(base / "message" / session, "msg_*.json"):
            files.append(message)
            files.extend(children(parts / message.stem, "prt_*.json"))
        input_data = [stamp(item) for item in files]
    elif platform == "opencode" and (path.is_dir() or path.name == "opencode.db"):
        database = path / "opencode.db" if path.is_dir() else path
        input_data = [
            stamp(item)
            for item in (database, Path(str(database) + "-wal"))
            if item.exists() or item.is_symlink()
        ]
    else:
        input_data = [relative, stamp(file)]
    return hashlib.sha256(json.dumps(input_data).encode()).hexdigest()


def _refresh_codex_index(store, path, account, file_key, external_id, index):
    """Refresh one checkpointed rollout's metadata without reparsing its text."""
    from .collectors import codex_thread_metadata

    cid = None
    if index is None:
        return None
    state_key = f"codex-index:{file_key}:{external_id}"
    serialized = json.dumps(index, sort_keys=True)
    saved = store.connection.execute(
        "SELECT value FROM checkpoints WHERE key=?", (state_key,)
    ).fetchone()
    if saved and saved[0] == serialized:
        return None
    with store.import_transaction():
        # Verify the selected row after reserving the archive writer.
        if codex_thread_metadata(path, external_id).get(external_id) != index:
            return None
        previous = store.connection.execute(
            "SELECT id,title,models,updated_at FROM conversations WHERE platform=? AND account=? AND external_id=?",
            ("codex", account, external_id),
        ).fetchone()
        if previous:
            title = index["title"]
            if not title and saved and json.loads(saved[0]).get("title"):
                first = store.connection.execute(
                    "SELECT text FROM messages WHERE conversation_id=? AND role='user' ORDER BY sequence LIMIT 1",
                    (previous["id"],),
                ).fetchone()
                title = " ".join(first[0].split()) if first else None
                if title and len(title) > 80:
                    title = title[:77] + "..."
            model = index["model"]
            if (
                saved
                or (title and title != previous["title"])
                or (model and model not in json.loads(previous["models"]))
            ):
                cid = store.ingest(
                    {
                        "platform": "codex",
                        "account": account,
                        "external_id": external_id,
                        "title": title or None,
                        "models": [model] if model else [],
                        "updated_at": previous["updated_at"],
                        "messages": [],
                    }
                )
        store.connection.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
            (state_key, serialized),
        )
    return cid


def collect_into(
    store: Store,
    platform: str,
    path: Path,
    account: str = "local",
    consent: bool | None = None,
) -> dict:
    from .collectors import codex_rollout_files, codex_thread_metadata, collect, selected_source

    platform = platform.lower()
    # Hash the selected source even when it is missing or inaccessible. Its
    # identity stays stable across failure/recovery, without retaining its path.
    path = Path(path).expanduser().absolute()
    source_id = hashlib.sha256(f"{platform}:{account}:{path}".encode()).hexdigest()[:16]
    try:
        path = selected_source(path)
        key = f"{platform}:{account}:{path}"
        opencode_json = False
        # An explicit session file references message/part files beyond its own mtime.
        explicit_session = platform == "opencode" and path.is_file() and path.suffix == ".json"
        if path.is_dir() and platform == "codex":
            files = codex_rollout_files(path)
            independent_files = True
        elif path.is_dir() and platform == "opencode" and not (path / "opencode.db").exists():
            files = _opencode_sessions(path)
            opencode_json = independent_files = bool(files)
            if not files:
                files = [path]
        else:
            files = [path]
            independent_files = False
    except OSError as error:
        category = (
            "source_missing"
            if isinstance(error, FileNotFoundError)
            else "source_permission"
            if isinstance(error, PermissionError)
            else "source_read"
        )
        _save_failures(store, source_id, {"scope": "source", "categories": [category]})
        raise
    ids = []
    failures = []
    scanned = 0
    thread_metadata = codex_thread_metadata(path) if platform == "codex" else {}
    for file in files:
        relative = str(file.relative_to(path)) if independent_files else file.name
        try:
            fingerprint = _fingerprint(
                platform, path, file, relative, opencode_json or explicit_session
            )
        except (OSError, ValueError):
            failures.append(_file_failure(relative, "read"))
            continue
        file_key = f"{key}:{relative}" if independent_files else key
        checkpoint = store.connection.execute(
            "SELECT value FROM checkpoints WHERE key=?", (file_key,)
        ).fetchone()
        metadata_key = f"codex-source-conversations:{file_key}"
        known_ids = (
            store.connection.execute(
                "SELECT value FROM checkpoints WHERE key=?", (metadata_key,)
            ).fetchone()
            if platform == "codex"
            else None
        )
        if (
            checkpoint
            and checkpoint[0] == fingerprint
            and not consent
            and (platform != "codex" or known_ids)
        ):
            # Refresh indexed titles without parsing unchanged JSONL. Pre-metadata
            # checkpoints are read once to establish this file's conversation IDs.
            if known_ids:
                for external_id in json.loads(known_ids[0]):
                    cid = _refresh_codex_index(
                        store,
                        path,
                        account,
                        file_key,
                        external_id,
                        thread_metadata.get(external_id),
                    )
                    if cid is not None:
                        ids.append(cid)
            scanned += 1
            continue
        try:
            conversations = collect(
                platform,
                file if independent_files else path,
                account=account,
                _index=thread_metadata if platform == "codex" else None,
            )
            with store.import_transaction():
                current = store.connection.execute(
                    "SELECT value FROM checkpoints WHERE key=?", (file_key,)
                ).fetchone()
                if current and checkpoint != current and current[0] != fingerprint:
                    # Another importer finished a different version while this adapter read.
                    # Leave its newer checkpoint alone and retry on the next scan.
                    failures.append(_file_failure(relative, "source_changed"))
                    continue
                accepted = [
                    store.ingest(
                        c, consent=consent, reconcile_order=True, codex_snapshot=platform == "codex"
                    )
                    for c in conversations
                ]
                store.connection.execute(
                    "INSERT OR REPLACE INTO checkpoints VALUES(?,?)", (file_key, fingerprint)
                )
                if platform == "codex":
                    store.connection.execute(
                        "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
                        (metadata_key, json.dumps([c["external_id"] for c in conversations])),
                    )
                    store.connection.executemany(
                        "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
                        [
                            (
                                f"codex-index:{file_key}:{c['external_id']}",
                                json.dumps(thread_metadata[c["external_id"]], sort_keys=True),
                            )
                            for c in conversations
                            if c["external_id"] in thread_metadata
                        ],
                    )
            ids.extend(accepted)
            scanned += 1
        except subprocess.TimeoutExpired:
            # Timeout exceptions can contain command paths and partial transcript
            # output. Persist only a bounded category, never the exception text.
            failures.append(_file_failure(relative, "timeout"))
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            failures.append(_file_failure(relative, "parse"))
    _save_failures(
        store,
        source_id,
        {
            "failed_files": len(failures),
            "categories": sorted({failure["category"] for failure in failures}),
        }
        if failures
        else None,
    )
    ids = list(dict.fromkeys(ids))
    return {
        "imported_conversations": len(ids),
        "conversation_ids": ids,
        "unchanged": bool(scanned and not ids and not failures),
        "failed_files": len(failures),
        "file_failures": failures,
    }


def _file_failure(relative: str, category: str) -> dict:
    return {"file_id": hashlib.sha256(relative.encode()).hexdigest()[:16], "category": category}


def _save_failures(store: Store, source_id: str, summary: dict | None):
    # Serialize the read/modify/write so two collectors cannot erase one
    # another's source diagnostics, including when one successfully recovers.
    with store.import_transaction():
        prior = store.get_setting("import_failures", [])
        remaining = [failure for failure in prior if failure.get("source_id") != source_id]
        if summary:
            remaining.append({"source_id": source_id, **summary})
        store.connection.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('import_failures',?)",
            (json.dumps(remaining),),
        )
