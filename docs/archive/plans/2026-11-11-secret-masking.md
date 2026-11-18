# Secret masking and complete archive verification

**Goal:** Upload the user's Codex/OpenCode chats, including archived chats, while replacing recognized credential values with the same number of `*` characters.

**Architecture:** SQLite retains original text. Cloud snapshots are deterministic masked copies; their filenames hash the actual masked content. Existing sources are replaced only after the masked replacement is ready.

**Requirements:** User requested value-only masking on 2026-11-11. Preserve surrounding text, labels, quotes, line breaks, IDs and citation headings. Keep credential detection as a final upload check. Do not modify browser histories.

- [x] Privacy: regressions for API keys, passwords, bearer credentials, cookies, JWTs, PEM payloads, escaped quotes, short values, expressions and idempotence; implement `redact_sensitive(text)`.
- [x] Sync: mask before snapshot hashing; validate immutable snapshots; migrate pending, uncertain and already mapped legacy sources; preserve old sources until replacement readiness; test restart and cleanup.
- [x] Citations: retain exact heading verification and reject headings quoted inside message bodies.
- [x] Verification: full Python/Node checks, formatter, packages, MCP and real Google sync; independently compare local originals with cloud snapshots without printing secrets.
- [x] Handoff: report current native chat coverage, exclusions, NotebookLM link and web extension connection instructions; leave receiver ready for the user's web test.

Review focus: escaped strings must not leak suffixes; nested cookie/bearer labels must survive; code expressions must remain intact; a legacy upload must not be mistaken for a masked source; retries must retain both source identity and local originals.

Verified 2026-11-12: 233 Python tests and 14 extension tests passed. Real archive sync is up to date (485 conversations, 7,041 messages, seven ready sources); every message heading was checked against Google fulltext. Detailed local report: `data/verification/REPORT.mn.md`.
