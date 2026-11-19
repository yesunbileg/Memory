import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';
import { PROVIDERS } from '../providers/index.js';
const extension = 'a'.repeat(32);
const popup = { id: extension, url: 'chrome-extension://' + extension + '/popup.html' };
const owner = 'owner-one',
  other = 'owner-two';
const uuid = (n) => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
const project = uuid(1),
  child = uuid(2),
  ordinary = uuid(3),
  unseen = uuid(4),
  different = uuid(5);
const row = (kind, id) => ({ kind, id });
const c = (id) => row('conversation', id);
const p = (id) => row('project', id);
const entry = (principalId, projectIds = [], conversationIds = []) => ({
  principalId,
  projectIds,
  conversationIds,
});
const scope = (...entries) => ({ entries });
const group = (principalId = owner) => ({
  principalId,
  title: 'PRIVATE workspace title',
  projects: [{ id: project, title: 'PRIVATE project title' }],
  conversations: [
    { id: child, projectId: project, title: 'PRIVATE member name' },
    { id: ordinary, title: 'PRIVATE ordinary', transcript: 'PRIVATE answer' },
    { id: unseen, title: 'PRIVATE not on screen' },
  ],
});
const from = (tabId = 1, documentId = 'document-one', url = 'https://claude.ai/new') => ({
  id: extension,
  documentId,
  documentLifecycle: 'active',
  origin: new URL(url).origin,
  frameId: 0,
  url,
  tab: { id: tabId, url },
});
let mem = {},
  session = {},
  listener,
  alarm,
  calls,
  catalog,
  principals,
  refuse = false;
const area = (get) => ({
  get: async (k) => ({ [k]: structuredClone(get()[k]) }),
  set: async (obj) => {
    if (refuse && Object.hasOwn(obj, 'providerSyncScopes')) throw Error('quota');
    Object.assign(get(), structuredClone(obj));
  },
  remove: async (k) => {
    delete get()[k];
  },
});
const event = { addListener() {} };
globalThis.chrome = {
  runtime: {
    id: extension,
    getURL: (p) => 'chrome-extension://' + extension + '/' + p,
    onInstalled: event,
    onStartup: event,
    onMessage: { addListener: (fn) => (listener = fn) },
    sendNativeMessage: async () => {
      calls.native++;
      throw Error('no archive expected');
    },
  },
  storage: { local: area(() => mem), session: area(() => session) },
  alarms: {
    onAlarm: { addListener: (fn) => (alarm = fn) },
    get: async () => null,
    create: async () => {},
  },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
};
const noNative = chrome.runtime.sendNativeMessage;
await import('../background.js?page-worker');
const send = (message, sender = from()) =>
  new Promise((r) => {
    if (!listener(message, sender, r)) r({ ok: false });
  });
const read = (items, token, sender) =>
  send({ type: 'READ_PAGE_SELECTION', items, ...(token ? { token } : {}) }, sender);
const set = (item, checked, token, sender) =>
  send({ type: 'SET_PAGE_SELECTION', ...item, checked, token }, sender);
