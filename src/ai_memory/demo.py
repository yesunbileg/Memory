"""Repeatable synthetic collector demo; never impersonates a NotebookLM answer."""

import json
import resource
import time
from pathlib import Path

from .collectors import collect
from .segments import SegmentBuilder
from .store import Store


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def prepare_inputs(directory: Path) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    codex = directory / "codex" / "rollout-memory-demo.jsonl"
    codex.parent.mkdir(exist_ok=True)
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
    codex.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    opencode = directory / "opencode"
    _write_json(
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
        _write_json(
            opencode / f"storage/message/ses_memory/{mid}.json",
            {
                "id": mid,
                "sessionID": "ses_memory",
                "role": role,
                "time": {"created": ts},
                **({"modelID": "demo-model"} if role == "assistant" else {}),
            },
        )
        _write_json(
            opencode / f"storage/part/{mid}/prt_001.json",
            {
                "id": f"{mid}_part",
                "sessionID": "ses_memory",
                "messageID": mid,
                "type": "text",
                "text": text,
            },
        )
    return codex, opencode


def run_demo(output: Path) -> dict:
    output = Path(output).expanduser().resolve()
    marker = output / ".ai-memory-demo"
    if output.exists() and any(output.iterdir()) and not marker.exists():
        raise ValueError(
            "Choose an empty output directory; existing non-demo files will not be overwritten"
        )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.write_text("Synthetic AI Memory demo data only\n")
    start, cpu = time.perf_counter(), time.process_time()
    codex, opencode = prepare_inputs(output / "inputs")
    with Store(output / "conversations.sqlite") as store:
        ids = []
        for platform, path in (("codex", codex), ("opencode", opencode)):
            for c in collect(platform, path, account="synthetic-demo"):
                ids.append(store.ingest(c, consent=True))
        builder = SegmentBuilder(store)
        builder.build()
        segment_dir = output / "segments"
        segment_dir.mkdir(exist_ok=True, mode=0o700)
        for row in store.connection.execute("SELECT id FROM segments WHERE state!='retired'"):
            path = segment_dir / f"{row[0]}.md"
            path.write_text(builder.render(row[0]), encoding="utf-8", newline="")
            path.chmod(0o600)
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
