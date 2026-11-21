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
    clear: async (name) => alarms.delete(name),
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

// A healthy worker should finish queued slices without waiting for the alarm.
// Keep that alarm available as a recovery path if the worker is suspended.
const resumedStates = [];
PROVIDERS.chatgpt.sync = async ({ state }) => {
  resumedStates.push(state.slice ?? 0);
  const slice = (state.slice ?? 0) + 1;
  return { state: { slice }, conversations: 0, hasMore: slice < 3 };
};
await send({ type: 'syncNow' });
const waitUntil = Date.now() + 3_000;
while (Date.now() < waitUntil && (resumedStates.length < 3 || mem.status.chatgpt.running))
  await new Promise((r) => setTimeout(r, 20));
assert.deepEqual(resumedStates, [0, 1, 2]);
assert.equal(mem.status.chatgpt.continuationPending, false);
assert.ok(alarms.has('hstry-sync-continue-chatgpt'), 'the recovery alarm was persisted');

const stop = () =>
  new Promise((resolve) => {
    if (!listener({ type: 'stopSync' }, popup, resolve)) resolve({ ok: false });
  });
const until = async (check) => {
  for (let i = 0; i < 200 && !check(); i++) await new Promise((r) => setTimeout(r, 5));
  assert.ok(check(), 'worker did not reach the expected state');
};
const pausedRead = Promise.withResolvers();
const pausedCheckpoint = Promise.withResolvers();
let entered = false;
let followupReads = 0;
const continuationAlarm = 'hstry-sync-continue-chatgpt';
alarms.delete(continuationAlarm);
PROVIDERS.chatgpt.sync = async ({ guard, checkpoint }) => {
  assert.ok(alarms.has(continuationAlarm), 'a wake-up exists before the first provider read');
  entered = true;
  await pausedRead.promise;
  await checkpoint({ accounts: { fixture: { archivedUpdates: [['saved', 123]] } }, pending: true });
  await pausedCheckpoint.promise;
  await guard();
  followupReads++;
  return { state: {}, conversations: 0 };
};
await send({ type: 'syncNow' });
await until(() => entered);
assert.equal(mem.status.chatgpt.continuationPending, false, 'the first provider read is blocked');
alarms.delete(continuationAlarm); // One-shot alarms disappear when delivered.
alarmListener({ name: continuationAlarm });
await until(() => alarms.has(continuationAlarm));
pausedRead.resolve();
await until(() => mem.status.chatgpt.continuationPending);
assert.equal((await stop()).ok, true, 'the popup can stop an active run');
assert.equal(mem.status.chatgpt.progress.phase, 'stopping');
pausedCheckpoint.resolve();
await until(() => mem.status.chatgpt.progress.phase === 'cancelled');
assert.equal(followupReads, 0);
assert.equal(mem.status.chatgpt.continuationPending, false);
assert.deepEqual(mem.status.chatgpt.state.accounts.fixture.archivedUpdates, [['saved', 123]]);

let queuedRuns = 0;
PROVIDERS.chatgpt.sync = async () => {
  queuedRuns++;
  return { state: {}, conversations: 0, hasMore: true };
};
await send({ type: 'syncNow' });
await until(() => !mem.status.chatgpt.running && mem.status.chatgpt.continuationPending);
assert.equal((await stop()).ok, true, 'the popup can also stop a queued run');
assert.equal(mem.status.chatgpt.progress.phase, 'cancelled');
assert.ok(!alarms.has('hstry-sync-continue-chatgpt'));
assert.ok(alarms.has('hstry-sync'), 'stop does not disable the normal periodic schedule');
alarmListener({ name: 'hstry-sync-continue-chatgpt' });
await new Promise((r) => setTimeout(r, 450));
assert.equal(queuedRuns, 1, 'neither the queued timer nor the alarm may resume a stopped run');

// A one-shot alarm may be the event that starts a fresh worker. It should resume
// at the acknowledged checkpoint on that wake, without another retry delay.
const checkpoint = { accounts: { fixture: { archivedUpdates: [['saved', 123]] } }, pending: true };
mem.status.chatgpt = {
  ...mem.status.chatgpt,
  running: true,
  manualContinuation: true,
  state: checkpoint,
};
mem.settings.autoPaused = true;
let resumed;
PROVIDERS.chatgpt.sync = async ({ state }) => {
  resumed = state;
  return { state: { accounts: state.accounts }, conversations: 0 };
};
await import('../background.js?worker-restarted');
alarmListener({ name: continuationAlarm });
await until(() => resumed && !mem.status.chatgpt.running);
assert.deepEqual(resumed, checkpoint);
assert.equal(mem.status.chatgpt.progress.phase, 'complete');

// Discovery can be interrupted before the new run creates a checkpoint.
// Account records marked done by the previous run cannot finish the new one.
mem.status.chatgpt.running = true;
mem.status.chatgpt.state = {
  accountRun: { entries: [['test-owner', { done: true, state: { marker: 42 } }]], next: 0 },
};
resumed = undefined;
PROVIDERS.chatgpt.principalIds = async () => ['test-owner'];
PROVIDERS.chatgpt.sync = async ({ state }) => {
  resumed = state;
  return { state, conversations: 0 };
};
await import('../background.js?worker-restarted-before-checkpoint');
alarmListener({ name: continuationAlarm });
await until(() => resumed && !mem.status.chatgpt.running);
assert.deepEqual(resumed, { marker: 42 });
