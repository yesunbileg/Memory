"""Exercise the installed MV3 toolbar and a real framed native host using an empty profile.

All three provider APIs are replaced by local, synthetic metadata and messages.
On Linux use xvfb-run -a python scripts/extension_toolbar_check.py --browser-executable PATH.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from extension_ui_check import EXTENSION, LAYOUT, ROOT, assert_layout, browser_path
from package_extension import build_extension
from playwright.async_api import async_playwright

PROVIDERS = (ROOT / "tests/fixtures/popup-providers.js").read_text()
PROJECT = "g-p-11111111111141118111111111111111"
MEMBER = "33333333-3333-4333-8333-333333333333"
CLAUDE = "44444444-4444-4444-8444-444444444444"
GEMINI = "abcdef123456abcd"


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
    return json.loads(Path(json.loads(result.stdout)["connection_file"]).read_text())


def archived(db):
    with closing(sqlite3.connect(db)) as connection:
        return sorted(row[0] for row in connection.execute("SELECT external_id FROM conversations"))


async def wait_for(check, message):
    # Await actual values, not the truthiness of JavaScript Promise objects.
    try:
        async with asyncio.timeout(15):
            while not await check():
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise AssertionError(message) from exc


async def exercise(worker, page, descriptor, db):
    # The driver is empty HTML in the test copy. Nothing ever opens popup.html as a web tab.
    # Interactive elements, the storage writes and the sync all belong to the real popup.
    extension_id = worker.url.split("/")[2]
    await worker.evaluate(PROVIDERS)
    await page.goto(f"chrome-extension://{extension_id}/test-driver.html")
    await page.evaluate("""async () => {
        window.newTabIds = [];
        chrome.tabs.onCreated.addListener(tab => window.newTabIds.push(tab.id));
        window.originalTabIds = (await chrome.tabs.query({})).map(tab => tab.id).sort();
    }""")
    refused = await page.evaluate("()=>chrome.runtime.sendMessage({type:'syncNow'})")
    assert not refused.get("ok") and refused.get("code") == "untrusted_sender", refused
    await page.evaluate("() => chrome.action.openPopup()")
    await page.wait_for_function(
        "() => chrome.extension.getViews({type:'popup'})[0]?.document.querySelector('#load-catalog')"
    )
    popup_contexts = await page.evaluate(
        "()=>chrome.runtime.getContexts({contextTypes:['POPUP']}).then(items=>items.map(i=>i.contextType))"
    )
    assert popup_contexts == ["POPUP"], popup_contexts

    async def popup(expression, arg=None):
        return await page.evaluate(
            "arg => {const w=chrome.extension.getViews({type:'popup'})[0];"
            "const d=w.document; const q=s=>d.querySelector(s); return (" + expression + ")(arg);}",
            arg,
        )

    async def click(selector):
        enabled = (
            "s => {const el=q(s);return !!el && !el.disabled && !!el.getClientRects().length;}"
        )
        await wait_for(
            lambda: popup(enabled, selector), f"disabled/missing popup control {selector}"
        )
        await popup("s=>q(s).click()", selector)

    async def state(key):
        return await worker.evaluate("key=>chrome.storage.local.get(key).then(x=>x[key])", key)

    async def settings_match(test):
        return test(await state("settings") or {})

    assert not await worker.evaluate("qa.requests.length")
    assert await popup("()=>q('#sync').disabled")
    await click("#settings")
    await popup("""()=>{const input=q('#connection-json');
      input.closest('details').open=true; input.value='not JSON'; q('#verify-json').click();}""")
    await wait_for(
        lambda: popup(
            "()=>!!q('#pair-status').textContent && !q('#pair-status').textContent.includes('Verifying')"
        ),
        "invalid pairing did not report an error",
    )
    assert not (await state("settings") or {}).get("archiveId"), (
        "invalid JSON changed the connection"
    )
    await popup(
        """value=>{
      const input=q('#connection-file'), dt=new w.DataTransfer();
      dt.items.add(new w.File([JSON.stringify(value)],'connection.json',{type:'application/json'}));
      input.files=dt.files; input.dispatchEvent(new w.Event('change',{bubbles:true}));
    }""",
        descriptor,
    )
    await wait_for(
        lambda: settings_match(lambda s: s.get("archiveId") == descriptor["archive_instance_id"]),
        "popup did not pair",
    )
    await click("#auto-paused")
    await wait_for(
        lambda: settings_match(lambda s: s.get("autoPaused")), "could not pause automatic runs"
    )
    for provider in ("chatgpt", "claude", "gemini"):
        await click("#toggle-" + provider)
        await wait_for(
            lambda name=provider: settings_match(lambda s: s.get("providers", {}).get(name)),
            f"could not enable {provider}",
        )
    await click("#settings")
    assert not await worker.evaluate("qa.requests.length"), "setup opened/read a provider"
    assert await popup("()=>q('#sync').disabled"), "no saved items must mean no sync"

    # Explicit catalog load may authenticate but must not open the sign-in site.
    await click("#load-catalog")
    await wait_for(lambda: worker.evaluate("qa.requests.length>0"), "no authentication request")
    await wait_for(
        lambda: popup("()=>!q('#load-catalog').disabled && !q('#catalog-items input')"),
        "sign-in error did not recover to retryable state",
    )
    assert await popup("()=>/sign.in|retry/i.test(q('#picker-status').textContent)")
    assert not archived(db)
    await worker.evaluate("qa.signedIn=true")

    for provider, kind, item in (
        ("chatgpt", "project", PROJECT),
        ("claude", "conversation", CLAUDE),
        ("gemini", "conversation", GEMINI),
    ):
        await click("#tab-" + provider)
        await click("#load-catalog")
        # Gemini summary IDs are opaque and may retain the provider's c_ prefix.
        row = ",".join(
            f'#catalog-items input[data-kind="{kind}"][data-id="{i}"]'
            for i in ([item, "c_" + item] if provider == "gemini" else [item])
        )
        await click(row)
        assert not archived(db) and not await worker.evaluate("qa.detail.length")
        await click("#add-selection")

        async def has_saved(name=provider, expected=item):
            scope = (await state("providerSyncScopes") or {}).get(name)
            return any(
                expected
                in {
                    v.removeprefix("c_")
                    for v in e.get("projectIds", []) + e.get("conversationIds", [])
                }
                for e in (scope or {}).get("entries", [])
            )

        await wait_for(has_saved, f"selection not saved for {provider}")
        saved_row = row.replace("#catalog-items input", "#saved-items button").replace(
            f'[data-kind="{kind}"]', ""
        )
        await wait_for(
            lambda selector=saved_row: popup("s=>!!q(s)", selector),
            f"saved {provider} row is not manageable",
        )
        assert await popup("s=>d.querySelectorAll(s).length", saved_row) == 1
        if provider == "gemini":
            await click("#saved-heading")
            await click(saved_row)
            await wait_for(
                lambda selector=saved_row: popup("s=>!q(s)", selector), "Remove failed inside popup"
            )
            assert not await worker.evaluate("qa.detail.length") and not archived(db)
            await click(row)
            await click("#add-selection")
            await wait_for(has_saved, "could not re-add removed item")
            await click("#saved-heading")
        assert not await worker.evaluate("qa.detail.length"), "catalog or Save read a transcript"
        assert not archived(db), "Save wrote conversations before Sync"
    layout = await popup(f"()=>({LAYOUT})(d)")
    assert_layout(layout)
    await click("#sync")
    target = sorted([MEMBER, CLAUDE, GEMINI])

    async def completed():
        status = await state("status") or {}
        return all(
            status.get(k, {}).get("running") is False for k in ("chatgpt", "claude", "gemini")
        )

    await wait_for(completed, "manual popup sync did not complete")
    report = await worker.evaluate("({detail:qa.detail,unexpected:qa.unexpected})")
    assert archived(db) == target, (archived(db), report, await state("status"))
    assert sorted(report["detail"]) == target, report
    assert not report["unexpected"], report
    opened = await page.evaluate("window.newTabIds")
    assert not opened, ("popup opened a web/extension tab", opened)
    assert await page.evaluate("""async()=>JSON.stringify((await chrome.tabs.query({})).map(t=>t.id).sort())
      === JSON.stringify(window.originalTabIds)""")
    assert page.url.endswith("/test-driver.html")
    print(
        json.dumps(
            {
                "installed_mv3_popup": "passed",
                "actual_popup_contexts": len(popup_contexts),
                "extra_tabs": len(opened),
                "real_native_archive_records": len(target),
                "providers": ["ChatGPT", "Claude", "Gemini"],
            }
        )
    )


async def verify(executable):
    with tempfile.TemporaryDirectory(prefix="ai-memory-toolbar-") as directory:
        folder = Path(directory)
        profile = folder / "config/chromium"
        env = {**os.environ, "XDG_CONFIG_HOME": str(folder / "config")}
        extension = folder / "extension"
        archive = folder / "release.zip"
        build_extension(EXTENSION, archive)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extension)
        (extension / "test-driver.html").write_text("<!doctype html><title>Fixture driver</title>")
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(profile),
                executable_path=str(executable),
                headless=False,
                env=env,
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
                ignore_default_args=["--disable-extensions"],
            )
            try:
                await context.set_offline(True)
                worker = (
                    context.service_workers[0]
                    if context.service_workers
                    else await context.wait_for_event("serviceworker")
                )
                descriptor = register(folder, profile, worker.url.split("/")[2], env)
                await exercise(worker, context.pages[0], descriptor, folder / "fixture.sqlite")
            finally:
                await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", type=Path)
    asyncio.run(verify(browser_path(parser.parse_args().browser_executable)))
