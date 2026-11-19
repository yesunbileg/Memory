"""Install the unpacked MV3 extension in an empty Chromium profile and open its actual toolbar popup.

Uses the OS native host and one empty synthetic archive. No cloud calls, real accounts or transcripts.
On Linux use xvfb-run -a python scripts/extension_toolbar_check.py --browser-executable PATH.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from extension_sidebar_check import verify_sidebar
from extension_ui_check import EXTENSION, LAYOUT, assert_layout, browser_path
from playwright.async_api import async_playwright


def register(folder, profile, extension_id, env):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_memory",
            "--db",
            str(folder / "fixture.sqlite"),
            "connect",
            "--extension-id",
            extension_id,
            "--browser",
            "chromium",
            "--browser-user-data-dir",
            str(profile),
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
        text=True,
    )
    return Path(json.loads(result.stdout)["connection_file"])


async def verify(executable):
    with tempfile.TemporaryDirectory(prefix="ai-memory-toolbar-") as temporary:
        folder = Path(temporary)
        profile = folder / "config/chromium"
        env = {**os.environ, "XDG_CONFIG_HOME": str(folder / "config")}
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(profile),
                executable_path=str(executable),
                headless=False,
                env=env,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
                ignore_default_args=["--disable-extensions"],
            )
            try:
                worker = (
                    context.service_workers[0]
                    if context.service_workers
                    else await context.wait_for_event("serviceworker")
                )
                extension_id = worker.url.split("/")[2]
                file = register(folder, profile, extension_id, env)
                page = context.pages[0]
                await page.goto(f"chrome-extension://{extension_id}/options.html")
                await page.get_by_text("Not connected", exact=True).wait_for()
                await page.locator("#import-connection").focus()
                await page.locator("#connection-file").set_input_files(file)
                await page.get_by_text("Connected to your computer", exact=True).wait_for()
                await page.wait_for_function("document.activeElement.id==='change-connection'")
                await page.locator("#auto-paused").check()
                await page.locator("#provider-gemini").check()
                assert await page.locator("#sync-now").is_disabled()
                # Never substitute a tab navigated to popup.html. Browser action
                # sizing and focus lifecycle are specific to the actual widget.
                await page.bring_to_front()
                await page.evaluate("() => chrome.action.openPopup()")
                await page.wait_for_function("""() => {
                  const view=chrome.extension.getViews({type:'popup'})[0];
                  return view?.document.getElementById('api-title')?.textContent==='Connected to your computer';
                }""")
                kinds = await page.evaluate(
                    """async()=> (await chrome.runtime.getContexts({contextTypes:['POPUP']})).map(c=>c.contextType)"""
                )
                assert kinds == ["POPUP"], kinds
                layout = await page.evaluate(
                    f"() => ({LAYOUT})(chrome.extension.getViews({{type:'popup'}})[0].document)"
                )
                assert_layout(layout)
                assert layout["width"] >= 300, layout
                result = await page.evaluate("""() => {
                  const d=chrome.extension.getViews({type:'popup'})[0].document;
                  return {blocked:d.getElementById('sync').disabled, reason:d.getElementById('plan-hint').textContent};
                }""")
                assert result["blocked"] and "Ordinary Recents" in result["reason"], result
                await verify_sidebar(context, worker, page, folder / "fixture.sqlite")
                print(
                    json.dumps(
                        {
                            "installed_mv3_toolbar": "passed",
                            "installed_sidebar_and_scoped_native_capture": "passed",
                            "native_archive_pairing": "passed",
                            "toolbar_width": layout["width"],
                        }
                    )
                )
            finally:
                await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", type=Path)
    asyncio.run(verify(browser_path(parser.parse_args().browser_executable)))
