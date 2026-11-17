"""Incremental local import, per-file checkpoints, and isolated failures."""

import hashlib
import json
import subprocess
from pathlib import Path

from .store import Store


def collect_into(
    store: Store,
    platform: str,
    path: Path,
    account: str = "local",
    consent: bool = False,
) -> dict:
    from .collectors import codex_rollout_files, codex_thread_metadata, collect

    platform = platform.lower()
    # Hash the selected source even when it is missing or inaccessible. Its
    # identity stays stable across failure/recovery, without retaining its path.
    path = Path(path).expanduser().absolute()
    source_id = hashlib.sha256(f"{platform}:{account}:{path}".encode()).hexdigest()[:16]
    try:
        path = path.resolve(strict=True)
        key = f"{platform}:{account}:{path}"
        opencode_json = False
        # An explicit session file references message/part files beyond its own mtime.
        explicit_session = (
            platform.lower() == "opencode" and path.is_file() and path.suffix == ".json"
        )
        if path.is_dir() and platform.lower() == "codex":
            files = codex_rollout_files(path, max_depth=None)
            independent_files = True
        elif (
            path.is_dir() and platform.lower() == "opencode" and not (path / "opencode.db").exists()
        ):
            current = sorted(f for f in path.glob("storage/session/*/ses_*.json") if f.is_file())
            current_ids = {f.name for f in current}
            legacy = sorted(
                f
                for f in path.glob("project/*/storage/session/info/ses_*.json")
                if f.is_file() and f.name not in current_ids
            )
            files = current + legacy
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
    inventory = None
    tree_fingerprint = None
    thread_metadata = codex_thread_metadata(path) if platform == "codex" else {}
    for file in files:
        relative = str(file.relative_to(path)) if independent_files else file.name
        try:
            if path.is_dir() and (opencode_json or not independent_files):
                if inventory is None:
                    inventory = []
                    for item in sorted(p for p in path.rglob("*") if p.is_file()):
                        stat = item.stat()
                        inventory.append(
                            (str(item.relative_to(path)), stat.st_size, stat.st_mtime_ns)
                        )
                if opencode_json:
                    # Message/part edits need not touch session JSON. Hash the tree
                    # once, then checkpoint successful sessions independently.
                    if tree_fingerprint is None:
                        tree_fingerprint = hashlib.sha256(
                            json.dumps(inventory).encode()
                        ).hexdigest()
                    fingerprint_input = [relative, tree_fingerprint]
                else:
                    fingerprint_input = inventory
            else:
                stat = file.stat()
                fingerprint_input = [relative, stat.st_size, stat.st_mtime_ns]
            fingerprint = hashlib.sha256(json.dumps(fingerprint_input).encode()).hexdigest()
        except OSError:
            failures.append(
                {"file_id": hashlib.sha256(relative.encode()).hexdigest()[:16], "category": "read"}
            )
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
            and not explicit_session
            and (platform != "codex" or known_ids)
        ):
            # Refresh indexed titles without parsing unchanged JSONL. Pre-metadata
            # checkpoints are read once to establish this file's conversation IDs.
            if known_ids:
                for external_id in json.loads(known_ids[0]):
                    title = thread_metadata.get(external_id, {}).get("title")
                    previous = store.connection.execute(
                        "SELECT title,updated_at FROM conversations WHERE platform=? AND account=? AND external_id=?",
                        (platform, account, external_id),
                    ).fetchone()
                    if previous and title and title != previous["title"]:
                        ids.append(
                            store.ingest(
                                {
                                    "platform": platform,
                                    "account": account,
                                    "external_id": external_id,
                                    "title": title,
                                    "updated_at": previous["updated_at"],
                                    "messages": [],
                                }
                            )
                        )
            scanned += 1
            continue
        try:
            conversations = collect(platform, file if independent_files else path, account=account)
            for c in conversations:
                ids.append(store.ingest(c, consent=consent, reconcile_order=True))
            with store.connection:
                store.connection.execute(
                    "INSERT OR REPLACE INTO checkpoints VALUES(?,?)", (file_key, fingerprint)
                )
                if platform == "codex":
                    store.connection.execute(
                        "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
                        (metadata_key, json.dumps([c["external_id"] for c in conversations])),
                    )
            scanned += 1
        except subprocess.TimeoutExpired:
            # Timeout exceptions can contain command paths and partial transcript
            # output. Persist only a bounded category, never the exception text.
            failures.append(
                {
                    "file_id": hashlib.sha256(relative.encode()).hexdigest()[:16],
                    "category": "timeout",
                }
            )
        except (OSError, RuntimeError, ValueError, UnicodeError):
            failures.append(
                {"file_id": hashlib.sha256(relative.encode()).hexdigest()[:16], "category": "parse"}
            )
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
    return {
        "imported_conversations": len(ids),
        "conversation_ids": ids,
        "unchanged": bool(scanned and not ids and not failures),
        "failed_files": len(failures),
        "file_failures": failures,
    }


def _save_failures(store: Store, source_id: str, summary: dict | None):
    # Serialize the read/modify/write so two collectors cannot erase one
    # another's source diagnostics, including when one successfully recovers.
    with store.connection:
        store.connection.execute("BEGIN IMMEDIATE")
        prior = store.get_setting("import_failures", [])
        remaining = [failure for failure in prior if failure.get("source_id") != source_id]
        if summary:
            remaining.append({"source_id": source_id, **summary})
        store.connection.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('import_failures',?)",
            (json.dumps(remaining),),
        )
