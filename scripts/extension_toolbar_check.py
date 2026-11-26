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
RECENT_ONLY = "55555555-5555-4555-8555-555555555555"
LATE_MEMBER = "88888888-8888-4888-8888-888888888888"
CLAUDE = "44444444-4444-4444-8444-444444444444"
CLAUDE_PROJECT = "77777777-7777-4777-8777-777777777777"
CLAUDE_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GEMINI = "abcdef123456abcd"


def register(folder, profile, extension_id, env, browser):
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
            browser,
            "--browser-user-data-dir",
            str(profile),
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
        text=True,
    )
    registration = json.loads(result.stdout)
    assert registration["auto_connect"] is True
    connection_file = Path(registration["connection_file"])
    archive_id = json.loads(connection_file.read_text())["archive_instance_id"]
    connection_file.unlink()  # Prove the extension setup works without the optional file.
    return archive_id


def archived(db):
    with closing(sqlite3.connect(db)) as connection:
        return sorted(row[0] for row in connection.execute("SELECT external_id FROM conversations"))


def fixture_message(db, external_id):
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute(
            "SELECT m.text FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE c.external_id=?",
            (external_id,),
        ).fetchone()[0]


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


async def pair_and_enable(toolbar, worker, archive_id):
    async def settings_match(test):
        return test(await state(worker, "settings") or {})

    await wait_for(
        lambda: settings_match(lambda s: s.get("archiveId") == archive_id),
        "opening the popup did not connect the registered archive automatically",
    )
    await wait_for(
        lambda: toolbar.ui("()=>q('#api-title').textContent==='Local archive connected'"),
        "popup did not show the automatic connection",
    )
    assert await worker.evaluate("qa.native[0]==='discover' && qa.native.includes('check')")
    await toolbar.click("#settings")
    await toolbar.ui("""()=>{const input=q('#connection-json');
      const details=input.closest('details'); details.parentElement.closest('details').open=true;
      details.open=true; input.value='not JSON'; q('#verify-json').click();}""")
    await wait_for(
        lambda: toolbar.ui(
            "()=>q('#connection-json').getAttribute('aria-invalid')==='true'"
            " && !!q('#json-status').textContent && q('#connection-json').value==='not JSON'"
        ),
        "invalid pairing did not report an error",
    )
    assert (await state(worker, "settings") or {}).get("archiveId") == archive_id, (
        "invalid JSON changed the automatic connection"
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


async def expect_scope(worker, provider, projects=(), chats=(), *, recents=False):
    """Compare the complete persisted allowlist, not just membership of the last click."""

    async def matches():
        scopes = (await state(worker, "providerSyncScopes")) or {}
        entries = scopes.get(provider, {}).get("entries", [])
        if not entries:
            return not projects and not chats and not recents
        return (
            len(entries) == 1
            and bool(entries[0].get("principalId"))
            and sorted(entries[0]["projectIds"]) == sorted(projects)
            and sorted(entries[0]["conversationIds"]) == sorted(chats)
            and entries[0].get("includeRecents", False) == recents
        )

    await wait_for(
        matches,
        f"incorrect {provider} scope; expected projects={projects}, chats={chats}, recents={recents}",
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
    assert (
        await worker.evaluate("qa.native.filter(op=>!['check','discover'].includes(op))") == []
    ), "selection sent data to the native host"


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
    recents = box(site, "recents", "recents")
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
    await click_row(recents)
    await expect_scope(worker, "chatgpt", recents=True)
    await expect(standalone).to_have_js_property("indeterminate", True)
    await expect(member).to_have_js_property("indeterminate", False)
    await click_row(recents)
    await expect_scope(worker, "chatgpt")
    await expect(standalone).to_have_js_property("indeterminate", False)
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
    await click_row(recents)
    await expect_scope(worker, "chatgpt", [PROJECT], [MEMBER, STANDALONE], recents=True)
    await expect(box(site, RECENT_ONLY)).to_have_js_property("indeterminate", True)
    await mode_off(toolbar, site, "chatgpt")


async def exercise(context, worker, driver, archive_id, db):
    # Keep each signed-in provider tab open, including when the popup is opened elsewhere.
    extension_id = worker.url.split("/")[2]
    await worker.evaluate(PROVIDERS)
    await driver.goto(f"chrome-extension://{extension_id}/test-driver.html")
    refused = await driver.evaluate("()=>chrome.runtime.sendMessage({type:'syncNow'})")
    assert not refused.get("ok") and refused.get("code") == "untrusted_sender", refused
    sites = {name: await context.new_page() for name in SITES}
    site = sites["chatgpt"]
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
        if route.request.is_navigation_request() and route.request.frame.parent_frame is None:
            if route.request.url in SITES.values():
                return await route.fulfill(content_type="text/html", path=SIDEBAR)
            unexpected_navigation.append(route.request.url)
        elif route.request.url.startswith(("https://claude.ai/api/", "https://gemini.google.com/")):
            request = route.request
            payload = await worker.evaluate(
                """async ({url, method, post}) => {
                  const reply = await fetch(url, {method, body:post ? new URLSearchParams(post) : undefined});
                  return {status:reply.status, headers:Object.fromEntries(reply.headers), body:await reply.text()};
                }""",
                {"url": request.url, "method": request.method, "post": request.post_data},
            )
            return await route.fulfill(**payload)
        await route.abort()

    for provider_site in sites.values():
        await provider_site.route("**/*", fixture)
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
    await pair_and_enable(toolbar, worker, archive_id)
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
        site = sites[provider]
        await site.goto(SITES[provider])
        await expect(site.locator(BOX)).to_have_count(0)
        await toolbar.open(site)
        if provider == "claude":
            await site.locator("aside").evaluate("el=>el.hidden=true")
            await toolbar.click("#bulk-select")
            await wait_for(
                lambda: toolbar.ui("()=>q('#bulk-status').textContent==='3 chats selected.'"),
                "the toolbar did not select all chats in the closed Claude sidebar",
            )
            await expect_scope(worker, provider, chats=[CLAUDE, CLAUDE_MEMBER, RECENT_ONLY])
            await no_capture(worker, db)
            await toolbar.click("#bulk-clear")
            await expect_scope(worker, provider)
            await toolbar.click("#bulk-undo")
            await expect_scope(worker, provider, chats=[CLAUDE, CLAUDE_MEMBER, RECENT_ONLY])
            await toolbar.click("#bulk-clear")
            await expect_scope(worker, provider)
            await site.locator("aside").evaluate("el=>el.hidden=false")
        await toolbar.toggle(True)
        individual = box(site, item)
        await expect(individual).to_be_visible()
        projects = [CLAUDE_PROJECT] if provider == "claude" else []
        if projects:
            await expect(site.locator("#dialog-chat " + BOX)).to_have_count(0)
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
    await wait_for(
        lambda: toolbar.ui(
            """()=>['ChatGPT','Claude','Gemini'].every(name=>q('#sync-scope').textContent.includes(name))
            && q('#schedule').textContent==='Auto-sync paused' && !q('#sync-scope').hidden"""
        ),
        "the ready popup did not show all three saved providers",
    )
    started = await worker.evaluate("Date.now()")
    await toolbar.click("#sync")
    await wait_for(
        lambda: toolbar.ui("()=>!q('#sync-progress').hidden"),
        "popup did not show sync progress",
    )

    async def completed():
        status = await state(worker, "status") or {}
        return all(
            status.get(k, {}).get("running") is False
            and status[k].get("lastRunMs", 0) >= started
            and status[k].get("trigger") == "manual"
            for k in SITES
        )

    await wait_for(completed, "manual popup sync did not complete")
    target = sorted([MEMBER, STANDALONE, RECENT_ONLY, LATE_MEMBER, CLAUDE, CLAUDE_MEMBER, GEMINI])
    report = await worker.evaluate("""({
      detail:qa.detail,unexpected:qa.unexpected,native:qa.native,
      claudeDetailBytes:qa.claudeDetailBytes
    })""")
    status = await state(worker, "status")
    assert all(status[k]["progress"]["phase"] == "complete" for k in SITES), status
    assert archived(db) == target, (archived(db), report, status)
    assert sorted(report["detail"]) == target, (
        "missing, extra, or duplicate transcript reads: " + str(report)
    )
    assert "ingest" in report["native"] and not report["unexpected"], report
    claude_bytes = report["claudeDetailBytes"]
    assert set(claude_bytes) == {CLAUDE, CLAUDE_MEMBER} and all(
        n > 0 for n in claude_bytes.values()
    )
    await worker.evaluate("qa.detail.length=0")
    started = await worker.evaluate("Date.now()")
    await toolbar.click("#sync")
    await wait_for(completed, "unchanged repeat sync did not complete")
    assert await worker.evaluate("qa.detail") == [], "unchanged transcripts were downloaded"
    status = await state(worker, "status")
    assert status["chatgpt"]["progress"]["alreadyCurrent"] == 4
    assert status["claude"]["progress"]["alreadyCurrent"] == 2
    await worker.evaluate("qa.changed=true")
    started = await worker.evaluate("Date.now()")
    await toolbar.click("#sync")
    await wait_for(completed, "changed chat repeat sync did not complete")
    assert await worker.evaluate("qa.detail") == [STANDALONE], "repeat sync read unexpected chats"
    assert (await state(worker, "status"))["chatgpt"]["progress"]["updated"] == 1
    assert archived(db) == target
    assert fixture_message(db, STANDALONE) == "Updated GPT message"

    await worker.evaluate("Object.assign(qa, {detail: [], changed: false, holdDetail: true})")
    await toolbar.click("#sync")
    await wait_for(lambda: worker.evaluate("qa.detail.length > 0"), "held detail did not start")
    await wait_for(
        lambda: toolbar.ui("()=>q('#sync').textContent==='Stop sync'"),
        "the toolbar offered no way to stop a running sync",
    )
    await toolbar.click("#sync")
    await wait_for(
        lambda: toolbar.ui(
            "()=>q('#sync').textContent==='Sync now' && q('#notice-label').textContent==='Sync stopped'"
        ),
        "stop did not return the toolbar to an idle state",
    )
    assert await worker.evaluate("qa.aborted && qa.detail.length === 1")
    status = (await state(worker, "status"))["chatgpt"]
    assert status["progress"]["phase"] == "cancelled" and not status["continuationPending"]
    assert fixture_message(db, STANDALONE) == "Updated GPT message"
    await toolbar.click("#settings")
    await toolbar.ui("""()=>{
      q('#provider-status').closest('details').open=true;
      q('#provider-status button').focus();
    }""")
    await worker.evaluate("""()=>chrome.storage.local.set({
      activityLog:[{provider:'chatgpt',outcome:'success',timestampMs:Date.now()}]
    })""")
    await wait_for(
        lambda: toolbar.ui("()=>q('#activity').textContent.includes('ChatGPT · success')"),
        "the popup did not render the activity update",
    )
    assert await toolbar.ui("()=>d.activeElement===q('#provider-status button')")
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
                "repeat_detail_reads": {"unchanged": 0, "after_one_update": 1},
                "claude_synthetic_detail_response_bytes": {
                    "standalone": claude_bytes[CLAUDE],
                    "project_member": claude_bytes[CLAUDE_MEMBER],
                },
                "stop_sync_aborts_active_detail": True,
                "providers": ["ChatGPT", "Claude", "Gemini"],
            }
        )
    )


async def verify(executable):
    with tempfile.TemporaryDirectory(prefix="ai-memory-toolbar-") as directory:
        folder = Path(directory)
        config = folder / "config"
        browser = "brave" if executable.name.startswith("brave") else "chromium"
        profile = config / ("BraveSoftware/Brave-Browser" if browser == "brave" else "chromium")
        env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
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
                archive_id = register(folder, profile, worker.url.split("/")[2], env, browser)
                await exercise(
                    context, worker, context.pages[0], archive_id, folder / "fixture.sqlite"
                )
            finally:
                await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", type=Path)
    asyncio.run(verify(browser_path(parser.parse_args().browser_executable)))
