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

for (const [provider, sync] of [
  ['ChatGPT', syncChatGPT],
  ['Claude', syncClaude],
]) {
  const gpt = provider === 'ChatGPT';
  const workspace = 'fixture-workspace';
  const principalId = JSON.stringify(['fixture-user', workspace]);
  const pages = [],
    accepted = [];
  globalThis.fetch = async (url) => {
    const { pathname, searchParams } = new URL(url);
    if (pathname === '/api/auth/session')
      return Response.json({
        user: { id: 'fixture-user' },
        account: { id: workspace },
        accessToken: 'private fixture token',
      });
    if (pathname.includes('/accounts/check/'))
      return Response.json({ accounts: { one: { account: { account_id: workspace } } } });
    if (pathname === '/api/account')
      return Response.json({
        uuid: 'fixture-user',
        memberships: [{ organization: { uuid: workspace } }],
      });
    if (pathname === '/api/organizations') return Response.json([{ uuid: workspace }]);
    if (pathname.endsWith('/sidebar') || pathname.endsWith('/projects')) {
      pages.push('projects');
      return Response.json(gpt ? { items: [] } : []);
    }
    if (pathname.endsWith('/conversations') || pathname.endsWith('/chat_conversations_v2')) {
      const offset = Number(searchParams.get('offset'));
      pages.push(offset);
      const rows = Array.from({ length: Math.min(50, 251 - offset) }, (_, i) => {
        const id = 'fixture-' + (offset + i);
        return gpt
          ? { id, title: 'private listing title' }
          : { uuid: id, name: 'private listing title' };
      });
      return Response.json(gpt ? { items: rows, total: 251 } : rows);
    }
    assert.match(pathname, /\/fixture-250$/);
    const message = {
      id: 'm',
      author: { role: 'user' },
      content: { content_type: 'text', parts: ['fixture'] },
    };
    return Response.json(
      gpt
        ? { id: 'fixture-250', current_node: 'm', mapping: { m: { id: 'm', message } } }
        : { uuid: 'fixture-250', chat_messages: [{ uuid: 'm', sender: 'human', text: 'fixture' }] },
    );
  };
  const runSelected = (state) =>
    sync({
      state,
      selectedScope: {
        entries: [{ principalId, projectIds: [], conversationIds: ['fixture-250'] }],
      },
      checkpoint: async (saved) => {
        assert.doesNotMatch(JSON.stringify(saved), /private listing title|private fixture token/);
      },
      push: async (_source, _adapter, batch) => {
        accepted.push(...batch.map((chat) => chat.externalId));
        return batch.length;
      },
    });
  const first = await runSelected({});
  assert.equal(first.hasMore, true, provider + ' must yield during selected discovery');
  assert.deepEqual(pages, ['projects', 0, 50, 100]);
  assert.deepEqual(accepted, []);
  const second = await runSelected(first.state);
  assert.equal(second.hasMore, false);
  assert.deepEqual(pages, ['projects', 0, 50, 100, 150, 200, 250]);
  assert.deepEqual(accepted, ['fixture-250']);
  assert.equal(second.state.discovery, undefined);
}

const { geminiPrincipalIds } = await import('../providers/gemini.js');
const selectedPages = [];
globalThis.fetch = async (url, init) => {
  const u = new URL(url);
  if (u.pathname === '/app')
    return new Response('<script>{"SNlM0e":"fixture-at","FdrFJe":"fixture-sid"}</script>');
  const id = u.searchParams.get('rpcids');
  const [raw, cursor = 0] = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
  let result;
  if (id === 'MaZiqc') {
    selectedPages.push(cursor);
    result = [
      null,
      cursor < 5 ? cursor + 1 : null,
      [['c_fixture-' + cursor, cursor === 5 ? 'Selected title' : 'private listing title']],
    ];
  } else {
    assert.equal(id, 'hNvQHb');
    assert.equal(raw, 'c_fixture-5');
    result = [[['turn', null, [['fixture']]]], null];
  }
  return new Response(")]}'\n" + JSON.stringify([['wrb.fr', id, JSON.stringify(result)]]));
};
const [selectedGeminiPrincipal] = await geminiPrincipalIds();
const runSelectedGemini = (state) =>
  syncGemini({
    state,
    selectedScope: {
      entries: [
        { principalId: selectedGeminiPrincipal, projectIds: [], conversationIds: ['fixture-5'] },
      ],
    },
    checkpoint: async (saved) =>
      assert.doesNotMatch(JSON.stringify(saved), /private listing title|fixture-at|fixture-sid/),
    push: async (_source, _adapter, batch) => {
      assert.deepEqual(
        batch.map((chat) => [chat.externalId, chat.title]),
        [['fixture-5', 'Selected title']],
      );
      return batch.length;
    },
  });
const firstSelectedGemini = await runSelectedGemini({});
assert.equal(firstSelectedGemini.hasMore, true, 'Gemini must yield during selected discovery');
assert.deepEqual(selectedPages, [0, 1, 2, 3]);
const secondSelectedGemini = await runSelectedGemini(firstSelectedGemini.state);
assert.equal(secondSelectedGemini.conversations, 1);
assert.equal(secondSelectedGemini.hasMore, false);
assert.deepEqual(selectedPages, [0, 1, 2, 3, 4, 5]);
