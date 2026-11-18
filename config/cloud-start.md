# Cloud startup instructions

Use the existing `/workspace/Memory` checkout. Cloud tasks already run in an isolated environment; do not create a Git worktree unless explicitly requested.

Run commands from `/workspace/Memory`. Refresh retained dependencies with `UV_CACHE_DIR=/workspace/.cache/uv uv sync --frozen`. The cloud home is read-only; use `export NOTEBOOKLM_HOME=/workspace/Memory/data/notebooklm` for notebooklm-py login/CLI. Do not change `HOME` or `CODEX_HOME`.

For the local demo, run `.venv/bin/ai-memory demo --output data/demo`, then `.venv/bin/python scripts/mcp_smoke.py`. Readiness means two synthetic conversations, five messages, one Markdown segment, and a successful real MCP stdio handshake exposing `memory_ask` and `memory_status`. There are no required detached services for the local demo.

For agent memory, configure Codex/OpenCode using the examples in this directory. The client starts `/workspace/Memory/.venv/bin/ai-memory --db /workspace/Memory/data/conversations.sqlite mcp` over stdio; do not background a separate stdio process.

Real NotebookLM use requires a Google browser session and one notebook ID. Follow README for optional browser login dependencies and `ai-memory init --notebook-id ID`. If using a saved auth file, specify `--storage PATH` without printing its contents. Never treat `GOOGLE_APPLICATION_CREDENTIALS` as NotebookLM browser authentication. The initial live check was blocked by absent auth and a proxy CONNECT 403 for `notebooklm.google.com`. Required domain additions are `notebooklm.google.com` and `accounts.google.com`; preserve existing package access. Browser sign-in may report additional Google static/redirect hosts. After changes, retry `ai-memory check` and `scripts/live_demo.py --notebook-id YOUR_EMPTY_DEMO_NOTEBOOK_ID --output data/new-live-demo`. A live answer must be inspected; simulated tests do not establish retrieval quality.

Only collect personal histories at explicitly selected paths. Imported conversations default to no upload consent; approve reviewed IDs explicitly. Optional collection worker: `ai-memory watch --codex-path PATH --opencode-path PATH`. For web capture, configure the native host on the same Linux desktop as the browser, not inside an unrelated remote checkout. Load the extension, copy its installed ID, then run `ai-memory --db PATH connect --extension-id YOUR_EXTENSION_ID --browser brave` and choose the printed v2 file. No port, token entry or persistent receiver terminal is needed. See [native pairing](../docs/native-pairing.md).

Run `bash config/cloud-install.sh` for complete local readiness verification. See `docs/validation.md` for what passed, what was simulated, and the external blockers. Configuration persistence/publication is separate from these working local files.
