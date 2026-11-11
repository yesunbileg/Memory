"""Regressions from the reviewed local Codex changes."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_memory.collectors import collect


def test_rollout_discovery_does_not_follow_source_root_symlinks(tmp_path):
    from ai_memory.collectors import codex_rollout_files

    source = tmp_path / "codex"
    source.mkdir()
    outside = tmp_path / "unselected"
    outside.mkdir()
    (outside / "rollout.jsonl").write_text("", encoding="utf-8")
    (source / "sessions").symlink_to(outside, target_is_directory=True)
    (source / "archived_sessions").symlink_to(outside, target_is_directory=True)
    assert codex_rollout_files(source) == []


def test_incremental_import_keeps_deeply_nested_explicit_sources(tmp_path):
    from ai_memory.importer import collect_into
    from ai_memory.store import Store

    source = tmp_path / "selected"
    nested = source / "a/b/c/d/e"
    nested.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parent / "fixtures/codex-visible.jsonl", nested / "rollout.jsonl"
    )
    with Store(tmp_path / "archive.sqlite") as store:
        result = collect_into(store, "codex", source)
        assert result["failed_files"] == 0
        assert store.status()["conversations"] == 1
        assert store.status()["messages"] == 5


def test_codex_directory_retries_individual_rollouts_after_batch_serialization_overflow(
    tmp_path, monkeypatch
):
    from ai_memory import collectors

    source = tmp_path / "sessions"
    source.mkdir()

    def write_rollout(filename, session_id, timestamp, text):
        records = [
            {"type": "session_meta", "payload": {"id": session_id, "timestamp": timestamp}},
            {
                "type": "response_item",
                "timestamp": timestamp,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": f"user-{session_id}",
                    "content": [{"type": "input_text", "text": text}],
                },
            },
        ]
        (source / filename).write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )

    write_rollout("rollout-a.jsonl", "session-a", "2025-01-01T00:00:00Z", "older")
    write_rollout("rollout-b.jsonl", "session-b", "2025-01-02T00:00:00Z", "newer")

    real_run = subprocess.run
    calls = 0

    def fail_first_batch(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                args, 1, '{"error":"RangeError: Invalid string length"}\n', ""
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(collectors.subprocess, "run", fail_first_batch)
    conversations = collect("codex", source)

    assert calls == 3
    assert [conversation["external_id"] for conversation in conversations] == [
        "session-b",
        "session-a",
    ]
    assert [conversation["messages"][0]["text"] for conversation in conversations] == [
        "newer",
        "older",
    ]


@pytest.mark.parametrize(
    "older,expected",
    [
        (
            ["prefix", "a", "middle", "b", "suffix"],
            ["prefix", "a", "middle", "b", "suffix", "tail"],
        ),
        (["first", "second"], ["a", "b", "tail", "first", "second"]),
        (["a", "x", "y", "b"], ["a", "x", "y", "b", "tail"]),
    ],
)
def test_codex_partial_snapshots_keep_missing_turns_near_known_anchors(older, expected):
    from ai_memory.collectors import _merge_codex_snapshots

    def snapshot(ids, timestamp):
        return {
            "platform": "codex",
            "account": "synthetic",
            "external_id": "partial",
            "messages": [{"native_id": native_id, "text": native_id} for native_id in ids],
            "_snapshot_rank": (timestamp, 0, ""),
        }

    merged = _merge_codex_snapshots(
        [
            snapshot(older, "2025-01-01"),
            snapshot(["a", "b", "tail"], "2025-01-02"),
        ]
    )[0]
    assert [message["native_id"] for message in merged["messages"]] == expected
    assert [message["sequence"] for message in merged["messages"]] == list(
        range(1, len(expected) + 1)
    )


def test_codex_directory_merges_snapshots_without_churning_stable_archive_state(tmp_path):
    from ai_memory.store import Store

    source = tmp_path / "sessions"
    source.mkdir()

    def message(role, native_id, text, timestamp):
        kind = "input_text" if role == "user" else "output_text"
        return {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "message",
                "role": role,
                "id": native_id,
                "content": [{"type": kind, "text": text}],
            },
        }

    created = "2025-01-01T00:00:00Z"
    older = [
        {"type": "session_meta", "payload": {"id": "snapshot-session", "timestamp": created}},
        message("user", "user-1", "question", "2025-01-01T00:00:01Z"),
        message("assistant", "answer-1", "earlier answer", "2025-01-01T00:00:02Z"),
        message("user", "historical-user", "older unique turn", "2025-01-01T00:00:03Z"),
    ]
    newer = [
        {"type": "session_meta", "payload": {"id": "snapshot-session", "timestamp": created}},
        message("user", "user-1", "question", "2025-01-03T00:00:01Z"),
        message("assistant", "answer-1", "final answer", "2025-01-03T00:00:02Z"),
        message("assistant", "tail-1", "new tail", "2025-01-03T00:00:03Z"),
    ]
    (source / "rollout-older.jsonl").write_text(
        "\n".join(json.dumps(item) for item in older) + "\n", encoding="utf-8"
    )
    (source / "rollout-newer.jsonl").write_text(
        "\n".join(json.dumps(item) for item in newer) + "\n", encoding="utf-8"
    )

    conversations = collect("codex", source, account="synthetic")
    assert len(conversations) == 1
    conversation = conversations[0]
    assert [m["native_id"] for m in conversation["messages"]] == [
        "user-1",
        "answer-1",
        "historical-user",
        "tail-1",
    ]
    assert [m["text"] for m in conversation["messages"]] == [
        "question",
        "final answer",
        "older unique turn",
        "new tail",
    ]
    assert [m["sequence"] for m in conversation["messages"]] == [1, 2, 3, 4]

    with Store(tmp_path / "archive.sqlite") as store:
        conversation_id = store.ingest(conversation)
        first = store.get_conversation(conversation_id)
        store.ingest(conversation)
        second = store.get_conversation(conversation_id)
        assert second["revision"] == first["revision"]
        assert second["messages"] == first["messages"]
