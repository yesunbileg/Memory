"""Regressions from the reviewed local Codex changes."""

import json

import pytest

from ai_memory.collectors import collect, normalize_conversation


@pytest.mark.parametrize(
    "text",
    [
        '<codex_internal_context source="goal">hidden</codex_internal_context>\n'
        "PRESERVE_THIS_USER_TEXT\n"
        '<codex_internal_context source="other">visible XML</codex_internal_context>',
        '<codex_internal_context source="goal">hidden</codex_internal_context>\nVisible tail',
        '```xml\n<codex_internal_context source="goal">example</codex_internal_context>\n```',
        '<codex_internal_context source="other">visible XML</codex_internal_context>',
    ],
)
def test_codex_context_filter_preserves_visible_user_text(text):
    raw = {
        "externalId": "mixed-context",
        "messages": [
            {"id": "user", "role": "user", "content": text},
        ],
    }
    messages = normalize_conversation(raw, "codex", "synthetic")["messages"]
    assert [message["text"] for message in messages] == [text]


def test_codex_context_filter_uses_visible_parts_instead_of_content_fallback():
    raw = {
        "externalId": "multipart-context",
        "messages": [
            {
                "id": "user",
                "role": "user",
                "content": '<codex_internal_context source="goal">hidden</codex_internal_context>',
                "parts": [{"type": "text", "text": "Visible user text"}],
            },
        ],
    }
    messages = normalize_conversation(raw, "codex", "synthetic")["messages"]
    assert [message["text"] for message in messages] == ["Visible user text"]


def test_codex_mixed_context_survives_real_rollout_parser(tmp_path):
    text = (
        '<codex_internal_context source="goal">internal</codex_internal_context>\n'
        "PRESERVE_THIS_USER_TEXT\n"
        '<codex_internal_context source="other">visible XML</codex_internal_context>'
    )
    records = [
        {"type": "session_meta", "payload": {"id": "mixed-wrapper"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "real-user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    ]
    source = tmp_path / "rollout.jsonl"
    source.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    conversation = collect("codex", source)[0]
    assert [message["text"] for message in conversation["messages"]] == [text]


@pytest.mark.parametrize(
    "platform,role,expected",
    [
        ("codex", "user", []),
        ("codex", "assistant", ["wrapper"]),
        ("opencode", "user", ["wrapper"]),
    ],
)
def test_codex_context_filter_scopes_reserved_envelopes_to_codex_user_messages(
    platform, role, expected
):
    text = '<codex_internal_context source="user_goal">x < y && z > 0</codex_internal_context>'
    raw = {
        "externalId": "context-scope",
        "messages": [
            {
                "id": "message",
                "role": role,
                "parts": [
                    {"type": "text", "text": text[:40]},
                    {"type": "text", "text": text[40:]},
                ],
            },
        ],
    }
    messages = normalize_conversation(raw, platform, "synthetic")["messages"]
    assert [message["text"] for message in messages] == ([text] if expected else [])


def test_codex_internal_goal_context_envelopes_are_filtered_but_quoted_xml_is_kept(tmp_path):
    source = tmp_path / "rollout-context.jsonl"
    wrapped_goal = (
        '<codex_internal_context source="goal">synthetic hidden goal</codex_internal_context>'
    )
    wrapped_user_goal = '<codex_internal_context source="user_goal">synthetic hidden user goal</codex_internal_context>'
    quoted_xml = f"Please explain what {wrapped_goal} means."
    records = [{"type": "session_meta", "payload": {"id": "context-filter"}}]
    inputs = [
        ("user", wrapped_goal),
        ("user", wrapped_user_goal),
        ("user", quoted_xml),
        ("assistant", wrapped_goal),
        ("user", '<codex_internal_context data-source="goal">visible XML</codex_internal_context>'),
        (
            "user",
            "<codex_internal_context title=\"source='goal'\">visible XML</codex_internal_context>",
        ),
    ]
    for index, (role, text) in enumerate(inputs, 1):
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "id": f"{role}-{index}",
                    "content": [
                        {"type": "input_text" if role == "user" else "output_text", "text": text}
                    ],
                },
            }
        )
    source.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    conversation = collect("codex", source)[0]
    messages = conversation["messages"]

    assert [message["native_id"] for message in messages] == [
        "user-3",
        "assistant-4",
        "user-5",
        "user-6",
    ]
    assert messages[0]["text"] == quoted_xml
    assert messages[1]["text"] == wrapped_goal
    assert conversation["title"] == quoted_xml[:77] + "..."
