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


def _native_message_id(message: dict, meta: dict) -> str | None:
    return _string(message.get("clientId")) or _string(meta.get("id")) or _string(message.get("id"))


def _message_id(message: dict, fallback_counts: dict[str, int], *, consume: bool = True) -> str:
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    native_id = _native_message_id(message, meta)
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
        if consume:
            fallback_counts[signature] = occurrence + 1
        native_id = f"derived:{signature[:32]}:{occurrence}"
    return native_id


def _codex_record_alias(message: dict) -> str | None:
    meta = message.get("metadata", {})
    record = meta.get("identityRecord", meta.get("record"))
    if type(record) is not int or record < 1 or not meta.get("source"):
        return None
    # Scope to this exact source record, not merely a line number which can be
    # reused by a partial snapshot with a different layout.
    signature = hashlib.sha256(
        json.dumps(
            [
                record,
                meta["source"],
                message.get("createdAt"),
                message.get("role"),
                message.get("content"),
                message.get("model"),
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"codex-record:{signature}"


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
    platform = platform.lower()
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
        mirror = meta.get("mirror") if platform == "codex" else None
        mirror_id = (
            _message_id(mirror, fallback_counts, consume=False)
            if isinstance(mirror, dict)
            else None
        )
        native_id = _message_id(message, fallback_counts)
        aliases: list[str] = []
        if platform == "codex":
            has_native = _native_message_id(message, meta)
            record_alias = _codex_record_alias(message) if not has_native or mirror_id else None
            if record_alias:
                aliases.append(record_alias)
            if isinstance(mirror, dict):
                aliases.append(mirror_id)
                # Reserve the mirrored occurrence even when the canonical item
                # has a native ID. Otherwise a later equal event with no time
                # would reuse the first event's legacy fallback identity.
                if mirror_id != native_id:
                    _message_id(mirror, fallback_counts)
                mirror_alias = _codex_record_alias(mirror)
                if mirror_alias:
                    aliases.append(mirror_alias)

        groups: list[tuple[str, str, str | None]] = []
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
                    previous_kind, previous_text, previous_update = groups[-1]
                    updates = [t for t in (previous_update, _timestamp(part.get("updatedAt"))) if t]
                    groups[-1] = (
                        previous_kind,
                        previous_text + part["text"],
                        max(updates, default=None),
                    )
                else:
                    groups.append((kind, part["text"], _timestamp(part.get("updatedAt"))))
        elif isinstance(message.get("content"), str) and message["content"]:
            kind = meta.get("kind", "text")
            if kind in {"text", "progress"} or (
                kind == "thinking_summary" and meta.get("visibility") == "ui"
            ):
                groups.append((kind, message["content"], None))

        if (
            platform == "codex"
            and message["role"] == "user"
            and len(groups) == 1
            and groups[0][0] == "text"
            and _is_codex_internal_context(groups[0][1])
        ):
            continue

        kind_counts: dict[str, int] = {}
        for kind, content, part_update in groups:
            ordinal = kind_counts.get(kind, 0)
            kind_counts[kind] = ordinal + 1
            if ordinal == 0 and (kind in {"text", "progress"} or kind == meta.get("kind")):
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
            if aliases:
                suffix = group_id[len(native_id) :]
                if mirror_id and mirror.get("metadata", {}).get("record", 0) < meta.get(
                    "identityRecord", 0
                ):
                    # A fresh full parse and an earlier event-only import use
                    # the same archival identity when the event came first.
                    item["first_record_id"] = mirror_id + suffix
                item["native_aliases"] = list(
                    dict.fromkeys(alias + suffix for alias in aliases if alias != native_id)
                )
            updates = [t for t in (_timestamp(message.get("updatedAt")), part_update) if t]
            if updates:
                item["updated_at"] = max(updates)
            if model:
                item["model"] = model
                if model not in models:
                    models.append(model)
            messages.append(item)
    conversation_model = _string(raw.get("model"))
    if conversation_model and conversation_model not in models:
        models.append(conversation_model)
    title = _string(raw.get("title"))
    opening = _CODEX_CONTEXT_OPENING.match(title or "") if platform == "codex" else None
    if opening and _is_codex_context_tag(opening["attributes"]):
        first_user = next(
            (m["text"] for m in messages if m["role"] == "user" and m["text"].strip()), None
        )
        title = " ".join(first_user.split()) if first_user else None
        if title and len(title) > 80:
            title = title[:77] + "..."
    return {
        "platform": platform,
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
