import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const html = await readFile(new URL('../popup.html', import.meta.url), 'utf8');
let instance = 0;

// The browser owns these boundaries. The popup module, settings, pairing and scope
// parsing run unchanged. The Chromium toolbar test exercises the same flow in real DOM.
class Element {
  constructor(attrs = '') {
    this.value = '';
    this.checked = false;
    this.textContent = '';
    this.children = [];
    this.dataset = {};
    this.events = {};
    this.hidden = /\bhidden\b/.test(attrs);
    this.disabled = /\bdisabled\b/.test(attrs);
    this.attributes = Object.fromEntries(
      [...attrs.matchAll(/([\w-]+)="([^"]*)"/g)].map(([, key, value]) => [key, value]),
    );
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
  }
  getAttribute(k) {
    return this.attributes[k];
  }
  removeAttribute(k) {
    delete this.attributes[k];
  }
  addEventListener(k, v) {
    this.events[k] = v;
  }
  fire(k) {
    if (!(k === 'click' && this.disabled)) return this.events[k]?.({ target: this });
  }
}

async function popup({
  tab = { id: 23, url: 'https://chatgpt.com/' },
  enabled = false,
  data = {},
  workerReply = () => ({ ok: false, code: 'missing_token' }),
  pageReply,
} = {}) {
  const dom = new Map(
    [...html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)].map(([attrs, id]) => [
      id,
      new Element(attrs),
    ]),
  );
  const get = (id) => dom.get(id);
  const calls = [],
    queries = [],
    messages = [];
  let changed;
  const fixture = { tab };
  globalThis.document = { getElementById: get, createElement: () => new Element() };
  globalThis.chrome = {
    storage: {
      local: {
        get: async (keys) =>
          Object.fromEntries([].concat(keys).map((k) => [k, structuredClone(data[k])])),
        set: async (v) => Object.assign(data, structuredClone(v)),
      },
      onChanged: { addListener: (f) => (changed = f) },
    },
    tabs: {
      query: async (q) => {
        queries.push(q);
        return fixture.tab ? [fixture.tab] : [];
      },
      sendMessage: async (tabId, message, options) => {
        messages.push({ tabId, message, options });
        if (pageReply) return pageReply(message);
        if (message.type === 'SET_PAGE_LIST_MODE') enabled = message.enabled;
        return {
          ok: true,
          provider: new URL(fixture.tab.url).hostname.split('.')[0],
          enabled,
        };
      },
    },
    runtime: {
      id: 'a'.repeat(32),
      getManifest: () => ({ version: '0.0' }),
      sendMessage: async (m) => {
        calls.push(m);
        return workerReply(m);
      },
    },
  };
  await import('../popup.js?workflow=' + ++instance);
  return { get, data, calls, queries, messages, fixture, changed };
}

test('opening the popup is passive; its only selection control changes list mode on the active page', async () => {
  const { get, data, calls, queries, messages } = await popup();
  const toggle = get('list-mode');
  assert.ok(toggle, 'the toolbar exposes a list mode control');
  assert.ok(!get('catalog-items') && !get('saved-items') && !get('add-selection'));
  assert.equal(toggle.disabled, false, 'choosing on the page does not need a connected archive');
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  assert.match(get('mode-site').textContent, /ChatGPT/);
  assert.deepEqual(queries, [{ active: true, currentWindow: true }]);
  assert.deepEqual(messages, [
    { tabId: 23, message: { type: 'GET_PAGE_LIST_MODE' }, options: { frameId: 0 } },
  ]);
  await toggle.fire('click');
  assert.equal(toggle.getAttribute('aria-pressed'), 'true');
  assert.equal(get('sync').disabled, true);
  await toggle.fire('click');
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  assert.deepEqual(
    messages.slice(1).map(({ message }) => message),
    [
      { type: 'SET_PAGE_LIST_MODE', enabled: true },
      { type: 'SET_PAGE_LIST_MODE', enabled: false },
    ],
  );
  assert.deepEqual(
    calls.map((m) => m.type),
    ['checkApi'],
    'toggling must not fetch a catalog or sync',
  );
  assert.deepEqual(data, {}, 'mode is not a persistent setting or a saved item');
});

