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

Codex messages retain native IDs or original JSONL record evidence. Mirrored
`event_msg`/`response_item` messages reconcile one-to-one, passing matched identity
aliases through archive ingestion without deduplicating by reply text. Source
record order wins over unreliable clocks. Partial rollouts retain an order proof
(native IDs, source update clock and a content digest only). Incremental scans
and batch collection place older-only messages at their shared anchors. If
timestamps tie, a deterministic content rank breaks the tie, not filenames or
file mtimes. Without a native edit clock this is a deterministic choice, not proof
of which edit a human made last. Indexed title/model changes update the matching
archive rows without reparsing the rollouts. Commentary
maps to visible progress; analysis channels, private subagent sessions,
inter-agent routing, tool/system traffic, and compaction summaries are excluded.
Supported message and event payloads explicitly marked `visibility: hidden` are
excluded before mirror reconciliation. Visible payload markers are retained.
Only complete reserved bootstrap wrappers are filtered. A plain
`# AGENTS.md` heading alone does not establish hidden context, so ordinary user
messages with that heading are retained. An explicitly selected file or tree
does not inspect an unrelated default Codex home or its SQLite index. A standalone
exported JSONL outside a native sessions tree does not authorize a sibling index.
Selecting the containing directory explicitly does.

OpenCode retains message IDs and available native message or part modification
times, uses v2's native `seq` order, and reads large SQLite archives in bounded
session batches within one read-only SQLite transaction across the entire import.
An explicit `opencode.db` includes its live WAL. Current and legacy JSON layouts
are combined, with current files preferred when session IDs overlap. Session,
message and part IDs/parent links are checked before dependent reads. A symlink
is not an alternative source of transcript content. Per-file archive rows,
revisions, message IDs, sequence evidence and success checkpoint commit together.
A damaged unit remains retryable; independently committed conversation IDs
appear in the import result. It excludes synthetic, ignored, hidden, tool, and system content.
Unmarked reasoning never enters the archive. Thinking summaries require explicit
`visibility: ui`, so hidden reasoning is not exposed for containing readable text.

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
no-op because this receiver exposes health, an authenticated connection probe,
and ingest, plus the necessary CORS preflights. All providers start disabled. The configured ingest token
is required before any provider fetch. The namespace distinguishes Gemini
accounts/browser profiles; ChatGPT account IDs and Claude organization IDs take
precedence. Capture adds no Google upload consent.

The authenticated probe identifies the logical archive with a persistent, opaque
instance ID. Capture checkpoints are reused only for the matching archive, account
namespace, provider principal where known, and selection policy. Older state
without that scope is replayed rather than trusted. The worker binds new ingest
requests to the probed archive to catch a receiver change between probe and send.
A receiver without an instance ID can still answer the low-level connection
probe for compatibility. Popup/Settings checks and capture share the stricter
archive-identity requirement, so a legacy receiver is shown as incompatible before
any provider reads occur.

The receiver requires constant-time Bearer authentication, checks Host/Origin,
permits recognized extension and exact loopback origins without wildcard CORS,
bounds input to 16 MiB/100 conversations, and validates the complete packet before
writing. The worker caps the serialized UTF-8 body and can split an oversized
conversation at stable message boundaries. An indivisible oversized item is
reported rather than truncated. Repeated native message IDs update existing
records, and identical replays are counted as `unchanged`. Partial snapshots
retain absent archive history, preserve known sequences, and append newly seen
IDs. No search, dashboard, transcript-read, cloud-upload, or source-deletion API
is exposed.

## Selective browser capture (2026-11-11)

The shared `extension/providers/selection.js` policy defaults to projects,
pinned/favorites, and GPT/agent conversations enabled, with ordinary Recents
disabled. Selection accepts only explicit provider identifiers, project/folder
objects, literal pin/star/favorite booleans, or membership returned by a known
collection endpoint. ChatGPT's `pinned_time` is an explicit pin record; ordinary
creation/update dates, titles, workspace accounts, sidebar ordering, and Gemini's
opaque tuple flags never establish category membership. A conversation matching
any enabled organized category is eligible, including a project or pinned chat
also shown in Recents. Recents is the ordinary/unclassified fallback: enabling it
does not override an explicitly deselected project, pin or agent category. No
existing archived conversation is removed when the selection changes.

The following public sources were inspected over HTTPS for protocol evidence.
They are unofficial clients; no code was copied from them and no logged-in user
session was used. These references establish why these endpoint shapes were
implemented, not that a live account was tested:

