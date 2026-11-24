import pytest

from ai_memory.privacy import find_sensitive, redact_sensitive


@pytest.mark.parametrize("padding", ["  ", " \t ", "\u2003\u2003"])
def test_json_escaped_credential_at_end_preserves_trailing_whitespace(padding):
    text = r"password = \"synthetic-value\"" + padding
    expected = r"password = \"***************\"" + padding
    assert find_sensitive(text) == ["credential_assignment"]
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


@pytest.mark.parametrize("following", ["\n\n---\n", "\n+ next item", "\r\nif needed"])
def test_next_line_markdown_cannot_disguise_a_literal_credential(following):
    text = 'password="synthetic-secret"' + following
    assert find_sensitive(text)
    assert redact_sensitive(text) == 'password="****************"' + following


def test_incomplete_private_key_masks_the_entire_remaining_message():
    text = (
        "-----BEGIN PRIVATE KEY-----\nsynthetic-key-material\nUnrelated explanation stays intact."
    )
    expected = "".join("\n" if char == "\n" else "*" for char in text)
    assert redact_sensitive(text) == expected
    assert "private_key" in find_sensitive(text)
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


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
        "password = prefix + suffix",
        'password = f"{get_password()}"',
        "password = ${SECRET}",
        'password = "${SECRET}"',
    ],
)
def test_dynamic_assignment_expressions_remain_entirely_unchanged(text):
    assert redact_sensitive(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password = "SyntheticSecret987!" + suffix', 'password = "*******************" + suffix'),
        ('password = "SyntheticSecret987!" == guess', 'password = "*******************" == guess'),
        ('password = "prefix_" + get_password()', 'password = "*******" + get_password()'),
        (
            'password = "value" if flag else "fallback"',
            'password = "*****" if flag else "********"',
        ),
        ('password = prefix + "SyntheticSecret987!"', 'password = prefix + "*******************"'),
        (
            'password = "secret" + get_password("ENV_NAME")',
            'password = "******" + get_password("ENV_NAME")',
        ),
        (
            'password = "secret" or settings["password"]',
            'password = "******" or settings["password"]',
        ),
        (
            'password = "${SECRET}" + "SyntheticSecret987!"',
            'password = "${SECRET}" + "*******************"',
        ),
        (
            'password = f"{get_password()}" + "SyntheticSecret987!"',
            'password = f"{get_password()}" + "*******************"',
        ),
    ],
)
def test_static_literals_in_credential_expressions_are_masked(text, expected):
    assert "credential_assignment" in find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected
    assert len(expected) == len(text)


