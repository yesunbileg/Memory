import assert from 'node:assert/strict';
import { elements, storageListeners } from './options-init.js';

const archiveId = 'a'.repeat(32);
const rejected = 'b'.repeat(32);
const slowId = 'c'.repeat(32);
const pair = (archiveId) => ({ archiveId, nativeHost: `org.ai_memory.archive_${archiveId}` });
let settings = {
  ...pair(archiveId),
  account: 'kept-account',
  intervalMinutes: 37,
  autoPaused: true,
  providers: { gemini: true },
  captureTypes: { projects: true, pinned: true, agents: true, recents: false },
};
const flush = () => new Promise(setImmediate);
const previousResult = {
  gemini: {
    captureScope: JSON.stringify([archiveId, 'gemini', 'profile', 'kept-account', {}]),
    lastRunMs: 100,
    progress: { phase: 'complete', created: 7 },
  },
};
chrome.storage.local.get = async (key) =>
  key === 'settings'
    ? { settings: structuredClone(settings) }
    : key === 'status'
      ? { status: previousResult }
      : {};
chrome.storage.local.set = async (value) => {
  if (value.settings) settings = structuredClone(value.settings);
};
let resolveFirst;
let signal;
const started = new Promise((resolve) => {
  signal = resolve;
});
const sent = [];
chrome.runtime.sendMessage = async (message) => {
  sent.push(message.type);
  if (message.type === 'verifyPairing') {
    if (message.candidate.archiveId === slowId) {
      signal();
      return new Promise((resolve) => {
        resolveFirst = resolve;
      });
    }
    return message.candidate.archiveId === rejected
      ? { ok: false, code: 'offline' }
      : { ok: true, archiveId: message.candidate.archiveId };
  }
  if (message.type === 'checkApi') return { ok: true, archiveId: settings.archiveId };
  return { ok: true };
};
async function replace(newSettings) {
  settings = newSettings;
  for (const fn of storageListeners) fn({ settings: { newValue: settings } }, 'local');
  await flush();
  await flush();
}
const choose = (id) => {
  const file = JSON.stringify({
    schema: 2,
    application: 'ai-memory',
    transport: 'native',
    archive_instance_id: id,
    native_host: pair(id).nativeHost,
  });
  return elements.get('connection-file').listeners.get('change')({
    target: { files: [{ size: file.length, text: async () => file }], value: 'fixture.json' },
  });
};
await replace(settings);
assert.equal(elements.get('connection-label').textContent, 'Connected');
assert.equal(elements.get('sync-now').disabled, true);
assert.ok(
  elements.get('resync-list').children.every((row) => row.querySelector('button').disabled),
);
await elements.get('sync-now').listeners.get('click')();
assert.ok(!sent.includes('syncNow'), 'ineligible captures never dispatch');
assert.match(elements.get('capture-outcome').textContent, /7 new/);
const before = structuredClone(settings);
const slow = choose(slowId);
await started;
await choose(rejected);
resolveFirst({ ok: true, archiveId: slowId });
await slow;
assert.deepEqual(
  settings,
  before,
  'neither a rejected candidate nor a stale success may overwrite a working pair or its preferences',
);
await choose('d'.repeat(32));
assert.equal(settings.archiveId, 'd'.repeat(32));
assert.doesNotMatch(
  elements.get('capture-outcome').textContent,
  /7 new/,
  'old acknowledgement no longer belongs to this archive',
);
assert.equal(settings.intervalMinutes, 37);
assert.equal(settings.autoPaused, true);
assert.equal(settings.providers.gemini, true);
assert.equal(settings.account, 'kept-account');
await replace({
  ...settings,
  providers: { claude: true },
  captureTypes: { projects: false, pinned: false, agents: false, recents: false },
});
assert.equal(elements.get('sync-now').disabled, true);
assert.ok(
  elements.get('resync-list').children.every((row) => row.querySelector('button').disabled),
);
await replace({ ...settings, captureTypes: { ...settings.captureTypes, pinned: true } });
assert.equal(elements.get('sync-now').disabled, false);
assert.equal(elements.get('resync-list').children[1].querySelector('button').disabled, false);
console.log('PASS verified pairing race, preference migration, and unavailable capture/rescan');
