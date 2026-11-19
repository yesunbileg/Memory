"""Select the local archive for host integrations and optionally install the OpenCode bridge."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "ai-memory-hooks-v1"


def absolute_file(path):
    if not path.is_absolute() or not path.is_file():
        raise ValueError("paths must be absolute and point to existing files")
    return str(path)


def write_private(path, payload, *, replace=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            if replace:
                os.replace(tmp, path)
            else:
                os.link(tmp, path)  # Never clobber another file created concurrently.
        finally:
            tmp.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path, help="absolute initialized archive path")
    p.add_argument("--cli", type=Path, default=ROOT / ".venv/bin/ai-memory")
    p.add_argument("--opencode-db", type=Path, help="absolute OpenCode source DB; installs bridge")
    p.add_argument("--account", help="local source account label (new settings default to local)")
    args = p.parse_args()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        p.error("XDG_CONFIG_HOME must be absolute")
    settings = Path.home() / ".config/ai-memory/hooks.json"
    try:
        cli = absolute_file(args.cli)
        db = absolute_file(args.db)
        if not os.access(cli, os.X_OK):
            raise ValueError("the selected ai-memory CLI is not executable")
        if settings.is_symlink():
            raise ValueError("refusing to replace a settings symlink")
        exists = settings.exists()
        original = settings.read_bytes() if exists else b""
        data = json.loads(original) if exists else {"schema": SCHEMA, "account": "local"}
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError("unrecognized existing hook settings; no changes made")
        data.update(cli=cli, db=db)
        if args.account is not None:
            if not args.account.strip():
                raise ValueError("account label must not be empty")
            data["account"] = args.account
        if args.opencode_db is not None:
            data["opencodeDb"] = absolute_file(args.opencode_db)
            if args.db.samefile(args.opencode_db):
                raise ValueError("the OpenCode source cannot be the archive database")
            bridge = (ROOT / "config/opencode-memory.js").read_bytes()
            opencode_dir = Path(os.environ.get("OPENCODE_CONFIG_DIR") or base / "opencode")
            if not opencode_dir.is_absolute():
                raise ValueError("OPENCODE_CONFIG_DIR must be absolute")
            plugin = opencode_dir / "plugins/ai-memory.js"
            if plugin.is_symlink() or (plugin.exists() and plugin.read_bytes() != bridge):
                raise ValueError("OpenCode plugin path is occupied; refusing to overwrite it")
        encoded = (json.dumps(data, indent=2) + "\n").encode()
        if encoded != original:
            write_private(settings, encoded, replace=exists)
        if args.opencode_db is not None and not plugin.exists():
            write_private(plugin, bridge)
    except (OSError, ValueError) as exc:
        sys.exit(f"AI Memory hook setup failed: {exc}")
    print(f"Local runtime configured at {settings}")
    if args.opencode_db is not None:
        print(f"OpenCode bridge installed at {plugin}")


if __name__ == "__main__":
    main()
