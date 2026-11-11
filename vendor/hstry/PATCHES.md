# Local patch record

Upstream: <https://github.com/byteowlz/hstry>

Commit: `b8719cf630ec15a8ef6b9fa6f1a629be9aa53fbf`

License: MIT; [LICENSE](LICENSE) is unchanged.

[UPSTREAM.json](UPSTREAM.json) lists the upstream file hashes and modified files.
[The donor audit](../../docs/donor-audit.md) describes every behavior change and
the tested boundary. The retained files are an intentionally small subset of
hstry; they do not include its Rust services or search/memory implementation.

Patch groups:

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
