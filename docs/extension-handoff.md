# Extension handoff

On signed-in ChatGPT, tick project rows or standalone, unpinned conversations in Recents. Click **Add to sync list** to save those scope IDs. Ticking is a draft. Add does not fetch transcripts, capture chats or grant upload consent. The next manual capture (or an enabled scheduled browser capture) uses that saved list. A saved project covers its conversations. Manage or remove saved scopes under **ChatGPT sync list** in Settings. Removing every item leaves an empty list, not permission to capture everything.

Until the first Add, ChatGPT retains category-based capture. Claude and Gemini continue to use the category checklist. Projects, pin/favorite and GPT/agent membership in category mode must come from native metadata or verified project discovery. Gemini's organized membership is unsupported and the UI explains that before capture. A partial provider failure reports incomplete capture. The installed version appears in Settings.

Register the native host for the installed extension ID and selected archive, then import the printed v2 file in Settings. No receiver terminal stays open. See [native pairing](native-pairing.md). Reload at `brave://extensions` after pulling code.
See [Монгол setup](extension-setup.mn.md) and the full [local test guide](local-testing.mn.md).

`python scripts/package_extension.py` prints the ZIP path, version and checksum. Extract that ZIP and load the folder containing
`manifest.json`. Referenced runtime assets and both MIT notices are included; tests, connection files
and authentication data are excluded.

The current verification record is [validation.md](validation.md). Real provider
accounts require an expressly chosen local test. The Chromium page-shim check and installed-extension toolbar check are separate.
Earlier results are retained in [the archive](archive/extension-0.5.2.md).
