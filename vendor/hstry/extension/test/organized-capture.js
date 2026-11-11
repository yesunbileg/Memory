// Real provider modules with realistic, entirely synthetic private-API fixtures.
// Endpoint evidence and unsupported surfaces are recorded in docs/donor-audit.md.
import assert from 'node:assert/strict';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';
import { syncGemini } from '../providers/gemini.js';

const realTimeout = globalThis.setTimeout;
globalThis.setTimeout = (callback, delay, ...args) =>
  realTimeout(callback, delay === 400 ? 0 : delay, ...args);
const only = (key) => ({
  projects: false,
  pinned: false,
  agents: false,
  recents: false,
  [key]: true,
});
const raw = "  Монгол — 漢字 🦦\n```js\n  return '✓';\n```\n\t";
const old = '2024-01-01T00:00:00Z';
let failures = 0;
async function test(name, run) {
  try {
    await run();
    console.log(`PASS ${name}`);
  } catch (error) {
    failures++;
    console.error(`FAIL ${name}: ${error.stack}`);
  }
}

function chatgptFixture(options = {}) {
  const calls = [];
  const details = [];
  const main = options.main ?? [
    { id: 'ordinary', project_id: null, gizmo_id: null, is_starred: false, update_time: old },
    { id: 'pinned', project_id: null, gizmo_id: null, is_starred: true, update_time: old },
    { id: 'agent', project_id: null, gizmo_id: 'g-custom', is_starred: false, update_time: old },
    {
      id: 'overlap',
      project_id: 'g-project-one',
      gizmo_id: 'g-project-one',
      is_starred: false,
      update_time: old,
    },
    { id: 'unknown', update_time: old },
  ];
  globalThis.fetch = async (input) => {
    const url = new URL(input);
    calls.push(url);
    if (url.pathname === '/api/auth/session')
      return Response.json({ accessToken: 'synthetic-token' });
    if (url.pathname.includes('/accounts/check/'))
      return Response.json({
        accounts: { a: { account: { account_id: 'synthetic-account', structure: 'personal' } } },
      });
    if (url.pathname === '/backend-api/conversations') {
      const offset = options.ignoreOffsets ? 0 : Number(url.searchParams.get('offset') ?? 0);
      return Response.json({
        items: main.slice(offset, offset + 50),
        total: options.nullTotal ? null : main.length,
        offset,
        limit: 50,
      });
    }
    if (url.pathname === '/backend-api/gizmos/snorlax/sidebar') {
      if (options.sidebarFailure) return Response.json({ error: 'unsupported' }, { status: 404 });
      return Response.json(
        url.searchParams.get('cursor') === 'sidebar-page-two'
          ? {
              items: [
                {
                  gizmo: {
                    gizmo: { id: 'g-project-two', display: { name: 'Second synthetic project' } },
                  },
                },
              ],
              cursor: null,
            }
          : {
              items: [
                {
                  gizmo: { gizmo: { id: 'g-project-one', display: { name: 'Synthetic project' } } },
                },
              ],
              cursor: 'sidebar-page-two',
            },
      );
    }
    if (url.pathname === '/backend-api/gizmos/g-project-one/conversations') {
      if (options.badProjectShape) return Response.json({ unexpected: true });
      return Response.json(
        url.searchParams.get('cursor') === 'project-page-two'
          ? { items: [{ id: 'project-page-two', update_time: old }], cursor: null }
          : {
              items: [
                { id: 'project-only', update_time: old },
                { id: 'overlap', update_time: old },
              ],
              cursor: 'project-page-two',
            },
      );
    }
    if (url.pathname === '/backend-api/gizmos/g-project-two/conversations')
      return Response.json({ items: [], cursor: null });
    if (url.pathname.startsWith('/backend-api/conversation/')) {
      const id = decodeURIComponent(url.pathname.split('/').at(-1));
      details.push(id);
      return Response.json({
        title: `Synthetic ${id}`,
        current_node: 'a',
        mapping: {
          u: {
            id: 'u',
            parent: null,
            message: {
              id: `${id}:user`,
              author: { role: 'user' },
              content: { content_type: 'text', parts: [raw] },
            },
          },
          a: {
            id: 'a',
            parent: 'u',
            message: {
              id: `${id}:assistant`,
              author: { role: 'assistant' },
              content: { content_type: 'text', parts: ['  Answer.\n'] },
            },
          },
        },
      });
    }
    return Response.json({ error: 'unexpected fixture endpoint' }, { status: 404 });
  };
  return { calls, details };
}

