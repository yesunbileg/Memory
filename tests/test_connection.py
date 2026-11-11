"""Private, repeatable connection artifacts; no existing user files are overwritten."""

import json
import stat

import pytest

SYNTHETIC_TOKEN = "synthetic-private-connection-token-123456789"


def test_connection_files_are_private_and_repeatable(tmp_path):
    from ai_memory.connection import prepare_connection

    db = tmp_path / "new" / "archive.sqlite"
    files = prepare_connection(db, 41770)
    content = json.loads(files.connection_file.read_text())
    public = {key: value for key, value in content.items() if key != "token"}
    assert public == {"schema": 1, "application": "ai-memory", "host": "127.0.0.1", "port": 41770}
    tokens_match = content["token"] == files.token_file.read_text().strip() == files.token
    assert tokens_match
    assert len(content["token"]) >= 24
    assert not db.exists()
    for file in (files.token_file, files.connection_file):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    original = [file.read_bytes() for file in (files.token_file, files.connection_file)]
    repeated = prepare_connection(db, 41770)
    artifacts_unchanged = original == [file.read_bytes() for file in (repeated.token_file, repeated.connection_file)]
    assert artifacts_unchanged
    token_exposed = files.token in repr(files)
    assert not token_exposed


def test_existing_private_token_is_reused_without_rotation(tmp_path):
    from ai_memory.connection import prepare_connection

    token = tmp_path / "ingest.token"
    token.write_text(SYNTHETIC_TOKEN + "\n")
    token.chmod(0o600)
    files = prepare_connection(tmp_path / "archive.sqlite", 43210)
    assert files.token == SYNTHETIC_TOKEN
    assert token.read_text() == SYNTHETIC_TOKEN + "\n"
    assert json.loads(files.connection_file.read_text())["port"] == 43210


def test_existing_private_connection_can_restore_missing_token_file(tmp_path):
    from ai_memory.connection import prepare_connection

    file = tmp_path / "extension-connection.json"
    file.write_text(json.dumps({"schema": 1, "application": "ai-memory", "host": "127.0.0.1",
                                "port": 41770, "token": SYNTHETIC_TOKEN}))
    file.chmod(0o600)
    before = file.read_bytes()
    files = prepare_connection(tmp_path / "archive.sqlite", 41770)
    assert files.token == SYNTHETIC_TOKEN
    assert file.read_bytes() == before
    assert files.token_file.read_text().strip() == SYNTHETIC_TOKEN


@pytest.mark.parametrize("which", ["ingest.token", "extension-connection.json"])
def test_existing_unrecognized_user_file_is_never_overwritten(tmp_path, which):
    from ai_memory.connection import prepare_connection

    file = tmp_path / which
    file.write_text("Existing personal note. Do not replace.")
    file.chmod(0o600)
    with pytest.raises(ValueError):
        prepare_connection(tmp_path / "archive.sqlite", 41770)
    assert file.read_text() == "Existing personal note. Do not replace."


@pytest.mark.parametrize("which", ["ingest.token", "extension-connection.json"])
def test_symlinked_secret_files_are_refused_without_modifying_targets(tmp_path, which):
    from ai_memory.connection import prepare_connection

    user_file = tmp_path / "existing-user-file"
    user_file.write_text("Unrelated user file")
    user_file.chmod(0o600)
    (tmp_path / which).symlink_to(user_file)
    with pytest.raises(ValueError, match="private|regular|symbolic"):
        prepare_connection(tmp_path / "archive.sqlite", 41770)
    assert user_file.read_text() == "Unrelated user file"


def test_public_token_permissions_are_refused_without_changing_file(tmp_path):
    from ai_memory.connection import prepare_connection

    token = tmp_path / "ingest.token"
    token.write_text(SYNTHETIC_TOKEN)
    token.chmod(0o644)
    with pytest.raises(ValueError, match="permission|private|0600"):
        prepare_connection(tmp_path / "archive.sqlite", 41770)
    assert stat.S_IMODE(token.stat().st_mode) == 0o644
    assert token.read_text() == SYNTHETIC_TOKEN


def test_conflicting_connection_config_is_preserved_and_rejected(tmp_path):
    from ai_memory.connection import prepare_connection

    files = prepare_connection(tmp_path / "archive.sqlite", 41770)
    original = files.connection_file.read_bytes()
    with pytest.raises(ValueError, match="different|existing|already"):
        prepare_connection(tmp_path / "archive.sqlite", 43210)
    configuration_unchanged = files.connection_file.read_bytes() == original
    assert configuration_unchanged
