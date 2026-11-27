# Native browser pairing on Linux

Load the AI Memory extension, then copy its 32-letter ID from the browser's extension manager. Use the Python environment where AI Memory is installed and keep that environment available. On Linux Brave:

```sh
ai-memory --db /path/to/archive.sqlite connect --browser brave --extension-id YOUR_EXTENSION_ID
```

For Google Chrome use `--browser chrome`. For Chromium use `--browser chromium`. For a custom user-data root, pass `--browser-user-data-dir /absolute/user-data-root` using the directory that contains the profiles. Open **AI Memory** from the toolbar. If the popup is already open, click **Settings → Check connection**. The popup connects automatically to the first archive registered for this extension ID in that browser root. The command reports `auto_connect: true` for that archive. Browser-managed native messaging authenticates the local connection and launches the host on demand.

An existing verified pairing stays pinned to its archive. If you register a second archive and the command reports `auto_connect: false`, use **Settings → Connect a different archive manually** with the `connection_file` printed by the command. If the file chooser closes the popup, use **Paste connection file contents instead** and click **Verify connection**. The optional version 2 file is under `<database filename>.files/native/<extension-id>/`.

The registration restricts each host to the specified extension origin. The discovery host reports the archive identity after checking the database in read-only mode. The archive host validates that identity, accepts atomic ingest packets, and returns counts. Per-user manifests are under `NativeMessagingHosts` in the browser's Linux configuration root. Existing different files are not overwritten. After moving or restoring a database, stop capture, inspect/remove only the previously registered AI Memory host manifest and discovery manifest pointing to that archive, then register the restored path and pair again. Do not remove any host manifest you cannot identify. Automatic registration currently supports Linux.

Existing browser selections and intervals survive the update. If an unpaired extension was registered before automatic connection was available, rerun the command with the updated local installation. This also upgrades legacy bearer pairing. `serve` and `connect --port` remain available for explicit legacy HTTP clients with separate per-database files. Specify the old token file explicitly for an existing trusted legacy client if necessary.

In **List mode**, the extension verifies page items against provider listing metadata. Checking or unchecking an item immediately updates local scope identifiers. A later browser capture sends transcripts through the native host. Existing archive records remain after you remove a saved scope.

Each browser profile keeps its own capture namespace. Checkpoints also include archive, provider, configured account label and saved selection. ChatGPT/Claude validate the current principal set before resuming. Gemini uses a hashed Google account identifier only when its bootstrap fields corroborate one. Otherwise it binds selections to a fingerprint of the current signed-in session. A changed fallback session is not proof of the original identity. If Gemini no longer recognizes the old selection, use List mode to reselect under the new session. A session without sufficient identity evidence cannot resume safely.

Discovery checkpoints retain only required listing metadata. Each worker slice fetches at most four new list pages and reads details for up to ten selected conversations. Previously acknowledged work is not a new import. If a worker stops during an individual Gemini detail, it rereads that one detail before releasing it to the archive. Partial turns are never committed out of order. A receipt or storage failure retains the old completed watermark. Byte-sized native packets are atomic; an oversized conversation with stable native message IDs may span multiple packets. Earlier acknowledged packets may survive a later failure and are idempotently replayed.

ChatGPT and Claude captures are stored under the verified combination of user and workspace or organization. If an older record has the same conversation ID and only a workspace or organization, the archive preserves that record and saves the verified account's capture separately.

Protocol reference: [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging).