async function capture(sync, captureTypes, state = {}) {
  const conversations = [];
  const reports = [];
  const result = await sync({
    state,
    captureTypes,
    log() {},
    report: async (value) => reports.push(structuredClone(value)),
    push: async (_source, _adapter, batch) => {
      conversations.push(...batch);
      return batch.length;
    },
  });
  return {
    result,
    conversations,
    reports,
    ids: conversations.map((value) => value.externalId).sort(),
  };
}

await test('ChatGPT defaults discover project-only chats, pins and GPTs but exclude ordinary/unknown Recents', async () => {
  const fixture = chatgptFixture();
  const captured = await capture(syncChatGPT);
  assert.deepEqual(captured.ids, [
    'agent',
    'overlap',
    'pinned',
    'project-only',
    'project-page-two',
  ]);
  assert.equal(new Set(captured.ids).size, captured.ids.length);
  assert.ok(!fixture.details.includes('ordinary'));
  assert.ok(!fixture.details.includes('unknown'));
  assert.equal(captured.result.selection.selected, 5);
  assert.equal(captured.result.selection.skipped, 2);
  assert.equal(captured.result.selection.unknown, 1);
  assert.match(captured.result.selectionWarning, /metadata/i);
  assert.ok(fixture.calls.some((url) => url.searchParams.get('cursor') === 'sidebar-page-two'));
  assert.ok(fixture.calls.some((url) => url.searchParams.get('cursor') === 'project-page-two'));
  for (const conversation of captured.conversations) {
    assert.equal(conversation.messages[0].content, raw);
    assert.equal(conversation.messages[0].metadata.id, `${conversation.externalId}:user`);
    assert.equal(conversation.metadata.accountId, 'synthetic-account');
  }
});

for (const [category, expected] of [
  ['projects', ['overlap', 'project-only', 'project-page-two']],
  ['pinned', ['pinned']],
  ['agents', ['agent']],
  ['recents', ['ordinary', 'unknown']],
])
  await test(`ChatGPT independently selects ${category}`, async () => {
    const fixture = chatgptFixture();
    const captured = await capture(syncChatGPT, only(category));
    assert.deepEqual(captured.ids, expected);
    if (category === 'recents')
      assert.ok(!fixture.calls.some((url) => url.pathname.includes('/gizmos/')));
  });

await test('ChatGPT unavailable project API never widens selection and retains its retry watermark', async () => {
  chatgptFixture({ sidebarFailure: true });
  const captured = await capture(syncChatGPT, only('projects'));
  assert.deepEqual(captured.ids, ['overlap']);
  assert.match(captured.result.selectionWarning, /project/i);
  assert.equal(captured.result.state.accounts.syntheti.lastSyncMs, null);
});

await test('ChatGPT malformed project pages warn without capturing unrelated Recents', async () => {
  chatgptFixture({ badProjectShape: true });
  const captured = await capture(syncChatGPT, only('projects'));
  assert.deepEqual(captured.ids, ['overlap']);
  assert.match(captured.result.selectionWarning, /project/i);
});

