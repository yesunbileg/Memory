// AI Memory boundary: supported providers, local-only destination, required token.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from '../providers/index.js';

assert.deepEqual(Object.keys(PROVIDERS), ['chatgpt', 'claude', 'gemini']);
assert.deepEqual(DEFAULT_PROVIDER_SETTINGS, { chatgpt: false, claude: false, gemini: false });
const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
assert.deepEqual(manifest.host_permissions, [
  'https://chatgpt.com/*', 'https://chat.openai.com/*', 'https://claude.ai/*',
  'https://gemini.google.com/*', 'http://127.0.0.1/*',
]);

let settings = { port: 41770, token: '', providers: { chatgpt: true } };
let status = {};
let messageListener;
const writes = [];
const warnings = [];
globalThis.chrome = {
  storage: { local: {
    get: async key => key === 'settings' ? { settings } : { status },
    set: async value => { if (value.status) { status = structuredClone(value.status); writes.push(status); } },
  } },
  runtime: {
    onInstalled: { addListener() {} }, onStartup: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
  },
  alarms: { onAlarm: { addListener() {} }, get: async () => null, create: async () => {} },
  action: { onClicked: { addListener() {} }, setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
};
const calls = [];
globalThis.fetch = async url => { calls.push(url); throw new Error('No request should occur without an ingest token'); };
const realWarn = console.warn;
console.warn = (...args) => warnings.push(args.map(String).join(' '));
await import('../background.js');
messageListener({ type: 'syncNow' }, {}, () => {});
for (let turn = 0; turn < 100; turn++) {
  await new Promise(resolve => setTimeout(resolve, 1));
  if (writes.some(state => state.chatgpt?.lastError)) break;
}
assert.equal(calls.length, 0);
assert.match(status.chatgpt.lastError, /token|connection/i);
assert.equal(status.chatgpt.running, false);

function send(message) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Worker did not respond')), 2000);
    messageListener(message, {}, response => { clearTimeout(timeout); resolve(response); });
  });
}
async function finished() {
  for (let turn = 0; turn < 200; turn++) {
    if (!status.chatgpt?.running) return;
    await new Promise(resolve => setTimeout(resolve, 1));
  }
  throw new Error('Worker did not finish');
}

globalThis.fetch = async url => {
  calls.push(url);
  return new Response(JSON.stringify({ status: 'ok' }));
};
const missing = await send({ type: 'checkApi' });
assert.equal(missing.ok, false, 'public health cannot establish a connection without a token');
assert.equal(missing.code, 'missing_token');
assert.equal(calls.length, 0);
const blockedFull = await send({ type: 'fullSyncProvider', provider: 'chatgpt' });
assert.equal(blockedFull.ok, false, 'blocked sync must not acknowledge a successful start');
assert.equal(blockedFull.code, 'missing_token');

settings = { ...settings, token: 'synthetic-private-worker-token-123456' };
status.chatgpt = { state: { lastSyncMs: 123, accounts: { legacy: true }, pending: ['old'] }, lastSuccessMs: 111 };
const providerInputs = [];
const originalSync = PROVIDERS.chatgpt.sync;
PROVIDERS.chatgpt.sync = async ({ state, captureTypes, report }) => {
  providerInputs.push(structuredClone({ state, captureTypes }));
  const selection = { selected: 2, skipped: 3, unknown: 1 };
  await report({ selection, selectionWarning: null });
  return { state: { cursor: 'saved' }, conversations: 2, selection, selectionWarning: null };
};
globalThis.fetch = async (url, options) => {
  calls.push(url);
  assert.equal(new URL(url).pathname, '/auth/check');
  assert.equal(options.method, 'POST');
  assert.equal(options.body, '{}');
  return new Response('', { status: 401 });
};
const rejected = await send({ type: 'syncNow' });
assert.equal(rejected.ok, false);
assert.equal(rejected.code, 'unauthorized');
assert.equal(providerInputs.length, 0, 'no remote provider reads before successful local authentication');
assert.equal(status.chatgpt.lastSuccessMs, 111);
assert.equal(status.chatgpt.lastErrorCode, 'unauthorized');

