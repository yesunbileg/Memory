import test from 'node:test';
import assert from 'node:assert/strict';
import { syncTabScopes } from '../providers/tab-scopes.js';

test('one provider follows each verified tab account across continuation and tab changes', async () => {
  const seen = [];
  const p = {
    principalIds: async (ctx) => ctx.owners,
    async sync({ tabContext, selectedScope, state, report }) {
      const [entry] = selectedScope.entries;
      assert.equal(selectedScope.entries.length, 1);
      assert.ok(tabContext.owners.includes(entry.principalId));
      assert.ok(!state.owner || state.owner === entry.principalId);
      seen.push([tabContext.tabId, entry.principalId]);
      await report({ phase: 'importing', detected: 1, processed: 1 });
      return { conversations: 1, state: { owner: entry.principalId }, hasMore: false };
    },
  };
  const selectedScope = {
    entries: ['first', 'second', 'third', 'closed'].map((principalId) => ({
      principalId,
      projectIds: [],
      conversationIds: ['same-id'],
    })),
  };
  const contexts = [
    { tabId: 1, owners: ['first'] },
    { tabId: 8, owners: ['first'] },
    { tabId: 2, owners: ['second'] },
    { tabId: 3, owners: ['third'] },
  ];
  const updates = [];
  const run = (state, continuing) =>
    syncTabScopes(p, contexts, {
      state,
      continuing,
      selectedScope,
      report: async (progress) => updates.push(progress),
      guard: async () => {},
      checkpoint: async () => {},
    });
  const first = await run({}, false);
  assert.equal(first.hasMore, true);
  assert.equal(updates.at(-1).accountsChecked, 1);
  contexts[2] = { tabId: 7, owners: ['second'] };
  const second = await run(first.state, true);
  assert.equal(second.hasMore, true);
  assert.equal(updates.at(-1).accountsChecked, 2);
  const third = await run(second.state, true);
  assert.equal(third.hasMore, false);
  assert.deepEqual(seen, [
    [1, 'first'],
    [7, 'second'],
    [3, 'third'],
  ]);
  assert.equal(updates.at(-1).accountsUnavailable, 1);
  assert.equal(updates.at(-1).processed, 3);
  assert.match(third.selectionWarning, /1 account/);
  assert.doesNotMatch(JSON.stringify(third.state), /tabId|owners/);
});
