// Nonvisual worker integration: destination and namespace changes replay history.
import assert from 'node:assert/strict';
import { PROVIDERS } from '../providers/index.js';
let settings = {
  port: 41770,
  token: 'synthetic-private-ingest-token',
  account: 'one',
  providers: { chatgpt: true },
  captureTypes: { recents: true },
};
let status = {},
  listener,
  archive = 'logical-archive-one';
const states = [];
globalThis.chrome = {
  storage: {
    local: {
      get: async (key) =>
        key === 'settings'
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
globalThis.fetch = async () =>
  new Response(
    JSON.stringify({
      status: 'ok',
      service: 'ai-memory',
      schema: 1,
      archive_id: 'same-location',
      ...(archive ? { archive_instance_id: archive } : {}),
    }),
  );
PROVIDERS.chatgpt.sync = async ({ state }) => {
  states.push(structuredClone(state));
  return { state: { watermark: 123 }, conversations: 0 };
};
await import('../background.js?checkpoint-scope');
async function run() {
  await new Promise((resolve) =>
    listener({ type: 'syncProvider', provider: 'chatgpt' }, {}, resolve),
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
archive = 'logical-archive-two';
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
await run();
await run();
assert.equal(
  states.length,
  5,
  'unverifiable legacy receiver must not read providers or loop continuations',
);
assert.equal(status.chatgpt.lastErrorCode, 'incompatible_server');
assert.equal(status.chatgpt.continuationPending, false);
console.log(
  'PASS authenticated destination/account checkpoint scope and conservative legacy replay',
);
