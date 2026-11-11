# Extension setup and selective capture

The user requested an iPhone-like friendly extension, a logo, and a checklist that excludes ordinary Recents by default while allowing projects, pinned/favorite chats, and GPT/agent conversations. Preserve existing archive text and local Codex collection. No deletion of previously archived chats is authorized.

## Chosen approach

Keep the existing dependency-free Manifest V3 extension and Python receiver. Replace the exposed technical form with a guided connection card, private connection-file import, platform toggles, category checkboxes, and a compact matching popup. Advanced manual port/token/profile/interval controls remain available behind a disclosure. A full frontend framework or browser cookie extension permissions would add unnecessary cost; no such dependency is needed.

The extension must not say Connected based only on public /health. POST /auth/check validates the same bearer token used by /ingest and never archives data. `ai-memory connect` creates/reuses a private ingest token, writes a private connection JSON beside the selected archive, displays the file path (never token), and starts the receiver. The extension imports only validated loopback configuration, then verifies credentials. Existing `serve --token-file` remains supported.

## Capture contract

Settings add captureTypes: {projects:true,pinned:true,agents:true,recents:false}. Missing old settings migrate to those defaults. Every provider sync receives this selection. A shared provider helper classifies explicit source metadata only, including project/folder identifiers, pinned/starred/favorite booleans, GPT/gizmo/agent identifiers. Display position in Recents is not membership evidence: a pinned/project/agent chat is eligible even if also listed in Recents. An ordinary unclassified conversation is skipped unless recents is explicitly enabled. Missing metadata is never evidence that the user opted into a conversation.

Provider-specific discovery should enumerate project collections and pinned lists only where the upstream source has a known supported interface; private API variants must be documented as best-effort and tested with realistic fixtures. Gemini's opaque tuple positions must not be invented. Report skipped/unknown counts and unsupported metadata clearly. A changed capture selection invalidates legacy watermarks so newly eligible old conversations are considered. No branch may fall back to capturing everything when a discovery endpoint fails.

This filter concerns browser providers (ChatGPT, Claude, Gemini). Codex CLI remains a separate local collector and is identified as such in the UI. Browser Codex tasks are not silently represented as supported ChatGPT conversations.

## Visual direction

Use a light, spacious Apple-inspired settings layout with rounded grouped cards, system typography, restrained blue accent, large controls, clear status text, dark-mode support and responsive widths. Use an original memory/app icon, with real manifest PNG sizes and a reusable source asset. Preserve keyboard operation, visible focus, labels, live feedback and reduced-motion preferences. Show only task-relevant concepts; paths and token mechanics belong in help/advanced steps.

## Verification

Test auth failures, token redaction, file permissions, connection import validation and tampered hosts. Test each category independently, ordinary recents exclusion, overlapping categories, selection changes and unknown metadata, including Claude project/pinned and existing Gemini continuation behavior. Render real UI in Chromium via an isolated test harness if extension installation remains blocked by administrator policy; distinguish that from a genuine installed-extension/live-provider test. Run full Python and all extension Node suites, package build, a fresh review, and commit/push the reviewed change under the user's existing authorization.
