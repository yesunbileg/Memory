"""One-shot native event capture. No discovery, network, or upload approval."""

import json
from pathlib import Path
from typing import BinaryIO

from .collectors import codex_thread_metadata, collect, selected_source
from .store import Store

# Native Stop includes the final reply. Only routing fields are needed here.
_MAX_EVENT = 16 * 1024 * 1024


def capture_event(
    store: Store, platform: str, account: str, source: Path | None, stream: BinaryIO
) -> None:
    raw = stream.read(_MAX_EVENT + 1)
    if len(raw) > _MAX_EVENT:
        raise ValueError("Event exceeds size limit")
    event = json.loads(raw)
    if not isinstance(event, dict) or not account:
        raise ValueError("Invalid hook input")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not 0 < len(session_id) <= 512:
        raise ValueError("Missing native session ID")
    if platform == "codex":
        if event.get("hook_event_name") != "Stop" or "transcript_path" not in event:
            raise ValueError("Expected a Codex Stop event")
        transcript = event["transcript_path"]
        if transcript is None:
            # A non-persisted session has no local source. Never fall back to cwd.
            return
        if not isinstance(transcript, str):
            raise ValueError("Expected a Codex rollout")
        source = Path(transcript)
        if not source.name.endswith((".jsonl", ".jsonl.zst")):
            raise ValueError("Expected a Codex rollout")
    elif platform != "opencode" or event.get("event") != "session.idle":
        raise ValueError("Expected an OpenCode idle event")
    if source is None or not source.is_absolute() or not selected_source(source).is_file():
        raise ValueError("Expected an absolute native session source file")
    local_codex = platform == "codex"
    conversations = collect(
        platform,
        source,
        account=account,
        _index=codex_thread_metadata(source, session_id) if local_codex else None,
        _session_id=None if local_codex else session_id,
    )
    # A valid but entirely hidden/subagent rollout can yield no visible conversation.
    if not conversations:
        return
    if len(conversations) != 1 or conversations[0]["external_id"] != session_id:
        raise ValueError("Source did not match the event session")
    store.ingest(conversations[0], reconcile_order=True, codex_snapshot=local_codex)
