# AI Memory

A lightweight archive for AI conversations, built from the [supplied architecture](docs/specification.mn.md).

Codex / OpenCode / web chats → hstry text collectors → SQLite → Markdown segments → **NotebookLM** → MCP.

SQLite stores original text and operational state. NotebookLM is the only AI memory engine. There is no local LLM, vector database, automatic summary, dashboard, or paid API dependency. notebooklm-py uses Google's unofficial API; availability and free-account limits depend on Google.

## Install and run the local demo

Requires Python 3.12+, Node.js 24+, and [uv](https://docs.astral.sh/uv/).

```bash
cd /workspace/Memory
UV_CACHE_DIR=/workspace/.cache/uv uv sync --frozen
.venv/bin/ai-memory demo
.venv/bin/python scripts/mcp_smoke.py
.venv/bin/pytest -q
node vendor/hstry/extension/test/lossless.js
```

On your own machine, use the checkout directory and your usual writable uv cache. The existing cloud checkout is already isolated; no Git worktree is needed.

The demo imports **synthetic** Codex and OpenCode histories through the actual donor adapters. It creates `data/demo/conversations.sqlite` and `data/demo/segments/SEG-001.md`, prints conversation IDs and process resource measurements, and can be repeated without duplicate messages. No data is sent to Google. MCP smoke testing starts a real stdio server and calls `memory_status`; it does not invent a NotebookLM answer.

## Collect your conversations

```bash
.venv/bin/ai-memory init
.venv/bin/ai-memory collect codex ~/.codex/sessions
.venv/bin/ai-memory collect opencode ~/.local/share/opencode
.venv/bin/ai-memory status
.venv/bin/ai-memory show CX-REPLACE_WITH_RETURNED_ID
.venv/bin/ai-memory approve CX-REPLACE_WITH_RETURNED_ID
.venv/bin/ai-memory build
```

The default archive is `data/conversations.sqlite`; use `--db PATH` **before** the subcommand to select another archive. Import prints the actual conversation IDs. Histories are imported locally without upload consent. `approve` grants consent for the specified conversation, including future continuations; `collect --approve-upload` grants it during import. Review the archived text before approving. A conservative credential detector blocks uploads while keeping the original local text intact; it cannot identify every possible secret.

Incremental scans checkpoint the selected path's file inventory. Unchanged histories skip parsing; changed trees are reparsed with idempotent native-ID upserts. Partial snapshots never delete absent messages. This initial version does not implement upstream message deletion/tombstones. Collectors retain user text, assistant text/code and explicit visible progress/summary parts; unknown or hidden reasoning, system prompts, tool events, logs and attachments are excluded. Missing timestamps and model names remain unknown.

## Connect NotebookLM

Sign in to NotebookLM in **the Brave, Chrome or Firefox profile you already use**, then import that session with notebooklm-py's supported cookie reader. This does not launch another browser or require a separate Chromium/Playwright installation. A service-account credential does not authenticate NotebookLM. Keep the saved session private and outside version control.

Keep your existing auth location when reauthenticating. For a new setup on a cloud host with a read-only home, first set a writable location with `export NOTEBOOKLM_HOME="$PWD/data/notebooklm"`.

```bash
# Install the optional cookie reader; no browser download is needed.
uv pip install --python .venv/bin/python 'notebooklm-py[cookies]==0.8.6'

# Use the same auth directory or --storage path as your worker/MCP server.
.venv/bin/notebooklm login --browser-cookies brave
.venv/bin/notebooklm create 'AI Memory'
# Copy the notebook ID printed by the command:
.venv/bin/ai-memory init --notebook-id YOUR_NOTEBOOK_ID
.venv/bin/ai-memory check
.venv/bin/ai-memory sync
.venv/bin/ai-memory ask 'Өмнө ямар шийдвэр гаргасан бэ?'
```

Replace `brave` with `chrome` or `firefox` for those browsers. For a specific Brave profile, use `--browser-cookies 'brave::Profile 1'`; for multiple signed-in Google accounts, add `--account YOUR_EMAIL`. Run this on the same computer and as the same operating-system user as the signed-in browser; a cloud host cannot read your laptop's browser profile. If cookie extraction fails, check the selected profile and desktop keyring access before retrying.

Use the same `NOTEBOOKLM_HOME` for login, the worker, MCP and the demo. If your archive already uses an explicit storage file, pass that file to login with `--storage /private/path/storage_state.json`; configure the archive with `init --storage` only when changing its auth location. Reauthentication does not require a new notebook. To test against Google using **only synthetic data** in a dedicated demo notebook:

```bash
.venv/bin/python scripts/live_demo.py
```

This live demo creates or reuses `AI Memory — synthetic integration demo`, syncs its generated Markdown and asks a real source-filtered question. It retains the demo notebook so you can inspect it. Errors exit nonzero. A successful request still needs answer-quality review; offline tests cannot establish Google's retrieval quality.

In restricted cloud networking, allow `notebooklm.google.com` and `accounts.google.com` for API/auth traffic. Signing in through your usual browser may also require the specific Google static/redirect hosts it reports. Existing package-registry rules must remain. This environment's initial live attempt was blocked by missing browser auth and an HTTP CONNECT 403 for NotebookLM; see [validation](docs/validation.md).

## MCP with Codex and OpenCode

Use the examples in [config/](config/) and the short [agent instructions](docs/agent-instructions.md). Replace checkout/database paths as appropriate; do not overwrite your existing client configuration.

```bash
codex mcp add memory -- /workspace/Memory/.venv/bin/ai-memory \
  --db /workspace/Memory/data/conversations.sqlite mcp
```

Tools: `memory_status` reports archive/sync/connectivity state; `memory_ask` asks NotebookLM using only committed archive sources. The gateway returns conversation/message IDs only when exact citation headings can be verified. Stale or uncertain citations keep source-level references. Historical instructions remain historical context; current user instructions take priority.

## Web capture and background collection

Load `vendor/hstry/extension` as an unpacked Chrome/Brave extension. From the project folder, run:

```bash
.venv/bin/ai-memory connect
```

Keep the terminal open. The command creates/reuses private connection files and starts the local receiver. Open extension Settings → **Choose connection file**, then select `data/extension-connection.json` (or the path printed for your selected archive). Connection status verifies the bearer token and application identity; public health alone cannot show Connected. When reusing an occupied port, the CLI also verifies the exact archive identity and refuses another database.

Enable the desired ChatGPT, Claude or Gemini providers. All are off initially. The category checklist defaults to **Projects & folders**, **Pinned & favorites**, and **GPTs & agents** on, with **Ordinary Recents** off. Organized chats can appear in Recents and still match their selected category; Recents-only does not override unchecked organized categories. Unknown membership stays out unless Recents is explicitly enabled. Existing archived history is retained. Codex/OpenCode local collection is independent of these browser filters.

ChatGPT/Claude project discovery and native pin/agent metadata are supported where their private APIs expose them. Gemini's verified schema has no dependable organized-membership fields: it skips with a warning unless ordinary Recents are enabled. Selected detail failures produce an incomplete-capture warning and preserve retry cursors. Changing capture selection, provider enablement or connection settings invalidates active runs before subsequent batches are sent; already accepted batches remain in the archive. No-category runs are rejected. These guarantees are tested with synthetic service fixtures, not live signed-in accounts.

Advanced settings retain manual port/token entry, interval and a browser-profile namespace (`browser-local` by default). Use a distinct namespace for each profile; native ChatGPT/Claude account IDs take precedence. Existing `serve --token-file` remains available. The server binds loopback, checks Host/Origin and bearer credentials, bounds bodies, and exposes public `/health`, authenticated `/auth/check`, and `/ingest`. Web capture does not grant NotebookLM upload consent. See the [Mongolian setup guide](docs/extension-setup.mn.md) and [current handoff status](docs/extension-handoff.md).

```bash
.venv/bin/ai-memory watch --codex-path ~/.codex/sessions \
  --opencode-path ~/.local/share/opencode
```

The optional worker scans every 30 seconds and batches new cloud sync every 12 hours. Pending jobs are revisited during scans with their persisted retry backoff; high CPU load postpones sync. It does not implicitly approve new histories. There is no continuously running browser. Adapt [the user systemd example](config/ai-memory.service.example) for your Arch Linux paths; automatic service installation is not performed.

## Reliability and scope

Segments target 150,000 words, with a 450,000-word merge ceiling and at most 49 active sources plus one replacement slot. Sealed segments stay stable unless their archived messages are edited. Continuations append to the current segment. A SQLite queue persists upload/reconciliation/readiness/cleanup stages with backoff. New sources must be ready before their predecessors are deleted. Overflow merges adjacent old segments without rewriting text; exhausted capacity pauses instead of summarizing or creating another notebook.

The current implementation supports Codex and OpenCode local adapters plus the three web providers. Other local agents, full historical web coverage, long-running Google auth stability, 150k/300k/450k retrieval benchmarks, and real Mongolian retrieval quality remain unverified. See [donor audit](docs/donor-audit.md), [validation](docs/validation.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
