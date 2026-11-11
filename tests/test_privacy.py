import pytest

from ai_memory.privacy import find_sensitive, redact_sensitive


@pytest.mark.parametrize("following", ["\n\n---\n", "\n+ next item", "\r\nif needed"])
def test_next_line_markdown_cannot_disguise_a_literal_credential(following):
    text = 'password="synthetic-secret"' + following
    assert find_sensitive(text)
    assert redact_sensitive(text) == 'password="****************"' + following


def test_incomplete_private_key_does_not_mask_unrelated_text_and_still_blocks_upload():
    text = (
        "-----BEGIN PRIVATE KEY-----\nsynthetic-key-material\nUnrelated explanation stays intact."
    )
    assert redact_sensitive(text) == text
    assert "private_key" in find_sensitive(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Authorization: Basic synthetic-base64-secret",
            "Authorization: Basic ***********************",
        ),
        (
            'authorization="Basic synthetic-base64-secret"',
            'authorization="Basic ***********************"',
        ),
        (
            "Authorization: Token synthetic-base64-secret",
            "Authorization: Token ***********************",
        ),
    ],
)
def test_basic_authorization_masks_payload_and_preserves_scheme(text, expected):
    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


@pytest.mark.parametrize(
    "text",
    [
        "password = get_password_from_env()",
        "api_key = settings.OPENAI_API_KEY",
        "access_token = response.json()['access_token']",
    ],
)
def test_source_expressions_are_not_literal_credentials(text):
    assert find_sensitive(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "password = 'SyntheticPass987654!'",
        'api_key = "synthetic_literal_0123456789"',
        "access_token = SyntheticToken987654321",
        "Authorization: Bearer SyntheticToken987654321",
        "api_key = sk-" + "synthetic123" * 3,
    ],
)
def test_literal_credentials_remain_blocked(text):
    assert find_sensitive(text)


def test_benign_expression_does_not_hide_later_literal_on_same_line():
    assert find_sensitive("api_key = settings.OPENAI_API_KEY; password = 'SyntheticPass987654!'")


def test_literal_credential_in_json_escaped_metadata_is_blocked():
    import json

    assert find_sensitive("title: " + json.dumps('password = "SyntheticPass987654!"'))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "password = 'SyntheticPass987654!' remains useful",
            "password = '********************' remains useful",
        ),
        (
            'api_key: "synthetic_literal_0123456789", next=keep_this',
            'api_key: "****************************", next=keep_this',
        ),
        (
            "Authorization: Bearer SyntheticToken987654321.",
            "Authorization: Bearer ***********************.",
        ),
    ],
)
def test_assignment_redaction_preserves_labels_quotes_and_surrounding_text(text, expected):
    assert redact_sensitive(text) == expected


def test_source_expressions_remain_intact_when_redacted():
    text = "password = get_password_from_env(); api_key = settings.OPENAI_API_KEY"
    assert redact_sensitive(text) == text


def test_recognized_api_key_cookie_and_jwt_values_are_masked_in_place():
    api_key = "sk-" + "a" * 20
    cookie_value = "cookie-secret-value"
    cookie = "__Secure-1PSID=" + cookie_value
    jwt = ".".join(("eyJ" + "a" * 12, "eyJ" + "b" * 12, "eyJ" + "c" * 12))
    text = f"key {api_key}; cookie {cookie}; token {jwt}! keep"
    expected = (
        f"key {'*' * len(api_key)}; cookie __Secure-1PSID={'*' * len(cookie_value)}; "
        f"token {'*' * len(jwt)}! keep"
    )

    assert redact_sensitive(text) == expected
    assert find_sensitive(redact_sensitive(text)) == []


def test_short_cookie_value_is_redacted_without_masking_cookie_name():
    text = "SID=x; keep"

    assert redact_sensitive(text) == "SID=*; keep"
    assert find_sensitive("SID=x")
    assert find_sensitive("SID=*") == []


def test_private_key_payload_is_masked_without_changing_pem_boundaries_or_newlines():
    text = (
        "before\n-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
        "AQEAAQ==\n"
        "-----END PRIVATE KEY-----\nafter"
    )
    expected = (
        "before\n-----BEGIN PRIVATE KEY-----\n"
        "********************************\n"
        "********\n"
        "-----END PRIVATE KEY-----\nafter"
    )

    assert redact_sensitive(text) == expected
    assert find_sensitive(text)
    assert find_sensitive(expected) == []


def test_short_nonempty_password_literals_are_redacted_and_then_clear_the_gate():
    text = "password='abc123'"

    assert redact_sensitive(text) == "password='******'"
    assert find_sensitive(text)
    assert find_sensitive(redact_sensitive(text)) == []


def test_unquoted_alphabetic_password_value_is_redacted():
    assert redact_sensitive("password=secretvalue") == "password=***********"


def test_redaction_is_idempotent_and_never_removes_original_length():
    text = 'api_key = "synthetic_literal_0123456789"'
    redacted = redact_sensitive(text)

    assert redacted == 'api_key = "****************************"'
    assert len(redacted) == len(text)
    assert redact_sensitive(redacted) == redacted


def test_escaped_json_assignment_redacts_only_the_literal_value():
    import json

    text = "title: " + json.dumps('password = "SyntheticPass987654!"')
    expected = 'title: "password = \\"********************\\""'

    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


def test_escaped_quotes_inside_password_are_part_of_the_masked_value():
    text = 'password = "first\\"second"'

    assert redact_sensitive(text) == 'password = "*************"'


def test_quoted_bearer_preserves_the_authorization_scheme():
    token = "synthetic-token-123456"
    text = f'authorization = "Bearer {token}"'

    assert redact_sensitive(text) == f'authorization = "Bearer {"*" * len(token)}"'
    assert find_sensitive(redact_sensitive(text)) == []


def test_cookie_pair_redaction_preserves_names_separators_and_quotes():
    text = 'cookie="SID=short; HSID=other-cookie-value"'
    expected = 'cookie="SID=*****; HSID=******************"'

    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


@pytest.mark.parametrize(
    "text",
    [
        'password = "prefix_" + get_password()',
        "password = prefix + suffix",
        'password = "value" if flag else "fallback"',
        'password = "value" == expected',
        'password = f"{get_password()}"',
        "password = ${SECRET}",
        'password = "${SECRET}"',
    ],
)
def test_dynamic_assignment_expressions_remain_entirely_unchanged(text):
    assert redact_sensitive(text) == text


def test_static_template_literal_credential_is_redacted():
    value = "synthetic-template-secret"
    text = f"password = `{value}`"

    assert redact_sensitive(text) == f"password = `{'*' * len(value)}`"


def test_multiline_quoted_credential_masks_payload_without_changing_line_breaks():
    text = 'password = """first\nsecond"""'

    assert redact_sensitive(text) == 'password = """*****\n******"""'
