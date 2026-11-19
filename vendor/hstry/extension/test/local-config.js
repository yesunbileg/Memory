// AI Memory boundary: supported providers and a verified, local native archive.
import assert from 'node:assert/strict';
import { runtimeId, popup, providerSyncScopes } from './fixtures/worker-popup.js';
import { readFile } from 'node:fs/promises';
import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from '../providers/index.js';

assert.deepEqual(Object.keys(PROVIDERS), ['chatgpt', 'claude', 'gemini']);
assert.deepEqual(DEFAULT_PROVIDER_SETTINGS, { chatgpt: false, claude: false, gemini: false });
const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
assert.deepEqual(manifest.host_permissions, [
  'https://chatgpt.com/*',
  'https://claude.ai/*',
  'https://gemini.google.com/*',
]);

const archiveId = 'a'.repeat(32),
  nativeHost = 'org.ai_memory.archive_' + archiveId;
let settings = { port: 41770, token: '', providers: { chatgpt: true } };
let scopes = providerSyncScopes;
let status = {};
let messageListener;
const writes = [];
const warnings = [];
globalThis.chrome = {
  storage: {
    local: {
      get: async (key) =>
        key === 'providerSyncScopes'
          ? { providerSyncScopes: scopes }
          : key === 'settings'
            ? { settings }
            : { status },
      set: async (value) => {
        if (value.status) {
          status = structuredClone(value.status);
          writes.push(status);
        }
      },
    },
  },
  runtime: {
    id: runtimeId,
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: {
      addListener(listener) {
        messageListener = listener;
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
const calls = [];
globalThis.fetch = async (url) => {
  calls.push(url);
  throw new Error('No request should occur without an ingest token');
};
const realWarn = console.warn;
console.warn = (...args) => warnings.push(args.map(String).join(' '));
await import('../background.js');
messageListener({ type: 'syncNow' }, popup, () => {});
for (let turn = 0; turn < 100; turn++) {
  await new Promise((resolve) => setTimeout(resolve, 1));
  if (writes.some((state) => state.chatgpt?.lastError)) break;
}
assert.equal(calls.length, 0);
assert.match(status.chatgpt.lastError, /token|connection/i);
assert.equal(status.chatgpt.running, false);

function send(message) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Worker did not respond')), 2000);
    messageListener(message, popup, (response) => {
      clearTimeout(timeout);
      resolve(response);
    });
  });
}
async function finished() {
  for (let turn = 0; turn < 200; turn++) {
    if (!status.chatgpt?.running) return;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error('Worker did not finish');
}

globalThis.fetch = async (url) => {
  calls.push(url);
  return new Response(JSON.stringify({ status: 'ok' }));
};
assert.deepEqual(
  await send({ type: 'settingsChanged' }),
  {
    ok: true,
    sources: { chatgpt: { source: 'chatgpt-web', adapter: 'chatgpt-web' } },
  },
  'configured sources are descriptors only: no network, registration or consent change',
);
assert.equal(calls.length, 0);
const missing = await send({ type: 'checkApi' });
assert.equal(missing.ok, false, 'public health cannot establish a connection without a token');
assert.equal(missing.code, 'missing_token');
assert.equal(calls.length, 0);
const originalUnpairedSettings = settings;
settings = { ...settings, archiveId: [archiveId], nativeHost: [nativeHost] };
assert.equal(
  (await send({ type: 'checkApi' })).code,
  'incompatible_server',
  'coercible values in storage are not a valid native pairing',
);
settings = originalUnpairedSettings;
const blockedFull = await send({ type: 'fullSyncProvider', provider: 'chatgpt' });
assert.equal(blockedFull.ok, false, 'blocked sync must not acknowledge a successful start');
assert.equal(blockedFull.code, 'missing_token');

settings = { ...settings, archiveId, nativeHost, token: 'synthetic-private-worker-token-123456' };
status.chatgpt = {
  state: { lastSyncMs: 123, accounts: { legacy: true }, pending: ['old'] },
  lastSuccessMs: 111,
};
const providerInputs = [];
const originalSync = PROVIDERS.chatgpt.sync;
PROVIDERS.chatgpt.sync = async ({ state, captureTypes, report }) => {
  providerInputs.push(structuredClone({ state, captureTypes }));
  const selection = { selected: 2, skipped: 3, unknown: 1 };
  await report({ selection, selectionWarning: null });
  return { state: { cursor: 'saved' }, conversations: 2, selection, selectionWarning: null };
};
chrome.runtime.sendNativeMessage = async (host, packet) => {
  calls.push(host);
  assert.equal(host, nativeHost);
  assert.deepEqual(packet, { op: 'check', archive_instance_id: archiveId });
  return { error: 'unauthorized' };
};
const rejected = await send({ type: 'syncNow' });
assert.equal(rejected.ok, false);
assert.equal(rejected.code, 'unauthorized');
assert.equal(
  providerInputs.length,
  0,
  'no remote provider reads before successful local authentication',
);
assert.equal(status.chatgpt.lastSuccessMs, 111);
assert.equal(status.chatgpt.lastErrorCode, 'unauthorized');

chrome.runtime.sendNativeMessage = async () => ({
  status: 'ok',
  service: 'ai-memory',
  schema: 2,
  archive_instance_id: archiveId,
});
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.deepEqual(providerInputs[0], {
  state: {},
  captureTypes: { projects: true, pinned: true, agents: true, recents: false },
});
assert.deepEqual(status.chatgpt.capturePolicy, {
  projects: true,
  pinned: true,
  agents: true,
  recents: false,
});
assert.deepEqual(status.chatgpt.selection, { selected: 2, skipped: 3, unknown: 1 });
assert.deepEqual(status.chatgpt.progress.selection, status.chatgpt.selection);
assert.equal(status.chatgpt.lastError, null);

assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.deepEqual(providerInputs[1].state, { cursor: 'saved' });
settings.captureTypes = { projects: true, pinned: true, agents: true, recents: true };
assert.equal((await send({ type: 'settingsChanged' })).ok, true);
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.deepEqual(providerInputs[2].state, {});
assert.equal(providerInputs[2].captureTypes.recents, true);
assert.deepEqual(settings.providers, { chatgpt: true });

let releaseSnapshotRun;
let snapshotInput;
PROVIDERS.chatgpt.sync = async (input) => {
  snapshotInput = structuredClone(input.captureTypes);
  return new Promise((resolve) => {
    releaseSnapshotRun = resolve;
  });
};
const originalPolicy = structuredClone(settings.captureTypes);
assert.equal((await send({ type: 'syncNow' })).ok, true);
for (let turn = 0; !releaseSnapshotRun && turn < 100; turn++) {
  await new Promise((resolve) => setTimeout(resolve, 1));
}
assert.equal(typeof releaseSnapshotRun, 'function');
settings.captureTypes = { projects: false, pinned: true, agents: false, recents: false };
await send({ type: 'settingsChanged' });
releaseSnapshotRun({ state: { cursor: 'previous-policy' }, conversations: 0 });
await finished();
assert.deepEqual(snapshotInput, originalPolicy);
assert.deepEqual(status.chatgpt.capturePolicy, originalPolicy);
PROVIDERS.chatgpt.sync = async ({ state, captureTypes }) => {
  providerInputs.push(structuredClone({ state, captureTypes }));
  return { state: {}, conversations: 0 };
};
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.deepEqual(providerInputs.at(-1), { state: {}, captureTypes: settings.captureTypes });

const archiveRequests = [];
chrome.runtime.sendNativeMessage = async (host, packet) => {
  if (packet.op === 'check')
    return { status: 'ok', service: 'ai-memory', schema: 2, archive_instance_id: archiveId };
  archiveRequests.push({ host, packet });
  return { conversations: 1, created: 1, updated: 0, unchanged: 0 };
};
PROVIDERS.chatgpt.sync = async ({ push }) => {
  await push('chatgpt-web', 'chatgpt-web', [{ externalId: 'synthetic', messages: [] }]);
  return { state: {}, conversations: 1 };
};
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.equal(archiveRequests.length, 1);
assert.equal(archiveRequests[0].host, nativeHost);
assert.equal(archiveRequests[0].packet.archive_instance_id, archiveId);
assert.ok(!JSON.stringify(archiveRequests).includes(settings.token));
assert.equal(status.chatgpt.progress.accepted, 1);

const previousSuccess = status.chatgpt.lastSuccessMs;
PROVIDERS.chatgpt.sync = async () => ({
  state: {},
  conversations: 0,
  selection: { skipped: 4, unknown: 4, selected: 0 },
  selectionWarning: 'Some category metadata is unavailable.',
});
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.equal(status.chatgpt.selectionWarning, 'Some category metadata is unavailable.');
assert.equal(status.chatgpt.progress.selectionWarning, status.chatgpt.selectionWarning);
assert.notEqual(status.chatgpt.progress.phase, 'complete');
assert.equal(
  status.chatgpt.lastSuccessMs,
  previousSuccess,
  'unsupported metadata does not establish a successful sync',
);

PROVIDERS.chatgpt.sync = async () => {
  throw new Error(`unsafe provider diagnostic ${settings.token}`);
};
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.equal(status.chatgpt.lastError.includes(settings.token), false);
assert.equal(warnings.join('\n').includes(settings.token), false);

// A pending provider must not post a batch after the user revokes capture.
let releaseBatch;
let batchHeld;
const held = new Promise((resolve) => {
  batchHeld = resolve;
});
PROVIDERS.chatgpt.sync = async ({ push }) => {
  await new Promise((resolve) => {
    releaseBatch = resolve;
    batchHeld();
  });
  await push('chatgpt-web', 'chatgpt-web', [{ externalId: 'revoked', messages: [] }]);
  return { state: { cursor: 'revoked' }, conversations: 1 };
};
const beforeRevocation = archiveRequests.length;
assert.equal((await send({ type: 'syncNow' })).ok, true);
await held;
settings = {
  ...settings,
  providers: { chatgpt: false },
  captureTypes: { projects: false, pinned: false, agents: false, recents: false },
};
await send({ type: 'settingsChanged' });
releaseBatch();
await finished();
assert.equal(archiveRequests.length, beforeRevocation, 'revoked batches must never reach ingest');
assert.notEqual(status.chatgpt.progress.phase, 'complete');
assert.notEqual(status.chatgpt.state?.cursor, 'revoked');

settings = { ...settings, providers: { chatgpt: true } };
scopes = {};
const noTypes = await send({ type: 'syncNow' });
assert.equal(noTypes.ok, false);
assert.equal(noTypes.code, 'no_saved_selection');
assert.equal(
  (await send({ type: 'fullSyncProvider', provider: 'chatgpt' })).code,
  'no_saved_selection',
);
PROVIDERS.chatgpt.sync = originalSync;
console.warn = realWarn;
console.log(
  'PASS local-only settings, authenticated worker startup, capture-policy resets, and safe partial status',
);
