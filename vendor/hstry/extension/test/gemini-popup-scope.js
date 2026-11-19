import assert from 'node:assert/strict';
import test from 'node:test';
import { geminiPrincipalIds, listGeminiCatalog, syncGemini } from '../providers/gemini.js';

const rpc = (id, value) =>
  new Response(")]}'\n" + JSON.stringify([['wrb.fr', id, JSON.stringify(value)]]));
const row = (id) => ['c_' + id, 'Title ' + id, { ignored: 'private tuple field' }];
function fixture(list) {
  const calls = { list: [], detail: [], pushed: [], session: 0 };
  const f = { at: 'fixture-at', sid: 'fixture-sid', switchOnSession: Infinity };
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url);
    if (u.pathname === '/app') {
      ++calls.session;
      return new Response(
        '<script>window.WIZ_global_data=' +
          JSON.stringify({
            ...f.metadata,
            SNlM0e: f.at,
            FdrFJe: calls.session >= f.switchOnSession ? 'switched' : f.sid,
          }) +
          ';</script>',
      );
    }
    const id = u.searchParams.get('rpcids');
    const args = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
    if (id === 'MaZiqc') {
      calls.list.push(args);
      return rpc(id, list(...args));
    }
    if (id === 'hNvQHb') {
      calls.detail.push(args[0]);
      return rpc(id, [[['T', null, [['private transcript']], null]], null]);
    }
    throw Error('unexpected request');
  };
  const run = (selectedScope, state) =>
    syncGemini({
      state,
      selectedScope,
      captureTypes: { projects: false, pinned: false, agents: false, recents: false },
      push: async (_src, _provider, rows) => {
        calls.pushed.push(...rows);
        return rows.length;
      },
    });
  return { f, calls, run };
}
const saved = (principalId, ids) => ({
  entries: [{ principalId, projectIds: [], conversationIds: ids }],
});

test('popup catalog follows metadata cursors, exposes no private fields or imaginary projects', async () => {
  const { calls } = fixture((_size, cursor) => [
    null,
    cursor == null ? 'page2' : null,
    [row(cursor ? 'two' : 'one')],
  ]);
  const [id] = await geminiPrincipalIds();
  assert.deepEqual(calls.list, []);
  const cat = await listGeminiCatalog();
  assert.equal(cat.incomplete, false);
  assert.equal(cat.entries.length, 1);
  assert.equal(cat.entries[0].principalId, id);
  assert.deepEqual(cat.entries[0].projects, []);
  assert.deepEqual(cat.entries[0].conversations, [
    { id: 'one', title: 'Title one' },
    { id: 'two', title: 'Title two' },
  ]);
  assert.deepEqual(calls.detail, []);
  assert.doesNotMatch(JSON.stringify(cat), /fixture-at|fixture-sid|private/);
});

test('catalog grows saturated no-cursor window and marks a failed expansion partial', async () => {
  const { calls } = fixture((size) => {
    if (size > 200) throw Error('unavailable');
    return [null, null, Array.from({ length: size }, (_, i) => row(String(i)))];
  });
  const cat = await listGeminiCatalog();
  assert.equal(cat.incomplete, true);
  assert.equal(cat.entries[0].conversations.length, 200);
  assert.deepEqual(calls.list, [[200], [1000]]);
  assert.deepEqual(calls.detail, []);
});

test('only verified selected Gemini chats are read even when categories are disabled', async () => {
  const { calls, f, run } = fixture(() => [null, null, [row('selected'), row('ignored')]]);
  const [id] = await geminiPrincipalIds();
  await run(saved(id, ['selected']));
  assert.deepEqual(calls.detail, ['c_selected']);
  assert.deepEqual(
    calls.pushed.map((row) => row.externalId),
    ['selected'],
  );
  f.sid = 'other-google-session';
  await run(saved(id, ['selected']));
  f.sid = '';
  assert.deepEqual(await geminiPrincipalIds(), []);
  await run(saved(id, ['selected']));
  await run(saved(id, []));
  assert.deepEqual(calls.detail, ['c_selected']);
});

test('session rotation during metadata discovery cannot release a selected transcript', async () => {
  const { calls, f, run } = fixture(() => [null, null, [row('selected')]]);
  const [id] = await geminiPrincipalIds();
  f.switchOnSession = calls.session + 2;
  await run(saved(id, ['selected'])).catch(() => {});
  assert.deepEqual(calls.detail, []);
  assert.deepEqual(calls.pushed, []);
});

test('scope change invalidates pending selected chats', async () => {
  const ids = Array.from({ length: 12 }, (_, i) => String(i));
  const { calls, run } = fixture(() => [null, null, ids.map(row)]);
  const [principal] = await geminiPrincipalIds();
  const first = await run(saved(principal, ids));
  assert.equal(first.hasMore, true);
  calls.detail.length = 0;
  await run(saved(principal, ['0']), first.state);
  assert.deepEqual(calls.detail, ['c_0']);
});

test('corroborated active Google account survives token rotation but never an account switch', async () => {
  const { f, calls, run } = fixture(() => [null, null, [row('one')]]);
  const account = '100000000000000000001';
  f.metadata = { S06Grb: account, W3Yyqf: account, oPEP7c: 'person@example.test' };
  const [id] = await geminiPrincipalIds();
  assert.match(id, /^gemini-account-[a-f0-9]{64}$/);
  f.sid += '-rotated';
  f.at += '-rotated';
  assert.deepEqual(await geminiPrincipalIds(), [id]);
  await run(saved(id, ['one']));
  assert.deepEqual(calls.detail, ['c_one']);
  f.metadata.S06Grb = f.metadata.W3Yyqf = '100000000000000000002';
  await run(saved(id, ['one']));
  assert.deepEqual(calls.detail, ['c_one']);
  const cat = await listGeminiCatalog();
  assert.doesNotMatch(JSON.stringify(cat), /10000000000000000000|person@example/);
});
