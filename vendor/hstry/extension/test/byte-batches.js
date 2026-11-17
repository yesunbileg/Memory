// Nonvisual transport integration, exercising the real background worker.
import assert from 'node:assert/strict';
import { PROVIDERS } from '../providers/index.js';
const LIMIT = 16 * 1024 * 1024;
let status = {},
  listener,
  alarmListener,
  alarmFinished;
let rejectBodies = false,
  ackOverride = null,
  failAtPost = null,
  nextWatermark = 999;
const posted = [];
const settings = {
  port: 41770,
  token: 'synthetic-private-ingest-token',
  account: 'synthetic-namespace',
  providers: { chatgpt: true },
  captureTypes: { recents: true },
};
globalThis.chrome = {
  storage: {
    local: {
      get: async (key) =>
        key === 'settings'
          ? { settings }
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
  alarms: {
    onAlarm: {
      addListener(fn) {
        alarmListener = fn;
      },
    },
    get: async () => null,
    create: async () => {},
  },
  action: {
    onClicked: { addListener() {} },
    setBadgeText: async ({ text }) => {
      if (text !== '…' && alarmFinished) {
        alarmFinished();
        alarmFinished = null;
      }
    },
    setBadgeBackgroundColor: async () => {},
  },
};
const raw = '漢🦦\n '.repeat(1_000_000); // UTF-8, not JS string length, determines wire size.
const conversation = (id, n = 1) => ({
  externalId: id,
  updatedAt: 1700000000000,
  metadata: { accountId: 'native-account' },
  messages: Array.from({ length: n }, (_, i) => ({
    role: i % 2 ? 'assistant' : 'user',
    content: raw,
    metadata: { id: `native-${i}` },
  })),
});
let packet = [conversation('one'), conversation('two')];
globalThis.fetch = async (url, options) => {
  if (new URL(url).pathname === '/auth/check')
    return new Response(
      JSON.stringify({
        status: 'ok',
        service: 'ai-memory',
        schema: 1,
        archive_instance_id: 'synthetic-archive',
      }),
    );
  const bytes = new TextEncoder().encode(options.body).length;
  posted.push({ bytes, packet: JSON.parse(options.body) });
  if (failAtPost === posted.length) return new Response('{}', { status: 503 });
  if (rejectBodies || bytes > LIMIT) return new Response('{}', { status: 413 });
  return new Response(
    JSON.stringify({
      conversations: ackOverride ?? JSON.parse(options.body).conversations.length,
      created: 1,
      updated: 0,
      unchanged: 0,
    }),
  );
};
PROVIDERS.chatgpt.sync = async ({ push }) => {
  await push('chatgpt-web', 'chatgpt-web', packet);
  return { state: { watermark: nextWatermark }, conversations: packet.length };
};
await import('../background.js?byte-batches');
async function run() {
  await new Promise((resolve) =>
    listener({ type: 'syncProvider', provider: 'chatgpt' }, {}, resolve),
  );
  for (let i = 0; i < 100 && status.chatgpt?.running; i++)
    await new Promise((r) => setTimeout(r, 2));
}
await run();
assert.equal(
  status.chatgpt.lastErrorCode,
  null,
  'count-only batching must not hit the 16 MiB receiver limit',
);
assert.ok(posted.length > 1);
assert.ok(posted.every((p) => p.bytes <= LIMIT));
assert.deepEqual(
  posted.flatMap((p) => p.packet.conversations),
  packet,
);
posted.length = 0;
packet = [conversation('long', 4)];
await run();
assert.equal(status.chatgpt.lastErrorCode, null);
assert.ok(posted.length > 1);
assert.ok(posted.every((p) => p.bytes <= LIMIT));
assert.deepEqual(
  posted.flatMap((p) => p.packet.conversations.flatMap((c) => c.messages)),
  packet[0].messages,
);
assert.ok(
  posted.every(
    (p) =>
      p.packet.account === 'synthetic-namespace' &&
      p.packet.conversations.every(
        (c) => c.externalId === 'long' && c.metadata.accountId === 'native-account',
      ),
  ),
);
posted.length = 0;
packet = [
  {
    externalId: 'indivisible',
    messages: [{ role: 'user', metadata: { id: 'big' }, content: 'x'.repeat(LIMIT) }],
  },
];
await run();
assert.equal(status.chatgpt.lastErrorCode, 'body_too_large');
assert.equal(posted.length, 0, 'indivisible oversize is rejected locally, never retried');
assert.deepEqual(
  status.chatgpt.state,
  { watermark: 999 },
  'failed push does not advance acknowledged state',
);
posted.length = 0;
rejectBodies = true;
packet = [
  {
    externalId: 'fits-but-rejected',
    messages: [{ role: 'user', content: 'short text', metadata: { id: 'm' } }],
  },
];
await run();
assert.equal(posted.length, 1);
assert.equal(status.chatgpt.lastErrorCode, 'body_too_large');
await new Promise((resolve) => {
  alarmFinished = resolve;
  alarmListener({ name: 'hstry-sync' });
});
assert.equal(posted.length, 1, 'permanent 413 must not loop on automatic interval capture');
await run();
assert.equal(posted.length, 2, 'explicit manual retry remains possible');
posted.length = 0;
rejectBodies = false;
packet = [conversation('duplicate-native-ids', 4)];
packet[0].messages[2].metadata.id = packet[0].messages[0].metadata.id;
await run();
assert.equal(status.chatgpt.lastErrorCode, 'body_too_large');
assert.equal(posted.length, 0, 'duplicate native IDs across chunks must fail before any write');
posted.length = 0;
packet = [
  {
    externalId: 'unacknowledged',
    messages: [{ role: 'user', content: 'short', metadata: { id: 'm' } }],
  },
];
ackOverride = 0;
nextWatermark = 1000;
await run();
assert.equal(
  status.chatgpt.lastErrorCode,
  'archive_unavailable',
  'partial/zero HTTP success acknowledgement is not full success',
);
assert.deepEqual(status.chatgpt.state, { watermark: 999 });
ackOverride = null;
posted.length = 0;
packet = [conversation('partial-chunks', 3)];
failAtPost = 2;
await run();
assert.equal(posted.length, 2);
assert.equal(status.chatgpt.lastErrorCode, 'archive_unavailable');
assert.deepEqual(
  status.chatgpt.state,
  { watermark: 999 },
  'saved first chunk cannot advance a failed snapshot watermark',
);
failAtPost = null;
posted.length = 0;
await run();
assert.equal(status.chatgpt.lastErrorCode, null);
assert.equal(posted.length, 3);
assert.deepEqual(status.chatgpt.state, { watermark: 1000 });
console.log(
  'PASS UTF-8 byte batching, exact native message chunks, bounded permanent oversized failure',
);
