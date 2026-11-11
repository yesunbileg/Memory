# Extension status — version 0.5.1

Development resumed after the earlier 0.5.0 snapshot. The four recorded follow-up defects are now covered by regressions and fixed.

## Fixed after resuming

1. The authenticated receiver probe now returns an opaque archive identity. `connect --db` refuses to reuse a receiver serving a different SQLite file, even with the same directory/token. Filesystem paths are not returned by the HTTP probe.
2. Active capture snapshots are invalidated when relevant settings change. Before each subsequent archive batch and before committing a successful run/cursor, the worker rereads current settings. Revoked runs are cancelled and cannot record an old-policy success. Batches already accepted by the receiver remain archived.
3. Selected detail-request failures in ChatGPT, Claude and Gemini produce safe incomplete-capture warnings and preserve retry watermarks. Gemini keeps failure status across continuation chunks.
4. Both manual capture and provider rescan reject an empty category checklist before authentication/provider reads. The UI already disables those actions.

The 0.5.0 improvements remain: responsive light/dark settings and popup; original logo/icons; guided private connection-file import; automatic local receiver startup with `ai-memory connect`; token-authenticated connection state; provider/category checklists; ordinary Recents off by default; verified-metadata project/pin/agent capture; local Codex collection unchanged.

## Verification

- Full Python suite: **119 passed**.
- All **13 Node extension test scripts** passed, including revocation, empty checklist, partial provider failure, settings migration and UI auth-race regressions.
- Real Chromium UI harness passed file import, incorrect token/host handling, checklist persistence, actual local HTTP authentication, light/dark and mobile rendering, popup and no horizontal overflow. This is an HTTP UI harness, not an installed-extension/live-account test.
- Frozen dependency sync, wheel build/resource inspection, Ruff and whitespace checks passed.
- Independent focused review found no Important issues in the four fixes; additional synthetic unchanged-settings and Gemini cross-chunk failure checks passed.

## Remaining external/coverage limits

Gemini organized metadata remains unsupported in the evidenced private schema: it skips with a warning unless Ordinary Recents is explicitly enabled. Pin/agent capture requires native metadata. Real signed-in provider APIs and installed-extension behavior still need verification; this cloud Chromium blocks unpacked extensions by administrator policy. Google NotebookLM browser authentication/network prerequisites described in validation.md remain unresolved.

## Update and connect

From the project folder: `git pull --ff-only`, then `uv sync --frozen`. Stop the old receiver with Ctrl+C and run `.venv/bin/ai-memory connect`. Keep its terminal open. Reload AI Memory at `brave://extensions` or `chrome://extensions`. Open Settings → Choose connection file → select `data/extension-connection.json` or the private file path printed by the command. Enable providers and select projects/pinned/agents; leave Ordinary Recents unchecked. See [the Mongolian setup guide](extension-setup.mn.md). Existing transcripts and approval settings are retained.
