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
