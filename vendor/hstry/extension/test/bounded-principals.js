import assert from 'node:assert/strict';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';
const policy = { projects: false, pinned: false, agents: false, recents: true };
for (const [name, sync] of [
  ['chatgpt', syncChatGPT],
  ['claude', syncClaude],
]) {
  let principal = 'principal-a',
    lists = 0;
  const details = [],
    saved = [];
  globalThis.fetch = async (input) => {
    const p = new URL(input).pathname;
    if (p === '/api/auth/session') return Response.json({ accessToken: 'synthetic' });
    if (p.includes('/accounts/check/'))
      return Response.json({ accounts: { a: { account: { account_id: principal } } } });
    if (p === '/api/organizations') return Response.json([{ uuid: principal }]);
    const items = Array.from({ length: 12 }, (_, i) => ({ id: 'c-' + i, uuid: 'c-' + i }));
    if (p === '/backend-api/conversations' || p.endsWith('/chat_conversations')) {
      lists++;
      return Response.json(name === 'chatgpt' ? { items, total: 12 } : items);
    }
    const id = p.split('/').at(-1);
    details.push([principal, id]);
    return Response.json(
      name === 'chatgpt'
        ? {
            id,
            current_node: 'm',
            mapping: {
              m: {
                id: 'm',
                message: {
                  id: 'm',
                  author: { role: 'user' },
                  content: { content_type: 'text', parts: ['fixture'] },
                },
              },
            },
          }
        : { uuid: id, chat_messages: [{ uuid: 'm', sender: 'human', text: 'fixture' }] },
    );
  };
  const run = (state) =>
    sync({
      state,
      captureTypes: policy,
      checkpoint: async (s) => saved.push(structuredClone(s)),
      push: async (_s, _a, b) => b.length,
    });
  const first = await run({});
  assert.equal(first.hasMore, true);
  assert.equal(details.length, 10);
  assert.deepEqual(
    saved.at(-1),
    first.state,
    'acknowledged chunk persisted before the worker exits',
  );
  const second = await run(saved.at(-1));
  assert.equal(second.hasMore, false);
  assert.equal(lists, 1);
  assert.deepEqual(
    details.map((d) => d[1]),
    Array.from({ length: 12 }, (_, i) => 'c-' + i),
  );
  principal = 'principal-b';
  await run(first.state);
  assert.equal(lists, 2);
  assert.equal(details[12][0], principal);
  assert.equal(details[12][1], 'c-0');
}
