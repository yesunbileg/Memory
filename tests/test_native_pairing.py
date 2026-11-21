"""Run the real connect command and a browser-framed synthetic native host."""

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

from ai_memory.store import Store


def test_registered_host_checks_origin_and_archive_before_any_write(tmp_path):
    db = tmp_path / "my archive.sqlite"
    extension = "a" * 32
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "browser")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_memory",
            "--db",
            str(db),
            "connect",
            "--extension-id",
            extension,
            "--browser",
            "chromium",
        ],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    pairing = json.loads(Path(result["connection_file"]).read_text())
    assert pairing["schema"] == 2 and "token" not in pairing
    manifest = json.loads(
        (
            tmp_path
            / "browser"
            / "chromium"
            / "NativeMessagingHosts"
            / (pairing["native_host"] + ".json")
        ).read_text()
    )
    assert manifest["allowed_origins"] == ["chrome-extension://" + extension + "/"]

    def request(msg, origin=manifest["allowed_origins"][0]):
        data = json.dumps(msg).encode()
        reply = subprocess.run(
            [manifest["path"], origin],
            input=struct.pack("=I", len(data)) + data,
            capture_output=True,
            check=True,
        )
        assert not reply.stderr
        assert struct.unpack("=I", reply.stdout[:4])[0] == len(reply.stdout) - 4
        return json.loads(reply.stdout[4:])

    archive = pairing["archive_instance_id"]
    probe = {"op": "check", "archive_instance_id": archive}
    assert request(probe)["archive_instance_id"] == archive
    assert request(probe, "chrome-extension://" + "b" * 32 + "/") == {"error": "unauthorized"}
    packet = {
        "archive_instance_id": archive,
        "source": "chatgpt-web",
        "adapter": "chatgpt-web",
        "conversations": [
            {"externalId": "synthetic", "messages": [{"role": "user", "content": " Сайн 🐾\r\n"}]}
        ],
    }
    assert request({**packet, "archive_instance_id": "0" * 32}) == {"error": "archive_changed"}
    assert request(packet) == {"conversations": 1, "created": 1, "updated": 0, "unchanged": 0}
    assert request(packet)["unchanged"] == 1
    with Store(db) as store:
        assert store.connection.execute("select text from messages").fetchone()[0] == " Сайн 🐾\r\n"
        assert store.connection.execute("select consent from conversations").fetchone()[0] == 0
    # The second write fails *after* the first write in the same packet.
    with Store(db) as store:
        store.connection.execute("""CREATE TRIGGER deny_fixture BEFORE INSERT ON conversations
          WHEN NEW.external_id='late-failure' BEGIN SELECT RAISE(ABORT, 'synthetic private diagnostic'); END""")
    mixed = {
        **packet,
        "conversations": [
            {**packet["conversations"][0], "externalId": key} for key in ("early", "late-failure")
        ],
    }
    assert request(mixed) == {"error": "archive_unavailable"}
    with Store(db) as store:
        assert store.connection.execute("select count(*) from conversations").fetchone()[0] == 1
        store.connection.execute("DROP TRIGGER deny_fixture")
    assert request(mixed) == {"conversations": 2, "created": 2, "updated": 0, "unchanged": 0}
    # A different database at the same path does not inherit its host registration.
    db.rename(tmp_path / "old.sqlite")
    db.write_bytes(b"Unrelated personal file.")
    db.chmod(0o644)
    assert request(packet) == {"error": "archive_changed"}
    assert db.read_bytes() == b"Unrelated personal file."
    assert db.stat().st_mode & 0o777 == 0o644
    db.unlink()
    with Store(db) as other:
        assert other.archive_instance_id() != archive
    assert request(packet) == {"error": "archive_changed"}
    with Store(db) as other:
        assert not other.connection.execute("select count(*) from conversations").fetchone()[0]


def _connect(tmp_path, db, extension, browser="chromium"):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "browser")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_memory",
            "--db",
            str(db),
            "connect",
            "--extension-id",
            extension,
            "--browser",
            browser,
        ],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _manifest(tmp_path, host, browser="chromium"):
    path = tmp_path / "browser" / browser / "NativeMessagingHosts" / (host + ".json")
    return json.loads(path.read_text())


def _request(manifest, msg, origin=None):
    origin = manifest["allowed_origins"][0] if origin is None else origin
    data = json.dumps(msg).encode()
    reply = subprocess.run(
        [manifest["path"], origin],
        input=struct.pack("=I", len(data)) + data,
        capture_output=True,
        check=True,
    )
    assert not reply.stderr
    assert struct.unpack("=I", reply.stdout[:4])[0] == len(reply.stdout) - 4
    return json.loads(reply.stdout[4:])


