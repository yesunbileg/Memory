# Current verification — 2026-11-11

This record covers the final cleanup and integration of the reviewed local
changes. Previous results are in [archive/](archive/). The runtime architecture
remains SQLite → lossless Markdown → NotebookLM → two MCP tools.

## Automated checks

- Final cleanup suite: **189 Python tests passed in 39.72s**, including actual hstry
  Codex/OpenCode parsers, HTTP ingest, SQLite transactions, partial snapshots,
  merge/retry recovery and real stdio MCP.
- **14 Node extension scripts passed** through the built-in Node test runner.
- Ruff lint and Python formatting pass. Prettier checks pass for all selected
  extension assets and development JSON; original upstream TypeScript formatting
  is retained. Frozen uv sync and npm installation pass.
- The uploaded local XML filter reproduced two losses of visible user text.
  Regressions now preserve mixed/quoted wrappers and multipart visible text.
  Only one complete reserved Codex user-context envelope is excluded.
- Direct Codex directory reads merge overlapping snapshots without revision
  churn and retry individual rollouts after batch serialization overflow.
  Archive imports still isolate malformed files and retain successful checkpoints.
  Shared discovery rejects source-root symlinks while incremental imports retain
  their existing support for deeply nested explicitly selected histories.

## Packaging and UI

- Extension **0.5.3**: all UI/local-auth harness checks pass in an isolated
  installed Chromium context, including file import, wrong-token/host handling,
  category persistence, Gemini warnings, light/dark pages and mobile overflow.
  The pages were also visually inspected. This does not install the extension.
- The ZIP contains **21 public runtime/license files**. Two independent builds
  produce SHA-256 `c35db42bce7e0553f53677b389bd6e8333356347296686a76aedfdd85a5a661e`.
- The wheel builds successfully with **29 files**, including both Node adapters
  and their license/provenance, excluding browser assets and extension tests.
  A fresh install outside the checkout runs the real-parser synthetic demo
  (2 conversations, 5 messages, 1 segment) and both tools over real stdio MCP.
- Source-checkout demo and MCP smoke checks also pass. Local documentation links,
  JSON/TOML examples, workflow YAML, upstream modified flags and whitespace pass.
- A GitHub Actions workflow now defines offline checks and builds. Its remote
  result is separate from these locally observed results.

Verified tools: Python 3.12.14, Node 24.22.1, uv 0.13.5, Ruff 0.17.4,
notebooklm-py 0.8.6 and MCP SDK 1.30.0. Review was performed by the author without
subagents, as requested. An AST comparison confirms formatting-only Python files
retain their behavior; changed collector/import and context behavior has focused
regression coverage.

## Client configuration

The Codex example uses valid TOML and current tool approval fields. The
OpenCode 2 example uses `mcp.servers`, matching OpenCode 2.2.7's upstream CLI and
schema types. A separate legacy example retains the flat `mcp` map. Both use
one archive and the installed `ai-memory ... mcp` command. A real OpenCode
agent session has not been run here.

## Local checks still needed

- Capture from signed-in ChatGPT/Claude accounts and confirm selected
  projects/pinned/agent chats are saved while ordinary Recents stay excluded.
- Import the existing Brave/Chrome/Firefox NotebookLM session, then test real
  upload, source readiness, retrieval and answer quality with synthetic data.
- Confirm installed-extension behavior and unattended Codex/OpenCode calls on
  the user's machine.

This cloud executor has no usable Google browser session. Its earlier live
NotebookLM request failed authentication and network access was denied with
proxy CONNECT 403. Chromium administrator policy also blocks installing an
unpacked extension. The UI harness can test pages and local auth but does not
establish live provider API coverage. No personal browser profile was opened
and no personal conversations were uploaded for these checks.

Gemini organized membership, Claude Code/Gemini CLI adapters, long-term auth
stability, real-account quotas, large-source performance and Mongolian retrieval
quality remain outside the verified scope. Follow the [local test guide](local-testing.mn.md).
Conflicting snapshots without reliable source timestamps cannot always be
ordered with certainty; filesystem timestamps are only a fallback.
