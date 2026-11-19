import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const html = await readFile(new URL('../popup.html', import.meta.url), 'utf8');
const js = await readFile(new URL('../popup.js', import.meta.url), 'utf8');
test('the three pickers and connection settings live inside the toolbar popup', () => {
  for (const id of [
    'tab-chatgpt',
    'tab-claude',
    'tab-gemini',
    'picker-search',
    'picker-kind',
    'add-selection',
    'saved-items',
    'sync',
    'connection-file',
    'auto-paused',
    'toggle-chatgpt',
    'toggle-claude',
    'toggle-gemini',
  ])
    assert.ok(html.includes('id="' + id + '"'), id);
  assert.doesNotMatch(html, /<a\b|target="_blank"|options\.html/);
  assert.doesNotMatch(js, /openOptionsPage|window\.open|tabs\.create|windows\.create/);
});
test('saving a filtered catalog keeps all previously saved visible items and separates principals', async () => {
  const { catalogRows, filterRows, idKey, selectionFor } = await import('../popup-picker.js');
  const model = {
    entries: [
      {
        principalId: 'A',
        title: 'Lab',
        projects: [{ id: 'p', title: 'Design' }],
        conversations: [{ id: 'c', title: 'Notes' }],
      },
      {
        principalId: 'B',
        title: 'Office',
        projects: [],
        conversations: [{ id: 'c', title: 'Other' }],
      },
    ],
  };
  const rows = catalogRows(model);
  assert.equal(rows.length, 3);
  assert.deepEqual(
    filterRows(rows, 'NOt', 'conversation').map((r) => r.title),
    ['Notes'],
  );
  const filtered = filterRows(rows, 'Office', 'all');
  assert.equal(filtered.length, 1);
  assert.notEqual(idKey(rows[1]), idKey(rows[2]));
  assert.deepEqual(
    selectionFor([...filtered, rows[0], rows[0]]).entries.sort((a, b) =>
      a.principalId.localeCompare(b.principalId),
    ),
    [
      { principalId: 'A', projectIds: ['p'], conversationIds: [] },
      { principalId: 'B', projectIds: [], conversationIds: ['c'] },
    ],
  );
});

test('popup initializes fail-closed, merges settings writes, verifies pairing, and refreshes edited saved lists', async () => {
  class Element {
    constructor() {
      this.value = '';
      this.checked = false;
      this.textContent = '';
      this.children = [];
      this.dataset = {};
      this.events = {};
      this.hidden = false;
    }
    append(...nodes) {
      this.children.push(...nodes);
    }
    replaceChildren(...nodes) {
      this.children = nodes;
    }
    setAttribute(k, v) {
      this[k] = String(v);
    }
    addEventListener(k, v) {
      this.events[k] = v;
    }
    fire(k) {
      return this.events[k]?.({ target: this });
    }
  }
  const dom = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => [m[1], new Element()]));
  const get = (id) => dom.get(id);
  const archive = 'a'.repeat(32),
    host = (id) => 'org.ai_memory.archive_' + id;
  const data = {
    settings: {
      archiveId: archive,
      nativeHost: host(archive),
      providers: { chatgpt: true },
      autoPaused: true,
      intervalMinutes: 45,
    },
    providerSyncScopes: { chatgpt: { entries: { invalid: true } } },
  };
  const calls = [];
  let changed, finishSlow, signalSlow;
  const startSlow = new Promise((r) => (signalSlow = r));
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
    runtime: {
      id: 'a'.repeat(32),
      getManifest: () => ({ version: '0.0' }),
      sendMessage: async (m) => {
        calls.push(m);
        if (m.type === 'verifyPairing') {
          const id = m.candidate.archiveId;
          if (id === 'b'.repeat(32)) {
            signalSlow();
            return new Promise((r) => (finishSlow = () => r({ ok: true, archiveId: id })));
          }
          if (id === 'c'.repeat(32)) return { ok: false, code: 'offline' };
          return { ok: true, archiveId: id };
        }
        if (m.type === 'LIST_SYNC_CATALOG')
          return {
            ok: true,
            catalogToken: 'verified',
            entries: [
              {
                principalId: 'A',
                title: 'Alice',
                projects: [],
                conversations: [{ id: 'c', title: 'Budget' }],
              },
            ],
            selectedScope: { entries: [] },
          };
        return { ok: true };
      },
    },
  };
  await import('../popup.js?test-initialization');
  assert.equal(
    get('sync').disabled,
    true,
    'malformed scope does not enable sync or break settings',
  );
  assert.deepEqual(
    calls.map((x) => x.type),
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
  await get('load-catalog').fire('click');
  assert.match(get('saved-heading').textContent, /\(0\)/);
  data.providerSyncScopes = {
    chatgpt: {
      entries: [
        { principalId: 'A', projectIds: [], conversationIds: ['c'] },
        { principalId: 'B', projectIds: [], conversationIds: ['private'] },
      ],
    },
  };
  await changed({ providerSyncScopes: { newValue: data.providerSyncScopes } }, 'local');
  assert.match(get('saved-heading').textContent, /\(1\)/);
  assert.equal(get('sync').disabled, false);
  assert.ok(!calls.some((m) => m.type === 'syncNow' || m.type === 'fullSyncProvider'));
});
