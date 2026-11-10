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

Authenticate with **your Google browser session**, using notebooklm-py's supported login flow. A service-account credential does not authenticate NotebookLM. Keep the browser state private and outside version control.

```bash
# Only needed to log in; no browser stays running during normal sync or MCP use.
uv pip install --python .venv/bin/python 'notebooklm-py[browser]==0.8.6'
.venv/bin/playwright install chromium

# Cloud home is read-only, so use this writable application directory.
export NOTEBOOKLM_HOME=/workspace/Memory/data/notebooklm
.venv/bin/notebooklm login
.venv/bin/notebooklm create 'AI Memory'
# Copy the notebook ID printed by the command:
.venv/bin/ai-memory init --notebook-id YOUR_NOTEBOOK_ID
.venv/bin/ai-memory check
.venv/bin/ai-memory sync
.venv/bin/ai-memory ask 'Өмнө ямар шийдвэр гаргасан бэ?'
```

If your authentication state lives elsewhere, use `init --storage /private/path/storage_state.json`. To test against Google using **only synthetic data** in a dedicated demo notebook:

```bash
NOTEBOOKLM_HOME=/workspace/Memory/data/notebooklm .venv/bin/python scripts/live_demo.py
```

This live demo creates or reuses `AI Memory — synthetic integration demo`, syncs its generated Markdown and asks a real source-filtered question. It retains the demo notebook so you can inspect it. Errors exit nonzero. A successful request still needs answer-quality review; offline tests cannot establish Google's retrieval quality.

In restricted cloud networking, allow `notebooklm.google.com` and `accounts.google.com` for API/auth traffic. Browser login may also require the specific Google static/redirect hosts it reports. Existing package-registry rules must remain. This environment's initial live attempt was blocked by missing browser auth and an HTTP CONNECT 403 for NotebookLM; see [validation](docs/validation.md).

## MCP with Codex and OpenCode

Use the examples in [config/](config/) and the short [agent instructions](docs/agent-instructions.md). Replace checkout/database paths as appropriate; do not overwrite your existing client configuration.

```bash
codex mcp add memory -- /workspace/Memory/.venv/bin/ai-memory \
  --db /workspace/Memory/data/conversations.sqlite mcp
```

Tools: `memory_status` reports archive/sync/connectivity state; `memory_ask` asks NotebookLM using only committed archive sources. The gateway returns conversation/message IDs only when exact citation headings can be verified. Stale or uncertain citations keep source-level references. Historical instructions remain historical context; current user instructions take priority.

## Web capture and background collection

Load `vendor/hstry/extension` as an unpacked Chromium extension. It supports ChatGPT, Claude and Gemini, with all providers disabled initially. In extension options, choose the desired providers, set the local ingest token, and give each browser profile a stable account namespace (default `browser-local`). ChatGPT/Claude native account or organization IDs override that namespace when available. The extension reads conversations using your signed-in browser; enable it only for chats you intend to archive. Web captures arrive without Google upload consent.

```bash
# Create a private token file without displaying its contents.
mkdir -p data
.venv/bin/python -c 'import os,secrets; p="data/ingest.token"; fd=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(fd,secrets.token_urlsafe(32).encode()); os.close(fd)'
.venv/bin/ai-memory serve --token-file data/ingest.token
```

The ingest server binds `127.0.0.1:41770`, requires bearer authentication, validates browser origins/Host, bounds request bodies, and exposes `/health` and `/ingest`. Enter the same private token directly into the extension options; never paste it into chat or commit it.

```bash
.venv/bin/ai-memory watch --codex-path ~/.codex/sessions \
  --opencode-path ~/.local/share/opencode
```

The optional worker scans every 30 seconds and batches new cloud sync every 12 hours. Pending jobs are revisited during scans with their persisted retry backoff; high CPU load postpones sync. It does not implicitly approve new histories. There is no continuously running browser. Adapt [the user systemd example](config/ai-memory.service.example) for your Arch Linux paths; automatic service installation is not performed.

## Reliability and scope

Segments target 150,000 words, with a 450,000-word merge ceiling and at most 49 active sources plus one replacement slot. Sealed segments stay stable unless their archived messages are edited. Continuations append to the current segment. A SQLite queue persists upload/reconciliation/readiness/cleanup stages with backoff. New sources must be ready before their predecessors are deleted. Overflow merges adjacent old segments without rewriting text; exhausted capacity pauses instead of summarizing or creating another notebook.

The current implementation supports Codex and OpenCode local adapters plus the three web providers. Other local agents, full historical web coverage, long-running Google auth stability, 150k/300k/450k retrieval benchmarks, and real Mongolian retrieval quality remain unverified. See [donor audit](docs/donor-audit.md), [validation](docs/validation.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
