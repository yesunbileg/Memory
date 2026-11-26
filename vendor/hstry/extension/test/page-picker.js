import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const file = (name) => new URL('../' + name, import.meta.url);
const src = (name) => readFileSync(file(name), 'utf8');
function fixture({ href = 'https://chatgpt.com/', framed = false, read, fetch } = {}) {
  let listener;
  const sends = [],
    touches = [],
    observers = [],
    sheets = [];
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
      const attrs = {};
      const element = {
        dataset: {},
        textContent: '',
        setAttribute(name, value) {
          attrs[name] = String(value);
        },
        getAttribute(name) {
          return attrs[name] ?? null;
        },
        removeAttribute(name) {
          delete attrs[name];
        },
        remove() {
          mounted.delete(element);
        },
      };
      return element;
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
    URLSearchParams,
    AbortSignal,
    fetch,
    AbortController,
    setTimeout,
    clearTimeout,
    CSSStyleSheet: class {
      replaceSync(text) {
        sheets.push(text);
      }
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
    sheets,
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

test('same-origin provider reads use the tab cookie context and reject unrelated endpoints', async () => {
  const requests = [];
  const f = fixture({
    href: 'https://claude.ai/new',
    fetch: async (url, init) => {
      requests.push([url, init]);
      return { url, status: 200, headers: new Headers(), text: async () => '{}' };
    },
  });
  const worker = { ...f.sender, url: f.sender.url.replace('popup.html', 'background.js') };
  const packet = {
    type: 'FETCH_PROVIDER_TAB',
    url: 'https://claude.ai/api/account',
    method: 'GET',
  };
  assert.equal(await f.message(packet), undefined);
  assert.equal(
    await f.message({ ...packet, url: 'https://example.com/api/account' }, worker),
    null,
  );
  assert.equal(await f.message({ ...packet, method: 'POST' }, worker), null);
  assert.equal(await f.message({ ...packet, url: 'https://claude.ai/api/other' }, worker), null);
  assert.equal((await f.message(packet, worker)).body, '{}');
  assert.equal(requests.length, 1);
  assert.equal(requests[0][1].credentials, 'same-origin');
});

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

test('an unverified open chat does not hide verified sidebar rows', async () => {
  const open = '33333333-3333-4333-8333-333333333333';
  const saved = '22222222-2222-4222-8222-222222222222';
  const project = 'g-p-11111111111141118111111111111111';
  const f = fixture({
    href: 'https://chatgpt.com/c/' + open,
    read: async () => ({
      ok: true,
      token: 'proof',
      items: [
        { kind: 'conversation', id: saved, checked: true },
        { kind: 'project', id: project, checked: false },
      ],
    }),
  });
  const row = (href) => ({
    isConnected: true,
    parentElement: f.doc.body,
    textContent: 'Sidebar row',
    checkVisibility: () => true,
    closest: () => null,
    matches: () => true,
    getBoundingClientRect: () => ({ left: 0, right: 200, width: 200, height: 30 }),
    getAttribute: (name) => (name === 'href' ? href : null),
    setAttribute() {},
    style: { setProperty() {} },
    after(box) {
      this.nextElementSibling = box;
      f.mounted.add(box);
    },
  });
  const create = f.doc.createElement;
  f.doc.createElement = (tag) => ({ ...create(tag), style: { setProperty() {} } });
  f.doc.querySelectorAll = (selector) =>
    selector.includes('a[href')
      ? [row('/c/' + open), row('/c/' + saved), row('/g/' + project + '/project')]
      : [];
  const result = await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true });
  assert.equal(result.enabled, true);
  const boxes = [...f.mounted].filter((element) => element.type === 'checkbox');
  assert.deepEqual(
    boxes.map((box) => [box.dataset.memorySyncId, box.checked]),
    [
      [saved, true],
      [project, false],
    ],
  );
});

test('a mounted compact Claude drawer gets controls when it becomes visible', async () => {
  const id = '44444444-4444-4444-8444-444444444444';
  const f = fixture({
    href: 'https://claude.ai/new',
    read: async (request) => ({ ok: true, token: 'proof', items: request.items }),
  });
  const drawer = {
    closest: (selector) =>
      selector.includes('[role="dialog"]') && !selector.includes(':not(aside > #frame-peek-popover')
        ? drawer
        : null,
    matches: (selector) => selector.includes('[role="dialog"]'),
    querySelector: () => null,
  };
  let opened = false;
  const row = {
    isConnected: true,
    parentElement: f.doc.body,
    textContent: 'A compact sidebar chat',
    checkVisibility: () => opened,
    closest: (selector) => drawer.closest(selector),
    matches: () => true,
    getBoundingClientRect: () => ({ left: 0, right: 200, width: 200, height: 30 }),
    getAttribute: (name) => (name === 'href' ? '/chat/' + id : null),
    setAttribute() {},
    style: { setProperty() {} },
    after(box) {
      this.nextElementSibling = box;
      f.mounted.add(box);
    },
  };
  const create = f.doc.createElement;
  f.doc.createElement = (tag) => ({ ...create(tag), style: { setProperty() {} } });
  f.doc.querySelectorAll = (selector) => (selector.includes('a[href') ? [row] : []);
  assert.equal((await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true })).enabled, true);
  assert.equal([...f.mounted].filter((element) => element.type === 'checkbox').length, 0);

  opened = true;
  f.observers[0].notify([{ target: drawer, type: 'attributes', attributeName: 'hidden' }]);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.deepEqual(
    [...f.mounted]
      .filter((element) => element.type === 'checkbox')
      .map((box) => box.dataset.memorySyncId),
    [id],
  );
});