| Surface | Source evidence and implemented pagination |
| --- | --- |
| ChatGPT projects | [chatgpt-exporter `023cf2b1ece2d41d9be326c026f7ae6ae10c9ae5`, API declarations](https://github.com/pionxzh/chatgpt-exporter/blob/023cf2b1ece2d41d9be326c026f7ae6ae10c9ae5/src/api.ts#L472) and [project enumeration](https://github.com/pionxzh/chatgpt-exporter/blob/023cf2b1ece2d41d9be326c026f7ae6ae10c9ae5/src/api.ts#L647): `GET /backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0&cursor=…`, response `items[].gizmo.gizmo` and next `cursor`; `GET /backend-api/gizmos/{id}/conversations?limit=50&cursor=…`, response `items` and next cursor. Both catalogs are enumerated and IDs deduplicate against the main conversation list. |
| ChatGPT pins/GPTs | [The same client's native list item fields](https://github.com/pionxzh/chatgpt-exporter/blob/023cf2b1ece2d41d9be326c026f7ae6ae10c9ae5/src/api.ts#L340) include `is_starred`, `pinned_time`, and `gizmo_id`. Known project gizmo IDs are distinguished from custom GPT IDs. An unavailable project catalog cannot authorize ambiguous gizmos as GPTs. No separate, unverified pin endpoint is probed. |
| Claude project catalog | [ClaudeSync/ctxsync `3ae5228a8388587341ed1b91c3f99eeabd3cc507`](https://github.com/jahwag/ctxsync/blob/3ae5228a8388587341ed1b91c3f99eeabd3cc507/src/ctxsync/providers/base_claude_ai.py#L185): `GET /api/organizations/{org}/projects` returns a complete array with project `uuid`s. An unknown paginated envelope is rejected rather than guessed. |
| Claude project conversations | [withLinda exporter `6a204107a23ce224c75c60cb61b71a67bfa59d97`](https://github.com/withLinda/claude-project-conversations-exporter/blob/6a204107a23ce224c75c60cb61b71a67bfa59d97/claude_project_export_script.js#L207): `GET /api/organizations/{org}/projects/{project}/conversations_v2?limit=…&offset=…`, response `{data:[…]}` (or an array). AI Memory requests 50-item pages until a short page and deduplicates against the ordinary list. |
| Claude native metadata | [Claude-voyager `db192e6b4943073df90c1014dfc32dd8eef9a1fb` captured-response type](https://github.com/TanChuping/Claude-voyager/blob/db192e6b4943073df90c1014dfc32dd8eef9a1fb/src/features/conversationApi/types.ts#L102) identifies `is_starred` and `project_uuid`. Pin membership is used when explicitly present in list metadata. No trustworthy separate pinned-list route was found; absent pin/agent metadata produces a visible warning and an excluded conversation. Third-party extensions' own local stars/folders are not native provider membership. |

Organized modes scan list metadata independently of message timestamps, since
pinning or moving an old chat may not update its message date. They fetch the
transcript only after a selected category matches. Ordinary Recents, when
explicitly enabled alone, retains the donor's incremental behavior. Known source
IDs, raw text, account IDs and organization IDs are preserved. Failed discovery,
unknown response shapes, repeated offset pages, cursor loops and the 200-page safety ceiling produce
an incomplete-selection warning; they never fall back to importing Recents.
Failed discovery retains the retry watermark. The worker resets prior provider
watermarks and Gemini continuations when the capture policy changes.

Every provider returns and reports `selection` counters: `classified`, `unknown`,
`skipped`, `selected`, and `byType`. `selected` describes eligible conversations,
while the existing accepted count describes captures; overlapping `byType`
counts must not be summed as distinct conversations. `selectionWarning` is a
string or null and identifies unavailable discovery/category metadata. Unknown
metadata is allowed only through the user's explicit Recents choice or an
independently verified selected category.

Gemini's retained private RPC parser exposes no verified project, pin or Gem
membership field. Its tuple booleans and positions are deliberately not guessed.
Organized-only selection captures nothing and returns a metadata warning.
Explicit Recents capture and the original bounded continuation behavior remain
available. This limitation is shown to the user instead of claiming selective
Gemini support. Local Codex collection is outside this browser-only policy.

ChatGPT and Claude distinguish recognized empty detail collections from absent or
malformed collections/message shapes. Unsupported details warn and retain the
retry watermark. ChatGPT account-discovery fallback can capture the default
account but reports incomplete discovery and retains unavailable workspace state.

Gemini distinguishes explicit empty pages from unsupported response shapes,
cursor loops, and pagination limits. Its checkpoint/continuation is bound to a
SHA-256 fingerprint of the already-used `SNlM0e`/`FdrFJe` session credentials plus
the redirected account slot. This is active-session continuity evidence, **not**
a stable Google principal ID. No raw provider credentials are persisted or logged,
and the slot alone is never trusted. A legacy checkpoint, changed credential,
session or slot forces rediscovery from the start. Credential rotation may thus
replay already archived history, with normal native-ID deduplication. A matching
session retains bounded ten-conversation continuation across worker restarts.
If session evidence is unavailable, each attempt starts fresh and stores no
completion watermark. It reads at most ten conversations, warns if history remains,
and does not schedule an unsafe endless continuation. Complete capture of a larger
history then requires usable session evidence. Separate user-selected namespaces
remain necessary to distinguish Google accounts in the archive. No undocumented
email/principal field or organized-category field is inferred.

Incomplete runs keep only a matching session's previous completion watermark.
Native turn IDs deduplicate overlapping pages. Completed conversations
from incomplete discovery remain eligible, but incomplete details are deferred so
their messages receive archive positions only after the full source order is known.

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

`node vendor/hstry/extension/test/selection.js` covers default migration, literal
metadata, independent checkboxes, overlapping membership, unknowns and category
false positives. `organized-capture.js` runs the real provider modules against
synthetic project catalogs and realistic cursor/offset pages: project-only chats,
deduplication, stars, GPT/project separation, unavailable/malformed collection
responses, old newly pinned chats, null totals, Gemini opaque metadata, and no
network requests with all categories off. Existing parser/continuation fixtures
explicitly enable ordinary Recents so they continue to verify their original
losslessness behavior.