await test('ChatGPT full metadata scan finds old newly pinned chats and handles null totals', async () => {
  const main = Array.from({ length: 101 }, (_, index) => ({
    id: `old-${index}`,
    project_id: null,
    gizmo_id: null,
    is_starred: index === 100,
    update_time: old,
  }));
  const fixture = chatgptFixture({ main, nullTotal: true });
  const captured = await capture(syncChatGPT, only('pinned'), {
    accounts: { syntheti: { lastSyncMs: Date.now() } },
  });
  assert.deepEqual(captured.ids, ['old-100']);
  assert.deepEqual(
    fixture.calls
      .filter((url) => url.pathname === '/backend-api/conversations')
      .map((url) => url.searchParams.get('offset')),
    ['0', '50', '100'],
  );
});

await test('ChatGPT repeated offset pages stop promptly and warn instead of claiming completion', async () => {
  const main = Array.from({ length: 101 }, (_, index) => ({
    id: `old-${index}`,
    project_id: null,
    gizmo_id: null,
    is_starred: false,
  }));
  const fixture = chatgptFixture({ main, nullTotal: true, ignoreOffsets: true });
  const captured = await capture(syncChatGPT, only('pinned'));
  assert.deepEqual(captured.ids, []);
  assert.equal(
    fixture.calls.filter((url) => url.pathname === '/backend-api/conversations').length,
    2,
  );
  assert.match(captured.result.selectionWarning, /discovery.*incomplete/i);
});

function claudeFixture(options = {}) {
  const calls = [];
  const details = [];
  const main = [
    { uuid: 'ordinary', project_id: null, agent_id: null, is_starred: false, updated_at: old },
    { uuid: 'pinned', project_id: null, agent_id: null, is_starred: true, updated_at: old },
    {
      uuid: 'overlap',
      project_uuid: 'project-one',
      agent_id: null,
      is_starred: false,
      updated_at: old,
    },
    { uuid: 'unknown', updated_at: old },
  ];
  globalThis.fetch = async (input) => {
    const url = new URL(input);
    calls.push(url);
    if (url.pathname === '/api/organizations') return Response.json([{ uuid: 'synthetic-org' }]);
    if (url.pathname === '/api/organizations/synthetic-org/projects') {
      if (options.projectsFailure) return Response.json({ error: 'unsupported' }, { status: 404 });
      return Response.json([{ uuid: 'project-one', name: 'Synthetic project', archived_at: null }]);
    }
    if (url.pathname === '/api/organizations/synthetic-org/projects/project-one/conversations_v2') {
      const offset = options.ignoreOffsets ? 0 : Number(url.searchParams.get('offset') ?? 0);
      const projectItems = options.paginated
        ? Array.from({ length: 51 }, (_, index) => ({ uuid: `project-${index}`, updated_at: old }))
        : [
            { uuid: 'project-only', updated_at: old },
            { uuid: 'overlap', updated_at: old },
          ];
      return Response.json({ data: projectItems.slice(offset, offset + 50) });
    }
    if (url.pathname === '/api/organizations/synthetic-org/chat_conversations')
      return Response.json(main);
    if (url.pathname.startsWith('/api/organizations/synthetic-org/chat_conversations/')) {
      const id = decodeURIComponent(url.pathname.split('/').at(-1));
      details.push(id);
      return Response.json({
        uuid: id,
        name: `Synthetic ${id}`,
        chat_messages: [
          { uuid: `${id}:user`, sender: 'human', content: [{ type: 'text', text: raw }] },
          {
            uuid: `${id}:assistant`,
            sender: 'assistant',
            content: [{ type: 'text', text: '  Answer.\n' }],
          },
        ],
      });
    }
    return Response.json({ error: 'unexpected fixture endpoint' }, { status: 404 });
  };
  return { calls, details };
}

