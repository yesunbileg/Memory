"""Read local conversations with audited hstry adapters (Node 24+).

The TypeScript adapters own source-format parsing. This bridge restricts their
output to visible text and translates the hstry contract into the archive's
small, lossless input contract. No personal paths are inspected implicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _vendor_root() -> Path:
    """Support the checkout and wheel data installed beside the package."""
    here = Path(__file__).resolve()
    candidates = [here.parent / "vendor" / "hstry", here.parents[2] / "vendor" / "hstry"]
    for candidate in candidates:
        if (candidate / "adapters").is_dir():
            return candidate
    raise RuntimeError("Vendored hstry adapters are missing; reinstall AI Memory")


def _timestamp(value: Any) -> str | None:
    # hstry's contract is Unix milliseconds. Unknown remains unknown.
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try:
            return (
                datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_conversation(raw: dict, platform: str, account: str) -> dict:
    """Normalize trusted adapter shape while excluding unmarked reasoning.

    Multipart text is concatenated in source order without inserted characters.
    Visible thinking requires an explicit ``visibility: ui`` marker. Canonical
    part IDs are adapter counters, so source message IDs (or record positions)
    determine identity instead.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
        raise ValueError("conversation must contain a messages list")
    external_id = _string(raw.get("externalId"))
    if external_id is None:
        raise ValueError("conversation externalId is required")
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    messages: list[dict] = []
    models: list[str] = []
    fallback_counts: dict[str, int] = {}
    for message in raw["messages"]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if (
            meta.get("visibility") == "hidden"
            or meta.get("source") == "response_item.agent_message"
        ):
            continue
        if meta.get("channel") in {"analysis", "justify", "confidence"}:
            continue
        model = _string(message.get("model"))
        native_id = (
            _string(message.get("clientId"))
            or _string(meta.get("id"))
            or _string(message.get("id"))
        )
        if not native_id:
            parts = message.get("parts")
            stable_parts = [
                {key: part.get(key) for key in ("type", "text", "visibility", "synthetic", "ignored")
                 if key in part}
                for part in parts if isinstance(part, dict)
            ] if isinstance(parts, list) else None
            signature = hashlib.sha256(json.dumps(
                [message["role"], message.get("createdAt"), message.get("model"),
                 message.get("content"), stable_parts],
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")).hexdigest()
            occurrence = fallback_counts.get(signature, 0)
            fallback_counts[signature] = occurrence + 1
            native_id = f"derived:{signature[:32]}:{occurrence}"

        groups: list[tuple[str, str, int]] = []
        parts = message.get("parts")
        if isinstance(parts, list) and parts:
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                    continue
                if (
                    part.get("visibility") == "hidden"
                    or part.get("synthetic") is True
                    or part.get("ignored") is True
                ):
                    continue
                kind = None
                if part.get("type") == "text":
                    kind = "progress" if meta.get("kind") == "progress" else "text"
                elif part.get("type") == "thinking" and part.get("visibility") == "ui":
                    kind = "thinking_summary"
                if kind is None or not part["text"]:
                    continue
                if groups and groups[-1][0] == kind:
                    previous_kind, previous_text, first_part = groups[-1]
                    groups[-1] = (previous_kind, previous_text + part["text"], first_part)
                else:
                    groups.append((kind, part["text"], part_index))
        elif isinstance(message.get("content"), str) and message["content"]:
            kind = meta.get("kind", "text")
            if kind in {"text", "progress"}:
                groups.append((kind, message["content"], 0))
            elif kind == "thinking_summary" and meta.get("visibility") == "ui":
                groups.append((kind, message["content"], 0))

        kind_counts: dict[str, int] = {}
        for kind, content, part_index in groups:
            ordinal = kind_counts.get(kind, 0)
            kind_counts[kind] = ordinal + 1
            if kind in {"text", "progress"} and ordinal == 0:
                group_id = native_id
            elif (
                kind == "thinking_summary"
                and meta.get("kind") == "thinking_summary"
                and ordinal == 0
            ):
                group_id = native_id
            else:
                group_id = f"{native_id}:{kind}:{ordinal}"
            item = {
                "native_id": group_id,
                "sequence": len(messages) + 1,
                "role": message["role"],
                "kind": kind,
                "text": content,
                "timestamp": _timestamp(message.get("createdAt")),
            }
            if model:
                item["model"] = model
                if model not in models:
                    models.append(model)
            messages.append(item)
    conversation_model = _string(raw.get("model"))
    if conversation_model and conversation_model not in models:
        models.append(conversation_model)
    return {
        "platform": platform.lower(),
        "account": account,
        "external_id": external_id,
        "title": _string(raw.get("title")),
        "project": _string(raw.get("workspace")),
        "models": models,
        "created_at": _timestamp(raw.get("createdAt")),
        "updated_at": _timestamp(raw.get("updatedAt")),
        "source_url": _string(metadata.get("url")),
        "messages": messages,
    }


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
        # Do not expose adapter stderr: source data and personal paths can occur.
        raise RuntimeError(f"hstry {platform} parser failed; verify the selected source format")
    return [normalize_conversation(raw, platform, account) for raw in parsed]