test('a ChatGPT project button with the native row attributes is verified and gets a checkbox', async () => {
  const id = 'g-p-11111111111141118111111111111111';
  const projectAttrs = {
    role: 'button',
    'data-app-action-sidebar-project-row': '',
    'data-app-action-sidebar-project-id': id,
  };
  const f = fixture({
    read: async (request) => ({
      ok: true,
      token: 'proof',
      items: request.items.filter((item) => item.kind === 'project' && item.id === id),
    }),
  });
  const row = {
    isConnected: true,
    parentElement: f.doc.body,
    textContent: 'A project',
    checkVisibility: () => true,
    closest: () => null,
    matches: (selector) => selector.includes('[data-app-action-sidebar-project-row]'),
    querySelector: () => null,
    getBoundingClientRect: () => ({ left: 0, right: 200, width: 200, height: 30 }),
    getAttribute: (name) => projectAttrs[name] ?? null,
    setAttribute() {},
    style: { setProperty() {} },
    after(box) {
      this.nextElementSibling = box;
      f.mounted.add(box);
    },
  };
  const create = f.doc.createElement;
  f.doc.createElement = (tag) => ({ ...create(tag), style: { setProperty() {} } });
  f.doc.querySelectorAll = (selector) => (row.matches(selector) ? [row] : []);
  await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true });
  const box = [...f.mounted].find((element) => element.type === 'checkbox');
  assert.equal(box?.dataset.memorySyncId, id);
  assert.equal(box?.dataset.memorySyncKind, 'project');
});

test('included-through-group rows get a visible legend, tooltip and a separated 24px target', async () => {
  const id = '55555555-5555-4555-8555-555555555555';
  const f = fixture({
    read: async (request) => ({
      ok: true,
      token: 'proof',
      items: request.items.map((item) => ({ ...item, checked: false, inherited: true })),
    }),
  });
  const row = {
    isConnected: true,
    parentElement: f.doc.body,
    textContent: 'An inherited chat',
    checkVisibility: () => true,
    closest: () => null,
    matches: () => true,
    getBoundingClientRect: () => ({ left: 0, right: 200, width: 200, height: 30 }),
    getAttribute: (name) => (name === 'href' ? '/c/' + id : null),
    setAttribute() {},
    style: { setProperty() {} },
    after(box) {
      this.nextElementSibling = box;
      f.mounted.add(box);
    },
  };
  const create = f.doc.createElement;
  f.doc.createElement = (tag) => ({ ...create(tag), style: { setProperty() {} } });
  f.doc.querySelectorAll = (selector) => (selector.includes('a[href') ? [row] : []);
  assert.equal((await f.message({ type: 'SET_PAGE_LIST_MODE', enabled: true })).enabled, true);

  const box = [...f.mounted].find((element) => element.type === 'checkbox');
  assert.equal(box?.indeterminate, true);
  assert.match(box.title, /Included through a selected group/);
  assert.match(box.getAttribute('aria-label'), /Included through a selected group/);
  const note = [...f.mounted].find(
    (element) => element.dataset && 'memoryPickerNote' in element.dataset,
  );
  assert.match(note?.textContent, /Dash = included via a project or Recents/);

  const css = f.sheets.join('\n');
  const target = css.match(/input\[data-memory-sync-id\]\{([^}]*)\}/)?.[1] || '';
  for (const property of [
    'width:24px!important',
    'height:24px!important',
    'min-width:24px!important',
    'min-height:24px!important',
    'transform:translate(8px,-50%)!important',
    'box-shadow:0 0 0 1px',
  ]) {
    assert.ok(target.includes(property), property);
  }
  assert.match(css, /\[data-memory-sync-row\]\{[^}]*padding-inline-start:44px!important/);
  assert.match(
    css,
    /input\[data-memory-sync-id\]:dir\(rtl\)\{[^}]*right:anchor\(right\)[^}]*translate\(-8px,-50%\)/,
  );
  assert.match(css, /input\[data-memory-sync-id\]:focus-visible\{[^}]*outline:2px/);
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
