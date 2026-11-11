# Friendly extension and selective capture implementation plan

> Execute tasks with test-first changes and an independent final review. Use the existing isolated cloud checkout; no extra worktree is needed.

**Goal:** Make connection understandable, add identity/icons, and expose a capture checklist that excludes plain Recents by default.
**Spec:** ../specs/2026-11-11-extension-design.md
**Stack:** Existing Python CLI/HTTP receiver and dependency-free MV3 HTML/CSS/JS.

## Shared interfaces

- `captureTypes`: projects/pinned/agents defaults true; recents default false. Stored under settings and passed to each provider's sync function. Provider classification belongs in providers/selection.js.
- `POST /auth/check`: empty JSON body allowed, same host/origin/bearer boundary as ingest, returns {status:'ok',service:'ai-memory',schema:1}; no transcript writes. Legacy /health remains public but does not prove authentication.
- Connection file: `{schema:1, application:'ai-memory', host:'127.0.0.1', port:41770, token:'private-generated-token'}`. Import accepts this exact application/schema/local host, validated port and bounded token. Never display or log raw token.
- Worker `checkApi`: returns `{ok,url,code?,error?}` with actionable codes missing_token/unauthorized/offline/incompatible_server. `settingsChanged` preserves user selections; capture-policy changes reset prior incremental state safely.

## Tasks

- [x] Provider boundary: add failing tests for default Recents exclusion, explicit categories, overlap and selection changes. Implement shared classifier and provider-specific collection/discovery; preserve text, timestamps, native IDs and account isolation. Document opaque API limits.
- [x] Connection boundary: add auth probe and `connect` CLI regression tests, implement private repeatable connection files and server startup, update worker auth checks and category pass-through/reset. Preserve existing serve and tests.
- [x] UI and assets: add original logo/manifest PNGs; replace options/popup with guided connection, file import, advanced fallback, provider toggles, category checklist, progress and helpful errors. Test settings migration and file validation before implementation; use a real-browser harness for interactions/screenshots.
- [x] Integrate: run full suites and install/build checks; read-only independent code review; regression-fix important findings; update README and validation, then commit and push the concrete verified change.

## Review focus

Unknown/opaque metadata must not broaden capture; a visible project can also occur in Recents. Old settings must not preserve legacy capture-all behavior. Public health cannot establish auth. Connection JSON must not enable remote token exfiltration. No existing archive, token file or local collector configuration may be overwritten silently. Generated secrets must stay out of git, stdout, screenshots and tests' failure output.

Completion follow-up: 0.5.1 resolves the four interruption handoff findings. Independent focused review reported no Important findings. Full evidence:119 Python tests,13 Node scripts, Chromium UI+local-auth harness, frozen install, wheel build/resources and lint passed. Live-account/installed-extension constraints remain in docs/extension-handoff.md.
