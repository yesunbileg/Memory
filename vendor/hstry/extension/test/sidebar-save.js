// The real service worker must treat a sidebar click as an additive, account-bound
// preference change, never a local archive operation or a conversation read.
import assert from 'node:assert/strict';

const id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const sender = { id, frameId: 0, url: 'https://chatgpt.com/', tab: { id: 1 } };
const project = 'g-p-' + '1'.repeat(32);
const chat = '22222222-2222-4222-8222-222222222222';
const other = '33333333-3333-4333-8333-333333333333';
const third = '44444444-4444-4444-8444-444444444444';
const data = { settings: { autoPaused: true } };
const accountA = 'workspace-a';
let account = accountA;
const requests = [];
const native = [];
let listener;
const event = { addListener() {} };
globalThis.chrome = {
  runtime: {
    id,
    onInstalled: event,
    onStartup: event,
    onMessage: { addListener: (fn) => (listener = fn) },
    sendNativeMessage: async (...args) => {
      native.push(args);
      throw Error('Saving a selection must not contact an archive');
    },
  },
  storage: {
    local: {
      get: async (keys) =>
        Object.fromEntries(
          (Array.isArray(keys) ? keys : [keys]).map((key) => [key, structuredClone(data[key])]),
        ),
      set: async (values) => Object.assign(data, structuredClone(values)),
    },
    onChanged: event,
  },
  alarms: { onAlarm: event, get: async () => null, create: async () => {} },
  action: { onClicked: event },
};
globalThis.fetch = async (url) => {
  requests.push(String(url));
  if (String(url) !== 'https://chatgpt.com/api/auth/session')
    throw Error('Saving a preference cannot read conversation or project endpoints');
  return Response.json({
    user: { id: 'user-fixture' },
    account: account ? { id: account } : {},
    accessToken: 'fixture-secret-not-for-storage',
  });
};
await import('../background.js?sidebar-save');
const send = (message, from = sender) =>
  new Promise((resolve) => {
    const handled = listener(message, from, resolve);
    if (!handled) resolve({ ok: false });
  });

const save = (projectIds, conversationIds, from = sender) =>
  send({ type: 'ADD_CHATGPT_SYNC_SELECTION', projectIds, conversationIds }, from);
const first = await save([project], [chat]);
assert.equal(first?.ok, true, 'the worker accepts an eligible sidebar selection');
const concurrent = await Promise.all([save([project], [chat, third]), save([], [other])]);
assert.ok(concurrent.every((result) => result?.ok));
const entry = data.chatgptSyncList.entries.find((entry) => entry.accountId === accountA);
assert.equal(entry.userId, 'user-fixture');
assert.deepEqual(entry.projectIds, [project]);
assert.deepEqual(entry.conversationIds.toSorted(), [chat, other, third]);
const persisted = structuredClone(data.chatgptSyncList);

for (const badSender of [
  { ...sender, url: 'https://example.org/' },
  { ...sender, frameId: 1 },
  { ...sender, id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' },
]) {
  assert.notEqual((await save([project], [chat], badSender))?.ok, true);
}
assert.deepEqual(data.chatgptSyncList, persisted, 'untrusted contexts cannot change saved IDs');
const before = requests.length;
assert.notEqual(
  (await send({ type: 'syncProvider', provider: 'chatgpt' }))?.ok,
  true,
  'content scripts cannot initiate sync',
);
assert.equal(requests.length, before);
account = null;
assert.notEqual((await save([], [other]))?.ok, true, 'no verified active account');
assert.deepEqual(data.chatgptSyncList, persisted, 'failed save does not erase earlier picks');
account = 'workspace-b';
assert.equal((await save([], [other]))?.ok, true);
assert.deepEqual(
  data.chatgptSyncList.entries.find((entry) => entry.accountId === accountA),
  entry,
  'switching accounts does not modify the previous list',
);
const second = data.chatgptSyncList.entries.find((entry) => entry.accountId === account);
assert.deepEqual(second.conversationIds, [other]);
assert.deepEqual(second.projectIds, []);
assert.ok(!JSON.stringify(data).includes('fixture-secret-not-for-storage'));
assert.ok(requests.every((url) => url === 'https://chatgpt.com/api/auth/session'));
assert.deepEqual(native, []);
assert.deepEqual(data.settings, { autoPaused: true });
assert.equal(data.status?.chatgpt, undefined);
console.log(
  'PASS sidebar save only merges local IDs, isolates accounts and never reads or uploads content',
);
