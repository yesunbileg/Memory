// Nonvisual worker integration: destination and namespace changes replay history.
import assert from 'node:assert/strict';
import { runtimeId, popup, providerSyncScopes } from './fixtures/worker-popup.js';
import { PROVIDERS } from '../providers/index.js';
const archiveId = 'a'.repeat(32),
  nativeHost = 'org.ai_memory.archive_' + archiveId;
let settings = {
  archiveId,
  nativeHost,
  port: 41770,
  token: 'synthetic-private-ingest-token',
  account: 'one',
  providers: { chatgpt: true },
  captureTypes: { recents: true },
};
let status = {},
  listener,
  archive = archiveId;
const states = [];
globalThis.chrome = {
  storage: {
    local: {
      get: async (key) =>
        key === 'providerSyncScopes'
          ? { providerSyncScopes }
          : key === 'settings'
            ? { settings: structuredClone(settings) }
            : key === 'status'
              ? { status: structuredClone(status) }
              : {},
      set: async (data) => {
        if (data.status) status = structuredClone(data.status);
      },
    },
  },
  runtime: {
    id: runtimeId,
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: {
      addListener(fn) {
        listener = fn;
      },
    },
  },
  alarms: { onAlarm: { addListener() {} }, get: async () => null, create: async () => {} },
  action: {
    onClicked: { addListener() {} },
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
  },
};
chrome.runtime.sendNativeMessage = async () => ({
  status: 'ok',
  service: 'ai-memory',
  schema: 2,
  archive_instance_id: archive,
});
PROVIDERS.chatgpt.sync = async ({ state }) => {
  states.push(structuredClone(state));
  return { state: { watermark: 123 }, conversations: 0 };
};
await import('../background.js?checkpoint-scope');
async function run() {
  await new Promise((resolve) =>
    listener({ type: 'syncProvider', provider: 'chatgpt' }, popup, resolve),
  );
  for (let i = 0; i < 100 && status.chatgpt?.running; i++)
    await new Promise((r) => setTimeout(r, 2));
  assert.equal(status.chatgpt.running, false);
}
await run();
await run();
assert.deepEqual(
  states[1],
  { watermark: 123 },
  'matching logical archive reuses acknowledged state',
);
archive = 'b'.repeat(32);
await run();
assert.equal(states.length, 2, 'wrong archive must not read any provider');
settings = { ...settings, archiveId: archive, nativeHost: 'org.ai_memory.archive_' + archive };
await run();
assert.deepEqual(states[2], {}, 'new database at identical receiver address must replay');
settings.account = 'two';
await run();
assert.deepEqual(states[3], {}, 'changed configured principal namespace must replay');
settings.token = 'rotated-synthetic-token';
settings.port = 43210;
await run();
assert.deepEqual(
  states[4],
  { watermark: 123 },
  'credentials and port are not logical archive identity',
);
archive = null;
const legacyProbe = await new Promise((resolve) => listener({ type: 'checkApi' }, popup, resolve));
assert.equal(legacyProbe.ok, false, 'UI connection checks must reflect capture eligibility');
assert.equal(legacyProbe.code, 'archive_changed');
await run();
await run();
assert.equal(
  states.length,
  5,
  'unverifiable legacy receiver must not read providers or loop continuations',
);
assert.equal(status.chatgpt.lastErrorCode, 'archive_changed');
assert.equal(status.chatgpt.continuationPending, false);
console.log(
  'PASS authenticated destination/account checkpoint scope and conservative legacy replay',
);
