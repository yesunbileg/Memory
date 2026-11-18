import assert from 'node:assert/strict';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';
const captureTypes = { projects: false, pinned: true, agents: false, recents: false };
for (const [name, sync] of [
  ['chatgpt', syncChatGPT],
  ['claude', syncClaude],
]) {
  let offsets = [];
  const checkpoints = [];
  globalThis.fetch = async (url) => {
    const u = new URL(url),
      p = u.pathname;
    if (p.endsWith('/auth/session')) return Response.json({ accessToken: 'synthetic-secret' });
    if (p.includes('/accounts/check'))
      return Response.json({ accounts: { a: { account: { account_id: 'principal' } } } });
    if (p.endsWith('/organizations')) return Response.json([{ uuid: 'principal' }]);
    const offset = Number(u.searchParams.get('offset'));
    offsets.push(offset);
    const items = Array.from({ length: Math.max(0, Math.min(50, 252 - offset)) }, (_, i) => ({
      id: 'i' + (offset + i),
      uuid: 'i' + (offset + i),
      is_pinned: false,
      private_preview: 'unselected content',
    }));
    return Response.json(name === 'chatgpt' ? { items, total: 252 } : items);
  };
  const run = (state) =>
    sync({
      state,
      captureTypes,
      push: async () => {
        throw Error('nothing selected');
      },
      checkpoint: async (s) => checkpoints.push(structuredClone(s)),
    });
  const first = await run({});
  assert.equal(first.hasMore, true, name + ' yields during discovery');
  assert.ok(offsets.length <= 4);
  assert.ok(!JSON.stringify(checkpoints).includes('unselected content'));
  assert.ok(!JSON.stringify(checkpoints).includes('synthetic-secret'));
  const next = await run(checkpoints.at(-1));
  assert.equal(next.hasMore, false);
  assert.deepEqual(offsets, [0, 50, 100, 150, 200, 250], 'no provider listing request replayed');
}
const { syncGemini } = await import('../providers/gemini.js');
const seen = [],
  saved = [];
globalThis.fetch = async (url, init) => {
  const u = new URL(url);
  if (u.pathname === '/app')
    return new Response(
      '<script>{"SNlM0e":"synthetic-csrf","FdrFJe":"session","cfb2h":"b"}</script>',
    );
  const id = u.searchParams.get('rpcids');
  assert.equal(id, 'MaZiqc');
  const arg = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
  const cursor = arg[1] ?? 0;
  seen.push(cursor);
  return new Response(
    ")]}'\n" +
      JSON.stringify([['wrb.fr', id, JSON.stringify([null, cursor < 5 ? cursor + 1 : null, []])]]) +
      '\n',
  );
};
const runGemini = (state) =>
  syncGemini({
    state,
    captureTypes: { projects: false, pinned: false, agents: false, recents: true },
    push: async () => 0,
    checkpoint: async (s) => saved.push(structuredClone(s)),
  });
const gemFirst = await runGemini({});
assert.equal(gemFirst.hasMore, true);
assert.deepEqual(seen, [0, 1, 2, 3]);
const gemNext = await runGemini(saved.at(-1));
assert.equal(gemNext.hasMore, false);
assert.deepEqual(seen, [0, 1, 2, 3, 4, 5]);
