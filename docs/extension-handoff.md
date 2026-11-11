# Extension status — version 0.5.3

AI Memory has guided private connection-file import, a logo and icons, responsive
settings/popup pages, and provider/category checklists. Ordinary Recents is off
by default. The cleanup release keeps this capture policy and standardizes the
extension's formatting with a pinned development formatter.

Projects, pin/favorite and GPT/agent membership must come from native metadata or
verified project discovery. ChatGPT/Claude support the available metadata;
Gemini organized membership is unsupported and is explained before capture.
Changing settings invalidates an active capture before subsequent batches. A
partial provider failure preserves retry state and reports incomplete capture.

Run `ai-memory connect`, keep its terminal open and import the printed private
connection file in Settings. Reload at `brave://extensions` after pulling code.
See [Монгол setup](extension-setup.mn.md) and the full [local test guide](local-testing.mn.md).

`python scripts/package_extension.py` produces
`dist/ai-memory-extension-0.5.3.zip`. Extract it and load the folder containing
`manifest.json`. Runtime assets and license are included; tests, connection files
and authentication data are excluded.

The current verification record is [validation.md](validation.md). Real provider
accounts and installed-extension operation still require the user's local test.
The UI harness uses an isolated browser context and a real local HTTP receiver.
Earlier results are retained in [the archive](archive/extension-0.5.2.md).
