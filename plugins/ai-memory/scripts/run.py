"""Private Codex plugin dispatch. Inherit stdin and keep stdout for the host protocol."""

import json
import os
import sys
from pathlib import Path

SCHEMA = "ai-memory-hooks-v1"


def settings_path():
    # Codex stdio servers inherit HOME, but not necessarily XDG_CONFIG_HOME.
    return Path.home() / ".config/ai-memory/hooks.json"


def dispatch(mode):
    data = json.loads(settings_path().read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("invalid settings")
    cli, db = Path(data["cli"]), Path(data["db"])
    if not (cli.is_absolute() and db.is_absolute() and db.is_file()):
        raise ValueError("invalid runtime paths")
    if mode == "codex":
        args = ["hook", "codex", "--account", data["account"]]
    elif mode == "mcp":
        args = ["mcp"]
    else:
        raise ValueError("invalid invocation")
    os.execv(cli, [str(cli), "--db", str(db), *args])


if __name__ == "__main__":
    try:
        dispatch(sys.argv[1])
    except (IndexError, KeyError, OSError, TypeError, ValueError):
        sys.exit("AI Memory could not start. Check config/install_hooks.py settings and paths.")