test('reopening shows the document’s actual mode; switching active tabs never toggles the other page by accident', async () => {
  const { get, messages, fixture } = await popup({ enabled: true });
  const toggle = get('list-mode');
  assert.ok(toggle);
  assert.equal(toggle.getAttribute('aria-pressed'), 'true');
  fixture.tab = { id: 24, url: 'https://claude.ai/recents' };
  await toggle.fire('click');
  assert.deepEqual(messages.at(-1), {
    tabId: 24,
    message: { type: 'GET_PAGE_LIST_MODE' },
    options: { frameId: 0 },
  });
  assert.match(get('mode-site').textContent, /Claude/);
  assert.equal(messages.filter((m) => m.message.type === 'SET_PAGE_LIST_MODE').length, 0);
});

test('ineligible tabs do not receive messages, and a page that needs reloading cannot falsely appear enabled', async () => {
  for (const tab of [
    null,
    { id: 3 },
    { id: 3, url: 'chrome://newtab/' },
    { id: 3, url: 'blob:https://chatgpt.com/temporary' },
    { id: 3, url: 'https://chatgpt.com.other.example/c/fake' },
    { id: 3, url: 'http://claude.ai/' },
    { id: 3, url: 'https://gemini.google.com:8080/app' },
  ]) {
    const { get, messages } = await popup({ tab });
    assert.ok(get('list-mode'));
    assert.ok(get('list-mode').disabled);
    assert.equal(messages.length, 0);
    assert.match(get('mode-status').textContent, /ChatGPT.*Claude.*Gemini/);
  }
  const { get } = await popup({
    tab: { id: 3, url: 'https://gemini.google.com/app' },
    pageReply: () => {
      throw Error('No receiving end');
    },
  });
  assert.ok(get('list-mode').disabled);
  assert.equal(get('list-mode').getAttribute('aria-pressed'), 'false');
  assert.match(get('mode-status').textContent, /reload/i);
});

test('unsupported provider routes ask for a chat page; reloaded tabs read fresh state without enabling anything', async () => {
  const { get } = await popup({
    tab: { id: 8, url: 'https://claude.ai/settings' },
    pageReply: () => ({ ok: false, enabled: false, code: 'page_unavailable' }),
  });
  assert.equal(get('list-mode').disabled, true);
  assert.match(get('mode-status').textContent, /chat list/i);
  assert.doesNotMatch(get('mode-status').textContent, /reload/i);
  const reloaded = await popup({ tab: { id: 8, url: 'https://claude.ai/recents' } });
  assert.equal(reloaded.get('list-mode').getAttribute('aria-pressed'), 'false');
  assert.deepEqual(
    reloaded.messages.map(({ message }) => message.type),
    ['GET_PAGE_LIST_MODE'],
  );
});

test('a failed sign-in check leaves mode off and retryable; a second click while checking is ignored', async () => {
  let complete, started;
  const checking = new Promise((r) => (started = r));
  let signedIn = false;
  const { get, messages } = await popup({
    pageReply(message) {
      if (message.type === 'GET_PAGE_LIST_MODE')
        return { ok: true, provider: 'chatgpt', enabled: false };
      if (signedIn) return { ok: true, provider: 'chatgpt', enabled: true };
      started();
      return new Promise((r) => (complete = r));
    },
  });
  const toggle = get('list-mode');
  const first = toggle.fire('click');
  await checking;
  assert.equal(toggle.getAttribute('aria-busy'), 'true');
  assert.match(get('mode-status').textContent, /checking/i);
  await toggle.fire('click');
  assert.equal(messages.length, 2);
  complete({ ok: false, enabled: false, code: 'not_logged_in' });
  await first;
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  assert.equal(toggle.disabled, false);
  assert.match(get('mode-status').textContent, /sign in to ChatGPT/i);
  signedIn = true;
  await toggle.fire('click');
  assert.equal(toggle.getAttribute('aria-pressed'), 'true');
});