@pytest.mark.parametrize(
    "value",
    ["Correct.Horse.Battery.Staple", "Synthetic.Secret.Passphrase", "correct.horse.battery.staple"],
)
def test_dotted_passphrases_are_masked_as_actual_values(value):
    text = "password=" + value
    expected = "password=" + "*" * len(value)
    assert redact_sensitive(text) == expected
    assert "credential_assignment" in find_sensitive(text)
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        'password=get_password("PASSWORD_ENV")',
        'password=settings["password"]',
        "password=config.credentials.password",
        "password=SecretProvider.current.password",
        "password=get_password() or default_password",
        'message = "value" if flag else "fallback"',
    ],
)
def test_ordinary_code_expressions_preserve_text_and_clear_gate(text):
    assert redact_sensitive(text) == text
    assert find_sensitive(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'opts = dict(password="secret" + suffix, label="PUBLIC TITLE")',
            'opts = dict(password="******" + suffix, label="PUBLIC TITLE")',
        ),
        (
            'opts = {password: "secret" + suffix, label: "PUBLIC TITLE"}',
            'opts = {password: "******" + suffix, label: "PUBLIC TITLE"}',
        ),
        (
            'opts = dict(password=prefix + "secret", label="PUBLIC TITLE")',
            'opts = dict(password=prefix + "******", label="PUBLIC TITLE")',
        ),
    ],
)
def test_expression_redaction_does_not_cross_into_other_arguments(text, expected):
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "uploaded_text"),
    [
        ('password = "SyntheticSecret987!" + suffix', 'password = "*******************" + suffix'),
        ('password = "SyntheticSecret987!" == guess', 'password = "*******************" == guess'),
        (
            "password=Correct.Horse.Battery.Staple",
            "password=" + "*" * len("Correct.Horse.Battery.Staple"),
        ),
    ],
)
async def test_expression_privacy_boundary_keeps_original_and_masks_or_blocks_upload(
    tmp_path, text, uploaded_text
):
    from test_store import conversation
    from test_sync import Remote

    from ai_memory.segments import SegmentBuilder
    from ai_memory.store import Store
    from ai_memory.sync import SyncEngine

    with Store(tmp_path / "synthetic.db") as store:
        packet = conversation()
        packet["messages"][0]["text"] = text
        conversation_id = store.ingest(packet, consent=True)
        remote = Remote()
        result = await SyncEngine(
            store, SegmentBuilder(store), remote, tmp_path / "upload-artifacts"
        ).run_once()

        assert store.get_conversation(conversation_id)["messages"][0]["text"] == text
        if uploaded_text is None:
            assert result["blocked"]
            assert remote.sources == {}
            assert remote.operations == []
        else:
            assert not result["blocked"]
            assert len(remote.sources) == 1
            artifact = next(iter(remote.sources.values()))["text"]
            assert uploaded_text in artifact
            assert "SyntheticSecret987!" not in artifact


def test_static_template_literal_credential_is_redacted():
    value = "synthetic-template-secret"
    text = f"password = `{value}`"

    assert redact_sensitive(text) == f"password = `{'*' * len(value)}`"


def test_multiline_quoted_credential_masks_payload_without_changing_line_breaks():
    text = 'password = """first\nsecond"""'

    assert redact_sensitive(text) == 'password = """*****\n******"""'


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ('"synthetic-only', '"' + "*" * len("synthetic-only")),
        ("'synthetic-only", "'" + "*" * len("synthetic-only")),
        ("`synthetic-only", "*" * len("`synthetic-only")),
        (
            '"""synthetic-only\nsecond line',
            "*" * len('"""synthetic-only') + "\n" + "*" * len("second line"),
        ),
        ("r'synthetic-only", "r'" + "*" * len("synthetic-only")),
        ('\\"synthetic-only', '\\"' + "*" * len("synthetic-only")),
        (
            '"synthetic-only\nUnrelated "quoted" explanation.',
            '"' + "*" * len("synthetic-only") + '\nUnrelated "quoted" explanation.',
        ),
    ],
)
def test_unterminated_literal_values_use_a_bounded_mask(value, masked):
    text = "password = " + value
    expected = "password = " + masked

    assert redact_sensitive(text) == expected
    assert "credential_assignment" in find_sensitive(text)
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


def test_unterminated_dynamic_template_is_preserved_and_clears_the_gate():
    text = 'password = f"{synthetic_variable}'

    assert redact_sensitive(text) == text
    assert find_sensitive(text) == []


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_completely_masked_multiline_credentials_clear_the_privacy_gate(newline):
    text = 'password = """synthetic' + newline + 'only"""'
    expected = 'password = """*********' + newline + '****"""'

    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