await test('Claude defaults discover project-only chats and explicit stars without ordinary Recents', async () => {
  const fixture = claudeFixture();
  const captured = await capture(syncClaude);
  assert.deepEqual(captured.ids, ['overlap', 'pinned', 'project-only']);
  assert.ok(!fixture.details.includes('ordinary'));
  assert.ok(!fixture.details.includes('unknown'));
  assert.equal(captured.result.selection.skipped, 2);
  assert.match(captured.result.selectionWarning, /metadata/i);
  assert.ok(
    captured.conversations.every(
      (value) => value.messages[0].content === raw && value.metadata.orgId === 'synthetic-org',
    ),
  );
});

for (const [category, expected] of [
  ['projects', ['overlap', 'project-only']],
  ['pinned', ['pinned']],
  ['recents', ['ordinary', 'unknown']],
])
  await test(`Claude independently selects ${category}`, async () => {
    claudeFixture();
    assert.deepEqual((await capture(syncClaude, only(category))).ids, expected);
  });

await test('Claude project conversations use limit/offset pagination and deduplicate overlap', async () => {
  const fixture = claudeFixture({ paginated: true });
  const captured = await capture(syncClaude, only('projects'));
  assert.equal(captured.ids.length, 52);
  assert.deepEqual(
    fixture.calls
      .filter((url) => url.pathname.endsWith('/conversations_v2'))
      .map((url) => url.searchParams.get('offset')),
    ['0', '50'],
  );
});

await test('Claude repeated project pages stop promptly without importing unrelated chats', async () => {
  const fixture = claudeFixture({ paginated: true, ignoreOffsets: true });
  const captured = await capture(syncClaude, only('projects'));
  assert.equal(captured.ids.length, 51);
  assert.ok(!captured.ids.includes('ordinary'));
  assert.equal(fixture.calls.filter((url) => url.pathname.endsWith('/conversations_v2')).length, 2);
  assert.match(captured.result.selectionWarning, /project.*incomplete/i);
});

await test('Claude unavailable projects fail closed while explicit project metadata still matches', async () => {
  claudeFixture({ projectsFailure: true });
  const captured = await capture(syncClaude, only('projects'));
  assert.deepEqual(captured.ids, ['overlap']);
  assert.match(captured.result.selectionWarning, /project/i);
});

await test('Gemini opaque metadata cannot authorize organized capture', async () => {
  const calls = [];
  const rpc = (id, data) =>
    `)]}'\n${JSON.stringify([['wrb.fr', id, JSON.stringify(data), null]])}\n`;
  globalThis.fetch = async (input) => {
    const url = new URL(input);
    calls.push(url);
    if (url.pathname === '/app')
      return new Response('<script>{"SNlM0e":"csrf","FdrFJe":"sid","cfb2h":"build"}</script>');
    const id = url.searchParams.get('rpcids');
    if (id === 'MaZiqc')
      return new Response(
        rpc(id, [
          null,
          null,
          [['c_unknown', 'Pinned project agent', true, true, true, [1700000000, 0], 1, 1]],
        ]),
      );
    if (id === 'hNvQHb')
      return new Response(rpc(id, [[['turn-id', null, [[raw]], [[[null, ['answer']]]]]], null]));
    throw new Error('unexpected fixture URL');
  };
  const captured = await capture(syncGemini);
  assert.deepEqual(captured.ids, []);
  assert.equal(captured.result.selection.unknown, 1);
  assert.equal(captured.result.selection.skipped, 1);
  assert.match(captured.result.selectionWarning, /Gemini.*metadata/i);
  assert.ok(!calls.some((url) => url.searchParams.get('rpcids') === 'hNvQHb'));
  assert.equal(captured.result.state.lastSyncMs, null);
});

await test('No selected categories cause no provider network requests', async () => {
  for (const sync of [syncChatGPT, syncClaude, syncGemini]) {
    globalThis.fetch = async () => {
      throw new Error('No network request is allowed');
    };
    const captured = await capture(sync, {
      projects: false,
      pinned: false,
      agents: false,
      recents: false,
    });
    assert.deepEqual(captured.ids, []);
  }
});

globalThis.setTimeout = realTimeout;
if (failures) process.exit(1);