test('an unverified page leaves list mode off and explains how to retry in the same tab', async () => {
  const tab = { id: 8, url: 'https://claude.ai/project/unverified' };
  const { get, messages } = await popup({
    tab,
    pageReply(message) {
      if (message.type === 'GET_PAGE_LIST_MODE')
        return { ok: true, provider: 'claude', enabled: false };
      return tab.url === 'https://claude.ai/'
        ? { ok: true, provider: 'claude', enabled: true }
        : { ok: false, enabled: false, code: 'page_unverified' };
    },
  });
  const toggle = get('list-mode');
  await toggle.fire('click');
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  assert.equal(toggle.disabled, false);
  assert.match(get('mode-status').textContent, /cannot verify.*Claude home page.*try list mode/i);
  assert.equal(tab.url, 'https://claude.ai/project/unverified');
  tab.url = 'https://claude.ai/';
  await toggle.fire('click');
  assert.equal(toggle.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(
    messages.map((m) => m.tabId),
    [8, 8, 8],
  );
});

test('the popup verifies pairing, merges preferences, and syncs only on request against a nonempty saved scope', async () => {
  const archive = 'a'.repeat(32),
    host = (id) => 'org.ai_memory.archive_' + id;
  let finishSlow, signalSlow;
  const startSlow = new Promise((r) => (signalSlow = r));
  const { get, data, calls, changed } = await popup({
    data: {
      settings: {
        archiveId: archive,
        nativeHost: host(archive),
        providers: { chatgpt: true },
        autoPaused: true,
        intervalMinutes: 45,
      },
      providerSyncScopes: { chatgpt: { entries: { invalid: true } } },
    },
    workerReply(m) {
      if (m.type === 'verifyPairing') {
        const id = m.candidate.archiveId;
        if (id === 'b'.repeat(32)) {
          signalSlow();
          return new Promise((r) => (finishSlow = () => r({ ok: true, archiveId: id })));
        }
        if (id === 'c'.repeat(32)) return { ok: false, code: 'offline' };
        return { ok: true, archiveId: id };
      }
      return { ok: true };
    },
  });
  assert.ok(get('sync').disabled, 'malformed saved scope does not enable sync or break settings');
  assert.deepEqual(
    calls.map((m) => m.type),
    ['checkApi'],
  );
  const paste = (id) => {
    get('connection-json').value = JSON.stringify({
      schema: 2,
      application: 'ai-memory',
      transport: 'native',
      native_host: host(id),
      archive_instance_id: id,
    });
    return get('verify-json').fire('click');
  };
  const slow = paste('b'.repeat(32));
  await startSlow;
  await paste('c'.repeat(32));
  finishSlow();
  await slow;
  assert.equal(data.settings.archiveId, archive, 'stale success cannot replace valid pairing');
  assert.equal(get('connection-json').value, '');
  await paste('d'.repeat(32));
  assert.equal(data.settings.archiveId, 'd'.repeat(32));
  assert.equal(data.settings.autoPaused, true);
  assert.equal(data.settings.intervalMinutes, 45);
  get('toggle-claude').checked = true;
  get('toggle-gemini').checked = true;
  await Promise.all([get('toggle-claude').fire('change'), get('toggle-gemini').fire('change')]);
  assert.equal(data.settings.providers.claude, true);
  assert.equal(data.settings.providers.gemini, true);
  data.providerSyncScopes = {
    chatgpt: { entries: [{ principalId: 'A', projectIds: ['p'], conversationIds: [] }] },
  };
  await changed({ providerSyncScopes: { newValue: data.providerSyncScopes } }, 'local');
  assert.ok(!get('sync').disabled);
  assert.ok(!calls.some((m) => m.type === 'syncNow' || m.type === 'fullSyncProvider'));
  await get('sync').fire('click');
  assert.equal(calls.at(-1).type, 'syncNow');
  const chatgptRescan = get('provider-status').children[0].children[1];
  assert.equal(chatgptRescan.getAttribute('aria-label'), 'Rescan saved ChatGPT items');
  await chatgptRescan.fire('click');
  assert.deepEqual(calls.at(-1), { type: 'fullSyncProvider', provider: 'chatgpt' });
});
