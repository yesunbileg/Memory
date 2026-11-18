"""Chromium page tests with a synthetic Chrome API bridge. No installed or signed-in profile.

The separately named extension_toolbar_check.py exercises Chromium's real popup and native host.
"""

import argparse
import asyncio
import functools
import json
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "vendor/hstry/extension"
ARCHIVE_ID = "13579bdf" * 4
ORGANIZED = {"projects": True, "pinned": True, "agents": True, "recents": False}


def descriptor(archive_id):
    return {
        "schema": 2,
        "application": "ai-memory",
        "transport": "native",
        "archive_instance_id": archive_id,
        "native_host": f"org.ai_memory.archive_{archive_id}",
    }


BRIDGE = """() => {
  const data = {settings: {}, status: {}, activityLog: []};
  const listeners = [];
  const messages = [];
  window.__memoryTest = {data,messages,scope:id=>JSON.stringify([data.settings.archiveId,id,'fixture-profile',data.settings.account ?? 'browser-local',data.settings.captureTypes])};
  window.chrome = {
    storage: {
      local: {
        get: async keys => Object.fromEntries((Array.isArray(keys)?keys:[keys]).map(k=>[k,structuredClone(data[k])])),
        set: async values => {
          const changes={};
          for(const [key,value] of Object.entries(values)) {
            changes[key]={newValue:structuredClone(value),oldValue:data[key]};
            data[key]=structuredClone(value);
          }
          for(const fn of listeners) fn(changes,'local');
        },
      },
      onChanged: {addListener: fn => listeners.push(fn)},
    },
    runtime: {
      id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      getManifest: () => (__MANIFEST__),
      openOptionsPage: async () => {window.__memoryTest.openedOptions=true;},
      sendMessage: async msg => {
        messages.push(msg.type);
        if(['checkApi','verifyPairing'].includes(msg.type)) {
          const {checkConnection}=await import('/worker-auth.js');
          return checkConnection(msg.candidate ?? data.settings,{sendNative: async (name,request)=>{
            if(name !== 'org.ai_memory.archive___ARCHIVE__') throw new Error('host unavailable');
            return {status:'ok',schema:2,service:'ai-memory', archive_instance_id:'__ARCHIVE__'};
          }});
        }
        return {ok:true};
      }
    }
  };
}"""


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


# A horizontal scroll assertion alone misses clipping behind an overflow:hidden parent.
LAYOUT = """doc => {
  const view=doc.defaultView, root=doc.documentElement, width=root.clientWidth;
  const controls=[...doc.querySelectorAll('button:not([hidden]),a,input')].filter(e=>e.getClientRects().length);
  return {viewport:view.innerWidth, width, scroll:root.scrollWidth, clipped:controls.filter(e=>{
    const r=e.getBoundingClientRect();
    return r.left<0 || r.right>width+0.5 || r.width<=0 || r.height<24;
  }).map(e=>e.id||e.getAttribute('aria-label')), gear:doc.getElementById('settings').getBoundingClientRect().width};
}"""


def assert_layout(result):
    assert result["scroll"] <= result["width"], result
    assert result["clipped"] == [], result
    assert result["gear"] >= 24, result


CONTRAST = r"""el => {
 const style=getComputedStyle, rgb=s=>s.match(/[\d.]+/g).slice(0,3).map(Number);
 let bg=el;
 while (bg && style(bg).backgroundColor === 'rgba(0, 0, 0, 0)') bg=bg.parentElement;
 const lum=arr=>arr.map(n=>n/255).map(s=>s<=.04045?s/12.92:((s+.055)/1.055)**2.4)
                      .reduce((a,b,i)=>a+b*[.2126,.7152,.0722][i],0);
 const a=lum(rgb(style(el).color)), b=lum(rgb(style(bg).backgroundColor));
 return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
}"""


async def load_file(page, file):
    await page.locator("#connection-file").set_input_files(
        {
            "name": "connection.json",
            "mimeType": "application/json",
            "buffer": json.dumps(file).encode(),
        }
    )


