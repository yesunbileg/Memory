// The Settings inspector and removal are limited to the active authenticated principal.
import assert from 'node:assert/strict';
const id = 'a'.repeat(32),
  runtimeId = 'p'.repeat(32);
const active = { userId: 'first', accountId: 'account', projectIds: [], conversationIds: [id] };
const foreign = { ...active, userId: 'other' };
const data = { chatgptSyncList: { version: 1, entries: [active, foreign] } };
let listener;
const event = { addListener() {} };
globalThis.chrome = {
  runtime: {
    id: runtimeId,
    getURL: (p) => 'chrome-extension://' + runtimeId + '/' + p,
    onInstalled: event,
    onStartup: event,
    onMessage: { addListener: (fn) => (listener = fn) },
  },
  alarms: { get: async () => null, create: async () => {}, onAlarm: event },
  action: { onClicked: event },
  storage: {
    local: {
      get: async (k) => ({ [k]: structuredClone(data[k]) }),
      set: async (obj) => Object.assign(data, obj),
    },
  },
};
const sender = { id: runtimeId, url: chrome.runtime.getURL('options.html') };
globalThis.fetch = async (input) => {
  assert.equal(String(input), 'https://chatgpt.com/api/auth/session');
  return Response.json({
    user: { id: 'first' },
    account: { id: 'account' },
    accessToken: 'fixture',
  });
};
await import('../background.js?manage-test');
const send = (message, from = sender) =>
  new Promise((resolve) => {
    if (!listener(message, from, resolve)) resolve({ ok: false });
  });
const response = await send({ type: 'GET_CHATGPT_SYNC_SELECTION' });
assert.deepEqual(response.entry, active);
assert.equal(
  (await send({ type: 'GET_CHATGPT_SYNC_SELECTION' }, { ...sender, url: 'https://chatgpt.com/' }))
    .ok,
  false,
);
assert.equal((await send({ type: 'REMOVE_CHATGPT_SYNC_SELECTION', ...foreign })).ok, false);
assert.deepEqual(data.chatgptSyncList.entries, [active, foreign]);
assert.equal((await send({ type: 'REMOVE_CHATGPT_SYNC_SELECTION', ...active })).ok, true);
assert.deepEqual(data.chatgptSyncList.entries, [foreign]);

// Revoking an item while a detail is in flight must stop it at the receiver boundary.
const { PROVIDERS } = await import('../providers/index.js');
const archiveId = 'f'.repeat(32);
data.settings = {
  nativeHost: 'org.ai_memory.archive_' + archiveId,
  archiveId,
  providers: { chatgpt: true, claude: false, gemini: false },
  captureTypes: { recents: false, projects: false, pinned: false, agents: false },
};
data.chatgptSyncList = { version: 1, entries: [active] };
const receipts = [];
chrome.runtime.sendNativeMessage = async (_host, body) => {
  if (body.op === 'check')
    return { status: 'ok', service: 'ai-memory', schema: 2, archive_instance_id: archiveId };
  receipts.push(body);
  return { conversations: 1, created: 1, updated: 0, unchanged: 0 };
};
chrome.action.setBadgeBackgroundColor = chrome.action.setBadgeText = async () => {};
const started = Promise.withResolvers(),
  release = Promise.withResolvers();
PROVIDERS.chatgpt.sync = async ({ push, chatgptSyncList }) => {
  assert.deepEqual(chatgptSyncList.entries, [active]);
  started.resolve();
  await release.promise;
  await push('chatgpt-web', 'chatgpt-web', [{ externalId: id, messages: [] }]);
  return { state: {}, conversations: 1 };
};
assert.equal((await send({ type: 'syncProvider', provider: 'chatgpt' })).ok, true);
await started.promise;
assert.equal((await send({ type: 'REMOVE_CHATGPT_SYNC_SELECTION', ...active })).ok, true);
release.resolve();
for (let i = 0; i < 50 && data.status.chatgpt.running; i++)
  await new Promise((r) => setTimeout(r, 2));
assert.equal(data.status.chatgpt.lastErrorCode, 'selection_changed');
assert.deepEqual(receipts, []);
assert.deepEqual(data.chatgptSyncList, { version: 1, entries: [] });
