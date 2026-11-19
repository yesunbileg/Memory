# Native browser pairing on Linux

Load the AI Memory extension, then copy its 32-letter ID from the browser's extension manager. Use the Python environment where AI Memory is installed and keep that environment available. On Linux Brave:

```sh
ai-memory --db /path/to/archive.sqlite connect --browser brave --extension-id YOUR_EXTENSION_ID
```

For Google Chrome use `--browser chrome`; for Chromium use `--browser chromium`. If you use a custom user-data root, pass `--browser-user-data-dir /absolute/user-data-root` (not the Default or Profile 1 subdirectory). In the toolbar popup’s Settings, choose the returned `connection_file`. If the OS file chooser closes the popup, reopen Settings, select **Paste connection file contents instead**, paste the v2 file contents and click **Verify connection**. No persistent HTTP server or terminal is needed. The command registers a local executable with one allowed extension origin. It creates a version 2 pairing file in `<database filename>.files/native/<extension-id>/`. Verify a candidate before replacing an existing working configuration. Browser-managed native messaging, not an archive ID supplied by an HTTP responder, authenticates the local transport.

The host refuses a replaced archive even at the same path, takes one atomic packet at a time, returns only counts, and never approves or uploads conversations. Per-user host manifests are under `NativeMessagingHosts` in the browser's Linux configuration root. Existing different files are not overwritten. After moving or restoring a database, stop capture, inspect/remove only the previously registered AI Memory host manifest for that archive, then register the restored path and pair again. Do not remove any host manifest you cannot identify. Windows and macOS host installation are not implemented.

Existing browser selections, interval and legacy token can remain in extension storage. Version 1 bearer files **cannot** enable browser capture. Pair explicitly using the command above. `serve` and `connect --port` are for explicit legacy HTTP clients only, with separate per-database files. Old directory-wide token/configuration files are left untouched; specify the old token file explicitly for an existing trusted legacy client if necessary.

The popup’s **Load items** action reads listing metadata. **Add to sync list** only saves scope identifiers locally. Only a later browser capture sends transcripts through the native host. Removing a saved scope does not delete existing archive records or change their upload consent.

Each browser profile keeps its own capture namespace. Checkpoints also include archive, provider, configured account label and saved selection. ChatGPT/Claude validate the current principal set before resuming. Gemini uses a hashed Google account identifier only when its bootstrap fields corroborate one. Otherwise it binds selections to a fingerprint of the current signed-in session. A changed fallback session is not proof of the original identity. If Gemini no longer recognizes the old selection, load its catalog and reselect under the new session. A session without sufficient identity evidence cannot resume safely. New imports are never automatically approved because another account happened to have the same conversation ID.

Discovery checkpoints retain only required listing metadata. Each worker slice fetches at most four new list pages and reads details for up to ten selected conversations. Previously acknowledged work is not a new import. If a worker stops during an individual Gemini detail, it rereads that one detail before releasing it to the archive. Partial turns are never committed out of order. A receipt or storage failure retains the old completed watermark. Byte-sized native packets are atomic; an oversized conversation with stable native message IDs may span multiple packets. Earlier acknowledged packets may survive a later failure and are idempotently replayed.

Protocol reference: [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging).
