import pytest

from ai_memory.privacy import find_sensitive


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
