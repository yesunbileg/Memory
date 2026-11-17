// F03 regressions exercise the real provider parser with synthetic RPC responses.
import assert from 'node:assert/strict';
import test from 'node:test';
import { syncGemini } from '../providers/gemini.js';

const policy = { projects: false, pinned: false, agents: false, recents: true };
const watermark = 1_700_000_000_000;
let acknowledgedState;
const summary = (id = 'one') => [`c_${id}`, `Synthetic ${id}`, null, null, null, [1800000000, 0]];
const turn = (id, text = `  question ${id}\n`) => [
  id,
  null,
  [[text]],
  [[[null, [`answer ${id}`]]]],
];
const rpc = (id, value) =>
  `)]}'\n${JSON.stringify([['wrb.fr', id, JSON.stringify(value), null]])}\n`;

async function capture({
  list = () => [null, null, [summary()]],
  detail = () => [[turn('t')], null],
  state = acknowledgedState,
  acknowledge,
} = {}) {
  const pushed = [];
  const reports = [];
  const calls = { list: 0, detail: 0 };
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(input);
    if (url.pathname === '/app')
      return new Response('<script>{"SNlM0e":"synthetic","FdrFJe":"synthetic-session"}</script>');
    const id = url.searchParams.get('rpcids');
    const arg = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
    const value = id === 'MaZiqc' ? list(calls.list++, arg) : detail(calls.detail++, arg);
    return new Response(rpc(id, value));
  };
  const result = await syncGemini({
    state,
    captureTypes: policy,
    report: async (value) => reports.push(structuredClone(value)),
    push: async (_source, _adapter, batch) => {
      pushed.push(...batch);
      return acknowledge ?? batch.length;
    },
  });
  return { result, pushed, reports, calls };
}

// Completeness tests start with a checkpoint acknowledged in the same session.
// Legacy/unbound checkpoints are exercised separately in gemini-session-scope.js.
acknowledgedState = {
  ...(await capture({ state: {}, list: () => [null, null, []] })).result.state,
  lastSyncMs: watermark,
};

function incomplete(captured) {
  assert.equal(
    captured.result.state.lastSyncMs,
    watermark,
    'incomplete work must retain retry watermark',
  );
  assert.match(captured.result.selectionWarning ?? '', /incomplete|unavailable/i);
}

test('valid explicit empty Gemini history completes without warning', async () => {
  const captured = await capture({ list: () => [null, null, []] });
  assert.equal(captured.result.conversations, 0);
  assert.equal(captured.result.selectionWarning, null);
  assert.ok(captured.result.state.lastSyncMs > watermark);
});

test('unknown Gemini discovery shape is incomplete, not an empty archive', async () => {
  const captured = await capture({ list: () => ({ unexpected: [] }) });
  incomplete(captured);
  assert.equal(captured.pushed.length, 0);
});

test('malformed discovery rows cannot silently disappear behind a successful watermark', async () => {
  incomplete(await capture({ list: () => [null, null, [{}]] }));
});

test('unknown Gemini detail shape does not complete the conversation', async () => {
  incomplete(await capture({ detail: () => ({ unexpected: [] }) }));
});

test('unknown turn schema does not masquerade as a valid empty conversation', async () => {
  incomplete(await capture({ detail: () => [[{ unexpected: 'turn' }], null] }));
});

test('cyclic discovery cursors preserve discovered chats and a retry path', async () => {
  const captured = await capture({
    list: (page) => [null, page % 2 ? 'b' : 'a', [summary(String(page))]],
  });
  incomplete(captured);
  assert.equal(captured.calls.list, 3);
  assert.equal(captured.pushed.length, 3);
});

test('discovery safety ceiling retains captured data without declaring success', async () => {
  let state = acknowledgedState;
  let captured;
  let pushed = 0;
  do {
    captured = await capture({
      state,
      list: (page) => [null, `next-${page}`, [summary(String(page))]],
    });
    state = captured.result.state;
    pushed += captured.pushed.length;
  } while (captured.result.hasMore);
  incomplete(captured);
  assert.equal(pushed, 200);
});

test('detail cursor cycles do not duplicate native message identities', async () => {
  const captured = await capture({
    detail: (page) => [[turn(`turn-${page % 2}`)], page % 2 ? 'b' : 'a'],
  });
  incomplete(captured);
  assert.equal(captured.calls.detail, 3);
  assert.deepEqual(captured.pushed, [], 'looping details cannot establish complete archive order');
});

test('identical no-ID pages cannot multiply synthetic message IDs', async () => {
  const captured = await capture({
    detail: (page) => [[turn(null, '  exact visible text\n')], `next-${page}`],
  });
  incomplete(captured);
  assert.equal(captured.calls.detail, 2);
  assert.equal(captured.pushed.length, 0, 'incomplete positional identities are deferred');
});

test('detail safety ceiling defers incomplete turns and retains completion watermark', async () => {
  const captured = await capture({ detail: (page) => [[turn(`t-${page}`)], `next-${page}`] });
  incomplete(captured);
  assert.equal(captured.calls.detail, 200);
  assert.deepEqual(captured.pushed, []);
});

