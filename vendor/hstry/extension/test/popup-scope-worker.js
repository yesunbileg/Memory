import assert from 'node:assert/strict';
import { PROVIDERS } from '../providers/index.js';
const id = 'a'.repeat(32),
  sender = { id, url: 'chrome-extension://' + id + '/popup.html' };
const A = 'a',
  B = 'b';
const entry = (principalId, projectIds = [], conversationIds = []) => ({
  principalId,
  projectIds,
  conversationIds,
});
const scope = (...entries) => ({ entries });
let owner = A,
  observations = 0,
  proofs = 0,
  native = 0,
  listener,
  alarm,
  listing = ['first', 'second'];
const mem = {},
  session = {};
const area = (data) => ({
  get: async (key) => ({ [key]: structuredClone(data[key]) }),
  set: async (value) => Object.assign(data, structuredClone(value)),
});
const event = { addListener() {} };
globalThis.chrome = {
  runtime: {
    id,
    getURL: (p) => 'chrome-extension://' + id + '/' + p,
    onInstalled: event,
    onStartup: event,
    onMessage: { addListener: (fn) => (listener = fn) },
    sendNativeMessage: async () => {
      native++;
      throw Error('no archive operations expected');
    },
  },
  storage: { local: area(mem), session: area(session) },
  alarms: {
    onAlarm: { addListener: (fn) => (alarm = fn) },
    create: async () => {},
    get: async () => null,
  },
  action: {
    onClicked: event,
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
  },
};
PROVIDERS.claude.listCatalog = async () => {
  observations++;
  return {
    entries: [
      {
        principalId: owner,
        title: 'workspace title',
        cookie: 'secret',
        projects: [{ id: 'project', title: 'Project title' }],
        conversations: listing.map((id) => ({
          id,
          title: 'title ' + id,
          transcript: 'private body',
        })),
      },
    ],
  };
};
PROVIDERS.claude.principalIds = async () => {
  proofs++;
  return [owner];
};
await import('../background.js?popup-scope');
const send = (message, from = sender) =>
  new Promise((resolve) => {
    if (!listener(message, from, resolve)) resolve({ ok: false });
  });
const list = () => send({ type: 'LIST_SYNC_CATALOG', provider: 'claude' });
const edit = (catalogToken, selection, type = 'SAVE_SYNC_SELECTION') =>
  send({ type, provider: 'claude', catalogToken, selection });
assert.equal(
  (
    await send(
      { type: 'LIST_SYNC_CATALOG', provider: 'claude' },
      { ...sender, url: 'https://claude.ai/' },
    )
  ).ok,
  false,
);
assert.equal(
  (
    await send(
      { type: 'LIST_SYNC_CATALOG', provider: 'claude' },
      { ...sender, tab: { id: 1 }, frameId: 0 },
    )
  ).ok,
  false,
  'an extension tab is not the toolbar popup',
);
assert.equal(observations, 0);
const catalog = await list();
assert.equal(catalog.ok, true, 'metadata only catalog in extension popup');
assert.deepEqual(catalog.selectedScope, scope());
assert.ok(!JSON.stringify(catalog).includes('private body'));
assert.ok(!JSON.stringify(catalog).includes('secret'));
assert.equal((await edit(catalog.catalogToken, scope(entry(A, [], ['unknown'])))).ok, false);
assert.equal((await edit(catalog.catalogToken, scope(entry(B, [], ['first'])))).ok, false);
assert.equal((await edit(catalog.catalogToken, scope(entry(A, ['project'], ['first'])))).ok, true);
assert.equal(
  (await edit(catalog.catalogToken, scope(entry(A, [], ['second'])))).ok,
  true,
  'a second Add never drops earlier items',
);
assert.deepEqual(mem.providerSyncScopes.claude, scope(entry(A, ['project'], ['first', 'second'])));
assert.ok(!JSON.stringify([mem, session]).includes('title'));
assert.equal(native, 0);
owner = B;
assert.equal((await edit(catalog.catalogToken, scope(entry(A)))).code, 'principal_changed');
let fresh = await list();
assert.deepEqual(fresh.selectedScope, scope());
await edit(fresh.catalogToken, scope(entry(B, [], ['second'])));
owner = A;
listing = ['first'];
fresh = await list();
assert.deepEqual(fresh.selectedScope, scope(entry(A, ['project'], ['first', 'second'])));
await edit(fresh.catalogToken, scope(entry(A, [], [])));
assert.deepEqual(
  mem.providerSyncScopes.claude,
  scope(entry(A, ['project'], ['first', 'second']), entry(B, [], ['second'])),
);
await edit(fresh.catalogToken, scope(entry(A, ['project'], ['first'])), 'REMOVE_SYNC_SELECTION');
await edit(fresh.catalogToken, scope(entry(A, [], ['second'])), 'REMOVE_SYNC_SELECTION');
assert.deepEqual(mem.providerSyncScopes.claude, scope(entry(B, [], ['second'])));
assert.ok(proofs > 0);
assert.equal(native, 0);
mem.settings = { providers: { chatgpt: true }, captureTypes: { recents: true } };
assert.equal((await send({ type: 'syncNow' })).ok, false);
alarm({ name: 'hstry-sync' });
await new Promise((r) => setTimeout(r, 10));
assert.equal(native, 0, 'neither scheduled nor manual capture can fall back to category policy');

