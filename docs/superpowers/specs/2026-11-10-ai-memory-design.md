# AI Memory implementation design

Authority: [the supplied architecture](../../specification.mn.md). The user asked to implement and demo it in the empty Memory repository.

Use SQLite as the lossless source of truth, one NotebookLM notebook as the only AI retrieval engine, Markdown/YAML segments, and two stdio MCP tools. No summaries, embeddings, local model, dashboard, or paid API.

## Approaches

Running all of hstry would retain its Rust search services, indexing, and unrelated features. Rewriting the collectors would discard its existing format handling. The implementation choice is to vendor the MIT-licensed Codex/OpenCode TypeScript adapters and browser extension, with documented minimal losslessness/security patches, and write a small Python archive/segment/sync layer using notebooklm-py and the official MCP SDK. Node 24 executes the adapter TypeScript without a bundler; built-in node:sqlite replaces the optional native SQLite dependency.

## Contracts

Collectors return dictionaries: platform, account, external_id, title, project, models, created_at, updated_at, source_url, messages. Each message has native_id, sequence (one-based), role, kind (text/progress/thinking_summary), text, timestamp, optional model. Unknown timestamps stay null. Only user-facing text is accepted. Native stable IDs win; otherwise original record position is used. Raw transcript text is never stripped, translated, or rewritten. Hidden reasoning and tool/system events are excluded.

The archive owns deduplication by platform/account/conversation and native message identity, revision hashes, upload consent, and settings/checkpoints. Segments contain every eligible message once, in arrival order with native conversation sequence preserved. Edits invalidate the containing segment; continuations append to the open segment. Default target is 150,000 words; maximum merged size is 450,000 words, with 49 active sources and one replacement slot. Overlarge individual messages remain archived and block packaging rather than being truncated.

The NotebookLM adapter exposes async list_sources(), upload(path, title), wait_ready(id), delete(id), ask(question, source_ids), and close(). Source records contain id/title/ready. Answers expose answer/references; citations contain source_id/cited_text/citation_number. All cloud reads go to NotebookLM. No local fallback answers.

Sync uses revision-specific deterministic filenames, a persistent queue, and a single file lock. It reconciles ambiguous uploads by title, waits for readiness, switches source_map transactionally, then deletes old sources. Retries retain jobs and sanitized error categories. Adjacent segments merge through the spare slot before old sources are deleted. Consent is explicit per conversation; detected credentials block uploads without modifying the local archive.

MCP tools memory_status and memory_ask read the SQLite state and call the adapter. Source IDs map to segments; conversation/message citations are returned only when the cited text contains a verifiable archive heading. Historical instructions remain untrusted context.

## Demo and limits

Synthetic Codex/OpenCode fixtures exercise real donor parsers, SQLite, Markdown, persistent retry/replacement/overflow, a local HTTP ingest server, and the MCP client/server. Network adapter contract tests simulate Google at the transport boundary and are labeled offline. A separate explicit live demo creates a dedicated NotebookLM notebook using synthetic data and queries it, only when user Google login and policy permit. Never claim offline tests establish real NotebookLM retrieval or live website capture. Record CPU/RAM and status. No automatic push or publication.