def test_multiline_mask_does_not_hide_an_unmasked_character():
    assert "credential_assignment" in find_sensitive('password = """***\n*x*"""')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'password = "synthetic-only',
            'password = "' + "*" * len("synthetic-only"),
        ),
        ('password = """synthetic\nonly"""', 'password = """*********\n****"""'),
    ],
)
async def test_incomplete_and_multiline_values_obey_the_upload_boundary(tmp_path, text, expected):
    from test_store import conversation
    from test_sync import Remote

    from ai_memory.segments import SegmentBuilder
    from ai_memory.store import Store
    from ai_memory.sync import SyncEngine

    with Store(tmp_path / "synthetic.db") as store:
        packet = conversation()
        packet["messages"][0]["text"] = text
        conversation_id = store.ingest(packet, consent=True)
        other = conversation(external_id="unrelated")
        other["messages"][0]["text"] = "Unrelated synthetic conversation"
        store.ingest(other, consent=True)
        remote = Remote()
        result = await SyncEngine(
            store, SegmentBuilder(store), remote, tmp_path / "upload-artifacts"
        ).run_once()

        assert store.get_conversation(conversation_id)["messages"][0]["text"] == text
        if expected is None:
            assert result["blocked"]
            assert remote.sources == {}
            assert remote.operations == []
        else:
            assert result["blocked"] == []
            assert len(remote.sources) == 1
            uploaded = next(iter(remote.sources.values()))["text"]
            assert expected in uploaded
            assert "Unrelated synthetic conversation" in uploaded
            assert "synthetic\nonly" not in uploaded


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_PASSWORD", "fake-db-pass"),
        ("OPENAI_API_KEY", "fake-openai-key"),
        ("SECRET_KEY", "fake-secret-key"),
        ("AWS_SECRET_ACCESS_KEY", "fake-aws-secret-key"),
        ("client_secret", "fake-client-secret"),
        ("clientSecret", "fake-client-secret"),
        ("auth_token", "fake-auth-token"),
        ("authToken", "fake-auth-token"),
        ("private_key", "fake-private-key"),
        ("privateKey", "fake-private-key"),
        ("secret", "fake-secret"),
    ],
)
def test_prefixed_environment_and_standalone_credential_names_are_masked(name, value):
    text = f'{name}: "{value}"'
    expected = f'{name}: "{"*" * len(value)}"'

    assert "credential_assignment" in find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected
    assert len(expected) == len(text)


def test_x_api_key_header_and_query_style_assignment_are_masked():
    text = "X-API-Key: fake-header-token\n?client_secret=fake-client-secret&next=keep"
    expected = "X-API-Key: *****************\n?client_secret=******************&next=keep"

    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD = os.environ['DB_PASSWORD']",
        'OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")',
        "client_secret = get_client_secret()",
        "auth_token = response.json()['auth_token']",
        "private_key = Path('key.pem').read_text()",
        "password: str = get_password()",
        "password = None",
        "secret = get_secret()",
        "if password == 'fake-pass':",
        "if DB_PASSWORD != stored_password:",
        "if 'fake' == auth_token:",
        "if auth_token: refresh()",
        "class Password: pass",
    ],
)
def test_dynamic_sources_lookups_calls_and_comparisons_are_preserved(text):
    assert redact_sensitive(text) == text
    assert find_sensitive(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'My password is "fake-prose-pass".',
            'My password is "***************".',
        ),
        (
            "The old password was 'fake-prose-pass'.",
            "The old password was '***************'.",
        ),
        (
            'password is: "fake-prose-pass"',
            'password is: "***************"',
        ),
        ('The secret is "fake-secret".', 'The secret is "***********".'),
    ],
)
def test_quoted_prose_password_phrases_are_masked(text, expected):
    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('нууц үг: "fake-mn-pass"', 'нууц үг: "************"'),
        ('Нууц үг нь "fake-mn-pass"', 'Нууц үг нь "************"'),
        ("Нэвтрэх үг = 'fake-login-pass'", "Нэвтрэх үг = '***************'"),
        ('API түлхүүр = "fake-api-key"', 'API түлхүүр = "************"'),
        ('Токен: "fake-token"', 'Токен: "**********"'),
        ('Нууц түлхүүр: "fake-secret-key"', 'Нууц түлхүүр: "***************"'),
    ],
)
def test_focused_mongolian_credential_names_are_masked(text, expected):
    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected

    assert "Нууц үг нь шаардлагатай." == redact_sensitive("Нууц үг нь шаардлагатай.")


