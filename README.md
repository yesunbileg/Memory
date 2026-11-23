# AI Memory

A lightweight archive for AI conversations, built from the [supplied architecture](docs/specification.mn.md).

Локалд шинэчлэх, extension суулгах, Google login болон шалгалтын дараалал: [Монгол заавар](docs/local-testing.mn.md).

Codex / OpenCode / web chats → hstry text collectors → SQLite → Markdown segments → **NotebookLM** → MCP.

SQLite stores original text and operational state. NotebookLM is the only AI memory engine. There is no local LLM, vector database, automatic summary, or paid API dependency. notebooklm-py uses Google's unofficial API; availability and free-account limits depend on Google.

## Install and run the local demo

Requires Python 3.12+, Node.js 24+, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yesunbileg/Memory.git
cd Memory
uv sync --frozen
.venv/bin/ai-memory demo
.venv/bin/python scripts/mcp_smoke.py
.venv/bin/python scripts/check.py
```

For an existing checkout, update it with `git pull --ff-only` after saving local
changes. See [development](docs/development.md) for formatting and tests. Managed
cloud executor setup is separate and documented in [config/cloud-start.md](config/cloud-start.md).

The demo imports **synthetic** Codex and OpenCode histories through the actual donor adapters. It creates `data/demo/conversations.sqlite` and `data/demo/segments/SEG-001.md`, prints conversation IDs and process resource measurements, and can be repeated without duplicate messages. A changed fixture, a symlink or non-demo archive contents are not overwritten; choose a new empty directory instead. No data is sent to Google. MCP smoke testing starts a real stdio server and calls `memory_status`; it does not invent a NotebookLM answer.

## Terminal interface

From the checkout, run `uv run mem.py`. To make `mem` available from any
directory on Linux, link the launcher into your user bin directory.

```bash
mkdir -p ~/.local/bin
ln -s "$(realpath scripts/mem)" ~/.local/bin/mem
mem
```

The TUI uses `data/conversations.sqlite` in the checkout. Use `--db PATH` for a
different archive. Press `Ctrl+P` for commands or open the **Урсгал** tab to see
how capture, NotebookLM sync and questions fit together.

For first-time setup, sign into NotebookLM in Brave, then use `/setup` to
create a notebook or paste the link or ID of one you already use. To renew
access, open `/connect` and choose **Нэвтрэх**. The form accepts a Brave profile
and a Google email when several accounts share that profile. The connection
screen shows the verified notebook, account and access when available.
`/login Profile 1` is also available for direct login with that profile.

The extension's **Sync now** saves selected web chats in the local archive.
Run `/sync-all` in `mem` to sync every chat in that archive to the configured
NotebookLM notebook. The command starts immediately and shows progress through
upload, readiness and cleanup. From the CLI, use `ai-memory sync --all-chats`.
Use `/auto on` to sync new and changed archived chats in the background while
`mem` is open. The preference is saved per archive. `/auto` toggles it, and
`/auto off` stops it. During an automatic sync, `/stop` also turns it off.
Type a question and press Enter for NotebookLM's reply. Use `/chats` to browse
the local archive.

## Import existing histories once

```bash
.venv/bin/ai-memory init
.venv/bin/ai-memory collect codex ~/.codex/sessions
.venv/bin/ai-memory collect opencode ~/.local/share/opencode
.venv/bin/ai-memory status
.venv/bin/ai-memory show CX-REPLACE_WITH_RETURNED_ID
.venv/bin/ai-memory preview CX-REPLACE_WITH_RETURNED_ID
```

The default archive is `data/conversations.sqlite`. Use `--db PATH` **before** the subcommand to select another archive. Import prints the conversation IDs, and `preview ID` shows the masked text. Before upload, recognized API keys, password/token assignments, cookies, JWTs and private-key payloads are replaced with the same number of `*` characters. Surrounding text and line breaks are preserved. SQLite and local Markdown exports retain the originals. The upload gate blocks residual secrets and recognized quoted values whose boundaries cannot be determined. Exports and snapshots use private temporary files. Detection cannot identify every possible secret.

The CLI also supports syncing a subset through its per-chat consent settings. Running `/sync-all` includes the entire local archive. See [consent, backup and restore](docs/consent-backup.md).

Incremental scans checkpoint the selected path's file inventory. Codex and OpenCode JSON sessions are imported independently: a broken session remains retryable while healthy sessions continue. OpenCode JSON checkpoints also observe message/part changes; SQLite remains preferred when present. Native-ID upserts preserve partial snapshots and reject known older revisions. Direct Codex directory reads reconcile overlapping snapshots and can fall back to individual files if batch serialization overflows. This version does not implement upstream message deletion/tombstones. Collectors retain user text, assistant text/code and explicit visible progress/summary parts; unknown or hidden reasoning, system prompts, tool events, logs and attachments are excluded. A complete Codex user message containing one reserved internal goal wrapper is excluded; mixed or quoted wrappers retain their visible text. Missing timestamps and model names remain unknown.

## Connect NotebookLM

Sign in to NotebookLM in **the Brave, Chrome or Firefox profile you already use**, then import that session with notebooklm-py's supported cookie reader. This does not launch another browser or require a separate Chromium/Playwright installation. A service-account credential does not authenticate NotebookLM. Keep the saved session private and outside version control.

Keep your existing auth location when reauthenticating. Login, sync and MCP
must use the same saved session. Custom locations use `NOTEBOOKLM_HOME` or an
explicit `--storage` file.

```bash
# Install the optional cookie reader; no browser download is needed.
uv sync --frozen --extra login

