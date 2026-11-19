import assert from 'node:assert/strict';
import { runtimeId, popup, providerSyncScopes } from './fixtures/worker-popup.js';
import { PROVIDERS } from '../providers/index.js';
const archiveId = 'a'.repeat(32),
  nativeHost = 'org.ai_memory.archive_' + archiveId;
let listener,
  failWrite = false,
  nativeReads = 0;
const mem = {
  providerSyncScopes,
  settings: {
    nativeHost,
    archiveId,
    providers: { chatgpt: true, claude: true },
    captureTypes: { recents: true },
  },
  status: { chatgpt: { running: true, continuationPending: true, state: { marker: 1 } } },
};
let alarmListener;
const alarms = new Map();
globalThis.chrome = {
  storage: {
    local: {
      get: async (k) => ({ [k]: structuredClone(mem[k]) }),
      set: async (values) => {
        if (failWrite && values.status) {
          failWrite = false;
          throw Error('temporary storage problem');
        }
        Object.assign(mem, structuredClone(values));
      },
    },
  },
  runtime: {
    id: runtimeId,
    sendNativeMessage: async () => {
      nativeReads++;
      return { schema: 2, service: 'ai-memory', status: 'ok', archive_instance_id: archiveId };
    },
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: {
      addListener(fn) {
        listener = fn;
      },
    },
  },
  alarms: {
    get: async (name) => alarms.get(name),
    create: async (name, v) => alarms.set(name, v),
    onAlarm: {
      addListener(fn) {
        alarmListener = fn;
      },
    },
  },
  action: {
    onClicked: { addListener() {} },
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
  },
};
await import('../background.js?recovery');
await new Promise((r) => setTimeout(r, 10));
assert.ok(
  alarms.has('hstry-sync-continue-chatgpt'),
  'lost continuation alarm is restored after worker restart',
);
assert.ok(alarms.has('hstry-sync'));
let providerReads = 0;
for (const p of ['chatgpt', 'claude'])
  PROVIDERS[p].sync = async () => {
    providerReads++;
    return { state: {}, conversations: 0 };
  };
const send = (msg) => new Promise((resolve) => listener(msg, popup, resolve));
failWrite = true;
await send({ type: 'syncNow' });
await new Promise((r) => setTimeout(r, 20));
await send({ type: 'syncNow' });
await new Promise((r) => setTimeout(r, 20));
assert.ok(
  providerReads >= 2,
  'temporary storage failure never poisons the provider lock or next capture',
);
const logKeys = [
  'id',
  'timestampMs',
  'provider',
  'trigger',
  'outcome',
  'errorCode',
  'durationMs',
  'counts',
].sort();
assert.ok(mem.activityLog.length > 0);
for (const record of mem.activityLog) {
  assert.deepEqual(Object.keys(record).sort(), logKeys);
  assert.deepEqual(record.counts, {
    detected: 0,
    processed: 0,
    created: 0,
    updated: 0,
    unchanged: 0,
    skipped: 0,
    unknown: 0,
  });
}
mem.settings.providers = { gemini: true };
mem.settings.captureTypes = { recents: false, projects: true, agents: true, pinned: true };
const before = nativeReads;
const result = await send({ type: 'syncNow' });
assert.equal(result.ok, false);
assert.equal(nativeReads, before);
assert.equal(providerReads >= 2, true);

// A scheduled wake earlier than the persisted retry deadline does not contact
// the provider and recreates the one-shot retry for the real deadline.
mem.settings.providers = { chatgpt: true };
mem.settings.captureTypes = { recents: true };
await send({ type: 'syncNow' });
await new Promise((r) => setTimeout(r, 10));
const next = Date.now() + 120_000;
mem.status.chatgpt.nextRetryMs = next;
const previous = providerReads;
alarms.delete('hstry-sync-continue-chatgpt');
alarmListener({ name: 'hstry-sync-continue-chatgpt' });
await new Promise((r) => setTimeout(r, 10));
assert.equal(providerReads, previous);
assert.equal(alarms.get('hstry-sync-continue-chatgpt')?.when, next);
