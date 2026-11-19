import assert from 'node:assert/strict';
import test from 'node:test';
import { geminiPrincipalIds, syncGemini } from '../providers/gemini.js';

function fixture(t) {
  const ids = Array.from({ length: 14 }, (_, i) => String(i));
  const f = { listed: ids, listCalls: 0, reads: [], sent: [], detailFailures: new Set() };
  t.mock.method(globalThis, 'fetch', async (input, init = {}) => {
    const u = new URL(input);
    if (u.pathname === '/app')
      return new Response(
        '<script>window.WIZ_global_data={"SNlM0e":"fixture-at","FdrFJe":"fixture-sid"};</script>',
      );
    const rpc = u.searchParams.get('rpcids');
    let data;
    if (rpc === 'MaZiqc') {
      f.listCalls++;
      data =
        f.listed === null ? null : [null, null, f.listed.map((id) => ['c_' + id, 'Chat ' + id])];
    } else {
      assert.equal(rpc, 'hNvQHb');
      const [raw] = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
      const id = raw.slice(2);
      f.reads.push(id);
      data = f.detailFailures.has(id) ? null : [[['turn-' + id, null, [['Visible']]]], null];
    }
    return new Response(")]}'\n" + JSON.stringify([['wrb.fr', rpc, JSON.stringify(data)]]));
  });
  f.run = async (state = {}) => {
    const [principalId] = await geminiPrincipalIds();
    return syncGemini({
      state,
      selectedScope: { entries: [{ principalId, projectIds: [], conversationIds: ids }] },
      push: async (_source, _adapter, batch) => {
        f.sent.push(...batch.map((c) => c.externalId));
        return batch.length;
      },
    });
  };
  return f;
}

test('explicit Gemini continuation verifies only remaining and retry IDs against the current catalog', async (t) => {
  const f = fixture(t);
  f.detailFailures.add('1');
  const first = await f.run();
  assert.equal(first.hasMore, true);
  assert.deepEqual(f.reads, ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']);
  f.reads.length = f.sent.length = 0;
  // Remove one already visited ID and one still pending. The live list is in a
  // different order. The detail endpoint would still return both removed IDs.
  f.detailFailures.clear();
  f.listed = ['outsider', '13', '12', '10', '9', '8', '7', '6', '5', '4', '3', '2', '0'];
  const next = await f.run(first.state);
  assert.deepEqual(f.reads, ['10', '12', '13']);
  assert.deepEqual(f.sent, ['10', '12', '13']);
  assert.equal(f.listCalls, 2);
  assert.equal(next.hasMore, false);
  assert.equal(next.state.pending, undefined, 'the revoked retry is removed too');
  assert.match(next.selectionWarning, /unavailable/i);
  assert.equal(next.state.lastSyncMs, first.state.lastSyncMs);
});

test('an unreadable Gemini continuation listing cannot authorize detail requests from cached rows', async (t) => {
  const f = fixture(t);
  const first = await f.run();
  assert.equal(first.hasMore, true);
  f.reads.length = f.sent.length = 0;
  f.listed = null;
  const next = await f.run(first.state);
  assert.deepEqual(f.reads, []);
  assert.deepEqual(f.sent, []);
  assert.match(next.selectionWarning, /incomplete/i);
  assert.equal(next.state.lastSyncMs, first.state.lastSyncMs);
});
