"""Incremental local import, per-file checkpoints, and isolated failures."""

import hashlib
import json
from pathlib import Path

from .store import Store


def collect_into(
    store: Store,
    platform: str,
    path: Path,
    account: str = "local",
    consent: bool = False,
) -> dict:
    from .collectors import codex_rollout_files, collect

    platform = platform.lower()
    path = Path(path).expanduser().resolve(strict=True)
    key = f"{platform}:{account}:{path}"
    opencode_json = False
    # An explicit session file references message/part files beyond its own mtime.
    explicit_session = platform.lower() == "opencode" and path.is_file() and path.suffix == ".json"
    if path.is_dir() and platform.lower() == "codex":
        files = codex_rollout_files(path, max_depth=None)
        independent_files = True
    elif path.is_dir() and platform.lower() == "opencode" and not (path / "opencode.db").exists():
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
    ids = []
    failures = []
    scanned = 0
    inventory = None
    tree_fingerprint = None
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
        if checkpoint and checkpoint[0] == fingerprint and not consent and not explicit_session:
            scanned += 1
            continue
        try:
            conversations = collect(platform, file if independent_files else path, account=account)
            for c in conversations:
                # Keep known native IDs at their archived positions and append new turns.
                existing = store.connection.execute(
                    "SELECT id FROM conversations WHERE platform=? AND account=? AND external_id=?",
                    (c["platform"], c["account"], c["external_id"]),
                ).fetchone()
                if existing:
                    previous = {
                        m["native_id"]: m["sequence"]
                        for m in store.get_conversation(existing[0])["messages"]
                    }
                    last = max(previous.values(), default=0)
                    for m in c["messages"]:
                        if m["native_id"] in previous:
                            m["sequence"] = previous[m["native_id"]]
                        else:
                            last += 1
                            m["sequence"] = last
                ids.append(store.ingest(c, consent=consent))
            with store.connection:
                store.connection.execute(
                    "INSERT OR REPLACE INTO checkpoints VALUES(?,?)", (file_key, fingerprint)
                )
            scanned += 1
        except (OSError, RuntimeError, ValueError, UnicodeError):
            failures.append(
                {"file_id": hashlib.sha256(relative.encode()).hexdigest()[:16], "category": "parse"}
            )
    source_id = hashlib.sha256(key.encode()).hexdigest()[:16]
    prior_failures = store.get_setting("import_failures", [])
    prior_failures = [
        failure for failure in prior_failures if failure.get("source_id") != source_id
    ]
    if failures:
        prior_failures.append(
            {
                "source_id": source_id,
                "failed_files": len(failures),
                "categories": sorted({failure["category"] for failure in failures}),
            }
        )
    store.set_setting("import_failures", prior_failures)
    return {
        "imported_conversations": len(ids),
        "conversation_ids": ids,
        "unchanged": bool(scanned and not ids and not failures),
        "failed_files": len(failures),
        "file_failures": failures,
    }
