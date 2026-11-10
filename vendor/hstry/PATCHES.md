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
