// A06: account slot alone never identifies a Google principal. Synthetic session only.
import assert from 'node:assert/strict';
import test from 'node:test';
import { syncGemini } from '../providers/gemini.js';

const policy = { projects: false, pinned: false, agents: false, recents: true };
const rpc = (id, value) =>
  `)]}'\n${JSON.stringify([['wrb.fr', id, JSON.stringify(value), null]])}\n`;
const summary = (id) => [`c_${id}`, 'Synthetic', null, null, null, [1_600_000_000, 0]];
async function capture({
  state = {},
  at = 'csrf-one',
  sid = 'session-one',
  index = '0',
  ids = ['old'],
  captureTypes = policy,
} = {}) {
  const pushed = [];
  let listCalls = 0;
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(input);
    if (url.pathname === '/app') {
      const response = new Response(
        `<script>${JSON.stringify({ SNlM0e: at, FdrFJe: sid, cfb2h: 'build' })}</script>`,
      );
      Object.defineProperty(response, 'url', { value: `https://gemini.google.com/u/${index}/app` });
      return response;
    }
    const rpcId = url.searchParams.get('rpcids');
    if (rpcId === 'MaZiqc') {
      listCalls++;
      return new Response(rpc(rpcId, [null, null, ids.map(summary)]));
    }
    if (rpcId === 'hNvQHb') {
      const [id] = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
      return new Response(rpc(rpcId, [[[`${id}-turn`, null, [[`Text ${id}`]], null]], null]));
    }
    throw new Error('Unexpected RPC');
  };
  const result = await syncGemini({
    state,
    captureTypes,
    push: async (_s, _a, batch) => {
      pushed.push(...batch);
      return batch.length;
    },
  });
  return { result, pushed, listCalls };
}

test('legacy watermark without session evidence cannot skip old Gemini history', async () => {
  const { result, pushed } = await capture({ state: { lastSyncMs: 1_900_000_000_000 } });
  assert.equal(pushed.length, 1);
  assert.equal(result.selectionWarning, null);
});

for (const changed of [{ at: 'csrf-two' }, { sid: 'session-two' }, { index: '1' }]) {
  test(`changed Gemini session evidence ${Object.keys(changed)[0]} replays prior history`, async () => {
    const first = await capture();
    const second = await capture({ state: first.result.state, ...changed });
    assert.equal(second.pushed.length, 1);
    assert.equal(second.result.selectionWarning, null);
  });
  test(`changed Gemini ${Object.keys(changed)[0]} discards old pending summaries`, async () => {
    const first = await capture({ ids: Array.from({ length: 12 }, (_, i) => `a-${i}`) });
    const second = await capture({ state: first.result.state, ids: ['b-old'], ...changed });
    assert.equal(second.listCalls, 1);
    assert.deepEqual(
      second.pushed.map((c) => c.externalId),
      ['b-old'],
    );
    assert.equal(second.result.hasMore, false);
  });
}

test('matching session retains bounded continuation and never persists raw credentials', async () => {
  const ids = Array.from({ length: 12 }, (_, i) => `a-${i}`);
  const first = await capture({ ids });
  assert.equal(first.pushed.length, 10);
  assert.equal(first.result.hasMore, true);
  const second = await capture({ state: first.result.state, ids });
  assert.equal(second.pushed.length, 2);
  assert.equal(second.listCalls, 0);
  assert.equal(second.result.hasMore, false);
  const third = await capture({ state: second.result.state, ids });
  assert.equal(third.pushed.length, 0, 'unchanged session watermark avoids redundant details');
  for (const state of [first.result.state, second.result.state]) {
    assert.equal(JSON.stringify(state).includes('csrf-one'), false);
    assert.equal(JSON.stringify(state).includes('session-one'), false);
  }
});

test('missing session binding replays without scheduling an endless partial continuation', async () => {
  const old = await capture({ ids: Array.from({ length: 12 }, (_, i) => `a-${i}`) });
  const unknown = await capture({
    state: old.result.state,
    sid: '',
    ids: Array.from({ length: 12 }, (_, i) => `b-${i}`),
  });
  assert.deepEqual(
    unknown.pushed.map((c) => c.externalId),
    Array.from({ length: 10 }, (_, i) => `b-${i}`),
  );
  assert.equal(unknown.result.hasMore, false, 'unbound progress cannot safely resume on an alarm');
  assert.equal(unknown.result.state.pending, undefined);
  assert.equal(unknown.result.state.lastSyncMs, null);
  assert.match(unknown.result.selectionWarning ?? '', /session.*unavailable|session.*verified/i);
  assert.match(unknown.result.selectionWarning, /incomplete/i);
});

test('missing session evidence never reuses a completed watermark', async () => {
  const first = await capture();
  const second = await capture({ state: first.result.state, sid: '' });
  assert.equal(second.pushed.length, 1);
  assert.equal(second.result.state.lastSyncMs, null);
});

test('session safeguards never opt Gemini into Recents', async () => {
  const first = await capture({ ids: Array.from({ length: 12 }, (_, i) => `a-${i}`) });
  const second = await capture({
    state: first.result.state,
    sid: '',
    captureTypes: { ...policy, recents: false, projects: true },
  });
  assert.deepEqual(second.pushed, []);
  assert.equal(second.result.hasMore, false);
  assert.match(second.result.selectionWarning, /Ordinary Recents/);
});
