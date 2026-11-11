# Local reliability and extension handoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Finish the two reproduced reliability defects, integrate existing-browser login, and deliver the extension with reproducible local checks.

**Architecture:** Retain SQLite, the vendored hstry adapters, notebooklm-py and the dependency-free Manifest V3 extension. Reuse the OpenCode adapter's single-session reader. Validate merge candidates transactionally against the existing 500,000 rendered-word ceiling.

**Tech Stack:** Python 3.12+, Node 24+, SQLite, plain JavaScript/HTML/CSS.

**Spec:** `docs/specification.mn.md`, `docs/superpowers/specs/2026-11-11-extension-design.md`, and the two reproduced failures in the 2026-11-11 full audit.

## Global constraints

- Preserve original visible text, native IDs, timestamps, existing consent and archived data.
- Keep Ordinary Recents off by default; never broaden capture to compensate for missing category metadata.
- Use one notebook, 49 active sources and one spare; retain existing remote sources until replacements are ready.
- No additional runtime service, local LLM, vector database, framework or custom authentication implementation.
- User will perform signed-in browser and Google testing locally; do not modify personal archives here.

## Review focus

- Changed OpenCode message/part files must invalidate checkpoints even when session JSON is unchanged.
- Broken session metadata, messages, parts and invalid UTF-8 must isolate one JSON session and remain retryable.
- SQLite must retain priority when a directory also contains legacy JSON.
- Failed merge candidates must leave no retired parents, dangling metadata, mappings or job changes; smaller and later candidates must remain eligible.
- Extension packaging must include runtime imports/icons and exclude tests, credentials and connection files; category warnings must preserve explicit user selections.

## Task 1: OpenCode session recovery

**Files:** `src/ai_memory/cli.py`, `vendor/hstry/adapters/opencode/adapter.ts`, `tests/test_opencode_recovery.py`.

**Interfaces:** Preserve `collect(platform, path, account) -> list[dict]` and `collect_into(...) -> dict`. Add support for an explicitly selected OpenCode session JSON file through the existing adapter's `parseSingleSession`. For JSON directories checkpoint sessions separately, using one shared tree fingerprint so part-only edits are observed. Prefer new-layout duplicates and retain SQLite priority.

- [x] Add real-parser regressions for both JSON layouts, session/message/part failure and repair, part-only edits, mixed layouts, and invalid UTF-8.
- [x] Run the new tests and confirm the intended pre-fix failures.
- [x] Implement single-session parsing with strict UTF-8 and per-session import/checkpoint isolation.
- [x] Run collector/CLI/worker tests and the Python suite; commit the fix.

## Task 2: Merge fallback

**Files:** `src/ai_memory/segments.py`, `tests/test_merge_recovery.py`.

**Interfaces:** Preserve `SegmentBuilder.merge_oldest() -> str`. Try the existing oldest adjacent window, then its smaller candidates, then later windows. Use a savepoint per candidate; retire parents only after full rendering and body validation pass.

- [x] Add a failing real 49-source regression with a three-parent oversized render and a feasible pair; assert convergence, readiness/deletion ordering, spare-slot budget and exact retained messages.
- [x] Add a later-window regression and preserve the existing all-unmergeable rollback regression.
- [x] Implement fallback without catching unexpected exceptions as capacity failures.
- [x] Run merge/sync tests and the Python suite; commit the fix.

## Task 3: Extension local handoff

**Files:** `vendor/hstry/extension/ui.js`, `options.js`, `popup.js`, tests, manifest, `scripts/package_extension.py`, `tests/test_extension_package.py`.

**Interfaces:** Extend the shared provider presentation with the provider ID so unsupported Gemini organized capture is explained before a first run. Keep capture policy unchanged. Package a ZIP with `manifest.json` at its root and only runtime files, icons and the upstream license; no browser profile, token, connection file or test fixture.

- [ ] Add an observable UI-state regression before changing the warning presentation.
- [ ] Implement the warning for settings and popup; keep existing selection and authentication tests passing.
- [ ] Add an archive integration regression, implement the stdlib packager, and bump the extension patch version.
- [ ] Run every Node script and the real browser UI/local-auth harness; build and inspect the ZIP.

## Task 4: Local instructions, review and main

**Files:** `docs/local-testing.mn.md`, `README.md`, `docs/validation.md`, `docs/extension-handoff.md`, `vendor/hstry/PATCHES.md`.

- [ ] Document update/install, existing-browser authentication, extension loading, synthetic demo, MCP and optional worker setup using a consistent archive/auth path.
- [ ] Run full Python/Node, Ruff, whitespace, frozen dependencies, wheel/resource/demo/MCP and the two original audit repros.
- [ ] Obtain one fresh whole-branch review and address important findings with focused regressions.
- [ ] Commit verified changes and push them to main under the user's standing authorization; confirm the remote commit and leave the working directory clean.

**Execution:** Inline in the existing cloud checkout. The user explicitly asked to finish autonomously and will perform personal-account tests locally. Existing-browser login is already cherry-picked from `2997ab0` onto the current main base.
