# Verification

For each commit on main or a pull request, use its **Checks → offline** job and its
**ai-memory-build** artifact. The bundle contains the wheel, source distribution,
extension ZIP and `verification.json`. That record reads the commit from a clean
checkout and the version, regular-file count and SHA-256 from the exact built files.
Historical counts and digests do not describe a newer build.

| Check | Evidence and scope |
| --- | --- |
| Python and Node | The run log reports Ruff, pytest and all extension test scripts, with actual suite counts. Dependencies come from the frozen uv and npm locks. |
| Synthetic browser pages | The page harness supplies a test Chrome API bridge. Passing does not prove a toolbar extension ran. |
| Installed extension | A separate Chromium test loads the real MV3 extension in a new profile and opens its toolbar. With synthetic provider pages and no personal account, the test toggles List mode from the toolbar, clicks on-page controls and checks the saved IDs. A later manual sync, not the selection gesture, writes the chosen conversations to SQLite. |
| Local plugins | Synthetic Codex Stop events and OpenCode idle events exercise the real adapters. Installer tests run against temporary configuration. A successful fixture does not establish deployment in a personal client. |
| Packaging | CI installs the built wheel in an empty environment, changes directory outside the checkout, runs the synthetic demo, and calls a real MCP server over stdio. |
| Live NotebookLM | Not part of offline CI. Only an explicitly selected empty demo notebook and newly generated synthetic data are supported by the live script. |

To reproduce the manifest from a clean checkout, run the build and record commands
in [development.md](development.md). Instructions for optional signed-in local
checks are in the [Mongolian test guide](local-testing.mn.md).
