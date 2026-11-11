"""Read local conversations with audited hstry adapters (Node 24+).

The TypeScript adapters own source-format parsing. This bridge restricts their
output to visible text and translates the hstry contract into the archive's
small, lossless input contract. No personal paths are inspected implicitly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .normalization import normalize_conversation as normalize_conversation


def _vendor_root() -> Path:
    """Support the checkout and wheel data installed beside the package."""
    here = Path(__file__).resolve()
    candidates = [here.parent / "vendor" / "hstry", here.parents[2] / "vendor" / "hstry"]
    for candidate in candidates:
        if (candidate / "adapters").is_dir():
            return candidate
    raise RuntimeError("Vendored hstry adapters are missing; reinstall AI Memory")


def codex_rollout_files(path: Path, *, max_depth: int | None = 4) -> list[Path]:
    """Find rollouts without following symlinks, preferring plain mirrors.

    Direct batch retries match the adapter's depth bound. Incremental archive
    imports pass no bound to retain their recursive explicit-source behavior.
    """
    roots = [path]
    roots.extend(
        candidate
        for name in ("sessions", "archived_sessions")
        if (candidate := path / name).is_dir() and not candidate.is_symlink()
    )
    files: set[Path] = set()
    for root in roots:
        for directory, directories, filenames in os.walk(root, followlinks=False):
            parent = Path(directory)
            depth = len(parent.relative_to(root).parts)
            directories[:] = [name for name in directories if not (parent / name).is_symlink()]
            if max_depth is not None and depth >= max_depth:
                directories.clear()
            for name in filenames:
                candidate = parent / name
                if (
                    not candidate.is_symlink()
                    and candidate.is_file()
                    and name.endswith((".jsonl", ".jsonl.zst"))
                ):
                    files.add(candidate)
    # Match hstry's preference when a rollout exists in both encodings.
    files = {f for f in files if not (f.name.endswith(".jsonl.zst") and f.with_suffix("").exists())}
    return sorted(files)


def _merge_codex_snapshots(conversations: list[dict]) -> list[dict]:
    """Merge snapshots per native conversation, preferring newest message versions."""
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for conversation in conversations:
        key = (
            conversation["platform"],
            conversation["account"],
            conversation["external_id"],
        )
        grouped.setdefault(key, []).append(conversation)

    merged: list[dict] = []
    for snapshots in grouped.values():
        snapshots.sort(key=lambda item: item.get("_snapshot_rank", ("", 0, "")))
        newest = snapshots[-1]
        result = dict(newest)
        for field in ("title", "project", "created_at", "updated_at", "source_url"):
            if result.get(field) is None:
                result[field] = next(
                    (
                        item.get(field)
                        for item in reversed(snapshots[:-1])
                        if item.get(field) is not None
                    ),
                    None,
                )
        result["models"] = list(
            dict.fromkeys(model for item in snapshots for model in item.get("models", []))
        )

        message_versions: dict[str, dict] = {}
        for snapshot in snapshots:
            for message in snapshot["messages"]:
                message_versions[message["native_id"]] = dict(message)

        order = list(dict.fromkeys(m["native_id"] for m in newest["messages"]))
        # Add messages unique to older snapshots at their nearest known position.
        for snapshot in reversed(snapshots[:-1]):
            ids = [m["native_id"] for m in snapshot["messages"]]
            order = _insert_missing_ids(order, ids)

        result["messages"] = []
        for sequence, native_id in enumerate(order, 1):
            message = message_versions[native_id]
            message["sequence"] = sequence
            result["messages"].append(message)
        result.pop("_snapshot_rank", None)
        merged.append(result)

    merged.sort(
        key=lambda item: (item.get("created_at") or "", item["external_id"]),
        reverse=True,
    )
    return merged


def _insert_missing_ids(order: list[str], older_ids: list[str]) -> list[str]:
    """Place older-only runs before their next anchor, or after the last one.

    Assemble a new list once per snapshot instead of repeatedly scanning and
    inserting into a growing transcript.
    """
    present = set(order)
    before: dict[str, list[str]] = {}
    missing: list[str] = []
    previous = None
    for native_id in dict.fromkeys(older_ids):
        if native_id in present:
            before[native_id] = missing
            missing = []
            previous = native_id
        else:
            missing.append(native_id)
    result = []
    for native_id in order:
        result.extend(before.get(native_id, []))
        result.append(native_id)
        if native_id == previous:
            result.extend(missing)
    if previous is None:
        result.extend(missing)
    return result


def _collect_codex_files(path: Path, account: str) -> list[dict]:
    conversations: list[dict] = []
    for rollout in codex_rollout_files(path):
        snapshot = collect("codex", rollout, account)
        rank = _snapshot_rank(snapshot[0], rollout) if snapshot else None
        for conversation in snapshot:
            if rank is not None:
                conversation["_snapshot_rank"] = rank
            conversations.append(conversation)
    return _merge_codex_snapshots(conversations)


def _snapshot_rank(conversation: dict, source_file: Path | None) -> tuple[str, int, str]:
    timestamp = conversation.get("updated_at") or conversation.get("created_at") or ""
    mtime_ns = 0
    filename = ""
    if source_file is not None:
        try:
            stat = source_file.stat()
            mtime_ns = stat.st_mtime_ns
            filename = source_file.name
        except OSError:
            pass
    return timestamp, mtime_ns, filename


def collect(platform: str, path: Path, account: str = "local") -> list[dict]:
    """Parse an explicitly selected Codex file/tree or OpenCode storage tree."""
    platform = platform.lower()
    if platform not in {"codex", "opencode"}:
        raise ValueError(f"Unsupported collector platform: {platform}")
    path = Path(path).expanduser().resolve(strict=True)
    if not isinstance(account, str) or not account:
        raise ValueError("account must be a nonempty string")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Collectors require Node.js 24 or newer")
    adapter = _vendor_root() / "adapters" / platform / "adapter.ts"
    env = os.environ.copy()
    env["HSTRY_REQUEST_STDIN"] = "1"
    result = subprocess.run(
        [node, "--disable-warning=ExperimentalWarning", str(adapter)],
        input=json.dumps(
            {"method": "parse", "params": {"path": str(path), "opts": {"includeTools": False}}}
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeError(
            "hstry adapter returned invalid output; Node.js 24+ is required"
        ) from error
    if result.returncode or not isinstance(parsed, list):
        if (
            platform == "codex"
            and path.is_dir()
            and isinstance(parsed, dict)
            and parsed.get("error") == "RangeError: Invalid string length"
        ):
            return _collect_codex_files(path, account)
        # Do not expose adapter stderr: source data and personal paths can occur.
        raise RuntimeError(f"hstry {platform} parser failed; verify the selected source format")
    conversations = []
    for raw in parsed:
        conversation = normalize_conversation(raw, platform, account)
        if platform == "codex":
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            source_file = metadata.get("file")
            conversation["_snapshot_rank"] = _snapshot_rank(
                conversation, Path(source_file) if isinstance(source_file, str) else None
            )
        conversations.append(conversation)
    return _merge_codex_snapshots(conversations) if platform == "codex" else conversations
