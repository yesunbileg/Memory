# Local patch record

Upstream: <https://github.com/byteowlz/hstry>

Commit: `b8719cf630ec15a8ef6b9fa6f1a629be9aa53fbf`

License: MIT; [LICENSE](LICENSE) is unchanged.

[UPSTREAM.json](UPSTREAM.json) lists the upstream file hashes and modified files.
[The donor audit](../../docs/donor-audit.md) describes every behavior change and
the tested boundary. The retained files are an intentionally small subset of
hstry; they do not include its Rust services or search/memory implementation.

Patch history:

1. Node 24 runtime and read-only `node:sqlite`; strict UTF-8/Unicode validation.
2. Exact text, stable source IDs, counted Codex mirror reconciliation, source
   ordering, and no unrelated personal-home metadata reads.
3. Visible-text filtering and explicit UI-only thinking summaries; no hidden
   reasoning, tool/system output, subagent traffic, or synthesized text.
4. Three opt-in browser providers, required local token, browser account
   namespace, no `/sources` endpoint dependency, and accurate unknown metadata.
5. Local synthetic regression tests and adapted retained upstream tests.
6. Browser-only capture categories with ordinary Recents excluded by default;
   source-backed ChatGPT/Claude project discovery and pagination, explicit pin/
   agent metadata, overlap deduplication, selection counts and fail-closed
   metadata warnings. Gemini organized membership remains unsupported without
   a verified schema. Existing capture fixtures explicitly select Recents.
7. OpenCode JSON session-file entrypoints reuse the native new/legacy readers
   with strict UTF-8, allowing per-session failure isolation and retries. SQLite
   remains preferred at a source root; mixed JSON layouts prefer new duplicates.
8. Extension 0.5.2 explains unsupported Gemini organized selection before the
   first capture without enabling Recents. A stdlib build script packages public
   runtime assets plus this license, excluding tests and connection/auth data.
9. Extension 0.5.3 cleanup standardizes JS/HTML/CSS/JSON with Prettier,
   preserving capture behavior. Original TypeScript adapter formatting is kept.
   `UPSTREAM.json` modified flags now reflect actual file hashes, including
   earlier local UI edits. Wheels ship adapters/license/provenance only; the
   extension has its own runtime ZIP.
10. Explicitly hidden Codex message/event payloads are rejected before mirror
    matching. Browser checkpoints use the authenticated archive and provider
    identity in addition to the selection policy. Gemini reports incomplete RPC
    discovery/detail and keeps its old watermark instead of completing silently.
    The local transport bounds serialized UTF-8 batches and splits long
    conversations only at stable message boundaries.
11. OpenCode preserves native message and part modification times and bounds
    SQLite session batches. Codex passes proven mirror provenance to archive
    identity reconciliation and no longer treats plain heading-like user text
    as bootstrap instructions. Incremental Codex imports can refresh matching
    titles directly from the selected source's index without reparsing rollouts.
12. Codex title shortening counts Unicode code points, whole envelopes and source
    flags control visibility, and per-session index lookups include model-only rows.
    OpenCode JSON layouts use one parent-checked reader. SQLite v1/v2 page through
    one read snapshot, refuse crossed parents, and keep native visibility flags.
    Explicit roots and dependency reads refuse symlinks.
13. The browser extension uses a registered local Native Messaging host.
    Token/HTTP notes above describe older versions, not the current transport.
    ChatGPT and Claude share account-bound capture startup; the selection and
    supported provider capabilities are unchanged.
14. Full Codex scans visit each native sessions subtree once. OpenCode JSON
    and v1 SQLite use one ordered part projection. Incremental JSON checkpoints
    cover the native session, messages and parts before skipping an unchanged
    session. The normalized text, IDs, order and edit clocks are unchanged.