@pytest.mark.parametrize(
    "token",
    [
        "sk_live_" + "a" * 24,
        "sk_test_" + "a" * 24,
        "xoxb-" + "1234567890-1234567890",
        "xoxp-" + "1234567890-1234567890",
        "AIza" + "a" * 35,
        "ya29." + "a" * 30,
        "github_pat_" + "a" * 40,
        "glpat-" + "a" * 20,
        "npm_" + "a" * 36,
        "whsec_" + "a" * 32,
        "ghp_" + "a" * 20,
        "AKIA" + "A" * 16,
    ],
)
def test_common_explicit_provider_token_formats_are_masked(token):
    text = f"provider token={token}! keep"
    expected = f"provider token={'*' * len(token)}! keep"

    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected
    assert len(expected) == len(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "postgres://dbuser:fake-db-pass@db.example.test:5432/app",
            "postgres://dbuser:************@db.example.test:5432/app",
        ),
        (
            "https://:fake-token@example.test/path",
            "https://:**********@example.test/path",
        ),
        (
            "redis://user:p%40ssword@cache.example.test/0",
            "redis://user:**********@cache.example.test/0",
        ),
    ],
)
def test_url_passwords_are_masked_without_touching_user_or_host(text, expected):
    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected
    assert len(expected) == len(text)


def test_url_without_credentials_is_unchanged():
    text = "https://example.test:8443/path?query=value"
    assert redact_sensitive(text) == text
    assert find_sensitive(text) == []


def test_database_password_flag_is_masked_without_touching_ports_or_variables():
    text = "mysql -P3306 -psynthetic\nssh -p2222 example.test\nmysql -p$MYSQL_SECRET"
    expected = "mysql -P3306 -p*********\nssh -p2222 example.test\nmysql -p$MYSQL_SECRET"
    assert find_sensitive(text) == ["database_password_flag"]
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


def test_pgp_private_key_payload_is_masked_and_armor_is_preserved():
    body = "Version: GnuPG v2\n\nZmFrZS1rZXktbWF0ZXJpYWw=\n=abcd"
    text = f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n{body}\n-----END PGP PRIVATE KEY BLOCK-----"
    expected_body = "".join("*" if char not in "\r\n" else char for char in body)
    expected = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        f"{expected_body}\n"
        "-----END PGP PRIVATE KEY BLOCK-----"
    )

    assert "private_key" in find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected
    assert len(expected) == len(text)


def test_unclosed_pgp_private_key_masks_to_the_end_of_its_message():
    text = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "ZmFrZS1rZXktbWF0ZXJpYWw=\n"
        "Unrelated explanation stays intact."
    )
    expected = "".join("\n" if char == "\n" else "*" for char in text)

    assert "private_key" in find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []
    assert redact_sensitive(expected) == expected


def test_defensive_corpus_has_no_high_entropy_false_positives():
    import json
    from pathlib import Path

    corpus = json.loads(Path("tests/fixtures/privacy_corpus.json").read_text(encoding="utf-8"))
    for text in corpus["benign"]:
        assert find_sensitive(text) == [], text
        assert redact_sensitive(text) == text, text


def test_subscript_credential_assignment_is_masked_but_lookup_is_preserved():
    text = 'os.environ["DB_PASSWORD"] = "fake-db-pass"; keep = os.environ["DB_PASSWORD"]'
    expected = 'os.environ["DB_PASSWORD"] = "************"; keep = os.environ["DB_PASSWORD"]'

    assert find_sensitive(text)
    assert redact_sensitive(text) == expected
    assert find_sensitive(expected) == []


def test_clipped_literal_wrapped_by_an_outer_yaml_quote_already_clears_the_gate():
    text = 'title: "clipped ... password = \'***"'

    assert find_sensitive(text) == []
    assert redact_sensitive(text) == text
