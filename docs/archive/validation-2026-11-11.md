> Historical results from before the final cleanup. See [current validation](../validation.md) for the latest checks.

# Validation record

## Current local handoff — 2026-11-11

Extension version: **0.5.2**. The V1 architecture remains SQLite → Markdown → NotebookLM → two-tool MCP. Signed-in provider/Google tests are for the user's local environment; this handoff did not access a personal browser profile or upload a personal archive.

Fresh verification before final review:

- Full Python suite: **172 passed in 40.75s**. Actual Codex/OpenCode parsers, SQLite/filesystem state, local HTTP and stdio MCP run; Google responses are simulated in contract/recovery tests.
- All **14 Node test scripts** passed. Includes default Recents exclusion, organized overlap, capture revocation, partial failure/cursors, auth races and the pre-capture Gemini warning.
- Real browser UI harness passed settings/popup, actual loopback token verification, connection-file import, light/dark, mobile overflow and the Gemini warning without enabling Recents. The harness uses a test Chrome bridge; it is not an installed-extension/live-account run.
- Extension ZIP built with `manifest.json` at its root. Integration tests verify runtime JS/HTML/icon dependencies, upstream license, exclusion of token/auth/connection/test data, and rejection of missing files/outside asset symlinks.
- Ruff and `git diff --check` passed. `uv sync --frozen --extra login --dry-run` found **43 installed packages** consistent with the lockfile. The SDK cookie reader is an optional project extra, so login does not require a Chromium download.
- Wheel build, installed-wheel resources/demo and genuine source/installed-wheel stdio MCP smoke passed. The installed module path was checked outside the checkout.
- One fresh read-only whole-branch reviewer found **no Critical, Important or Minor issues** and independently passed 29 focused Python tests, the Gemini warning script, whitespace checking and the documented SDK login help.

The two full-audit reproductions are now regressions:

| Failure | Verified result |
|---|---|
| A broken OpenCode JSON session blocks healthy sessions | New/legacy JSON sessions checkpoint independently; corrupt metadata/message/part remains retryable while healthy sessions import. Part-only changes and explicit session-file dependencies are observed. SQLite retains priority; new-layout duplicates win. |
| An oversized three-parent render blocks a feasible pair/later window | Each candidate uses a savepoint and validates the complete source. The real 49-source test retains **22,547 original messages**, peaks at **50 remote sources**, uploads the valid pair, deletes parents only after readiness, and repeats with no additional upload. All-infeasible rollback remains covered. |
| Worker ignores preflight blockers without queue jobs | Capacity/snapshot/quota notices are allowlisted and rate-limited, without raw details or duplicate queue-job logs. |

Synthetic demo: 2 conversations / 5 visible messages / 1 segment; 0.4222s wall time, parent peak RSS 17.93 MiB and max child RSS 66.37 MiB. This is a small import measurement, not a production soak test.

Current external limits remain explicit:

- The latest cloud live attempt stopped at `auth_required`; an unauthenticated NotebookLM network probe returned proxy CONNECT 403. No successful Google upload/retrieval is claimed for this handoff.
- Managed Chromium blocks installing arbitrary unpacked extensions. The harness validates real page code and local HTTP using a test bridge.
- Gemini project/pinned/agent metadata is not available in the verified schema. With Ordinary Recents off, Gemini capture stays excluded and the UI explains why.
- Claude Code/Gemini CLI adapters, browser Codex task capture, real account quotas, long-lived login, Mongolian retrieval quality and optimal 150k/300k/450k Google source sizes remain outside the verified V1 scope.
- A free-form answer's conversation/message IDs are not themselves verified citations. Structured IDs require an exact current archive heading; otherwise source/segment references remain.

Local steps, including installed-extension checks, synthetic Google testing, MCP and optional user services: [Монгол заавар](../local-testing.mn.md).

## Historical initial verification — 2026-11-10

Tests performed in this cloud environment on 2026-11-10 (Asia/Ulaanbaatar). The repository began empty; all input conversations in these checks are synthetic. No personal histories were scanned and no Google data was uploaded.

