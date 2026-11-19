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
  window.qaMessages=[];
  const copy = v => structuredClone(v);
  window.chrome = {
    runtime:{
      id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      getManifest:()=>({version:'fixture'}),
      sendMessage:async m=>{
        qaMessages.push(m.type);
        if(m.type==='LIST_SYNC_CATALOG') return {ok:true,catalogToken:'fixture',incomplete:false,
          entries:[{principalId:'fixture',title:'Personal',projects:[{id:'project-1',title:'Project one'}],
          conversations:[{id:'chat-1',title:'Conversation one'}]}],selectedScope:{entries:[]}};
        if(m.type==='SAVE_SYNC_SELECTION') return {ok:true,selectedScope:m.selection};
        if(m.type==='checkApi') return {ok:false,code:'missing_token'};
        return {ok:true};
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
            await page.locator("#load-catalog").press("Enter")
            box = page.locator('#catalog-items input[data-id="chat-1"]')
            await box.focus()
            await page.keyboard.press("Space")
            await expect(box).to_be_checked()
            assert not await page.evaluate("qaMessages.includes('SAVE_SYNC_SELECTION')")
            assert not await page.evaluate("qaMessages.includes('syncNow')")
            await page.locator("#picker-search").fill("Conversation")
            await expect(page.locator("#catalog-items input")).to_have_count(1)
            for width in (320, 380, 440):
                await page.set_viewport_size({"width": width, "height": 600})
                assert_layout(await page.evaluate(f"({LAYOUT})(document)"))
                for theme in ("light", "dark"):
                    await page.emulate_media(color_scheme=theme)
                    await page.screenshot(
                        path=output / f"popup-{width}-{theme}.png", full_page=True
                    )
            assert len(context.pages) == 1
            assert page.url == ORIGIN + "/popup.html"
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
