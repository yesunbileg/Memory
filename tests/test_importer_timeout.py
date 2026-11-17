"""A timed-out donor file must not starve healthy files or gain a checkpoint."""

import hashlib
import json
import subprocess

from ai_memory import collectors
from ai_memory.importer import collect_into
from ai_memory.store import Store


def test_parser_timeout_is_content_safe_isolated_and_retryable(tmp_path, monkeypatch):
    source = tmp_path / "selected"
    source.mkdir()
    for name in ("a-timeout", "b-healthy"):
        records = [
            {"type": "session_meta", "payload": {"id": name}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "u-1",
                    "content": [{"type": "input_text", "text": f"  exact {name}\n"}],
                },
            },
        ]
        (source / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8"
        )
    real_run = subprocess.run

    def timeout_first_file(args, **kwargs):
        request = json.loads(kwargs["input"])
        if request["params"]["path"].endswith("a-timeout.jsonl"):
            raise subprocess.TimeoutExpired(
                ["synthetic-private-path"],
                kwargs["timeout"],
                output="private transcript",
                stderr="synthetic private credential",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(collectors.subprocess, "run", timeout_first_file)
    with Store(tmp_path / "archive.sqlite") as store:
        result = collect_into(store, "codex", source)
        assert result["imported_conversations"] == 1
        assert result["failed_files"] == 1
        assert result["file_failures"] == [
            {
                "file_id": hashlib.sha256(b"a-timeout.jsonl").hexdigest()[:16],
                "category": "timeout",
            }
        ]
        failures = store.get_setting("import_failures")
        assert failures[0]["categories"] == ["timeout"]
        assert "private" not in json.dumps([result, failures])
        assert store.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
        assert store.status()["conversations"] == 1
        monkeypatch.setattr(collectors.subprocess, "run", real_run)
        retried = collect_into(store, "codex", source)
        assert retried["failed_files"] == 0
        assert retried["imported_conversations"] == 1
        assert store.status()["conversations"] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 2
        assert store.get_setting("import_failures") == []
