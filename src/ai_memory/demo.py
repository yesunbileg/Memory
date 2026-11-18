"""Repeatable synthetic collector demo; never impersonates a NotebookLM answer."""

import json
import resource
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .collectors import collect
from .diagnostics import PublicInputError
from .private_io import write_private_text
from .segments import SegmentBuilder
from .store import Store

_CLEAN_OUTPUT = "Choose an empty output directory; existing non-demo files will not be overwritten"


DEMO_ORIGINS = frozenset(
    {
        ("codex", "synthetic-demo", "memory-demo-codex"),
        ("opencode", "synthetic-demo", "ses_memory"),
    }
)


def _require_retained_demo(db: Path) -> None:
    """Inspect before creating any fixtures, changing permissions or migrating a DB."""
    if db.is_symlink():
        raise PublicInputError(_CLEAN_OUTPUT)
    if not db.exists():
        return
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
            origins = set(
                connection.execute("SELECT platform,account,external_id FROM conversations")
            )
            bound = connection.execute(
                "SELECT 1 FROM settings WHERE key IN ('notebook_id','storage_path') LIMIT 1"
            ).fetchone()
    except sqlite3.DatabaseError:
        raise PublicInputError(_CLEAN_OUTPUT) from None
    if origins != DEMO_ORIGINS or bound:
        raise PublicInputError(_CLEAN_OUTPUT)


def _fixture_text(path: Path, text: str) -> None:
    if path.resolve() != path.absolute() or (path.exists() and not path.is_file()):
        raise PublicInputError(_CLEAN_OUTPUT)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        write_private_text(path, text, overwrite=False)
    except FileExistsError:
        expected = text.encode("utf-8")
        if path.stat().st_size != len(expected) or path.read_bytes() != expected:
            raise PublicInputError(_CLEAN_OUTPUT) from None


def prepare_inputs(directory: Path) -> tuple[Path, Path]:
    files = set()

    def write_json(path, value):
        _fixture_text(path, json.dumps(value, ensure_ascii=False))
        files.add(path)

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    codex = directory / "codex" / "rollout-memory-demo.jsonl"
    records = [
        {
            "timestamp": "2026-11-10T08:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "memory-demo-codex",
                "timestamp": "2026-11-10T08:00:00Z",
                "cwd": "/synthetic/Memory",
            },
        },
        {
            "timestamp": "2026-11-10T08:00:00Z",
            "type": "turn_context",
            "payload": {"model": "demo-model"},
        },
        {
            "timestamp": "2026-11-10T08:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "demo-user",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "  Монгол demo: Яриаг эхээр нь хадгал.\nUse SQLite as the archive and NotebookLM as the only memory engine.\n",
                    }
                ],
            },
        },
        {
            "timestamp": "2026-11-10T08:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "demo-progress",
                "role": "assistant",
                "phase": "commentary",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Checking the lossless archive and Markdown segments.\n",
                    }
                ],
            },
        },
        {
            "timestamp": "2026-11-10T08:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "DO NOT ARCHIVE: hidden internal reasoning"}
                ],
            },
        },
        {
            "timestamp": "2026-11-10T08:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "tool-1",
                "output": "DO NOT ARCHIVE: terminal output",
            },
        },
        {
            "timestamp": "2026-11-10T08:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "demo-final",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Decision: keep original text in SQLite. No local LLM, embeddings, or summaries.\n\n```python\nassert original == archived\n```\n  ",
                    }
                ],
            },
        },
    ]
    _fixture_text(codex, "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")
    files.add(codex)
    opencode = directory / "opencode"
    write_json(
        opencode / "storage/session/project-demo/ses_memory.json",
        {
            "id": "ses_memory",
            "title": "Synthetic OpenCode memory decision",
            "directory": "/synthetic/Memory",
            "projectID": "project-demo",
            "time": {"created": 1794297720000, "updated": 1794297780000},
        },
    )
    for mid, role, ts, text in [
        ("msg_001", "user", 1794297720000, "NotebookLM source лимитийг яаж ашиглах вэ?\n"),
        (
            "msg_002",
            "assistant",
            1794297780000,
            "Use one notebook: 49 Markdown segments and one spare slot. Verify a replacement is ready before deleting the old source.",
        ),
    ]:
        write_json(
            opencode / f"storage/message/ses_memory/{mid}.json",
            {
                "id": mid,
                "sessionID": "ses_memory",
                "role": role,
                "time": {"created": ts},
                **({"modelID": "demo-model"} if role == "assistant" else {}),
            },
        )
        write_json(
            opencode / f"storage/part/{mid}/prt_001.json",
            {
                "id": "prt_001",
                "sessionID": "ses_memory",
                "messageID": mid,
                "type": "text",
                "text": text,
            },
        )
    if any(p.is_symlink() or (not p.is_dir() and p not in files) for p in directory.rglob("*")):
        raise PublicInputError(_CLEAN_OUTPUT)
    return codex, opencode


def run_demo(output: Path) -> dict:
    raw = Path(output).expanduser().absolute()
    output = raw.parent.resolve() / raw.name
    if output.is_symlink():
        raise PublicInputError(_CLEAN_OUTPUT)
    marker = output / ".ai-memory-demo"
    if output.exists() and any(output.iterdir()) and not marker.exists():
        raise PublicInputError(_CLEAN_OUTPUT)
    db = output / "conversations.sqlite"
    _require_retained_demo(db)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    _fixture_text(marker, "Synthetic AI Memory demo data only\n")
    start, cpu = time.perf_counter(), time.process_time()
    codex, opencode = prepare_inputs(output / "inputs")
    fixtures = [
        c
        for platform, path in (("codex", codex), ("opencode", opencode))
        for c in collect(platform, path, account="synthetic-demo")
    ]
    expected = {(c["platform"], c["account"], c["external_id"]): c for c in fixtures}
    with Store(db, existing=db.exists()) as store:
        for row in store.connection.execute(
            "SELECT id,platform,account,external_id FROM conversations"
        ):
            fixture = expected.get(tuple(row)[1:])
            current = store.get_conversation(row["id"])
            if (
                not fixture
                or not current["consent"]
                or any(current.get(k) != v for k, v in fixture.items() if k != "messages")
                or len(current["messages"]) != len(fixture["messages"])
                or any(
                    any(saved.get(k) != v for k, v in message.items())
                    for saved, message in zip(current["messages"], fixture["messages"], strict=True)
                )
            ):
                raise PublicInputError(_CLEAN_OUTPUT)
        ids = [store.ingest(c, consent=True) for c in fixtures]
        builder = SegmentBuilder(store)
        builder.build()
        segment_dir = output / "segments"
        segment_dir.mkdir(exist_ok=True, mode=0o700)
        for row in store.connection.execute("SELECT id FROM segments WHERE state!='retired'"):
            path = segment_dir / f"{row[0]}.md"
            _fixture_text(path, builder.render(row[0]))
        status = store.status()
    stats = {
        "wall_seconds": round(time.perf_counter() - start, 4),
        "parent_cpu_seconds": round(time.process_time() - cpu, 4),
        "child_cpu_seconds": round(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
            + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime,
            4,
        ),
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "max_child_rss_mib": round(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 2
        ),
        "archive_bytes": (output / "conversations.sqlite").stat().st_size,
    }
    return {
        "demo": "synthetic local pipeline",
        "conversation_ids": ids,
        "status": status,
        "stats": stats,
        "live_notebooklm_test": "not_run",
        "output": str(output),
    }
