# Donor audit

AI Memory reuses the format parsers and browser capture code from
[byteowlz/hstry](https://github.com/byteowlz/hstry), pinned to commit
`b8719cf630ec15a8ef6b9fa6f1a629be9aa53fbf` (MIT, copyright 2025 byteowlz).
[The upstream license](../vendor/hstry/LICENSE) is retained verbatim.
[UPSTREAM.json](../vendor/hstry/UPSTREAM.json) records each copied file's upstream
SHA-256 and whether it was modified. Newly written boundary tests are local code.

## Included components

| Component | Purpose |
| --- | --- |
| `adapters/codex/adapter.ts` | Codex rollout JSONL and Zstandard files; optional colocated SQLite thread titles |
| `adapters/opencode/adapter.ts` | OpenCode legacy/new JSON storage and SQLite v1/v2 |
| `adapters/types/{index,first-message}.ts` | Original hstry request protocol, canonical parts, and title helpers |
| `extension/` | Original Manifest V3 worker, settings, popup, provider code and selected synthetic tests; supported providers are ChatGPT, Claude, Gemini |

No hstry Rust daemon, search index, FTS retrieval, TUI, hstry MCP server, or other
provider adapter is included. Running the full project would add unrelated search
services and dependencies. AI Memory uses SQLite as an archive and NotebookLM as
its only AI retrieval engine. No donor's smart-memory/summary/embedding system is
used. This boundary does not summarize or translate conversations.

## Local changes

The parsers execute directly through Node 24's TypeScript support. Their SQLite
opener uses read-only `node:sqlite` in place of Bun or `better-sqlite3`; no bundler,
native addon, or Rust runtime is required. The Python bridge still invokes the
upstream adapter request/response entry point, so tests execute the actual format
parsers rather than a second Python parser.

Raw text is never trimmed or normalized. Consecutive text parts concatenate in
their original order without inserted separators, image markers, or code fences.
Code fences already present in a source remain exact. Invalid UTF-8, unpaired
surrogates, malformed JSON records, and corrupt source parts fail the import
instead of replacing characters or silently reporting an incomplete transcript.
A file being written can be retried by the watcher after the next poll.

Codex messages retain native IDs or original JSONL record positions. Mirrored
`event_msg`/`response_item` messages reconcile one-to-one, preserving legitimate
identical replies. Source record order wins over unreliable clocks. Commentary
maps to visible progress; analysis channels, private subagent sessions,
inter-agent routing, tool/system traffic, and compaction summaries are excluded.
Known bootstrap wrappers are filtered only at the start of a message; a user's
ordinary question about `AGENTS.md` is retained. An explicitly selected file/tree
does not inspect an unrelated default Codex home or its SQLite index.

OpenCode retains message IDs, uses v2's native `seq` order, and excludes synthetic,
ignored, hidden, tool, and system content. Unmarked reasoning never enters the
archive. Thinking summaries require explicit `visibility: ui`; hidden reasoning
is not made visible because it happens to contain readable text.

Web providers retain message UUIDs/IDs and Gemini turn IDs where the response
provides them, with original record positions as the fallback. ChatGPT no longer
recovers arbitrary strings from metadata, hidden messages, or tool recipients.
Claude keeps text blocks and explicitly marked visible summaries; tool blocks
and private thinking are omitted. Gemini no longer assigns the conversation
update timestamp to every message or invents a model name. Unknown timestamps
and models remain unknown. Known timestamps convert from hstry milliseconds to
UTC ISO 8601 strings at the Python boundary.

The extension sends the original hstry
`{source, adapter, conversations}` packet, with an optional local `account`
namespace, to `http://127.0.0.1:<port>/ingest`. `/sources` registration is a local
no-op because this receiver exposes only health and ingest, plus the necessary
ingest CORS preflight. All providers start disabled. The configured ingest token
is required before any provider fetch. The namespace distinguishes Gemini
accounts/browser profiles; ChatGPT account IDs and Claude organization IDs take
precedence. Capture adds no Google upload consent.

The receiver requires constant-time Bearer authentication, checks Host/Origin,
permits recognized extension and exact loopback origins without wildcard CORS,
bounds input to 16 MiB/100 conversations, and validates the complete packet before
writing. Repeated native message IDs update existing records. Partial snapshots
retain absent archive history, preserve known sequences, and append newly seen
IDs. No search, dashboard, transcript-read, cloud-upload, or source-deletion API
is exposed.

## Validation and limits

Synthetic fixtures exercise multilingual whitespace, code, multipart text,
mirrored events, repeated replies, stable IDs, hidden reasoning, synthetic
messages, out-of-order clocks, all supported OpenCode schemas, strict source
decoding, native account isolation, edits, partial snapshots, required token,
body bounds, CORS, DNS-rebinding Host checks, and real provider → loopback HTTP →
SQLite roundtrips. Selected upstream Node tests also cover backoff, inaccessible
Claude organizations, Gemini continuation, and settings initialization.

These tests stub provider network responses. They prove local parsing and ingest
behavior, not that today's private web APIs still match those fixtures. No live
ChatGPT/Claude/Gemini capture was performed: that needs a user-controlled logged-in
browser. Gemini's undocumented RPC schema can change. Native IDs are preferred;
when a provider supplies none, position-based identity cannot distinguish a
provider reordering old records from an edit. No completeness claim covers
attachments, images, private thoughts, or tool execution output.
