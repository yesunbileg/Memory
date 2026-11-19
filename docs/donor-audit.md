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
| `extension/` | Original Manifest V3 worker, popup and provider code, with selected synthetic tests; supported providers are ChatGPT, Claude, Gemini |

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
If a file is being written, retry the explicit import after the writer has finished.

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

The extension keeps hstry’s `{source, adapter, conversations}` shape and adds the
archive and account scope. On Linux, the browser delivers packets to its
registered native host. The v2 connection file selects an archive and the
registration allows only the installed extension’s origin. Neither an HTTP
bearer token nor an archive ID returned by an HTTP server authenticates a
browser connection. See [native pairing](native-pairing.md).

All providers start disabled. Checkpoints belong to the archive, provider,
profile namespace, capture selection and available account evidence. The worker
stores accepted progress only after an ingest acknowledgement. It bounds the
serialized UTF-8 payload and can split a long conversation at stable message
boundaries. An indivisible oversized item is reported, not truncated. The
receiver reports new, updated and unchanged records. Capture does not grant
permission to publish to Google.

Explicit legacy HTTP clients can use the separate authenticated loopback receiver.
It validates Host, Origin and bearer credentials, bounds the payload (16 MiB,
100 conversations), and uses the same archive ingest boundary. The browser
extension does not fall back to this endpoint.

## On-page selection and earlier category classification

The user-supplied ChatVault reference discovers sidebar chat links by route rather than by a provider’s generated CSS classes. It adds a checkbox only in multi-selection mode and removes it when leaving that mode. AI Memory applies this visible-link pattern to the provider page. The worker, not page text or a link alone, verifies allowed IDs and project membership against signed-in listing metadata. Only deliberate choices change the local sync scope. No exporter, donor code or new upload destination is bundled.

The earlier category classifier described below is still relevant to restricted legacy selections and the synthetic reader fixtures. It is **not** permission for an extension capture. An account with no saved IDs collects nothing. ChatGPT binds selections to the active user and account from the session; a [public client](https://github.com/bigfei/chats2md) uses those same identity fields. See the [current extension guide](extension-handoff.md).

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

Earlier category-mode fixtures exercise classification, project enumeration, deduplication and unknown membership. Current project capture uses account-scoped saved project IDs and verified collection membership. Saved individual chat IDs do not require selecting a whole category. Gemini exposes conversations but no reliable project/folder membership, so it offers chat selection only. Unavailable or malformed listings never select all chats.

ChatGPT and Claude distinguish recognized empty detail collections from absent or
malformed collections/message shapes. Unsupported details warn and retain the
retry watermark. A saved selection cannot authorize a different or unidentified workspace.

Gemini uses its listing RPC to verify chat IDs without reading message details. Selected capture requests message detail separately. It binds the visible scope to a hash of a corroborated active Google account ID when available. An unrecognized bootstrap falls back to a hash of the active session, not an email address, name or slot number. Rotating that session can require reselecting the chats. Raw credentials, names and emails are not persisted as scope identifiers. Gemini does not infer project membership from undocumented tuple flags.

Incomplete runs keep only a matching session's previous completion watermark.
Native turn IDs deduplicate overlapping pages. Completed conversations
from incomplete discovery remain eligible, but incomplete details are deferred so
their messages receive archive positions only after the full source order is known.

## Validation and limits

Synthetic fixtures exercise multilingual whitespace, code, multipart text,
mirrored events, repeated replies, stable IDs, hidden reasoning, synthetic
messages, out-of-order clocks, all supported OpenCode schemas, strict source
decoding, native account isolation, edits, partial snapshots, required token,
native registration and framed ingest, plus body limits, CORS and Host checks for the separate legacy HTTP client. Selected upstream Node tests also cover backoff, inaccessible
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
