"""Keyboard, theme and viewport checks for the popup page, with a synthetic Chrome bridge.

extension_toolbar_check.py separately installs the MV3 extension and exercises the real toolbar.
"""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "vendor/hstry/extension"
ORIGIN = "http://127.0.0.1:17771"

BRIDGE = """() => {
  const data = {}, listeners = [];
  const supported = !location.search.includes('unsupported');
  let mode = false;
  window.qaMessages=[];
  window.qaPageCommands=[];
  const copy = v => structuredClone(v);
  window.chrome = {
    runtime:{
      id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      getManifest:()=>({version:'fixture'}),
      sendMessage:async m=>{
        qaMessages.push(m.type);
        if(m.type==='checkApi') return {ok:false,code:'missing_token'};
        return {ok:false};
      }
    },
    tabs:{
      query:async()=>[{id:17,url:supported?'https://chatgpt.com/':'https://example.org/'}],
      sendMessage:async(id,m,target)=>{
        if(id!==17 || !supported || target?.frameId!==0) throw Error('Unsupported tab or frame');
        qaPageCommands.push(m.type);
        if(m.type==='SET_PAGE_LIST_MODE') mode=m.enabled;
        else if(m.type!=='GET_PAGE_LIST_MODE') throw Error('Unexpected tab command');
        return {ok:true,provider:'chatgpt',enabled:mode};
      }
    },
    storage:{local:{
      get:async k=>Object.fromEntries((k==null?Object.keys(data):Array.isArray(k)?k:[k]).map(key=>[key,copy(data[key])])),
      set:async obj=>{const changes={};for(const [k,v] of Object.entries(obj)){
        changes[k]={oldValue:data[k],newValue:copy(v)};data[k]=copy(v);}
        for(const f of listeners)f(changes,'local');}
    },onChanged:{addListener:f=>listeners.push(f)}}
  };
}"""

LAYOUT = """doc => {
  const el=doc.documentElement;
  const controls=[...doc.querySelectorAll('button,input:not([type=file]),select')].filter(e=>e.getClientRects().length);
  return {width:el.clientWidth,scroll:el.scrollWidth,clipped:controls.filter(e=>{
    const r=e.getBoundingClientRect(); return r.width<=0 || r.left<-.5 || r.right>el.clientWidth+.5;
  }).map(e=>e.id||e.getAttribute('aria-label'))};
}"""


def assert_layout(layout):
    assert layout["width"] > 0 and layout["scroll"] <= layout["width"], layout
    assert layout["clipped"] == [], layout


def browser_path(provided):
    selected = provided or next(
        filter(None, map(shutil.which, ("chromium", "chromium-browser", "google-chrome"))), None
    )
    if not selected or not Path(selected).is_file():
        raise SystemExit("Specify Chromium with --browser-executable.")
    return Path(selected).resolve()


async def verify(output, executable):
    import mimetypes
    from urllib.parse import urlsplit

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=str(executable))
        try:
            context = await browser.new_context(viewport={"width": 380, "height": 600})
            await context.add_init_script(f"({BRIDGE})()")

            async def local(route):
                # An intercepted virtual origin requires no server, port or real network.
                path = (EXTENSION / urlsplit(route.request.url).path.lstrip("/")).resolve()
                if (
                    not route.request.url.startswith(ORIGIN + "/")
                    or not path.is_relative_to(EXTENSION)
                    or not path.is_file()
                ):
                    return await route.abort()
                return await route.fulfill(path=path, content_type=mimetypes.guess_type(path)[0])

            await context.route("**/*", local)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.goto(ORIGIN + "/popup.html")
            assert await page.locator("#sync").is_disabled()
            await page.locator("#settings").focus()
            await page.keyboard.press("Enter")
            await expect(page.locator("#settings-panel")).to_be_visible()
            assert await page.locator("#connection-file").count() == 1
            await page.locator("#settings").press("Space")
            await expect(page.locator("#settings-panel")).to_be_hidden()
            await expect(page.locator("#list-mode")).to_be_hidden()
            await expect(page.locator("#archive-panel")).to_be_visible()
            await page.locator("#settings").click()
            await expect(page.locator("#archive-panel")).to_be_hidden()
            mode = page.get_by_role("button", name="List mode", exact=True)
            await expect(mode).to_be_enabled()
            await expect(mode).to_have_attribute("aria-pressed", "false")
            await mode.press("Enter")
            await expect(mode).to_have_attribute("aria-pressed", "true")
            await mode.press("Space")
            await expect(mode).to_have_attribute("aria-pressed", "false")
            assert not await page.evaluate("qaMessages.some(t=>t!=='checkApi')")
            assert (
                await page.evaluate("qaPageCommands.filter(t=>t==='SET_PAGE_LIST_MODE').length")
                == 2
            )
            await page.locator("#settings").click()
            for width in (320, 380, 440):
                await page.set_viewport_size({"width": width, "height": 600})
                assert_layout(await page.evaluate(f"({LAYOUT})(document)"))
                for theme in ("light", "dark"):
                    await page.emulate_media(color_scheme=theme)
                    await page.screenshot(
                        path=output / f"popup-{width}-{theme}.png", full_page=True
                    )
                    await page.locator("#settings").click()
                    assert_layout(await page.evaluate(f"({LAYOUT})(document)"))
                    await page.screenshot(
                        path=output / f"settings-{width}-{theme}.png", full_page=True
                    )
                    await page.locator("#settings").click()
            await page.goto(ORIGIN + "/popup.html?unsupported")
            await page.locator("#settings").click()
            await expect(mode).to_be_disabled()
            await expect(mode).to_have_attribute("aria-pressed", "false")
            assert await page.evaluate("qaPageCommands") == []
            assert len(context.pages) == 1
            assert not errors, errors
            print(
                json.dumps(
                    {"keyboard": "passed", "widths": [320, 380, 440], "themes": ["light", "dark"]}
                )
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data/ui-check")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(verify(args.output, browser_path(args.browser_executable)))