beforeEach(() => {
  calls = { catalog: 0, identity: 0, native: 0 };
  mem = {};
  session = {};
  refuse = false;
  catalog = { entries: [group()] };
  principals = [owner];
  chrome.runtime.sendNativeMessage = noNative;
  for (const id of ['chatgpt', 'claude']) {
    PROVIDERS[id].listCatalog = async () => {
      calls.catalog++;
      return structuredClone(catalog);
    };
    PROVIDERS[id].principalIds = async () => {
      calls.identity++;
      return [...principals];
    };
  }
});
test('only visible, verified IDs are returned with separate direct and inherited state', async () => {
  mem.providerSyncScopes = {
    gemini: scope(entry('gemini', [], ['untouched'])),
    claude: scope(entry(other, [], [different])),
  };
  mem.settings = { providers: { claude: false, gemini: true }, autoPaused: true };
  const first = await read([p(project), c(child), c(ordinary), c(different)]);
  assert.equal(first.ok, true, 'top-frame website can enter list mode');
  assert.deepEqual(first.items, [
    { ...p(project), checked: false, inherited: false },
    { ...c(child), checked: false, projectId: project, inherited: false },
    { ...c(ordinary), checked: false, inherited: false },
  ]);
  assert.ok(!JSON.stringify([first, session]).includes('PRIVATE'));
  assert.ok(!JSON.stringify(first).includes(owner));
  assert.equal(
    (await set(c(unseen), true, first.token)).ok,
    false,
    'cannot edit unseen catalog IDs',
  );
  assert.equal((await set(p(project), true, first.token)).ok, true);
  assert.deepEqual((await read([c(child)], first.token)).items, [
    { ...c(child), checked: false, projectId: project, inherited: true },
  ]);
  assert.equal((await set(c(child), true, first.token)).ok, true);
  assert.equal((await set(c(child), true, first.token)).ok, true);
  assert.equal((await set(p(project), false, first.token)).ok, true);
  assert.deepEqual((await read([c(child)], first.token)).items, [
    { ...c(child), checked: true, projectId: project, inherited: false },
  ]);
  assert.deepEqual(mem.providerSyncScopes, {
    claude: scope(entry(owner, [], [child]), entry(other, [], [different])),
    gemini: scope(entry('gemini', [], ['untouched'])),
  });
  assert.deepEqual(mem.settings, { providers: { claude: false, gemini: true }, autoPaused: true });
  assert.equal(calls.catalog, 1);
  assert.equal(calls.native, 0);
});
test('website never has popup authority and unsupported or locked pages never get a proof', async () => {
  for (const sender of [
    popup,
    { ...from(), id: 'another' },
    { ...from(), frameId: 1 },
    { ...from(), tab: undefined },
    { ...from(), documentId: undefined },
    { ...from(), documentLifecycle: 'prerender' },
    { ...from(), origin: 'https://example.org' },
    { ...from(), tab: { id: 1, url: 'https://chatgpt.com/' } },
    from(2, 'doc', 'https://claude.ai/settings'),
    from(2, 'doc', 'https://claude.ai/new#settings'),
    from(2, 'doc', 'https://chatgpt.com/locked'),
    from(2, 'doc', 'https://claude.ai.evil.test/new'),
  ]) {
    assert.equal((await read([c(child)], undefined, sender)).ok, false, JSON.stringify(sender));
  }
  for (const message of [
    { type: 'syncNow' },
    { type: 'fullSyncProvider', provider: 'claude' },
    { type: 'checkApi' },
    { type: 'verifyPairing' },
    { type: 'settingsChanged' },
    { type: 'clearActivityLog' },
    { type: 'READ_PAGE_SELECTION', items: [c(child)], provider: 'gemini' },
    { type: 'READ_PAGE_SELECTION', items: [{ ...c(child), principalId: other }] },
    { type: 'READ_PAGE_SELECTION', items: [c('../' + child)] },
    { type: 'READ_PAGE_SELECTION', items: Array.from({ length: 501 }, () => c(child)) },
  ]) {
    assert.equal((await send(message)).ok, false);
  }
  assert.equal(
    (await send({ type: 'LIST_SYNC_CATALOG', provider: 'claude' }, popup)).ok,
    false,
    'no old popup CRUD',
  );
  assert.equal(calls.catalog, 0);
  assert.equal(calls.native, 0);
});
test('proof is bound to tab, document and provider and invalidated on mode close or reopen', async () => {
  const first = await read([], undefined);
  assert.equal(first.ok, true);
  assert.deepEqual(first.items, []);
  assert.equal((await set(c(ordinary), true, first.token)).ok, false);
  const doc2 = from(2, 'two');
  const second = await read([c(ordinary)], undefined, doc2);
  assert.equal(second.ok, true);
  assert.notEqual(first.token, second.token);
  for (const sender of [
    from(2, 'document-one'),
    from(1, 'two'),
    from(1, 'document-one', 'https://chatgpt.com/'),
  ]) {
    assert.equal((await set(c(ordinary), true, first.token, sender)).ok, false);
  }
  const ready = await read([c(child)], first.token);
  assert.equal(ready.ok, true);
  assert.equal((await set(c(child), true, first.token)).ok, true);
  const fresh = await read([c(ordinary)]);
  assert.equal((await set(c(child), false, first.token)).code, 'catalog_expired');
  assert.equal((await send({ type: 'CLOSE_PAGE_SELECTION', token: fresh.token })).ok, true);
  assert.equal((await set(c(ordinary), true, fresh.token)).code, 'catalog_expired');
  assert.equal((await set(c(ordinary), true, second.token, doc2)).ok, true);
  assert.deepEqual(mem.providerSyncScopes.claude, scope(entry(owner, [], [child, ordinary])));
});
test('never guesses an organization from the first match and rejects mixed org rows across scans', async () => {
  catalog = {
    entries: [
      group(),
      { principalId: other, projects: [], conversations: [{ id: ordinary }, { id: different }] },
    ],
  };
  principals = [owner, other];
  assert.equal((await read([c(ordinary)])).ok, false, 'same ID in two organizations');
  assert.equal(
    (await read([c(child), c(different)])).ok,
    false,
    'two organizations in one visible list',
  );
  const single = await read([c(child)]);
  assert.equal(single.ok, true);
  assert.equal(
    (await read([c(different)], single.token)).ok,
    false,
    'same mode cannot cross organizations',
  );
  assert.equal((await set(c(different), true, single.token)).ok, false);
  assert.equal(
    (await set(c(child), true, single.token)).code,
    'catalog_expired',
    'an ambiguous new view revokes the previous organization grant',
  );
  assert.equal(mem.providerSyncScopes, undefined);
  catalog = { entries: [group()], incomplete: true };
  assert.equal((await read([c(child)])).ok, false, 'not enough catalog evidence to disambiguate');
});
test('stale account, expired proofs, malformed edits and storage failure cannot mutate selection', async () => {
  const stored = { claude: scope(entry(other, [], [different])) };
  mem.providerSyncScopes = structuredClone(stored);
  const fresh = await read([c(ordinary)]);
  for (const message of [
    {
      type: 'SET_PAGE_SELECTION',
      kind: 'project',
      id: ordinary,
      checked: true,
      token: fresh.token,
    },
    { type: 'SET_PAGE_SELECTION', ...c(ordinary), checked: 1, token: fresh.token },
    {
      type: 'SET_PAGE_SELECTION',
      ...c(ordinary),
      checked: true,
      principalId: owner,
      token: fresh.token,
    },
  ])
    assert.equal((await send(message)).ok, false);
  refuse = true;
  assert.equal((await set(c(ordinary), true, fresh.token)).code, 'storage_unavailable');
  refuse = false;
  const actualNow = Date.now;
  try {
    Date.now = () => actualNow() + 86_400_000;
    assert.equal((await set(c(ordinary), true, fresh.token)).code, 'catalog_expired');
  } finally {
    Date.now = actualNow;
  }
  const again = await read([c(ordinary)]);
  principals = [other];
  assert.equal((await set(c(ordinary), true, again.token)).code, 'principal_changed');
  assert.deepEqual(mem.providerSyncScopes, stored);
  assert.equal(calls.native, 0);
});
test('concurrent tabs merge single-row changes, and durable state survives a worker restart', async () => {
  const tabs = [from(11, 'aaa'), from(12, 'bbb')];
  const opens = await Promise.all(
    tabs.map((s, i) => read([c(i ? child : ordinary)], undefined, s)),
  );
  const saved = await Promise.all(
    tabs.map((s, i) => set(c(i ? child : ordinary), true, opens[i].token, s)),
  );
  assert.ok(saved.every((v) => v.ok));
  assert.deepEqual(mem.providerSyncScopes, { claude: scope(entry(owner, [], [child, ordinary])) });
  await import('../background.js?page-worker-restarted');
  assert.equal((await read([c(ordinary)], opens[0].token, tabs[0])).items[0].checked, true);
  assert.equal((await set(c(child), false, opens[1].token, tabs[1])).ok, true);
  assert.deepEqual(mem.providerSyncScopes, { claude: scope(entry(owner, [], [ordinary])) });
});
test('v1 picks retain their legacy restrictions until explicitly checked and are retired atomically', async () => {
  const principalId = JSON.stringify(['user', 'workspace']);
  const page = from(13, 'gpt', 'https://chatgpt.com/');
  mem.chatgptSyncList = {
    version: 1,
    entries: [
      { userId: 'user', accountId: 'workspace', projectIds: [], conversationIds: [child, unseen] },
    ],
  };
  principals = [principalId];
  catalog = {
    entries: [{ principalId, projects: [], conversations: [{ id: ordinary }, { id: child }] }],
  };
  const f = await read([c(ordinary), c(child)], undefined, page);
  assert.equal(f.ok, true);
  assert.equal(f.items[1].checked, true);
  assert.equal(mem.providerSyncScopes, undefined);
  assert.equal((await set(c(ordinary), true, f.token, page)).ok, true);
  assert.deepEqual(
    mem.providerSyncScopes.chatgpt,
    scope({
      ...entry(principalId, [], [child, ordinary, unseen]),
      legacyConversationIds: [child, unseen],
    }),
  );
  assert.deepEqual(mem.chatgptSyncList, { version: 1, entries: [] });
  assert.equal((await set(c(child), true, f.token, page)).ok, true);
  assert.deepEqual(
    mem.providerSyncScopes.chatgpt,
    scope({
      ...entry(principalId, [], [child, ordinary, unseen]),
      legacyConversationIds: [unseen],
    }),
  );
});
test('removing a website checkbox revokes an in-flight sync before delivery', async () => {
  const principalId = owner,
    archiveId = 'f'.repeat(32);
  mem.settings = {
    archiveId,
    nativeHost: 'org.ai_memory.archive_' + archiveId,
    providers: { claude: true },
  };
  mem.providerSyncScopes = { claude: scope(entry(principalId, [], [ordinary])) };
  const f = await read([c(ordinary)]);
  assert.equal(f.ok, true);
  const started = Promise.withResolvers(),
    resume = Promise.withResolvers();
  const packets = [];
  chrome.runtime.sendNativeMessage = async (_host, packet) => {
    if (packet.op === 'check')
      return { service: 'ai-memory', status: 'ok', schema: 2, archive_instance_id: archiveId };
    packets.push(packet);
    return { conversations: 1, created: 1, updated: 0, unchanged: 0 };
  };
  PROVIDERS.claude.sync = async ({ selectedScope, push }) => {
    assert.deepEqual(selectedScope, scope(entry(principalId, [], [ordinary])));
    started.resolve();
    await resume.promise;
    await push('claude-web', 'claude-web', [{ externalId: ordinary, messages: [] }]);
    return { state: {}, conversations: 1 };
  };
  assert.equal((await send({ type: 'syncProvider', provider: 'claude' }, popup)).ok, true);
  await started.promise;
  assert.equal((await set(c(ordinary), false, f.token)).ok, true);
  resume.resolve();
  for (let i = 0; i < 100 && mem.status.claude.running; i++)
    await new Promise((r) => setTimeout(r, 2));
  assert.equal(mem.status.claude.lastErrorCode, 'selection_changed');
  assert.deepEqual(packets, []);
  assert.equal((await send({ type: 'syncNow' }, popup)).code, 'no_saved_selection');
  alarm({ name: 'hstry-sync' });
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(packets, []);
});
