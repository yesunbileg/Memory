"""Normalize hstry output into exact visible text and stable archive identities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any
from xml.etree.ElementTree import ParseError, fromstring

_CODEX_CONTEXT_OPENING = re.compile(r"^\s*<codex_internal_context\b(?P<attributes>[^>]*)>")
_CODEX_CONTEXT_TAG = re.compile(r"</?codex_internal_context\b")


def _timestamp(value: Any) -> str | None:
    # hstry's contract is Unix milliseconds. Unknown remains unknown.
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try:
            return (
                datetime.fromtimestamp(value / 1000, tz=UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_codex_context_tag(attributes: str) -> bool:
    try:
        tag = fromstring(f"<codex_internal_context{attributes}/>")
    except ParseError:
        return False
    return tag.tag == "codex_internal_context" and tag.get("source") in {"goal", "user_goal"}


def _is_codex_internal_context(text: str) -> bool:
    """Recognize one complete reserved wrapper without swallowing user text.

    The body may contain ordinary Markdown and unescaped XML characters. Parse
    only the opening attributes; keep ambiguous, nested or adjacent wrappers.
    """
    opening = _CODEX_CONTEXT_OPENING.match(text)
    closing = "</codex_internal_context>"
    if not opening or not text.rstrip().endswith(closing):
        return False
    body = text.rstrip()[opening.end() : -len(closing)]
    return not _CODEX_CONTEXT_TAG.search(body) and _is_codex_context_tag(opening["attributes"])


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
            stable_parts = (
                [
                    {
                        key: part.get(key)
                        for key in ("type", "text", "visibility", "synthetic", "ignored")
                        if key in part
                    }
                    for part in parts
                    if isinstance(part, dict)
                ]
                if isinstance(parts, list)
                else None
            )
            signature = hashlib.sha256(
                json.dumps(
                    [
                        message["role"],
                        message.get("createdAt"),
                        message.get("model"),
                        message.get("content"),
                        stable_parts,
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            occurrence = fallback_counts.get(signature, 0)
            fallback_counts[signature] = occurrence + 1
            native_id = f"derived:{signature[:32]}:{occurrence}"

        groups: list[tuple[str, str]] = []
        parts = message.get("parts")
        if isinstance(parts, list) and parts:
            for part in parts:
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
                    previous_kind, previous_text = groups[-1]
                    groups[-1] = (previous_kind, previous_text + part["text"])
                else:
                    groups.append((kind, part["text"]))
        elif isinstance(message.get("content"), str) and message["content"]:
            kind = meta.get("kind", "text")
            if kind in {"text", "progress"}:
                groups.append((kind, message["content"]))
            elif kind == "thinking_summary" and meta.get("visibility") == "ui":
                groups.append((kind, message["content"]))

        if (
            platform.lower() == "codex"
            and message["role"] == "user"
            and len(groups) == 1
            and groups[0][0] == "text"
            and _is_codex_internal_context(groups[0][1])
        ):
            continue

        kind_counts: dict[str, int] = {}
        for kind, content in groups:
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
    title = _string(raw.get("title"))
    opening = _CODEX_CONTEXT_OPENING.match(title or "") if platform.lower() == "codex" else None
    if opening and _is_codex_context_tag(opening["attributes"]):
        first_user = next(
            (m["text"] for m in messages if m["role"] == "user" and m["text"].strip()), None
        )
        title = " ".join(first_user.split()) if first_user else None
        if title and len(title) > 80:
            title = title[:77] + "..."
    return {
        "platform": platform.lower(),
        "account": account,
        "external_id": external_id,
        "title": title,
        "project": _string(raw.get("workspace")),
        "models": models,
        "created_at": _timestamp(raw.get("createdAt")),
        "updated_at": _timestamp(raw.get("updatedAt")),
        "source_url": _string(metadata.get("url")),
        "messages": messages,
    }
