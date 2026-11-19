"""Installed MV3 sidebar regression, using only synthetic provider data and a temporary archive.

Called by extension_toolbar_check.py after it pairs the real extension with an empty native host.
"""

import asyncio
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from playwright.async_api import expect

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/chatgpt-sidebar.html"
PROJECT = "g-p-11111111111141118111111111111111"
RECENT = "33333333-3333-4333-8333-333333333333"
MEMBER = "88888888-8888-4888-8888-888888888888"

# Intercept the external boundary inside the installed worker itself, including
# service-worker fetches that are invisible to a page route. No provider call escapes.
PROVIDER = """({project, recent, member}) => {
  self.fixtureReads = [];
  self.fixtureSignedIn = false;
  self.fixtureNative = [];
  const sendNative = chrome.runtime.sendNativeMessage.bind(chrome.runtime);
  chrome.runtime.sendNativeMessage = async (host, packet) => {
    const kind = packet.op ?? 'ingest';
    try {
      const result = await sendNative(host, packet);
      self.fixtureNative.push([kind, result?.error ?? 'accepted']);
      return result;
    } catch (error) {
      self.fixtureNative.push([kind, error.message]);
      throw error;
    }
  };
  self.fetch = async (resource) => {
    const url = new URL(resource.url ?? resource);
    self.fixtureReads.push(url.pathname);
    if (url.origin !== 'https://chatgpt.com') throw Error('Unexpected origin');
    if (url.pathname === '/api/auth/session') {
      if (!self.fixtureSignedIn) return Response.json({}, {status:401});
      return Response.json({user:{id:'fixture-user'},account:{id:'fixture-account'},
        accessToken:'fake-token'});
    }
    if (url.pathname === '/backend-api/accounts/check/v4-2023-04-27')
      return Response.json({accounts:{a:{account:{account_id:'fixture-account',structure:'personal'}}}});
    if (url.pathname === '/backend-api/conversations')
      return Response.json({total:2,items:[recent,'55555555-5555-4555-8555-555555555555'].map(
        id=>({id,is_pinned:false,project_id:null,gizmo_id:null}))});
    if (url.pathname === '/backend-api/gizmos/snorlax/sidebar')
      return Response.json({items:[project,'g-p-22222222222242228222222222222222'].map(
        id=>({gizmo:{gizmo:{id}}})),cursor:null});
    if (url.pathname === '/backend-api/gizmos/'+project+'/conversations')
      return Response.json({items:[{id:member}],cursor:null});
    if ([recent,member].some(id=>url.pathname === '/backend-api/conversation/'+id)) {
      const id = url.pathname.split('/').pop();
      return Response.json({id,title:'Fixture '+id,current_node:'node',mapping:{node:{
        id:'node',parent:null,message:{id:'message-'+id,author:{role:'user'},
        content:{content_type:'text',parts:['Synthetic archive verification']}}
      }}});
    }
    throw Error('Unexpected request: '+url.pathname);
  };
}"""


def archived(db):
    with closing(sqlite3.connect(db)) as connection:
        return sorted(row[0] for row in connection.execute("SELECT external_id FROM conversations"))


async def wait_for_storage(page, predicate):
    # Playwright's wait_for_function tests the Promise itself for truthiness.
    # Evaluate and await each storage check before deciding whether to retry.
    async with asyncio.timeout(15):
        while not await page.evaluate(predicate):
            await asyncio.sleep(0.05)


