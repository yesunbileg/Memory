"""Render real extension pages with a test Chrome bridge and real local auth.

Run: UV_CACHE_DIR=/workspace/.cache/uv uv run --with playwright python scripts/extension_ui_check.py
This isolated harness does not install the extension or access signed-in websites.
"""

import asyncio
import functools
import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

from ai_memory.ingest import make_server

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "vendor/hstry/extension"
TOKEN = "synthetic-ui-private-connection-token"

BRIDGE = """() => {
  const data = {settings: {}, status: {}};
  const listeners = [];
  window.__memoryTest = {data, messages: []};
  window.chrome = {
    storage: {
      local: {
        get: async keys => typeof keys === 'string' ? {[keys]: data[keys]} : Object.fromEntries(keys.map(key => [key,data[key]])),
        set: async values => { const changes={}; for(const [key,value] of Object.entries(values)){changes[key]={newValue:value,oldValue:data[key]};data[key]=structuredClone(value);} for(const fn of listeners)fn(changes); },
      },
      onChanged: {addListener: fn => listeners.push(fn)},
    },
    runtime: {
      getManifest: () => ({version:'0.5.0'}),
      openOptionsPage: async () => {window.__memoryTest.openedOptions=true;},
      sendMessage: async message => {
        window.__memoryTest.messages.push(message.type);
        if(message.type==='checkApi'){
          const {checkConnection}=await import('/worker-auth.js');
          return checkConnection(data.settings,{fetchImpl: (_url, options) => fetch('/__test/auth',options)});
        }
        return {ok:true};
      },
    },
  };
}"""


def handler(receiver_port):
    class StaticBridge(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/__test/auth":
                self.send_error(404)
                return
            # This bridge uses synthetic credentials only and forwards to the real
            # receiver, so file import must actually authenticate to become green.
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(min(length, 16384))
            request = urllib.request.Request(
                f"http://127.0.0.1:{receiver_port}/auth/check", data=body,
                headers={"Content-Type": "application/json", "Authorization": self.headers.get("Authorization", "")},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    code, content = response.status, response.read()
            except urllib.error.HTTPError as error:
                code, content = error.code, error.read()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return functools.partial(StaticBridge, directory=str(EXTENSION))


async def verify_pages(origin, receiver_port, artifacts):
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path="/usr/bin/chromium", headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1200, "height": 1000}, color_scheme="light")
            await context.add_init_script(f"({BRIDGE})()")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(origin + "/options.html")
            await page.wait_for_function("window.__memoryTest.messages.includes('checkApi')")
            await page.get_by_text("Not connected", exact=True).wait_for()
            assert await page.locator("#sync-now").is_disabled()
            assert not await page.locator("#capture-recents").is_checked()
            for name in ("projects", "pinned", "agents"):
                assert await page.locator(f"#capture-{name}").is_checked(), name
            assert await page.locator(".brand img").evaluate("img => img.complete && img.naturalWidth>0")
            await page.screenshot(path=str(artifacts / "settings-setup.png"), full_page=True)

            await page.locator("#provider-chatgpt").check()
            await page.wait_for_function("window.__memoryTest.data.settings.providers?.chatgpt===true")
            connection = {"schema": 1, "application": "ai-memory", "host": "127.0.0.1", "port": receiver_port, "token": TOKEN}

            async def import_file(value):
                await page.locator("#connection-file").set_input_files({
                    "name": "extension-connection.json", "mimeType": "application/json",
                    "buffer": json.dumps(value).encode(),
                })

            await import_file({**connection, "host": "example.com"})
            await page.locator("#import-result.error").wait_for()
            assert not await page.evaluate("Boolean(window.__memoryTest.data.settings.token)")
            await import_file({**connection, "token": "synthetic-wrong-connection-token"})
            await page.get_by_text("Your connection needs an update", exact=True).wait_for()
            assert await page.locator("#sync-now").is_disabled()

            await import_file(connection)
            await page.get_by_text("Connected to your computer", exact=True).wait_for()
            assert await page.locator("#connection-guide").is_hidden()
            assert await page.locator("#sync-now").is_enabled()
            for name in ("projects", "pinned", "agents"):
                await page.locator(f"#capture-{name}").uncheck()
            await page.wait_for_function("Object.values(window.__memoryTest.data.settings.captureTypes).every(value=>!value)")
            assert await page.locator("#sync-now").is_disabled()
            await page.locator("#capture-pinned").check()
            await page.wait_for_function("window.__memoryTest.data.settings.captureTypes.pinned===true")
            assert await page.locator("#sync-now").is_enabled()
            await page.locator("#sync-now").click()
            await page.wait_for_function("window.__memoryTest.messages.includes('syncNow')")
            for name in ("projects", "agents"):
                await page.locator(f"#capture-{name}").check()
            await page.screenshot(path=str(artifacts / "settings-connected.png"), full_page=True)
            await page.emulate_media(color_scheme="dark")
            await page.screenshot(path=str(artifacts / "settings-dark.png"), full_page=True)
            await page.emulate_media(color_scheme="light")
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            await page.screenshot(path=str(artifacts / "settings-mobile.png"), full_page=True)

            popup = await context.new_page()
            popup.on("pageerror", lambda error: errors.append(str(error)))
            await popup.set_viewport_size({"width": 380, "height": 700})
            await popup.goto(origin + "/popup.html")
            await popup.get_by_text("Not connected", exact=True).wait_for()
            await popup.locator("#sync").click()
            assert await popup.evaluate("window.__memoryTest.openedOptions===true")
            await popup.screenshot(path=str(artifacts / "popup-setup.png"), full_page=True)
            assert not errors, errors
            print(json.dumps({"real_chromium_ui": "passed", "real_local_auth": "passed", "file_import": "passed", "recents_default": "excluded", "checklist_persistence": "passed", "mobile_overflow": "none", "screenshots": str(artifacts)}))
            await context.close()
        finally:
            await browser.close()


def main():
    artifacts = ROOT / "data/ui-check"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ai-memory-ui-") as temporary:
        receiver = make_server(Path(temporary) / "archive.sqlite", TOKEN, port=0)
        static = ThreadingHTTPServer(("127.0.0.1", 0), handler(receiver.server_port))
        for server in (receiver, static):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            asyncio.run(verify_pages(f"http://127.0.0.1:{static.server_port}", receiver.server_port, artifacts))
        finally:
            for server in (static, receiver):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
