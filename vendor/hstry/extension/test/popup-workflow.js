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
  focus() {
    document.activeElement = this;
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
  const body = new Element();
  globalThis.document = {
    getElementById: get,
    createElement: () => new Element(),
    body,
    activeElement: body,
  };
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

test('opening the popup is passive and list mode changes only on the active page', async () => {
  const { get, data, calls, queries, messages } = await popup();
  const toggle = get('list-mode');
  assert.ok(toggle, 'Settings exposes a list mode control');
  assert.equal(get('settings-panel').hidden, true);
  assert.equal(get('archive-panel').hidden, false);
  await get('settings').fire('click');
  assert.equal(get('settings-panel').hidden, false);
  assert.equal(get('archive-panel').hidden, true);
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

test('selecting all chats works with list mode off and reports the saved count', async () => {
  const data = {};
  const { get, calls, messages } = await popup({
    tab: { id: 23, url: 'https://claude.ai/new' },
    data,
    workerReply: (m) => {
      if (m.type !== 'changeBulkSelection') return { ok: false };
      data.providerSyncScopes = {
        claude: { entries: [{ principalId: 'owner', projectIds: [], conversationIds: ['chat'] }] },
      };
      return { ok: true, title: 'Personal', count: 42 };
    },
  });
  assert.equal(get('bulk-controls').hidden, false);
  assert.equal(get('list-mode').getAttribute('aria-pressed'), 'false');
  await get('bulk-select').fire('click');
  assert.deepEqual(calls.at(-1), {
    type: 'changeBulkSelection',
    tabId: 23,
    provider: 'claude',
    url: 'https://claude.ai/new',
    action: 'select',
  });
  assert.equal(get('bulk-account').textContent, 'Claude · Personal');
  assert.equal(get('bulk-status').textContent, '42 chats selected.');
  assert.equal(get('bulk-enable').hidden, false);
  assert.equal(messages.length, 1);
  assert.ok(!calls.some((m) => m.type === 'syncNow'));
});

test('multiple workspaces require an explicit choice before bulk selection', async () => {
  const { get, calls } = await popup({
    tab: { id: 24, url: 'https://claude.ai/new' },
    workerReply: (m) =>
      m.type !== 'changeBulkSelection'
        ? { ok: false }
        : m.token
          ? { ok: true, title: 'Team', count: 8 }
          : {
              ok: true,
              token: 'fixture-token',
              choices: [
                { title: 'Personal', index: 0 },
                { title: 'Team', index: 1 },
              ],
            },
  });
  get('bulk-select').focus();
  await get('bulk-select').fire('click');
  assert.equal(get('bulk-workspace-field').hidden, false);
  assert.equal(get('bulk-select').disabled, true);
  assert.equal(document.activeElement, get('bulk-workspace'));
  get('bulk-workspace').value = '1';
  get('bulk-workspace').fire('change');
  await get('bulk-select').fire('click');
  assert.equal(calls.at(-1).token, 'fixture-token');
  assert.equal(calls.at(-1).index, 1);
  assert.equal(get('bulk-status').textContent, '8 chats selected.');
});

test('clear names the workspace and focuses Undo, then restores focus to Clear', async () => {
  const { get, calls } = await popup({
    tab: { id: 24, url: 'https://claude.ai/new' },
    workerReply: (m) => {
      if (m.action === 'clear') return { ok: true, title: 'Team', undoToken: 'fixture-undo' };
      if (m.action === 'undo') return { ok: true, title: 'Team' };
      return { ok: true };
    },
  });
  get('bulk-clear').focus();
  await get('bulk-clear').fire('click');
  assert.equal(get('bulk-status').textContent, 'Selection cleared · Team');
  assert.equal(get('bulk-undo').hidden, false);
  assert.equal(document.activeElement, get('bulk-undo'));
  await get('bulk-undo').fire('click');
  assert.equal(calls.at(-1).token, 'fixture-undo');
  assert.equal(get('bulk-status').textContent, 'Selection restored · Team');
  assert.equal(get('bulk-undo').hidden, true);
  assert.equal(document.activeElement, get('bulk-clear'));
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
    assert.match(get('mode-status').textContent, /unavailable on this tab/i);
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
  assert.equal(JSON.parse(get('connection-json').value).archive_instance_id, 'c'.repeat(32));
  assert.equal(get('connection-json').getAttribute('aria-invalid'), 'true');
  await paste('d'.repeat(32));
  assert.equal(data.settings.archiveId, 'd'.repeat(32));
  assert.equal(get('connection-json').value, '');
  assert.match(get('json-status').textContent, /verified and saved/i);
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
  assert.equal(get('sync-scope').textContent, 'ChatGPT · 1 project');
  assert.equal(get('schedule').textContent, 'Auto-sync paused');
  assert.ok(!calls.some((m) => m.type === 'syncNow' || m.type === 'fullSyncProvider'));
  await get('sync').fire('click');
  assert.equal(calls.at(-1).type, 'syncNow');
  const chatgptRescan = get('provider-status').children[0].children[1];
  assert.equal(chatgptRescan.getAttribute('aria-label'), 'Rescan saved ChatGPT items');
  chatgptRescan.focus();
  await changed({ activityLog: { newValue: [] } }, 'local');
  assert.equal(get('provider-status').children[0].children[1], chatgptRescan);
  assert.equal(document.activeElement, chatgptRescan);
  await chatgptRescan.fire('click');
  assert.deepEqual(calls.at(-1), { type: 'fullSyncProvider', provider: 'chatgpt' });
});

test('offline recovery and last sync are labeled independently', async () => {
  const archiveId = 'a'.repeat(32);
  const { get } = await popup({
    data: {
      settings: { archiveId, providers: { claude: true } },
      status: {
        claude: {
          captureScope: JSON.stringify([archiveId, 'claude', 'profile', 'browser-local', {}]),
          lastRunMs: 1_700_000_000_000,
          progress: { unchanged: 2 },
        },
      },
    },
    workerReply: () => ({ ok: false, code: 'offline' }),
  });
  assert.equal(get('api-title').textContent, 'Archive offline');
  assert.equal(get('connection-action').hidden, false);
  assert.match(get('last-sync').textContent, /^Last sync · \S/);
  assert.equal(get('notice-label').textContent, 'Up to date');
  await get('connection-action').fire('click');
  assert.equal(get('settings-panel').hidden, false);
  assert.equal(get('automatic-connection').open, true);
  assert.equal(document.activeElement, get('retry'));
});

test('the empty sync plan points to an eligible tab and lists enabled provider selections', async () => {
  const archiveId = 'a'.repeat(32);
  const { get, data, changed } = await popup({
    tab: { id: 9, url: 'https://example.org/' },
    data: { settings: { archiveId, providers: { chatgpt: true, claude: true, gemini: false } } },
    workerReply: () => ({ ok: true }),
  });
  assert.match(get('plan-hint').textContent, /^Open ChatGPT, Claude, or Gemini/);
  assert.equal(get('bulk-controls').hidden, true);
  assert.equal(get('sync').disabled, true);
  const entry = (projectIds, conversationIds) => ({
    principalId: 'owner',
    projectIds,
    conversationIds,
  });
  data.providerSyncScopes = {
    chatgpt: { entries: [{ ...entry(['project'], ['a', 'b']), includeRecents: true }] },
    claude: { entries: [entry([], ['chat'])] },
    gemini: { entries: [entry([], ['chat'])] },
  };
  await changed({ providerSyncScopes: { newValue: data.providerSyncScopes } }, 'local');
  assert.equal(get('sync').disabled, false);
  assert.equal(
    get('sync-scope').textContent,
    'ChatGPT · 2 chats, 1 project, all Recents\nClaude · 1 chat',
  );
  assert.equal(get('schedule').textContent, 'Auto-sync every 15 min');
  assert.equal(get('scope-chatgpt').textContent, '2 chats, 1 project, all Recents');
  assert.equal(get('scope-claude').textContent, '1 chat');
  assert.equal(get('scope-gemini').textContent, '1 chat · Paused');
});

test('the popup replaces Sync now with Stop sync while a run is queued or active', async () => {
  const archiveId = 'a'.repeat(32);
  const entry = {
    captureScope: JSON.stringify([archiveId, 'chatgpt', 'profile', 'browser-local', {}]),
    lastRunMs: Date.now(),
    continuationPending: true,
    progress: { phase: 'queued', created: 2 },
  };
  const { get, calls } = await popup({
    data: {
      settings: {
        archiveId,
        nativeHost: 'org.ai_memory.archive_' + archiveId,
        providers: { chatgpt: true },
      },
      status: { chatgpt: entry },
    },
    workerReply: () => ({ ok: true }),
  });
  const button = get('sync');
  assert.equal(button.textContent, 'Stop sync');
  assert.equal(button.disabled, false);
  await button.fire('click');
  assert.deepEqual(calls.at(-1), { type: 'stopSync' });
});

test('a quiet or interrupted sync shows its actual state and keeps Stop available', async () => {
  const archiveId = 'a'.repeat(32);
  const entry = {
    captureScope: JSON.stringify([archiveId, 'chatgpt', 'profile', 'browser-local', {}]),
    lastRunMs: Date.now() - 40_000,
    progressAtMs: Date.now() - 36_000,
    running: true,
    progress: { phase: 'importing', processed: 3, accepted: 0 },
    selection: { selected: 74 },
  };
  const { get, data, changed } = await popup({
    data: {
      settings: {
        archiveId,
        nativeHost: 'org.ai_memory.archive_' + archiveId,
        providers: { chatgpt: true },
      },
      status: { chatgpt: entry },
    },
    workerReply: () => ({ ok: true }),
  });
  assert.equal(
    get('notice-label').textContent,
    'Waiting for sync progress… 3/74 checked · 0 saved',
  );
  assert.equal(get('sync').textContent, 'Stop sync');
  entry.running = false;
  entry.continuationPending = true;
  entry.progress.phase = 'recovering';
  await changed({ status: { newValue: data.status } }, 'local');
  assert.equal(
    get('notice-label').textContent,
    'Sync interrupted. Resuming from the last saved batch… 3/74 checked · 0 saved',
  );
  assert.equal(get('sync').disabled, false);
});

test('the popup groups missing sign-in tabs and shows only nonzero sync totals', async () => {
  const archiveId = 'a'.repeat(32);
  const status = Object.fromEntries(
    ['chatgpt', 'claude'].map((provider, index) => [
      provider,
      {
        captureScope: JSON.stringify([archiveId, provider, 'profile', 'browser-local', {}]),
        lastRunMs: Date.now(),
        progress: { created: index * 2, updated: 0, unchanged: index * 23, accountsUnavailable: 1 },
        selection: { skipped: 0 },
        selectionWarning: 'Open a signed-in tab for 1 account.',
      },
    ]),
  );
  const { get, data, changed } = await popup({
    data: { settings: { archiveId, providers: { chatgpt: true, claude: true } }, status },
    workerReply: () => ({ ok: true }),
  });
  assert.equal(get('notice-label').textContent, 'Partial sync');
  assert.equal(get('notice-counts').textContent, ' · 2 new · 23 unchanged');
  assert.equal(get('notice-actions').textContent, 'Open signed-in tabs · ChatGPT, Claude');

  for (const entry of Object.values(data.status)) entry.selectionWarning = null;
  await changed({ status: { newValue: data.status } }, 'local');
  assert.equal(get('notice-label').textContent, 'Done');
  assert.equal(get('notice-actions').textContent, '');
});
