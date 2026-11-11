"""Private connection files for the loopback browser receiver."""

from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

_CONNECTION_KEYS = {"schema", "application", "host", "port", "token"}


@dataclass(frozen=True)
class ConnectionFiles:
    connection_file: Path
    token_file: Path
    port: int
    token: str = field(repr=False)


def _valid_token(token: object) -> bool:
    return (
        isinstance(token, str) and 24 <= len(token) <= 512
        and all(33 <= ord(char) <= 126 for char in token)
    )


def _read_private(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f"Use a private regular file, not a symbolic link: {path}") from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise ValueError(f"Use a private regular file owned by this user: {path}")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError(f"Connection secrets need private permissions (0600): {path}")
        if details.st_size > 4096:
            raise ValueError(f"Existing connection file is not recognized; it was left unchanged: {path}")
        with os.fdopen(descriptor, "rb") as file:
            descriptor = -1
            content = file.read(4097)
        if len(content) > 4096:
            raise ValueError(f"Existing connection file is not recognized; it was left unchanged: {path}")
        return content
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _read_token(path: Path) -> str | None:
    content = _read_private(path)
    if content is None:
        return None
    try:
        token = content.decode("ascii").strip()
    except UnicodeError:
        token = ""
    if not _valid_token(token):
        raise ValueError(f"Existing token file is not recognized; it was left unchanged: {path}")
    return token


def _read_connection(path: Path) -> dict | None:
    content = _read_private(path)
    if content is None:
        return None
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError):
        data = None
    if (
        not isinstance(data, dict) or set(data) != _CONNECTION_KEYS
        or type(data.get("schema")) is not int or data["schema"] != 1
        or data.get("application") != "ai-memory" or data.get("host") != "127.0.0.1"
        or type(data.get("port")) is not int or not 1 <= data["port"] <= 65535
        or not _valid_token(data.get("token"))
    ):
        raise ValueError(f"Existing connection file is not recognized; it was left unchanged: {path}")
    return data


def _write_new_private(path: Path, content: bytes) -> None:
    # O_EXCL prevents overwriting files, including symlinks supplied at this path.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def prepare_connection(db_path: Path, port: int = 41770, *, write_config: bool = True) -> ConnectionFiles:
    """Create or reuse one private connection beside the selected archive."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Choose a local port between 1 and 65535.")
    directory = Path(db_path).expanduser().resolve().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = directory / "ingest.token"
    connection_file = directory / "extension-connection.json"
    existing = _read_connection(connection_file)
    token = _read_token(token_file)
    if existing and (
        existing["port"] != port
        or (token is not None and not hmac.compare_digest(existing["token"], token))
    ):
        raise ValueError(
            "This archive directory already has different connection settings. "
            "Use its existing port and token, or select a separate archive directory."
        )
    if token is None:
        token = existing["token"] if existing else secrets.token_urlsafe(32)
        try:
            _write_new_private(token_file, (token + "\n").encode("ascii"))
        except FileExistsError:
            # Another setup may have won the exclusive create. Never replace it.
            token = _read_token(token_file)
        if token is None or (existing and not hmac.compare_digest(existing["token"], token)):
            raise ValueError("Connection setup changed in another process. Run connect again.")
    files = ConnectionFiles(connection_file=connection_file, token_file=token_file, port=port, token=token)
    if write_config:
        write_connection_file(files)
    return files


def write_connection_file(files: ConnectionFiles) -> None:
    """Publish configuration only once its receiver is available or verified."""
    token = _read_token(files.token_file)
    if token is None or not hmac.compare_digest(token, files.token):
        raise ValueError("Connection setup changed in another process. Run connect again.")
    data = {"schema": 1, "application": "ai-memory", "host": "127.0.0.1", "port": files.port, "token": token}
    existing = _read_connection(files.connection_file)
    if existing is not None and existing != data:
        raise ValueError("Existing connection settings differ; the file was left unchanged.")
    if existing is None:
        try:
            _write_new_private(files.connection_file, (json.dumps(data, indent=2) + "\n").encode("ascii"))
        except FileExistsError:
            existing = _read_connection(files.connection_file)
            if existing != data:
                raise ValueError("Connection setup changed in another process. Run connect again.") from None


def check_running_receiver(port: int, token: str) -> str:
    """Classify an occupied loopback port without exposing credentials or replies."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("POST", "/auth/check", body="{}", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token,
        })
        response = connection.getresponse()
        if response.status in {401, 403}:
            return "unauthorized"
        if response.status != 200:
            return "incompatible"
        data = json.loads(response.read(4096))
        return "ok" if data == {"status": "ok", "service": "ai-memory", "schema": 1} else "incompatible"
    except (OSError, ValueError, http.client.HTTPException):
        return "incompatible"
    finally:
        connection.close()