# Use the same auth directory or --storage path as sync and your MCP server.
.venv/bin/notebooklm login --browser-cookies brave
.venv/bin/notebooklm create 'AI Memory'
# Copy the notebook ID printed by the command:
.venv/bin/ai-memory init --notebook-id YOUR_NOTEBOOK_ID
.venv/bin/ai-memory check
.venv/bin/ai-memory sync
.venv/bin/ai-memory ask 'Өмнө ямар шийдвэр гаргасан бэ?'
```

`ai-memory check` returns zero only when the configured NotebookLM connection
succeeds. Local-only `ai-memory status` does not require authentication and never creates a missing archive.

Replace `brave` with `chrome` or `firefox` for those browsers. For a specific Brave profile, use `--browser-cookies 'brave::Profile 1'`; for multiple signed-in Google accounts, add `--account YOUR_EMAIL`. Run this on the same computer and as the same operating-system user as the signed-in browser; a cloud host cannot read your laptop's browser profile. If cookie extraction fails, check the selected profile and desktop keyring access before retrying.

Use the same `NOTEBOOKLM_HOME` for login, sync, MCP and the demo. If your archive already uses an explicit storage file, pass that file to login with `--storage /private/path/storage_state.json`; configure the archive with `init --storage` only when changing its auth location. Reauthentication does not require a new notebook. To test against Google using **only synthetic data** in a dedicated demo notebook:

```bash
.venv/bin/python scripts/live_demo.py --notebook-id YOUR_EMPTY_DEMO_NOTEBOOK_ID --output data/new-live-demo
```

Create a new empty notebook specifically for the test and copy its ID. The live demo requires an empty local output directory, generates its own fixture, checks the selected notebook is empty, then syncs its Markdown and asks a source-filtered question. It never discovers a notebook by name or adopts an existing archive. It retains the demo notebook so you can inspect it. Errors exit nonzero. A successful request still needs answer-quality review; offline tests cannot establish Google's retrieval quality.

## MCP with Codex and OpenCode

Codex uses **one local plugin** for its Stop hook and the memory MCP server. Nothing is published to a plugin directory. Follow the [local installation instructions](docs/local-hooks.md) on the computer where Codex runs. OpenCode uses its own session-idle integration and [MCP configuration](docs/agent-instructions.md). Both capture only the session that fired the event, into the initialized archive. They do not upload or approve anything. Use the explicit import commands above for older sessions.

Tools: `memory_status` reports archive/sync/connectivity state; `memory_ask` asks NotebookLM using only committed archive sources. The gateway returns verified conversation/message IDs in structured `sources` only when exact citation headings can be verified. IDs in free-form answer text remain unverified. A known source with no verifiable message ID keeps a source-level reference. If a citation is outside the request’s approved source set, the response does not disclose the answer or passage. Historical instructions remain historical context; current user instructions take priority.

## Web capture

Load `vendor/hstry/extension` as an unpacked extension on Linux, using Chrome, Brave or Chromium with Chromium engine version 125 or later. Copy its 32-letter ID from the extensions page. From the project folder, register the selected archive for that ID (replace the placeholders):

```bash
.venv/bin/ai-memory --db data/conversations.sqlite connect --extension-id YOUR_EXTENSION_ID --browser brave
```

For a standalone extension, run `.venv/bin/python scripts/package_extension.py`. Extract the ZIP named by the command’s `output` field into a folder and load that folder as an unpacked extension. The ZIP contains runtime files, icons and both project/upstream MIT notices; connection/auth files and tests are excluded. For local development, loading `vendor/hstry/extension` directly makes updates easier.

Click **AI Memory** in the browser toolbar. On first open, the popup verifies the registered archive and connects automatically. The browser launches the local host as needed. If you registered with an older AI Memory version and the popup is still disconnected, run the command again using the updated local installation, then click **Settings → Check connection**. For a second archive or a custom browser data directory, see [native pairing](docs/native-pairing.md).

Open a signed-in ChatGPT, Claude or Gemini tab and turn on **List mode** in the AI Memory toolbar popup. Checkboxes appear beside supported links on that page. After installing or reloading the extension, refresh any provider tab that was already open. List mode is per tab and starts off on each new document.

| What you do | What happens |
| --- | --- |
| Check a project in the sidebar | Saves the project ID immediately. At sync, its eligible chats are included, even when the project was collapsed or a chat was not visible. Gemini offers chats only. |
| Check the **Recents** heading on ChatGPT | Includes all ordinary Recent chats in that account on each sync, including newly added ones. Pinned, project, GPT, locked and archived chats are excluded from this group. Turning it off keeps individual selections. |
| Check an individual chat | Saves that chat separately. A dash means a selected project already includes it. You can still check it to keep it on the list independently of its project. A chat covered twice is captured once. |
| Uncheck an item or turn off List mode | Unchecking removes that explicit selection. Turning the mode off hides the controls without changing the list. Neither action deletes archived content. |
| Enable the provider and click **Sync now** | Fetches the saved items into the paired **local SQLite archive**. Enabled automatic checks use the same saved list. Choosing an item does not start a sync or approve a Google upload. |

Existing selections keep their account and workspace restrictions on upgrade. In Brave, keep a signed-in provider tab open for each container you want to sync. You can select different chats or projects on each account, then click **Sync now** once. The extension looks for matching signed-in tabs across browser windows and reports when a saved account needs a tab. The popup shows accounts checked, chats checked and chats saved while capture runs. A provider switch alone does not authorize capture. If the account or item cannot be verified, it cannot be added. Sync reports incomplete provider listings rather than treating them as a complete project. An empty list captures nothing.

After the archive acknowledges a ChatGPT or Claude chat, later runs can skip its message body when the provider reports the same update time. New selections, missing update times, and updates from the last five minutes still get checked. **Settings → Sync activity and rescan → Rescan** rereads the saved selection for that provider.

**Stop sync** cancels the current sync and any remaining batches. Chats already saved stay in the local archive. The regular automatic schedule remains in effect unless you pause it in Settings.

Run `/sync-all` in `mem` to send archived chats to NotebookLM. The popup’s **Sync now** saves selected web chats to the local archive. Private provider interfaces can change. Offline verification uses synthetic data. See the [Mongolian setup guide](docs/extension-setup.mn.md).

## Reliability and scope

Segments target 150,000 body words, with a 450,000-body-word merge ceiling and a 500,000-word ceiling on the complete rendered source. At most 49 sources are active, with one replacement slot. A merge that exceeds the rendered ceiling rolls back its candidate and tries a smaller or later adjacent group. Sealed segments stay stable unless their archived messages are edited. Continuations append to the current segment. A SQLite queue persists upload/reconciliation/readiness/cleanup stages with backoff. New sources must be ready before their predecessors are deleted. Exhausted capacity pauses instead of summarizing or creating another notebook.

If a previously published source disappears remotely, sync records the missing source and retrieval excludes it. Re-publication is a separate choice: run `ai-memory republish SEGMENT_ID` for the segment reported missing, then `ai-memory sync`. A normal sync alone does not undo a manual deletion.

Run `ai-memory cleanup-legacy` to review superseded NotebookLM copies from an older archive. To remove specific copies, run `ai-memory cleanup-legacy --delete SOURCE_ID [...]` with the IDs from that preview. The command rechecks the current replacements before deleting.

The current implementation supports Codex and OpenCode local adapters plus the three web providers. Other local agents, full historical web coverage, long-running Google auth stability, 150k/300k/450k retrieval benchmarks, and real Mongolian retrieval quality remain unverified. See [donor audit](docs/donor-audit.md), [validation](docs/validation.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
