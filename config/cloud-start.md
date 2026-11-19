# Cloud checkout setup

Use a checkout of this repository, Python 3.12+, Node 24+ and uv. From any directory,
run `bash /path/to/Memory/config/cloud-install.sh` to install locked dependencies
and run the offline tests, synthetic demo and real stdio MCP smoke check. The script
locates its own checkout. If the cloud image supplies a writable dependency cache,
set `UV_CACHE_DIR` to its path. A browser session or Google login is not needed.

Initialize the archive and configure the local plugin from the checkout:

```sh
.venv/bin/ai-memory --db "$PWD/data/conversations.sqlite" init
python3 config/install_hooks.py --db "$PWD/data/conversations.sqlite"
codex plugin marketplace add "$PWD"
codex plugin add ai-memory@yesko-memory-local
```

In a new Codex session, review and trust the AI Memory Stop handler in `/hooks`.
The same plugin provides the local stdio MCP server. Do not add a second copy via
`config.toml`. Existing manual MCP entries can be removed after checking the new
plugin. To remove the plugin, run `codex plugin remove ai-memory@yesko-memory-local`.

To capture OpenCode sessions too, rerun the setup script with
`--opencode-db /absolute/path/to/opencode.db` (see `opencode debug paths` on v2).
It installs one local plugin for OpenCode 2.x or 1.18.29+, leaving existing
configuration intact. Reload OpenCode. For OpenCode MCP retrieval, merge the
matching v1 or v2 example in this directory; use the same absolute archive path.
To remove the OpenCode bridge, delete only its printed `plugins/ai-memory.js`.
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
