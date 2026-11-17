"""Exercise chronology from actual Codex records through to Markdown."""

import json

from ai_memory.importer import collect_into
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store


def test_overlapping_snapshot_backfill_preserves_order_in_archive_and_markdown(tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    first_time = "2026-01-01T00:00:00Z"
    meta = {"type": "session_meta", "payload": {"id": "backfill", "timestamp": first_time}}

    def turn(native_id, role, timestamp, text):
        return {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "message",
                "id": native_id,
                "role": role,
                "content": [
                    {"type": "output_text" if role == "assistant" else "input_text", "text": text}
                ],
            },
        }

    older = turn("user-one", "user", "2026-01-01T00:00:01Z", "Question found later.")
    newer = turn("assistant-two", "assistant", "2026-01-01T00:00:02Z", "Earlier imported answer.")

    def write(*records):
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records))

    with Store(tmp_path / "archive.db") as store:
        write(meta, newer)
        result = collect_into(store, "codex", rollout, consent=True)
        assert result["failed_files"] == 0
        (conversation_id,) = result["conversation_ids"]
        builder = SegmentBuilder(store)
        (first_segment,) = builder.build()
        segment_id = first_segment["id"]
        old_answer_id = store.get_conversation(conversation_id)["messages"][0]["id"]

        write(meta, older, newer)
        assert collect_into(store, "codex", rollout)["failed_files"] == 0
        builder.build()

        messages = store.get_conversation(conversation_id)["messages"]
        assert [(message["native_id"], message["sequence"]) for message in messages] == [
            ("user-one", 1),
            ("assistant-two", 2),
        ]
        assert messages[1]["id"] == old_answer_id
        markdown = builder.render(segment_id)
        assert markdown.index("Question found later.") < markdown.index("Earlier imported answer.")