## Verified locally

- Frozen uv installation with Python 3.12.14 and Node 24.22.1; notebooklm-py 0.8.6 and MCP SDK 1.30.0. Exact Python dependency hashes are in `uv.lock`.
- Pytest suite: **97 passed** at the final full run. It exercises actual upstream-derived Codex JSONL and OpenCode JSON/SQLite parsers, SQLite transactions, loopback HTTP ingest, Markdown generation, persistence/restarts, and the official MCP protocol.
- Seven Node extension test scripts exercise real provider parsers and browser API fixtures: lossless multipart text, native identities, visibility filtering, Gemini continuation, account failures, backoff, disabled defaults/no-token network suppression, and options initialization.
- Real notebooklm-py HTTP serialization/parsing against **simulated Google transport**: Markdown registration and exact file bytes, readiness, source-filtered query/citation decoding, and deletion. This is a client contract test, not live Google retrieval.
- Genuine official MCP client → subprocess stdio server tests, including the real `ai-memory ... mcp` entrypoint. Exactly `memory_ask` and `memory_status` are exposed; missing config/auth is reported honestly.
- Built a wheel, installed it in `/tmp/memory-wheel-venv`, and ran the synthetic demo from `/tmp`, proving vendored adapter resources ship outside the checkout.
- Codex 0.167.0-alpha.4 accepted the configured stdio MCP command/arguments via `codex ... mcp get memory --json`.
- Ruff checks pass for `src`, `tests`, and `scripts`. Application diffs pass whitespace checking; untouched upstream license/adapter whitespace is retained in vendored files.

## Synthetic demo measurements

One recorded fresh run of `.venv/bin/ai-memory demo --output data/demo` imported two conversations and five visible messages into one segment:

| Measurement | Result |
| --- | ---: |
| Wall time | 0.4762 s |
| Parent CPU | 0.0149 s |
| Child CPU (collector processes) | 0.7176 s |
| Parent peak RSS | 17.36 MiB |
| Largest child peak RSS | 66.09 MiB |
| Closed SQLite archive | 114,688 bytes |

These describe a tiny synthetic import, not a large-history or steady-state benchmark. The demo reports fresh measurements on every run. No always-running browser, local LLM, or vector service is required.

## Review regressions

A fresh read-only reviewer found six important issues and one minor issue. Regression tests were observed failing, then fixed:

- Oversized edited segments now stop before enqueue/upload; immutable snapshot digests are verified before upload/reconstruction.
- Repeated merges preserve original message arrival order and choose the oldest feasible adjacent window, including after an earlier segment reaches its ceiling.
- Edits invalidate still-mapped retired parent segments, suppressing stale message-level citations during a pending merge.
- Foreign sources count toward the physical source budget; new growth preserves the replacement slot and can trigger a merge before queue processing continues.
- Terminal processing failures discard/reset only the uncommitted failed replacement, retain committed old sources, and retry after backoff. Processing timeout remains distinct.
- Notebook IDs cannot change while source mappings or unresolved upload jobs exist.
- Explicit `--approve-upload` is honored even for unchanged imports.

An additional worker regression verifies pending jobs are revisited on the scan schedule while their SQLite retry deadlines enforce backoff, instead of waiting the normal 12-hour batching period.

The 49-source overflow test uses a simulated remote and real SQLite/filesystem state. It observes a peak of 50 sources and verifies every original message remains after merging. Restart, ambiguous response, processing failure, cleanup failure, locked-worker and tampered-snapshot tests also use simulated service failures.

## External blockers and unrun checks

