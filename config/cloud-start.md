# Cloud checkout setup

Use a checkout of this repository, Python 3.12+, Node 24+ and uv. From any directory,
run `bash /path/to/Memory/config/cloud-install.sh` to install locked dependencies
and run the offline tests, synthetic demo and real stdio MCP smoke check. The script
locates its own checkout. If the cloud image supplies a writable dependency cache,
set `UV_CACHE_DIR` to its path. A browser session or Google login is not needed.

Configure Codex or OpenCode with the examples in this directory and the archive’s
absolute path. The client launches `ai-memory --db PATH mcp` over stdio. Do not
start a separate background stdio process. Optional local history collection runs
only for explicitly selected paths, e.g. `ai-memory watch --codex-path PATH`.
Neither local collection nor browser capture grants upload consent.

If a live NotebookLM test is explicitly intended, use a writable `NOTEBOOKLM_HOME`
or storage file dedicated to the authorized test account. See [NotebookLM setup](../README.md#connect-notebooklm).
A Google service account is not a substitute for NotebookLM browser authentication.
Use `scripts/live_demo.py --notebook-id YOUR_EMPTY_DEMO_NOTEBOOK_ID --output data/new-live-demo`
with an empty output directory. Offline tests are not evidence of a working remote session.

Pair web capture on the same Linux desktop as the browser, not on an unrelated
remote host. Load the extension, copy its installed ID, register its native host,
and choose the printed v2 file. See [native pairing](../docs/native-pairing.md)
and the [verification guide](../docs/validation.md).
