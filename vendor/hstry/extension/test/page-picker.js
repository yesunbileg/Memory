import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const file = (name) => new URL('../' + name, import.meta.url);
const src = (name) => readFileSync(file(name), 'utf8');
function fixture({ href = 'https://chatgpt.com/', framed = false, read } = {}) {
  let listener;
  const sends = [],
    touches = [],
    observers = [];
  const window = new EventTarget();
  window.top = framed ? {} : window;
  window.innerWidth = 1024;
  const loc = new URL(href),
    mounted = new Set();
  const doc = Object.assign(new EventTarget(), {
    adoptedStyleSheets: [],
    documentElement: {},
    body: { append: (node) => mounted.add(node) },
    querySelectorAll: () => [],
    createElement: () => {
      const note = {
        dataset: {},
        textContent: '',
        setAttribute() {},
        remove() {
          mounted.delete(note);
        },
      };
      return note;
    },
  });
  const sender = { id: 'extension123', url: 'chrome-extension://extension123/popup.html' };
  const chrome = {
    runtime: {
      id: sender.id,
      getURL: (s) => 'chrome-extension://extension123/' + s,
      onMessage: {
        addListener: (f) => {
          listener = f;
        },
      },
      sendMessage: async (m) => {
        sends.push(m);
        return m.type === 'READ_PAGE_SELECTION' ? read?.(m) : { ok: true };
      },
    },
  };
  vm.runInNewContext(src('page-picker-routes.js') + src('page-picker.js'), {
    chrome,
    window,
    top: window.top,
    location: loc,
    URL,
    AbortController,
    setTimeout,
    clearTimeout,
    CSSStyleSheet: class {
      replaceSync() {}
    },
    document: read
      ? doc
      : new Proxy(
          {},
          {
            get(_node, prop) {
              touches.push(prop);
              throw Error('unexpected DOM access while OFF');
            },
          },
        ),
    MutationObserver: class {
      constructor(notify) {
        this.notify = notify;
        observers.push(this);
      }
      observe() {}
      disconnect() {
        this.disconnected = true;
      }
    },
  });
  function message(body, from = sender) {
    return new Promise((resolve, reject) => {
      if (!listener) return resolve(undefined);
      try {
        const keep = listener(body, from, resolve);
        if (keep !== true) resolve(undefined);
      } catch (err) {
        reject(err);
      }
    });
  }
  return {
    message,
    sends,
    observers,
    touches,
    registered: !!listener,
    sender,
    mounted,
    doc,
    navigate(path) {
      loc.href = new URL(path, loc).href;
      window.dispatchEvent(new Event('popstate'));
    },
  };
}

test('opening a provider page or checking mode from the popup does not inspect the DOM, install an observer or fetch metadata', async () => {
  const f = fixture();
  assert.ok(f.registered, 'top-frame popup mode listener is installed');
  for (const type of ['GET_PAGE_LIST_MODE', 'SET_PAGE_LIST_MODE']) {
    const r = await f.message({ type, enabled: false });
    assert.equal(r?.provider, 'chatgpt');
    assert.equal(r?.enabled, false);
    assert.equal(r?.ok, true);
  }
  assert.deepEqual(f.sends, []);
  assert.deepEqual(f.observers, []);
  assert.deepEqual(f.touches, []);
});

test('a site or other extension context cannot turn selection mode on', async () => {
  const f = fixture();
  assert.ok(f.registered, 'mode listener must be present for the sender test');
  for (const from of [
    { id: f.sender.id, url: 'https://chatgpt.com/', tab: { id: 1 } },
    { id: 'other-extension', url: f.sender.url },
    { id: f.sender.id, url: 'chrome-extension://extension123/other.html' },
    { ...f.sender, tab: { id: 4 } },
    { ...f.sender, frameId: 2 },
  ]) {
    assert.notEqual(
      (await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true }, from))?.ok,
      true,
    );
  }
  assert.deepEqual(f.sends, []);
  assert.deepEqual(f.observers, []);
  assert.deepEqual(f.touches, []);
});