async def verify_pages(origin, output, executable):
    manifest = json.loads((EXTENSION / "manifest.json").read_text())
    bridge = BRIDGE.replace("__MANIFEST__", json.dumps(manifest)).replace("__ARCHIVE__", ARCHIVE_ID)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=str(executable))
        try:
            context = await browser.new_context(viewport={"width": 1200, "height": 1000})
            await context.add_init_script(f"({bridge})()")
            errors = []
            page = await context.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.goto(origin + "/options.html")
            await page.get_by_text("Not connected", exact=True).wait_for()
            assert await page.locator("#sync-now").is_disabled()
            for key, value in ORGANIZED.items():
                assert await page.locator(f"#capture-{key}").is_checked() is value
            assert await page.locator(".brand img").evaluate(
                "el => el.complete && el.naturalWidth>0"
            )
            await page.screenshot(path=output / "settings-setup.png", full_page=True)
            await load_file(page, {**descriptor(ARCHIVE_ID), "transport": "http"})
            await page.locator("#import-result.error").wait_for()
            assert not await page.evaluate("Boolean(__memoryTest.data.settings.nativeHost)")
            await page.locator("#import-connection").focus()
            await load_file(page, descriptor(ARCHIVE_ID))
            await page.get_by_text("Connected to your computer", exact=True).wait_for()
            await page.wait_for_function("document.activeElement.id==='change-connection'")
            assert await page.locator("#connection-guide").is_hidden()
            await page.locator("#change-connection").click()
            await load_file(page, descriptor("9" * 32))
            await page.locator("#import-result.error").wait_for()
            assert await page.evaluate("__memoryTest.data.settings.archiveId") == ARCHIVE_ID
            assert await page.locator("#connection-label").inner_text() == "Connected"
            await page.locator("#test-connection").click()
            await page.locator("#provider-gemini").focus()
            await page.keyboard.press("Space")
            await page.wait_for_function("__memoryTest.data.settings.providers.gemini")
            assert await page.evaluate("document.activeElement.id") == "provider-gemini"
            assert await page.locator("#sync-now").is_disabled()
            assert await page.locator("#rescan-gemini").is_disabled()
            assert "Ordinary Recents" in await page.locator("#effective-plan").inner_text()
            await page.locator("#gemini-selection-warning").wait_for(state="visible")
            await page.screenshot(path=output / "settings-gemini-selection.png", full_page=True)
            await page.locator("#provider-chatgpt").check()
            for key in ("projects", "pinned", "agents"):
                await page.locator(f"#capture-{key}").uncheck()
            assert await page.locator("#sync-now").is_disabled()
            await page.locator("#capture-pinned").check()
            await page.locator("#auto-paused").check()
            await page.locator("#interval").fill("37")
            await page.locator("#interval").press("Tab")
            await page.wait_for_function(
                "__memoryTest.data.settings.intervalMinutes===37 && __memoryTest.data.settings.autoPaused"
            )
            await page.locator("#sync-now").click()
            await page.wait_for_function("__memoryTest.messages.includes('syncNow')")
            await page.evaluate(
                """chrome.storage.local.set({status:{chatgpt:{captureScope:__memoryTest.scope('chatgpt'),lastRunMs:Date.now(),progress:{phase:'complete',created:1,updated:0,unchanged:2}}}})"""
            )
            await page.get_by_text("Saved locally", exact=False).first.wait_for()
            await page.screenshot(path=output / "settings-connected.png", full_page=True)
            await page.emulate_media(color_scheme="dark")
            await page.screenshot(path=output / "settings-dark.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            await page.screenshot(path=output / "settings-mobile.png", full_page=True)

            popup = await context.new_page()
            popup.on("pageerror", lambda exc: errors.append(str(exc)))
            await popup.goto(origin + "/popup.html")
            await popup.get_by_text("Not connected", exact=True).wait_for()
            await popup.locator("#sync").click()
            assert await popup.evaluate("__memoryTest.openedOptions===true")
            await popup.screenshot(path=output / "popup-setup.png", full_page=True)
            await popup.evaluate(
                "settings=>chrome.storage.local.set({settings})",
                {
                    "nativeHost": descriptor(ARCHIVE_ID)["native_host"],
                    "archiveId": ARCHIVE_ID,
                    "providers": {"gemini": True},
                    "captureTypes": ORGANIZED,
                    "intervalMinutes": 37,
                    "autoPaused": True,
                },
            )
            await popup.get_by_text("Connected to your computer", exact=True).wait_for()
            await (
                popup.locator("#plan-hint").get_by_text("Ordinary Recents", exact=False).wait_for()
            )
            assert await popup.locator("#sync").is_disabled()
            assert not await popup.evaluate("__memoryTest.messages.includes('syncNow')")
            for theme in ("light", "dark"):
                await popup.emulate_media(color_scheme=theme)
                disabled = popup.locator("#sync")
                assert await disabled.evaluate("el => getComputedStyle(el).opacity") == "1"
                contrast = await disabled.evaluate(CONTRAST)
                assert contrast >= 4.5, ("#sync:disabled", theme, contrast)
            await popup.locator("#toggle-chatgpt").focus()
            await popup.keyboard.press("Enter")
            await popup.wait_for_function(
                "__memoryTest.data.settings.providers.chatgpt===true && document.activeElement.id==='toggle-chatgpt'"
            )
            await popup.keyboard.press("Space")
            await popup.wait_for_function(
                "__memoryTest.data.settings.providers.chatgpt===false && document.activeElement.id==='toggle-chatgpt'"
            )
            await popup.locator("#filter-recents").click()
            await popup.wait_for_function(
                "!document.getElementById('sync').disabled && document.activeElement.id==='filter-recents'"
            )
            await popup.locator("#sync").click()
            await popup.locator("#toggle-gemini").focus()
            await popup.evaluate(
                """chrome.storage.local.set({status:{gemini:{captureScope:__memoryTest.scope('gemini'),lastRunMs:Date.now(), running:true,progress:{detected:4,processed:2}}}})"""
            )
            await popup.get_by_role("progressbar", name="Gemini local capture").wait_for()
            await popup.wait_for_function("document.activeElement.id==='toggle-gemini'")
            await popup.evaluate(
                """chrome.storage.local.set({status:{gemini:{captureScope:__memoryTest.scope('gemini'),lastRunMs:Date.now(), progress:{phase:'complete',created:1,updated:2,unchanged:3},selection:{skipped:1}}}})"""
            )
            await (
                popup.locator("#notice")
                .get_by_text("1 new · 2 updated · 3 unchanged · 1 skipped", exact=False)
                .wait_for()
            )
            for width in (320, 380, 440):
                await popup.set_viewport_size({"width": width, "height": 600})
                assert_layout(await popup.evaluate(f"({LAYOUT})(document)"))
                for theme in ("light", "dark"):
                    await popup.emulate_media(color_scheme=theme)
                    for selector in ("#api-title", "#schedule", "#sync"):
                        contrast = await popup.locator(selector).evaluate(CONTRAST)
                        assert contrast >= 4.5, (selector, width, theme, contrast)
                    await popup.screenshot(
                        path=output / f"popup-{width}-{theme}.png", full_page=True
                    )
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "chromium_page_shim": "passed",
                        "themes": ["light", "dark"],
                        "widths": [320, 380, 440],
                        "synthetic_native_bridge": True,
                        "output": str(output),
                    }
                )
            )
        finally:
            await browser.close()


def browser_path(provided):
    candidates = ("chromium", "chromium-browser", "google-chrome", "brave")
    selected = provided or next(filter(None, map(shutil.which, candidates)), None)
    if not selected or not Path(selected).is_file():
        raise SystemExit("Specify an installed Chromium executable via --browser-executable.")
    return Path(selected).resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data/ui-check")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietFiles, directory=EXTENSION)
    ) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            asyncio.run(
                verify_pages(
                    f"http://127.0.0.1:{server.server_port}",
                    args.output,
                    browser_path(args.browser_executable),
                )
            )
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