async def verify_sidebar(context, worker, options, db):
    await worker.evaluate(PROVIDER, {"project": PROJECT, "recent": RECENT, "member": MEMBER})
    await options.evaluate("""async () => {
      const {settings} = await chrome.storage.local.get('settings');
      await chrome.storage.local.set({settings:{...settings, autoPaused:true,
        providers:{chatgpt:true,claude:false,gemini:false},
        captureTypes:{projects:true,pinned:true,agents:true,recents:false}}});
    }""")
    page = await context.new_page()
    requests = []

    async def fixture(route):
        if route.request.is_navigation_request() and route.request.url == "https://chatgpt.com/":
            return await route.fulfill(content_type="text/html", path=FIXTURE)
        requests.append(route.request.url)
        await route.abort()

    await page.route("**/*", fixture)
    await page.goto("https://chatgpt.com/")
    picks = page.get_by_role("checkbox")
    await expect(picks).to_have_count(4)
    for row in ("pinned-project", "project", "recent", "another"):
        scope = page.locator(f"#{row}")
        control = scope.get_by_role("checkbox")
        await expect(control).to_have_accessible_name(re.compile(r".+"))
        anchor = await scope.locator("a").first.bounding_box()
        box = await control.bounding_box()
        assert anchor and box, row
        assert anchor["y"] <= box["y"] + box["height"] / 2 <= anchor["y"] + anchor["height"], row
        assert anchor["x"] <= box["x"] < anchor["x"] + anchor["width"], row

    add = page.get_by_role("button", name="Add to sync list", exact=True)
    await expect(add).to_be_disabled()
    pinned = page.locator("#pinned-project").get_by_role("checkbox")
    recent = page.locator("#recent").get_by_role("checkbox")
    other = page.locator("#another").get_by_role("checkbox")
    await recent.evaluate("box => box.click()")
    await expect(recent).not_to_be_checked()
    await pinned.check()
    await recent.check()
    await other.check()
    await other.uncheck()
    # Host scripts can dispatch DOM events. A programmatic click is not an Add gesture.
    await add.evaluate("button => button.click()")
    await expect(recent).to_be_checked()
    assert await page.evaluate("hostClicks") == [], "checkbox clicks leaked to the site's rows"
    assert await worker.evaluate("fixtureReads") == [], "checking a box read provider data"
    assert await options.evaluate(
        "async()=>!(await chrome.storage.local.get('chatgptSyncList')).chatgptSyncList"
    )
    # The host may remove and rebuild a row with no extension nodes during navigation.
    await page.locator("#recent").evaluate("""el=>{
      const fresh=document.createElement('li'); fresh.id=el.id;
      const a=document.createElement('a'); a.href=el.querySelector('a').href;
      a.textContent='Renamed chat'; fresh.append(a); el.replaceWith(fresh);
    }""")
    await expect(recent).to_be_checked()
    await expect(picks).to_have_count(4)

    # A failed permission/session check must leave the pending picks available for retry.
    await add.click()
    await expect(add).to_be_enabled()
    await expect(page.get_by_role("status")).to_contain_text(re.compile(r"could not save", re.I))
    await expect(pinned).to_be_checked()
    await expect(recent).to_be_checked()
    assert not archived(db)
    await worker.evaluate("self.fixtureSignedIn=true")
    await add.click()
    await wait_for_storage(
        options,
        """async () => {
      const {chatgptSyncList}=await chrome.storage.local.get('chatgptSyncList');
      return !!chatgptSyncList?.entries?.length;
    }""",
    )
    stored = await options.evaluate("async()=>(await chrome.storage.local.get()).chatgptSyncList")
    entry = stored["entries"][0]
    assert entry["projectIds"] == [PROJECT] and entry["conversationIds"] == [RECENT], entry
    assert not archived(db), "Add wrote conversations to the archive before any capture request"
    assert set(await worker.evaluate("fixtureReads")) == {"/api/auth/session"}
    assert await page.evaluate("hostClicks") == []
    assert not requests, requests

    # Use a different extension page for the explicit capture request. The content script can only Add.
    result = await options.evaluate(
        "()=>chrome.runtime.sendMessage({type:'syncProvider',provider:'chatgpt'})"
    )
    assert result["ok"], result
    await wait_for_storage(
        options,
        """async () => {
      const {status}=await chrome.storage.local.get('status');
      return status?.chatgpt?.running === false;
    }""",
    )
    reads = await worker.evaluate("fixtureReads")
    records = archived(db)
    state = await options.evaluate(
        "async()=> (await chrome.storage.local.get('status')).status.chatgpt"
    )
    assert state["progress"]["phase"] == "complete" and records == sorted([MEMBER, RECENT]), {
        "records": records,
        "reads": reads,
        "native": await worker.evaluate("fixtureNative"),
        "failure": state.get("lastErrorCode"),
        "warning": state.get("selectionWarning"),
        "progress": state.get("progress"),
    }
    assert [
        p for p in reads if p.startswith("/backend-api/gizmos/") and p.endswith("/conversations")
    ] == [f"/backend-api/gizmos/{PROJECT}/conversations"]
    assert sorted(p for p in reads if p.startswith("/backend-api/conversation/")) == sorted(
        f"/backend-api/conversation/{item}" for item in (MEMBER, RECENT)
    )
    await page.close()