test('unsupported and locked locations never start a read or observer; subframes have no listener', async () => {
  for (const href of [
    'https://chatgpt.com/locked',
    'https://claude.ai/settings',
    'https://gemini.google.com/share/abc',
  ]) {
    const f = fixture({ href });
    const r = await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true });
    assert.equal(r?.ok, false);
    assert.deepEqual(f.sends, []);
    assert.deepEqual(f.observers, []);
  }
  assert.equal(fixture({ framed: true }).registered, false);
});

test('an unverified open conversation rejects mode and removes all of its UI, even if its URL looks ordinary', async () => {
  const href = 'https://chatgpt.com/c/33333333-3333-4333-8333-333333333333';
  const f = fixture({
    href,
    read: async () => ({ ok: true, token: 'proof', items: [], incomplete: true }),
  });
  const r = await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true });
  assert.equal(r.ok, false);
  assert.equal(r.enabled, false);
  assert.equal(r.code, 'page_unverified');
  assert.match(r.error, /home page/i);
  assert.equal(f.mounted.size, 0);
  assert.equal(f.doc.adoptedStyleSheets.length, 0);
  assert.ok(f.observers.every((o) => o.disconnected));
  assert.equal(f.sends.at(-1).type, 'CLOSE_PAGE_SELECTION');
});

test('turning mode off cancels an outstanding metadata read and closes its late proof', async () => {
  let resolve;
  const f = fixture({
    read: () =>
      new Promise((r) => {
        resolve = r;
      }),
  });
  const opening = f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true });
  assert.ok(resolve);
  assert.equal((await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: false })).enabled, false);
  resolve({ ok: true, token: 'old-proof', items: [] });
  await opening;
  assert.equal(f.sends.at(-1).token, 'old-proof');
  assert.equal(f.sends.at(-1).type, 'CLOSE_PAGE_SELECTION');
  assert.equal(f.mounted.size, 0);
  assert.ok(f.observers.every((o) => o.disconnected));
});

test('a late metadata response cannot supply the proof on a new same-document route', async () => {
  let resolve,
    count = 0;
  const f = fixture({
    read: () =>
      ++count === 1
        ? new Promise((r) => {
            resolve = r;
          })
        : { ok: true, token: 'new-proof', items: [] },
  });
  const first = f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true });
  f.navigate('/projects');
  resolve({ ok: true, token: 'old-proof', items: [] });
  await first;
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(count, 2);
  const reads = f.sends.filter((m) => m.type === 'READ_PAGE_SELECTION');
  assert.equal(reads[1].token, undefined);
  assert.ok(f.sends.some((m) => m.type === 'CLOSE_PAGE_SELECTION' && m.token === 'old-proof'));
  await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: false });
  assert.equal(f.sends.at(-1).token, 'new-proof');
});

test('opening a locked-chats dialog without navigating immediately ends mode', async () => {
  const f = fixture({ read: async () => ({ ok: true, token: 'proof', items: [] }) });
  assert.equal((await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true })).enabled, true);
  const modal = {
    nodeType: 1,
    checkVisibility: () => true,
    closest: () => null,
    matches: (s) => s.includes('dialog'),
    querySelector: () => null,
    getAttribute: () => 'Locked chats',
    getBoundingClientRect: () => ({ width: 240, height: 100, left: 0, right: 240 }),
  };
  f.doc.querySelectorAll = (selector) => (selector.includes('dialog[open]') ? [modal] : []);
  f.observers[0].notify([
    {
      target: { closest: (s) => (s.includes('main') ? {} : null) },
      type: 'childList',
      addedNodes: [modal],
      removedNodes: [],
    },
  ]);
  assert.equal((await f.message({ type: 'GET_PAGE_LIST_MODE' })).enabled, false);
  assert.equal(f.mounted.size, 0);
});
