"""Select on synthetic provider pages using the installed toolbar and a real framed native host.

All three provider APIs are replaced by local, synthetic metadata and messages.
On Linux use xvfb-run -a python scripts/extension_toolbar_check.py --browser-executable PATH.
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from extension_ui_check import EXTENSION, LAYOUT, ROOT, assert_layout, browser_path
from package_extension import build_extension
from playwright.async_api import async_playwright, expect

PROVIDERS = (ROOT / "tests/fixtures/popup-providers.js").read_text()
SIDEBAR = ROOT / "tests/fixtures/provider-sidebar.html"
SITES = {
    "chatgpt": "https://chatgpt.com/",
    "claude": "https://claude.ai/new",
    "gemini": "https://gemini.google.com/u/0/app",
}
BOX = "input[data-memory-sync-id]"
PROJECT = "g-p-11111111111141118111111111111111"
MEMBER = "33333333-3333-4333-8333-333333333333"
STANDALONE = "22222222-2222-4222-8222-222222222222"
LATE_MEMBER = "88888888-8888-4888-8888-888888888888"
CLAUDE = "44444444-4444-4444-8444-444444444444"
CLAUDE_PROJECT = "77777777-7777-4777-8777-777777777777"
CLAUDE_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
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


class Toolbar:
    """Address the *actual popup view* while an ordinary provider tab stays active."""

    def __init__(self, driver):
        self.driver = driver

    async def open(self, active):
        await active.bring_to_front()
        await wait_for(
            lambda: self.driver.evaluate(
                "url=>chrome.tabs.query({active:true,currentWindow:true}).then(([tab])=>tab?.url===url)",
                active.url,
            ),
            "the provider fixture is not the active tab",
        )
        await self.driver.evaluate("() => chrome.action.openPopup()")
        # The driver is a background tab; its requestAnimationFrame may be suspended.
        await wait_for(
            lambda: self.driver.evaluate(
                "()=>!!chrome.extension.getViews({type:'popup'})[0]?.document.querySelector('#list-mode')"
            ),
            "the toolbar popup did not open",
        )
        contexts = await self.driver.evaluate(
            "()=>chrome.runtime.getContexts({contextTypes:['POPUP']}).then(xs=>xs.map(x=>x.contextType))"
        )
        assert contexts == ["POPUP"], contexts

    async def ui(self, expression, arg=None):
        return await self.driver.evaluate(
            "arg => {const w=chrome.extension.getViews({type:'popup'})[0];"
            "const d=w.document; const q=s=>d.querySelector(s); return (" + expression + ")(arg);}",
            arg,
        )

    async def click(self, selector):
        await wait_for(
            lambda: self.ui(
                "s=>{const el=q(s); return !!el && !el.disabled && !!el.getClientRects().length}",
                selector,
            ),
            f"disabled/missing popup control {selector}",
        )
        await self.ui("s=>q(s).click()", selector)

    async def close(self):
        await self.ui("()=>w.close()")
        await wait_for(
            lambda: self.driver.evaluate("()=>!chrome.extension.getViews({type:'popup'}).length"),
            "the toolbar popup did not close",
        )

    async def expect_mode(self, enabled):
        await wait_for(
            lambda: self.ui(
                "v=>!q('#list-mode').disabled && q('#list-mode').getAttribute('aria-pressed')===v",
                str(enabled).lower(),
            ),
            f"popup did not report list mode {enabled}",
        )

    async def toggle(self, enabled):
        await self.expect_mode(not enabled)
        await self.click("#list-mode")
        await self.expect_mode(enabled)
        await self.close()


async def state(worker, key):
    return await worker.evaluate("k=>chrome.storage.local.get(k).then(x=>x[k])", key)


async def pair_and_enable(toolbar, worker, descriptor):
    async def settings_match(test):
        return test(await state(worker, "settings") or {})

    assert await toolbar.ui("()=>q('#sync').disabled")
    await toolbar.click("#settings")
    await toolbar.ui("""()=>{const input=q('#connection-json');
      input.closest('details').open=true; input.value='not JSON'; q('#verify-json').click();}""")
    await wait_for(
        lambda: toolbar.ui(
            "()=>!!q('#pair-status').textContent && !q('#pair-status').textContent.includes('Verifying')"
        ),
        "invalid pairing did not report an error",
    )
    assert not (await state(worker, "settings") or {}).get("archiveId"), (
        "invalid JSON changed the connection"
    )
    await toolbar.ui(
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
    await toolbar.click("#auto-paused")
    await wait_for(
        lambda: settings_match(lambda s: s.get("autoPaused")), "could not pause automatic runs"
    )
    for provider in SITES:
        await toolbar.click("#toggle-" + provider)
        await wait_for(
            lambda name=provider: settings_match(lambda s: s.get("providers", {}).get(name)),
            f"could not enable {provider}",
        )
    await toolbar.click("#settings")
    assert not await worker.evaluate("qa.requests.length"), "setup read a provider"
    assert await toolbar.ui("()=>q('#sync').disabled"), "no saved items must mean no sync"


async def expect_scope(worker, provider, projects=(), chats=()):
    """Compare the complete persisted allowlist, not just membership of the last click."""

    async def matches():
        scopes = (await state(worker, "providerSyncScopes")) or {}
        entries = scopes.get(provider, {}).get("entries", [])
        if not entries:
            return not projects and not chats
        return (
            len(entries) == 1
            and bool(entries[0].get("principalId"))
            and sorted(entries[0]["projectIds"]) == sorted(projects)
            and sorted(entries[0]["conversationIds"]) == sorted(chats)
        )

    await wait_for(
        matches, f"incorrect {provider} scope; expected projects={projects}, chats={chats}"
    )


def box(site, item, kind="conversation"):
    return site.locator(f'input[data-memory-sync-kind="{kind}"][data-memory-sync-id="{item}"]')


async def click_row(control):
    # Storage can finish in the worker just before the page processes its reply.
    await expect(control).not_to_have_attribute("aria-busy", "true")
    await control.click()


async def no_capture(worker, db):
    assert not archived(db), "selection changed the archive before sync"
    assert not await worker.evaluate("qa.detail.length"), "selection read transcript bodies"
    assert await worker.evaluate("qa.native.filter(op=>op!=='check')") == [], (
        "selection sent data to the native host"
    )


async def mode_off(toolbar, site, provider):
    assert site.url == SITES[provider], "the page changed while checking a row"
    assert await site.evaluate("hostClicks") == [], "checkbox input leaked to the site's rows"
    await toolbar.open(site)
    await toolbar.toggle(False)
    await expect(site.locator(BOX)).to_have_count(0)


async def chatgpt_checks(worker, toolbar, site):
    await toolbar.toggle(True)
    project = box(site, PROJECT, "project")
    member, standalone = box(site, MEMBER), box(site, STANDALONE)
    await expect(project).to_have_count(2)  # Pinned and Projects refer to the same ID.
    await expect(standalone).to_have_accessible_name(re.compile(".+"))
    anchor_rect = await site.locator("#individual a").bounding_box()
    box_rect = await standalone.bounding_box()
    assert anchor_rect and box_rect
    box_center_y = box_rect["y"] + box_rect["height"] / 2
    assert anchor_rect["y"] <= box_center_y <= anchor_rect["y"] + anchor_rect["height"], (
        "the checkbox is not aligned with its website row",
        anchor_rect,
        box_rect,
    )
    await expect(site.locator("main").get_by_role("checkbox")).to_have_count(0)
    await expect(site.locator("#project > button")).to_have_attribute("aria-expanded", "false")
    await expect(site.locator("#project ul a")).to_have_count(0)
    # Host scripts cannot silently opt a user in via an element.click().
    native_before = await worker.evaluate("qa.native.length")
    await standalone.evaluate("el=>el.click()")
    await expect(standalone).not_to_be_checked()
    await click_row(project.first)
    await expect_scope(worker, "chatgpt", [PROJECT])
    await expect(project.last).to_be_checked()
    await expect(member).to_have_js_property("indeterminate", True)
    # A user can keep an inherited chat explicitly. Removing the project then keeps only that chat.
    await member.press("Space")
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER])
    await click_row(project.last)
    await expect_scope(worker, "chatgpt", chats=[MEMBER])
    await expect(project.first).not_to_be_checked()
    await expect(member).to_be_checked()
    await expect(member).to_have_js_property("indeterminate", False)
    await click_row(project.first)
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER])
    await click_row(member)
    await expect_scope(worker, "chatgpt", [PROJECT])
    await expect(member).to_have_js_property("indeterminate", True)
    # Keep explicit/project overlap for the actual capture de-duplication check.
    await click_row(member)
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER])
    await click_row(standalone)
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER, STANDALONE])
    assert await worker.evaluate("qa.native.length") == native_before, (
        "a website checkbox contacted the native host"
    )
    await site.evaluate("replaceSelectedRow()")
    await expect(standalone).to_have_count(1)
    await expect(standalone).to_be_checked()
    await click_row(standalone)
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER])
    await click_row(standalone)
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER, STANDALONE])
    await mode_off(toolbar, site, "chatgpt")
    # Leaving list mode must only hide UI, not erase choices. Reopening restores the checkmarks.
    await toolbar.open(site)
    await toolbar.toggle(True)
    await expect(project.first).to_be_checked()
    await expect(standalone).to_be_checked()
    await mode_off(toolbar, site, "chatgpt")


async def exercise(context, worker, driver, descriptor, db):
    # Only the two fixture tabs below are created by the harness. Never put popup.html in a tab.
    extension_id = worker.url.split("/")[2]
    await worker.evaluate(PROVIDERS)
    await driver.goto(f"chrome-extension://{extension_id}/test-driver.html")
    refused = await driver.evaluate("()=>chrome.runtime.sendMessage({type:'syncNow'})")
    assert not refused.get("ok") and refused.get("code") == "untrusted_sender", refused
    site = await context.new_page()
    unexpected_navigation = []

    async def fixture(route):
        if route.request.url == "https://chatgpt.com/api/auth/session":
            signed_in = await worker.evaluate(
                "() => { qa.requests.push('chatgpt.com/api/auth/session'); return qa.signedIn; }"
            )
            return await route.fulfill(
                status=200 if signed_in else 401,
                json={
                    "user": {"id": "fixture-user"},
                    "account": {"id": "fixture-account"},
                    "accessToken": "synthetic-tab-token",
                }
                if signed_in
                else {},
            )
        if route.request.is_navigation_request() and route.request.frame == site.main_frame:
            if route.request.url in SITES.values():
                return await route.fulfill(content_type="text/html", path=SIDEBAR)
            unexpected_navigation.append(route.request.url)
        await route.abort()

    await site.route("**/*", fixture)
    await driver.evaluate("""async () => {
        window.newTabIds = [];
        chrome.tabs.onCreated.addListener(tab => window.newTabIds.push(tab.id));
        window.originalTabIds = (await chrome.tabs.query({})).map(tab => tab.id).sort();
    }""")
    toolbar = Toolbar(driver)
    await site.goto(SITES["chatgpt"])
    await expect(site.locator(BOX)).to_have_count(0)
    await toolbar.open(site)
    await toolbar.expect_mode(False)
    await pair_and_enable(toolbar, worker, descriptor)
    # A signed-out visit cannot authorize anything. A retry stays on the same site.
    await toolbar.click("#list-mode")
    await wait_for(lambda: worker.evaluate("qa.requests.length>0"), "sign-in was not verified")
    await wait_for(
        lambda: toolbar.ui(
            "()=>!q('#list-mode').disabled && /sign.in|retry|account|verif|could not|unavailable/i.test(q('#mode-status').textContent)"
        ),
        "sign-in error did not recover to a retryable state",
    )
    await expect(site.locator(BOX)).to_have_count(0)
    await expect_scope(worker, "chatgpt")
    await no_capture(worker, db)
    if await toolbar.ui("()=>q('#list-mode').getAttribute('aria-pressed')==='true'"):
        await toolbar.toggle(False)
        await toolbar.open(site)
    await worker.evaluate("qa.signedIn=true")
    await chatgpt_checks(worker, toolbar, site)
    await no_capture(worker, db)

    for provider, item in [("claude", CLAUDE), ("gemini", GEMINI)]:
        await site.goto(SITES[provider])
        await expect(site.locator(BOX)).to_have_count(0)
        await toolbar.open(site)
        await toolbar.toggle(True)
        individual = box(site, item)
        await expect(individual).to_be_visible()
        projects = [CLAUDE_PROJECT] if provider == "claude" else []
        if projects:
            await click_row(box(site, CLAUDE_PROJECT, "project"))
            await expect_scope(worker, provider, projects)
            await expect(site.locator("#project ul a")).to_have_count(0)
        await click_row(individual)
        await expect_scope(worker, provider, projects, [item])
        await click_row(individual)
        await expect_scope(worker, provider, projects)
        await click_row(individual)
        await expect_scope(worker, provider, projects, [item])
        await mode_off(toolbar, site, provider)
        await no_capture(worker, db)

    # The new member exists only on a second API page, after mode ended. No visible row ever had its ID.
    await worker.evaluate("qa.late=true")
    await toolbar.open(site)
    assert_layout(await toolbar.ui(f"()=>({LAYOUT})(d)"))
    started = await worker.evaluate("Date.now()")
    await toolbar.click("#sync")

    async def completed():
        status = await state(worker, "status") or {}
        return all(
            status.get(k, {}).get("running") is False
            and status[k].get("lastRunMs", 0) >= started
            and status[k].get("trigger") == "manual"
            for k in SITES
        )

    await wait_for(completed, "manual popup sync did not complete")
    target = sorted([MEMBER, STANDALONE, LATE_MEMBER, CLAUDE, CLAUDE_MEMBER, GEMINI])
    report = await worker.evaluate("({detail:qa.detail,unexpected:qa.unexpected,native:qa.native})")
    status = await state(worker, "status")
    assert all(status[k]["progress"]["phase"] == "complete" for k in SITES), status
    assert archived(db) == target, (archived(db), report, status)
    assert sorted(report["detail"]) == target, (
        "missing, extra, or duplicate transcript reads: " + str(report)
    )
    assert "ingest" in report["native"] and not report["unexpected"], report
    assert not unexpected_navigation, unexpected_navigation
    opened = await driver.evaluate("window.newTabIds")
    assert not opened, ("extension opened a new tab", opened)
    assert await driver.evaluate("""async()=>JSON.stringify((await chrome.tabs.query({})).map(t=>t.id).sort())
      === JSON.stringify(window.originalTabIds)""")
    assert driver.url.endswith("/test-driver.html") and site.url == SITES["gemini"]
    print(
        json.dumps(
            {
                "installed_mv3_popup_and_site_selection": "passed",
                "actual_popup_contexts_per_open": 1,
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
                await exercise(
                    context, worker, context.pages[0], descriptor, folder / "fixture.sqlite"
                )
            finally:
                await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", type=Path)
    asyncio.run(verify(browser_path(parser.parse_args().browser_executable)))
