# Extension handoff — 2026-11-11

Work stopped at the user’s request to conserve usage. This is an implementation snapshot, not a production-readiness claim.

Implemented: version0.5.0 Apple-inspired light/dark responsive options and popup; original logo and manifest PNGs; guided connection-file import and manual fallback; ai-memory connect with private token/config and server startup; authenticated POST /auth/check instead of health-only Connected; provider/category checklists; ordinary Recents excluded by default; ChatGPT/Claude project discovery and native pinned/GPT metadata filtering; policy reset and skipped/unknown warnings; unchanged local Codex collector. Existing archives are not deleted.

Verified during implementation: 117 Python tests and 11 Node scripts passed before the last UI regression additions; real Chromium HTTP UI harness passed file import, real local auth, invalid token/file handling, checklist persistence, light/dark/mobile rendering, popup and no horizontal overflow. New options auth-race regression passed. This harness is not an installed-extension/live-account test.

Known important follow-up findings still outstanding when stopped:
1. Reusing a running receiver checks service/token but does not distinguish two database files in the same directory. Use a separate data directory per archive until fixed.
2. Deselecting a provider/category during an active capture may allow its already-running batches to continue under the previous selection. Finish/stop capture before changing selection until guards are added.
3. Selected detail-request failures can preserve retry cursors yet still omit a partial-failure warning. Do not infer completeness from Up to date alone.
4. Empty-category rescan controls are disabled in UI, but worker-level rejection still needs verification.

Gemini organized metadata is unsupported in the evidenced private schema: it skips with a warning unless Ordinary Recents is explicitly enabled. Pin capture depends on native metadata being exposed. Real website API/account testing and installed-extension validation remain outstanding. Google NotebookLM auth/network blockers documented in validation.md still apply.

Update locally: git pull --ff-only; uv sync --frozen. Stop the old serve process, run .venv/bin/ai-memory connect, keep terminal open. Reload the unpacked extension at brave://extensions or chrome://extensions. Open its settings and choose data/extension-connection.json. Enable providers and select projects/pinned/agents; leave Ordinary Recents unchecked.

Tests: .venv/bin/pytest -q; run every vendor/hstry/extension/test/*.js with Node. Optional browser harness: UV_CACHE_DIR=/workspace/.cache/uv uv run --with playwright python scripts/extension_ui_check.py.