def test_discovery_host_is_read_only_and_rejects_denied_origins_and_stale_archives(tmp_path):
    db = tmp_path / "discovery.sqlite"
    extension = "c" * 32
    result = _connect(tmp_path, db, extension)
    assert result["auto_connect"] is True
    pairing = json.loads(Path(result["connection_file"]).read_text())
    archive = pairing["archive_instance_id"]
    discovery = _manifest(tmp_path, "org.ai_memory.connect_" + extension)
    assert discovery["name"] == "org.ai_memory.connect_" + extension
    assert discovery["allowed_origins"] == ["chrome-extension://" + extension + "/"]
    assert "--discover" in Path(discovery["path"]).read_text()
    assert str(db.resolve()) in Path(discovery["path"]).read_text()
    expected = {
        "status": "ok",
        "service": "ai-memory",
        "schema": 2,
        "archive_instance_id": archive,
    }
    assert _request(discovery, {"op": "discover"}) == expected

    # Discovery accepts exactly one packet shape and never ingests.
    assert _request(discovery, {"op": "check"}) == {"error": "invalid_payload"}
    assert _request(discovery, {"op": "discover", "archive_instance_id": archive}) == {
        "error": "invalid_payload"
    }
    packet = {
        "op": "discover",
        "archive_instance_id": archive,
        "source": "chatgpt-web",
        "adapter": "chatgpt-web",
        "conversations": [
            {"externalId": "synthetic", "messages": [{"role": "user", "content": "private"}]}
        ],
    }
    assert _request(discovery, packet) == {"error": "invalid_payload"}
    with Store(db) as store:
        assert store.connection.execute("select count(*) from conversations").fetchone()[0] == 0

    # The discovery alias has the same sole origin and denies every other origin.
    denied = "chrome-extension://" + "d" * 32 + "/"
    assert _request(discovery, {"op": "discover"}, denied) == {"error": "unauthorized"}

    # Discover is never accepted on the archive write host.
    write_host = _manifest(tmp_path, pairing["native_host"])
    assert _request(write_host, {"op": "discover", "archive_instance_id": archive}) == {
        "error": "invalid_payload"
    }

    # A replaced database at the same path is refused before any archive action.
    db.rename(tmp_path / "previous.sqlite")
    db.write_bytes(b"Unrelated personal file.")
    db.chmod(0o644)
    assert _request(discovery, {"op": "discover"}) == {"error": "archive_changed"}
    assert db.read_bytes() == b"Unrelated personal file."
    assert db.stat().st_mode & 0o777 == 0o644
    db.unlink()
    with Store(db) as replacement:
        assert replacement.archive_instance_id() != archive
    assert _request(discovery, {"op": "discover"}) == {"error": "archive_changed"}
    with Store(db) as replacement:
        count = replacement.connection.execute("select count(*) from conversations").fetchone()[0]
        assert not count


def test_second_archive_keeps_first_discovery_alias_and_manual_file(tmp_path):
    extension = "e" * 32
    first_db = tmp_path / "first.sqlite"
    second_db = tmp_path / "second.sqlite"
    first = _connect(tmp_path, first_db, extension)
    assert first["auto_connect"] is True
    first_archive = json.loads(Path(first["connection_file"]).read_text())["archive_instance_id"]
    discovery_path = (
        tmp_path
        / "browser"
        / "chromium"
        / "NativeMessagingHosts"
        / ("org.ai_memory.connect_" + extension + ".json")
    )
    original = discovery_path.read_bytes()
    first_discovery = json.loads(original)
    assert _request(first_discovery, {"op": "discover"})["archive_instance_id"] == first_archive

    second = _connect(tmp_path, second_db, extension)
    assert second["auto_connect"] is False
    second_pairing = json.loads(Path(second["connection_file"]).read_text())
    second_archive = second_pairing["archive_instance_id"]
    assert second_archive != first_archive
    assert second["connection_file"] != first["connection_file"]
    assert Path(first["connection_file"]).is_file()
    assert Path(second["connection_file"]).is_file()
    assert discovery_path.read_bytes() == original
    assert json.loads(discovery_path.read_text())["allowed_origins"] == [
        "chrome-extension://" + extension + "/"
    ]
    assert _request(first_discovery, {"op": "discover"})["archive_instance_id"] == first_archive

    # The second archive still has its own write host and manual pairing file.
    second_write = _manifest(tmp_path, "org.ai_memory.archive_" + second_archive)
    assert (
        _request(
            second_write,
            {"op": "check", "archive_instance_id": second_archive},
        )["archive_instance_id"]
        == second_archive
    )

    # Re-registering the same mapping is idempotent and never replaces the alias.
    again = _connect(tmp_path, first_db, extension)
    assert again["auto_connect"] is True
    assert discovery_path.read_bytes() == original
    again_second = _connect(tmp_path, second_db, extension)
    assert again_second["auto_connect"] is False
    assert discovery_path.read_bytes() == original
    assert _request(first_discovery, {"op": "discover"})["archive_instance_id"] == first_archive
