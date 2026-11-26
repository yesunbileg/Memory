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
these checks, two Chromium checks (page shim and installed toolbar), the wheel, sdist and extension ZIP builds, and a synthetic demo and real stdio MCP calls from a fresh wheel installation.
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
| `src/ai_memory/cli.py` | Commands, event dispatch and user-facing results |
| `src/ai_memory/collectors.py` | Node adapter execution, rollout discovery and Codex snapshot reconciliation |
| `src/ai_memory/normalization.py` | Visible text, stable native identities and metadata normalization |
| `src/ai_memory/importer.py` | Archive imports, per-file checkpoints and failure isolation |
| `src/ai_memory/store.py` | SQLite schema, transactions, consent and revisions |
| `src/ai_memory/segments.py` | Lossless Markdown layout, word budgets and merges |
| `src/ai_memory/sync.py` | Persistent upload, readiness, mapping, cleanup and retries |
| `src/ai_memory/notebook.py` | Lazy notebooklm-py client boundary and safe errors |
| `src/ai_memory/gateway.py` | Two MCP tools and verified structured citations |
| `src/ai_memory/connection.py`, `ingest.py` | Legacy authenticated HTTP receiver and shared validation for browser packets |
| `src/ai_memory/native.py` | Linux native messaging host registration, pairing verification and browser packets |
| `vendor/hstry/adapters/` | Audited upstream TypeScript parsers |
| `vendor/hstry/extension/` | Toolbar settings and sync, on-page list mode, account-scoped selections and provider readers |
| `tests/`, `vendor/hstry/extension/test/` | Synthetic behavior and recovery regressions |
| `scripts/` | Checks, smoke tests, UI verification and extension packaging |
| `config/` | Client/service examples and explicitly named cloud executor helpers |

Keep parser logic in the adapters and archive policy in Python. Avoid adding a
second retrieval engine or reimplementing notebooklm-py's authentication.
Retain public imports from `ai_memory.collectors` and `ai_memory.cli` when moving
internal responsibilities.

## Data and generated files

`data/` holds local archives, connection files, synthetic demo output and optional
screenshots. Archive-specific Markdown exports, pending snapshots and native host files live in a private `<database filename>.files/` directory. These workspaces are ignored even outside `data/`. `dist/` holds wheels, source distributions, extension ZIPs and verification records. Build outputs are ignored, as
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
uv build
.venv/bin/python scripts/release_record.py > dist/verification.json
```

The ZIP contains referenced runtime assets and both MIT notices. The record lists the clean checkout commit, package versions, regular-file counts and SHA-256 hashes read from the exact artifacts. CI compares the extension ZIP to the previous revision and requires a higher version if the bytes change. The Python sdist contains the sources, vendored parser dependencies and notices needed to rebuild the wheel. It is not a snapshot of the development repository. The wheel ships the same parser files so collectors work outside the checkout. Never bundle `data/`,
personal sessions, browser profiles, tokens or saved Google authentication.

For real local browser/Google checks use [local-testing.mn.md](local-testing.mn.md).
For this managed cloud executor only, use [cloud-start.md](../config/cloud-start.md).
