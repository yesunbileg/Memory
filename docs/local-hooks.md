# Local session capture

Set this up on the computer running Codex or OpenCode. The Codex plugin provides both session capture and the memory MCP server. The OpenCode event integration is separate. Neither requires a browser or a Google sign-in to save conversations.

## Prepare the archive

From this checkout, install the locked dependencies and choose one archive for capture, MCP and any future sync. Use the path of your existing archive if you have one.

```bash
uv sync --frozen
.venv/bin/ai-memory --db "$PWD/data/conversations.sqlite" init
python3 config/install_hooks.py --db "$PWD/data/conversations.sqlite"
```

The integration calls the installed `ai-memory` executable. Keep the checkout, virtual environment and archive available at their configured paths. If you move them, run the setup command again. It records paths under `~/.config/ai-memory/hooks.json`. Use `--account NAME` to match a non-default account label used for your manual imports. The installer runs on your machine. Updating the repository elsewhere does not install or enable anything in your personal clients.

## Codex

Use a Codex CLI with support for local plugin marketplaces and Stop hooks. From this checkout:

```bash
codex plugin marketplace add "$PWD"
codex plugin add ai-memory@yesko-memory-local
```

Start a new Codex session, open `/hooks`, review the plugin's Stop handler and trust it to enable capture. Do not separately register a second AI Memory MCP server. On an upgrade, if you previously registered the same standalone server, remove only your old AI Memory entry first. If you installed a local collection service yourself, stop and disable it before enabling the new integration (`systemctl --user disable --now ai-memory.service` for the former example unit). This is unnecessary on a fresh installation.

At the end of a Codex turn, the Stop handler reads the transcript identified by that event and checks its session ID. Sessions without a persisted transcript are skipped. It does not search other sessions. To remove the plugin, run `codex plugin remove ai-memory@yesko-memory-local`. To remove the local catalog as well, run `codex plugin marketplace remove yesko-memory-local`. Neither action deletes your archive.

## OpenCode

Use OpenCode 2.x or version 1.18.29 or later in the 1.x series. Pass the absolute path to the SQLite database **used by OpenCode**, not the AI Memory archive. This installs the separate OpenCode event integration in addition to configuring the shared archive. For the default Linux location:

```bash
python3 config/install_hooks.py --db "$PWD/data/conversations.sqlite" \
  --opencode-db "$HOME/.local/share/opencode/opencode.db"
```

Restart OpenCode to load the integration. A `session.idle` event selects that native session ID from the configured database. Other sessions are not imported. For retrieval, register the [OpenCode MCP server](agent-instructions.md). Older JSON-only histories can still be imported manually. On OpenCode 2, `opencode debug paths` helps locate the database. To remove the event integration, remove only the installed `plugins/ai-memory.js` file at the path printed by setup, then restart OpenCode.

## Check a local capture

Finish a short test turn in the configured client. Run `status` against the
same database to check the archive counts. To find the conversation and inspect
its original text, open `mem` using the same database and run `/chats`.

```bash
.venv/bin/ai-memory --db "$PWD/data/conversations.sqlite" status
mem --db "$PWD/data/conversations.sqlite"
```

An event can only save what its client has already written. If a turn is missing, check the client loaded the integration, the machine has access to the configured paths and the event completed. Repeated native message IDs are deduplicated. A configured notebook and enabled automatic sync allow captures to wake the publisher. Use `/auto off` to pause automatic publication. For missed events or historical data, run an explicit [one-time import](../README.md#import-existing-histories-once).
