# Extension handoff

All browser controls live in the AI Memory toolbar popup. Pair the local archive in **Settings**. With an existing provider sign-in, choose **Load items**, search the catalog, check rows and click **Add to sync list**. Loading reads listing metadata only. Adding saves identifiers, not transcripts. ChatGPT and Claude list projects and chats. Gemini lists chats.

**Sync now** and enabled automatic captures write saved items from enabled providers to the paired local archive. There is no category-based fallback, including on upgrade or after removing the last saved item. Local capture has no effect on NotebookLM consent. Google publication still needs explicit archive approval and the CLI sync command.

Pull the updated checkout, then reload the unpacked extension in your browser’s extension manager. Keep the same installation and ID to retain its local settings. Existing ChatGPT saved IDs remain scoped to their original account and restrictions. If you package the extension, extract the ZIP printed by `python scripts/package_extension.py` and load the directory containing `manifest.json`. Runtime files and both MIT notices are included, not personal data or tests.

See [native pairing](native-pairing.md), the [Mongolian setup guide](extension-setup.mn.md) and the [verification record](validation.md). Tests with isolated browser profiles and synthetic provider data are separate from signed-in, user-device verification.