// Import old ChatGPT picks without treating an unseen standalone ID as permission for a moved project chat.
const chat1 = '1'.repeat(32),
  chat2 = '2'.repeat(32),
  chat3 = '3'.repeat(32),
  principalId = JSON.stringify(['user', 'workspace']);
mem.chatgptSyncList = {
  version: 1,
  entries: [
    { userId: 'user', accountId: 'workspace', projectIds: [], conversationIds: [chat1, chat2] },
  ],
};
PROVIDERS.chatgpt.listCatalog = async () => ({
  entries: [
    {
      principalId,
      title: 'Active',
      projects: [],
      conversations: [chat1, chat3].map((id) => ({ id, title: id })),
    },
  ],
});
PROVIDERS.chatgpt.principalIds = async () => [principalId];
// Unmodified v1 profiles also use the identity-rechecking capture implementation.
const originalArchive = 'e'.repeat(32);
mem.settings = {
  archiveId: originalArchive,
  nativeHost: 'org.ai_memory.archive_' + originalArchive,
  providers: { chatgpt: true },
};
chrome.runtime.sendNativeMessage = async (_host, packet) => {
  assert.equal(packet.op, 'check');
  return { status: 'ok', service: 'ai-memory', schema: 2, archive_instance_id: originalArchive };
};
let received;
PROVIDERS.chatgpt.sync = async (input) => {
  received = input;
  return { state: {}, conversations: 0 };
};
assert.equal((await send({ type: 'syncProvider', provider: 'chatgpt' })).ok, true);
for (let i = 0; i < 100 && mem.status.chatgpt.running; i++)
  await new Promise((r) => setTimeout(r, 2));
assert.deepEqual(
  received.selectedScope,
  scope({ ...entry(principalId, [], [chat1, chat2]), legacyConversationIds: [chat1, chat2] }),
);
assert.equal(received.chatgptSyncList, undefined);
const legacy = await send({ type: 'LIST_SYNC_CATALOG', provider: 'chatgpt' });
assert.deepEqual(legacy.selectedScope, scope(entry(principalId, [], [chat1, chat2])));
await send({
  type: 'SAVE_SYNC_SELECTION',
  provider: 'chatgpt',
  catalogToken: legacy.catalogToken,
  selection: scope(entry(principalId, [], [chat3])),
});
assert.deepEqual(
  mem.providerSyncScopes.chatgpt,
  scope({
    ...entry(principalId, [], [chat1, chat2, chat3]),
    legacyConversationIds: [chat1, chat2],
  }),
);
// Migration is atomic: deleting a newer key must never resurrect a v1 item the user removed.
assert.deepEqual(mem.chatgptSyncList, { version: 1, entries: [] });
// Removing an item during capture revokes its in-flight batch before the native archive sees it.
const archiveId = 'f'.repeat(32);
mem.settings = {
  archiveId,
  nativeHost: 'org.ai_memory.archive_' + archiveId,
  providers: { chatgpt: true },
};
const receipts = [];
chrome.runtime.sendNativeMessage = async (_, packet) => {
  if (packet.op === 'check')
    return { service: 'ai-memory', status: 'ok', schema: 2, archive_instance_id: archiveId };
  receipts.push(packet);
  return { conversations: 1, created: 1, updated: 0, unchanged: 0 };
};
const started = Promise.withResolvers(),
  resume = Promise.withResolvers();
PROVIDERS.chatgpt.sync = async ({ push, selectedScope }) => {
  assert.ok(selectedScope.entries[0].conversationIds.includes(chat3));
  started.resolve();
  await resume.promise;
  await push('chatgpt-web', 'chatgpt-web', [{ externalId: chat3, messages: [] }]);
  return { state: {}, conversations: 1 };
};
assert.equal((await send({ type: 'syncProvider', provider: 'chatgpt' })).ok, true);
await started.promise;
assert.equal(
  (
    await send({
      type: 'REMOVE_SYNC_SELECTION',
      provider: 'chatgpt',
      catalogToken: legacy.catalogToken,
      selection: scope(entry(principalId, [], [chat3])),
    })
  ).ok,
  true,
);
resume.resolve();
for (let i = 0; i < 100 && mem.status.chatgpt.running; i++)
  await new Promise((r) => setTimeout(r, 2));
assert.equal(mem.status.chatgpt.lastErrorCode, 'selection_changed');
assert.deepEqual(receipts, []);