globalThis.fetch = async () => new Response(JSON.stringify({ status: 'ok', service: 'ai-memory', schema: 1 }));
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.deepEqual(providerInputs[0], {
  state: {}, captureTypes: { projects: true, pinned: true, agents: true, recents: false },
});
assert.deepEqual(status.chatgpt.capturePolicy, { projects: true, pinned: true, agents: true, recents: false });
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
PROVIDERS.chatgpt.sync = async input => {
  snapshotInput = structuredClone(input.captureTypes);
  return new Promise(resolve => { releaseSnapshotRun = resolve; });
};
const originalPolicy = structuredClone(settings.captureTypes);
assert.equal((await send({ type: 'syncNow' })).ok, true);
for (let turn = 0; !releaseSnapshotRun && turn < 100; turn++) {
  await new Promise(resolve => setTimeout(resolve, 1));
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
globalThis.fetch = async (url, options) => {
  if (new URL(url).pathname === '/auth/check') {
    return new Response(JSON.stringify({ status: 'ok', service: 'ai-memory', schema: 1 }));
  }
  archiveRequests.push({ url, options });
  return new Response(JSON.stringify({ conversations: 1, created: 1, updated: 0 }));
};
PROVIDERS.chatgpt.sync = async ({ push }) => {
  await push('chatgpt-web', 'chatgpt-web', [{ externalId: 'synthetic', messages: [] }]);
  return { state: {}, conversations: 1 };
};
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.equal(archiveRequests.length, 1);
assert.equal(archiveRequests[0].url, 'http://127.0.0.1:41770/ingest');
assert.equal(archiveRequests[0].options.redirect, 'error', 'archive payload must never follow a remote redirect');
assert.equal(status.chatgpt.progress.accepted, 1);

const previousSuccess = status.chatgpt.lastSuccessMs;
PROVIDERS.chatgpt.sync = async () => ({
  state: {}, conversations: 0, selection: { skipped: 4, unknown: 4, selected: 0 },
  selectionWarning: 'Some category metadata is unavailable.',
});
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.equal(status.chatgpt.selectionWarning, 'Some category metadata is unavailable.');
assert.equal(status.chatgpt.progress.selectionWarning, status.chatgpt.selectionWarning);
assert.notEqual(status.chatgpt.progress.phase, 'complete');
assert.equal(status.chatgpt.lastSuccessMs, previousSuccess, 'unsupported metadata does not establish a successful sync');

PROVIDERS.chatgpt.sync = async () => { throw new Error(`unsafe provider diagnostic ${settings.token}`); };
assert.equal((await send({ type: 'syncNow' })).ok, true);
await finished();
assert.equal(status.chatgpt.lastError.includes(settings.token), false);
assert.equal(warnings.join('\n').includes(settings.token), false);

// A pending provider must not post a batch after the user revokes capture.
let releaseBatch;
let batchHeld;
const held = new Promise(resolve => { batchHeld = resolve; });
PROVIDERS.chatgpt.sync = async ({push}) => {
  await new Promise(resolve => { releaseBatch = resolve; batchHeld(); });
  await push('chatgpt-web', 'chatgpt-web', [{externalId:'revoked',messages:[]}]);
  return {state:{cursor:'revoked'},conversations:1};
};
const beforeRevocation = archiveRequests.length;
assert.equal((await send({type:'syncNow'})).ok,true);
await held;
settings = {...settings,providers:{chatgpt:false},captureTypes:{projects:false,pinned:false,agents:false,recents:false}};
await send({type:'settingsChanged'});
releaseBatch();
await finished();
assert.equal(archiveRequests.length,beforeRevocation,'revoked batches must never reach ingest');
assert.notEqual(status.chatgpt.progress.phase,'complete');
assert.notEqual(status.chatgpt.state?.cursor,'revoked');

settings = {...settings,providers:{chatgpt:true}};
const noTypes = await send({type:'syncNow'});
assert.equal(noTypes.ok,false);
assert.equal(noTypes.code,'no_capture_types');
assert.equal((await send({type:'fullSyncProvider',provider:'chatgpt'})).code,'no_capture_types');
PROVIDERS.chatgpt.sync = originalSync;
console.warn = realWarn;
console.log('PASS local-only settings, authenticated worker startup, capture-policy resets, and safe partial status');