| Check | Observed result |
| --- | --- |
| Real NotebookLM auth/upload/query | `scripts/live_demo.py` exits nonzero with `auth_required`; no usable Google browser session. `GOOGLE_APPLICATION_CREDENTIALS` is not NotebookLM web authentication. |
| NotebookLM network access | `curl -I https://notebooklm.google.com/` failed with proxy HTTP CONNECT 403. Add `notebooklm.google.com` and `accounts.google.com` in environment settings, preserving existing package access; login may identify additional Google static/redirect hosts. |
| Installed extension in system Chromium | Real Playwright attempt used a fresh temporary profile in Chromium 156. Browser-target CDP returned `Loading of unpacked extensions is disabled by the administrator.` The options page returned `ERR_BLOCKED_BY_CLIENT`. No policy was changed. |
| Full Codex model → MCP execution | `codex exec --ephemeral --sandbox read-only` failed before model/MCP startup: `failed to initialize in-process app-server client: Read-only file system (os error 30)`. Supported `sqlite_home`/`log_dir` overrides did not resolve it. `HOME` and `CODEX_HOME` were preserved. Configuration recognition and standalone MCP protocol checks passed separately. |
| OpenCode agent execution | Parser and config tested; an actual OpenCode client session was not run. |
| Environment draft persistence | Two draft reads returned `INVALID_ARGUMENT: The current environment draft could not be verified`; the subsequent batched install/start save returned `INVALID_ARGUMENT: saving the draft could not be confirmed`. No configuration fields or network changes can be claimed saved. Reusable steps remain in `config/cloud-install.sh` and `config/cloud-start.md`. No publication occurred. |

Live ChatGPT/Claude/Gemini history coverage, transient UI progress completeness, Google account auth longevity, actual free-account quotas, Mongolian retrieval quality, and 150k/300k/450k-word source performance remain unverified. There is no implemented AI summarization when capacity is exhausted; the archive is retained and sync pauses. Upstream deletion/tombstones are not applied to the archive.

## Acceptance checklist against the supplied plan

| Requirement | Evidence / limit |
| --- | --- |
| Codex/OpenCode text 1:1 | Actual donor parsers with whitespace/code/multilingual synthetic fixtures; invalid Unicode fails instead of silently replacing text. |
| Web extension → SQLite | Real provider fixture → authenticated HTTP server → SQLite passes; installed/live extension blocked by browser policy. |
| Model/time/IDs/order | Parser, archive and lifecycle tests pass; unknown source fields stay unknown. |
| Visible thinking/progress | Explicit UI-visible parts tested; completeness on every live platform remains unverified. |
| Tool/log/raw diff exclusion | Collector/ingest fixtures pass. Assistant-written code remains original text. |
| Duplicate prevention | Native-ID upserts and repeated demo/import tests pass. |
| Markdown revisions | Edit/rollover tests pass; original text retained. |
| Old conversation continuation | New messages append to current segment; sealed metadata remains unchanged. |
| Failure/restart durability | Persistent SQLite queue and injected service failure tests pass. |
| 49 sources plus spare | Real archive/filesystem with simulated remote peak 50 passes, including foreign-source accounting. |
| Live NotebookLM ID retrieval | Unrun: auth/network prerequisites missing. Simulated SDK contract and conservative citation mapping tested. |
| Codex/OpenCode MCP use | Genuine stdio MCP and Codex config recognition pass; actual agent query not verified. |
| No extra AI memory engine | SQLite archive + NotebookLM adapter only; no local model/vector dependency. |
| CPU/RAM measurement | Tiny synthetic import measured above; large-source benchmarking remains open. |

The local implementation and offline demo are validated. The complete live V1 acceptance list and reusable cloud configuration are **not** certified ready until their stated external blockers are resolved and affected checks rerun.

## Extension 0.5.1 follow-up — 2026-11-11

After resuming development, all four handoff defects were fixed with failing-then-passing regressions: wrong-archive receiver reuse, active-capture revocation, selected-detail partial failures, and empty-checklist worker startup. Full verification:119 Python tests,13 Node scripts, real Chromium UI with real local auth through an isolated test bridge, frozen dependency sync, wheel build/resources, Ruff and application whitespace checks passed. This does not establish live Google/provider-account behavior or installed-extension readiness under the administrator block. Current details and local steps are in extension-handoff.md and extension-setup.mn.md.
