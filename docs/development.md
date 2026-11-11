# Development

Use Python 3.12+, Node.js 24+ and uv. From the repository:

```bash
uv sync --frozen --extra login
npm ci --ignore-scripts --no-audit --no-fund
.venv/bin/python scripts/check.py
npm run format:check
```

`scripts/check.py` runs Ruff lint/format checks, every Python test, and all Node
extension test scripts. It works from another directory too. GitHub Actions runs
these checks, the synthetic demo, stdio MCP smoke test, and both package builds.
These are offline checks; they do not access signed-in provider accounts.

To format after editing:

```bash
.venv/bin/ruff check --fix src tests scripts
.venv/bin/ruff format src tests scripts
npm run format
```

Ruff uses 100 columns, four-space indentation, sorted imports and Python 3.12
syntax. Extension JavaScript, HTML, CSS and JSON use pinned Prettier with two
spaces. npm installs only this development formatter; running the application,
collectors, extension or Python checks does not require npm packages.

## Project map

| Location | Responsibility |
| --- | --- |
| `src/ai_memory/cli.py` | Commands, worker scheduling and user-facing results |
| `src/ai_memory/collectors.py` | Node adapter execution, rollout discovery and Codex snapshot reconciliation |
| `src/ai_memory/normalization.py` | Visible text, stable native identities and metadata normalization |
| `src/ai_memory/importer.py` | Archive imports, per-file checkpoints and failure isolation |
| `src/ai_memory/store.py` | SQLite schema, transactions, consent and revisions |
| `src/ai_memory/segments.py` | Lossless Markdown layout, word budgets and merges |
| `src/ai_memory/sync.py` | Persistent upload, readiness, mapping, cleanup and retries |
| `src/ai_memory/notebook.py` | Lazy notebooklm-py client boundary and safe errors |
| `src/ai_memory/gateway.py` | Two MCP tools and verified structured citations |
| `src/ai_memory/connection.py`, `ingest.py` | Private connection files and authenticated loopback receiver |
| `vendor/hstry/adapters/` | Audited upstream TypeScript parsers |
| `vendor/hstry/extension/` | Browser UI, provider readers and category selection |
| `tests/`, `vendor/hstry/extension/test/` | Synthetic behavior and recovery regressions |
| `scripts/` | Checks, smoke tests, UI verification and extension packaging |
| `config/` | Client/service examples and explicitly named cloud executor helpers |
| `docs/archive/` | Historical designs, plans and previous verification records |

Keep parser logic in the adapters and archive policy in Python. Avoid adding a
second retrieval engine or reimplementing notebooklm-py's authentication.
Retain public imports from `ai_memory.collectors` and `ai_memory.cli` when moving
internal responsibilities.

## Data and generated files

`data/` holds local archives, connection files, synthetic demo output and optional
screenshots. `dist/` holds built wheels and extension ZIPs. Both are ignored, as
are caches, local agent state and auth/token files. Runtime never cleans an
archive or changes consent automatically. Do not run blanket reset/clean commands
to update an installation with local changes; inspect `git status` and save those
changes first.

Do not format the upstream TypeScript adapters wholesale. Their retained style,
license, original hashes and local behavior changes are recorded in
[`vendor/hstry/PATCHES.md`](../vendor/hstry/PATCHES.md) and
[`UPSTREAM.json`](../vendor/hstry/UPSTREAM.json). Update the `modified` flags
against the original hashes when an upstream-derived file changes. New local
extension files do not have invented upstream hashes.

Build distributable files with:

```bash
.venv/bin/python scripts/package_extension.py
uv build --wheel
```

The ZIP contains public runtime assets and the upstream license. The wheel also
ships adapters so collectors work outside the checkout. Never bundle `data/`,
personal sessions, browser profiles, tokens or saved Google authentication.

For real local browser/Google checks use [local-testing.mn.md](local-testing.mn.md).
For this managed cloud executor only, use [cloud-start.md](../config/cloud-start.md).