test('overlapping terminal detail pages deduplicate native turns and complete', async () => {
  const captured = await capture({
    detail: (page) =>
      page === 0
        ? [[turn('new'), turn('overlap')], 'older']
        : [[turn('overlap'), turn('old')], null],
  });
  assert.equal(captured.result.selectionWarning, null);
  assert.ok(captured.result.state.lastSyncMs > watermark);
  assert.deepEqual(
    captured.pushed[0].messages
      .filter((message) => message.role === 'user')
      .map((message) => message.metadata.id),
    ['old:user', 'overlap:user', 'new:user'],
  );
});

test('partial receiver acknowledgement does not advance completion watermark', async () => {
  incomplete(await capture({ acknowledge: 0 }));
});

test('a retry after incomplete detail can finish with the same native identities', async () => {
  const first = await capture({ detail: () => [[turn('stable')], 'same'] });
  incomplete(first);
  const second = await capture({
    state: first.result.state,
    detail: () => [[turn('stable')], null],
  });
  assert.ok(second.result.state.lastSyncMs > watermark);
  assert.equal(second.result.selectionWarning, null);
  assert.deepEqual(first.pushed, []);
  assert.deepEqual(
    second.pushed[0].messages.map(({ metadata, content }) => [metadata.id, content]),
    [
      ['stable:user', '  question stable\n'],
      ['stable:assistant', 'answer stable'],
    ],
  );
});

test('valid explicit empty detail completes without warning', async () => {
  const captured = await capture({ detail: () => [[], null] });
  assert.equal(captured.pushed.length, 0);
  assert.equal(captured.result.selectionWarning, null);
  assert.ok(captured.result.state.lastSyncMs > watermark);
});

test('incomplete no-ID turns are deferred until complete positional identities are known', async () => {
  const first = await capture({ detail: () => [[turn(null, '  newest\n')], 'same'] });
  incomplete(first);
  assert.equal(first.pushed.length, 0);
  const retry = await capture({
    state: first.result.state,
    detail: (page) =>
      page === 0 ? [[turn(null, '  newest\n')], 'older'] : [[turn(null, ' oldest\n')], null],
  });
  assert.equal(retry.result.selectionWarning, null);
  assert.deepEqual(
    retry.pushed[0].messages
      .filter((message) => message.role === 'user')
      .map(({ content, metadata }) => [content, metadata.id]),
    [
      [' oldest\n', 'record:1:user'],
      ['  newest\n', 'record:2:user'],
    ],
  );
});

test('later malformed detail pages defer the incomplete conversation and keep retry watermark', async () => {
  const captured = await capture({
    detail: (page) =>
      page === 0 ? [[turn('native', '  retained exactly\n')], 'older'] : { unavailable: true },
  });
  incomplete(captured);
  assert.deepEqual(captured.pushed, []);
});

test('recognized user text cannot hide an unsupported assistant field', async () => {
  const captured = await capture({
    detail: () => [[['native', null, [['visible user']], { changed: 'assistant schema' }]], null],
  });
  incomplete(captured);
});

test('missing detail continuation field is not an explicit terminal page', async () => {
  incomplete(await capture({ detail: () => [[]] }));
});

test('incomplete newest detail is deferred so a complete retry establishes oldest-first archive order', async () => {
  const first = await capture({
    detail: (page) =>
      page === 0 ? [[turn('new', ' newest exact text\n')], 'older'] : { unavailable: true },
  });
  incomplete(first);
  assert.deepEqual(
    first.pushed,
    [],
    'a partial newest page must not assign permanent archive sequence',
  );
  const retry = await capture({
    state: first.result.state,
    detail: (page) =>
      page === 0
        ? [[turn('new', ' newest exact text\n')], 'older']
        : [[turn('old', ' oldest exact text\n')], null],
  });
  assert.equal(retry.result.selectionWarning, null);
  assert.deepEqual(
    retry.pushed[0].messages
      .filter((message) => message.role === 'user')
      .map(({ metadata, content }) => [metadata.id, content]),
    [
      ['old:user', ' oldest exact text\n'],
      ['new:user', ' newest exact text\n'],
    ],
  );
});

test('incomplete discovery still archives completed chats while deferring incomplete details', async () => {
  const captured = await capture({
    list: () => [null, 'repeated', [summary('complete'), summary('incomplete')]],
    detail: (_page, [id, , cursor]) =>
      id === 'c_complete'
        ? [[turn('complete-native', ' complete exact text\n')], null]
        : cursor === null
          ? [[turn('newest-native', ' deferred exact text\n')], 'older']
          : { unavailable: true },
  });
  incomplete(captured);
  assert.equal(captured.result.hasMore, false);
  assert.equal(captured.pushed.length, 1);
  assert.equal(captured.pushed[0].externalId, 'complete');
  assert.equal(captured.pushed[0].messages[0].content, ' complete exact text\n');
  assert.equal(captured.pushed[0].messages[0].metadata.id, 'complete-native:user');
});
