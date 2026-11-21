"""Browser-owned Native Messaging channel. No network, user secrets or transcript logs."""

import argparse
import json
import os
import re
import shlex
import sqlite3
import stat
import struct
import sys
from contextlib import closing
from pathlib import Path

from .archive_files import workspace_for
from .diagnostics import PublicInputError
from .ingest import MAX_BODY_BYTES, ArchiveChanged, save_packet
from .store import Store

_BROWSER_DIRECTORIES = {
    "brave": "BraveSoftware/Brave-Browser",
    "chrome": "google-chrome",
    "chromium": "chromium",
}


def _install(path: Path, content: bytes, mode: int):
    """Create exclusively. Never overwrite an existing or foreign host registration."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or stat.S_IMODE(path.stat().st_mode) != mode
            or path.stat().st_size != len(content)
            or path.read_bytes() != content
        ):
            raise PublicInputError(
                "Existing native host files differ. They were not replaced."
            ) from None
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _install_discovery(
    executable: Path, launcher: bytes, manifest_file: Path, manifest: dict
) -> bool:
    """Register the immutable discovery alias, or report that another archive owns it."""
    content = (json.dumps(manifest, indent=2) + "\n").encode()
    try:
        _install(executable, launcher, 0o700)
        _install(manifest_file, content, 0o600)
    except PublicInputError:
        return False
    return True


def register(db_path: Path, extension_id: str, browser: str, user_data_dir: Path | None = None):
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise PublicInputError("Use the 32-letter ID displayed by the installed extension.")
    if not sys.platform.startswith("linux"):
        raise PublicInputError(
            "Automatic native host registration currently supports Linux Chrome/Chromium."
        )
    if browser not in _BROWSER_DIRECTORIES:
        raise PublicInputError("Choose brave, chrome or chromium.")
    db_path = Path(db_path).expanduser().resolve()
    with Store(db_path) as store:
        archive_id = store.archive_instance_id()
    if not re.fullmatch(r"[0-9a-f]{32}", archive_id):
        raise PublicInputError("Unsupported archive identity.")
    host_name = "org.ai_memory.archive_" + archive_id
    origin = f"chrome-extension://{extension_id}/"
    directory = workspace_for(db_path) / "native" / extension_id
    if directory.resolve() != directory:
        raise PublicInputError("Use a private archive directory, not a symbolic link.")
    executable = directory / "host"
    descriptor = directory / "extension-connection.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    folder = _BROWSER_DIRECTORIES[browser]
    root = (
        user_data_dir.expanduser().resolve() if user_data_dir is not None else config_home / folder
    )
    manifest_file = root / "NativeMessagingHosts" / (host_name + ".json")
    args = [sys.executable, "-m", "ai_memory.native", str(db_path), archive_id, origin]
    _install(executable, ("#!/bin/sh\nexec " + shlex.join(args) + ' "$@"\n').encode(), 0o700)
    manifest = {
        "name": host_name,
        "description": "AI Memory local archive",
        "path": str(executable),
        "type": "stdio",
        "allowed_origins": [origin],
    }
    _install(manifest_file, (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    pair = {
        "schema": 2,
        "application": "ai-memory",
        "transport": "native",
        "native_host": host_name,
        "archive_instance_id": archive_id,
    }
    _install(descriptor, (json.dumps(pair, indent=2) + "\n").encode(), 0o600)
    discovery_host = "org.ai_memory.connect_" + extension_id
    discovery_executable = directory / "discovery"
    discovery_manifest_file = root / "NativeMessagingHosts" / (discovery_host + ".json")
    discovery_args = [
        sys.executable,
        "-m",
        "ai_memory.native",
        "--discover",
        str(db_path),
        archive_id,
        origin,
    ]
    discovery_launcher = ("#!/bin/sh\nexec " + shlex.join(discovery_args) + ' "$@"\n').encode()
    discovery_manifest = {
        "name": discovery_host,
        "description": "AI Memory local archive discovery",
        "path": str(discovery_executable),
        "type": "stdio",
        "allowed_origins": [origin],
    }
    auto_connect = _install_discovery(
        discovery_executable, discovery_launcher, discovery_manifest_file, discovery_manifest
    )
    return {
        "connection_file": str(descriptor),
        "transport": "native",
        "browser": browser,
        "auto_connect": auto_connect,
    }


def _archive_unchanged(db: Path, archive_id: str) -> bool:
    if not db.is_file() or db.is_symlink():
        return False
    # Probe before Store() can initialize, migrate or chmod an unrelated file at
    # the previous path. Then recheck on the connection that performs the write.
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as probe:
            row = probe.execute(
                "SELECT value FROM settings WHERE key='archive_instance_id'"
            ).fetchone()
            if row is None or json.loads(row[0]) != archive_id:
                return False
    except (sqlite3.DatabaseError, ValueError):
        return False
    return True


def _handle(args, stream):
    if args.origin != args.allowed_origin:
        return {"error": "unauthorized"}
    size = struct.unpack("=I", stream.read(4))[0]
    if size > MAX_BODY_BYTES:
        return {"error": "body_too_large"}
    raw = stream.read(size)
    if len(raw) != size:
        return {"error": "invalid_payload"}
    packet = json.loads(raw.decode("utf-8"))
    if args.discover:
        if packet != {"op": "discover"}:
            return {"error": "invalid_payload"}
    elif not isinstance(packet, dict) or packet.get("archive_instance_id") != args.archive_id:
        return {"error": "archive_changed"}
    if not _archive_unchanged(args.db, args.archive_id):
        return {"error": "archive_changed"}
    if not args.discover and packet.get("op") == "discover":
        return {"error": "invalid_payload"}
    if args.discover or packet.get("op") == "check":
        return {
            "status": "ok",
            "service": "ai-memory",
            "schema": 2,
            "archive_instance_id": args.archive_id,
        }
    with Store(args.db) as store:
        if store.get_setting("archive_instance_id") != args.archive_id:
            return {"error": "archive_changed"}
        return save_packet(store, packet)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("db", type=Path)
    parser.add_argument("archive_id")
    parser.add_argument("allowed_origin")
    parser.add_argument("origin")
    args = parser.parse_args()
    try:
        result = _handle(args, sys.stdin.buffer)
    except ArchiveChanged:
        result = {"error": "archive_changed"}
    except (ValueError, TypeError, UnicodeError, RecursionError, struct.error):
        result = {"error": "invalid_payload"}
    except Exception:
        result = {"error": "archive_unavailable"}
    encoded = json.dumps(result, separators=(",", ":")).encode("ascii")
    sys.stdout.buffer.write(struct.pack("=I", len(encoded)) + encoded)


if __name__ == "__main__":
    main()
