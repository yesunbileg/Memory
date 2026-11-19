import assert from 'node:assert/strict';
import { test } from 'node:test';
import { syncChatGPT } from '../providers/chatgpt.js';

const id = (i) => String(i).padStart(32, '0');
const wanted = 'g-p-' + id(100),
  other = 'g-p-' + id(101);
const user = 'user-1',
  workspace = 'workspace-1';
const list = (conversationIds, projectIds = []) => ({
  version: 1,
  entries: [{ userId: user, accountId: workspace, projectIds, conversationIds }],
});
function fixture({
  userId = user,
  accountId = workspace,
  ordinary = [],
  members = [],
  catalog = [wanted, other].map((id) => ({ id })),
  detailInfo = {},
} = {}) {
  const reads = [],
    projects = [],
    writes = [],
    accounts = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(input).pathname;
    if (path === '/api/auth/session')
      return Response.json({
        user: { id: userId },
        account: { id: accountId },
        accessToken: 'fixture',
      });
    if (path.includes('/accounts/check/'))
      return Response.json({
        accounts: {
          active: { account: { account_id: workspace, structure: 'workspace' } },
          different: { account: { account_id: 'workspace-other', structure: 'workspace' } },
        },
      });
    accounts.push(init.headers['chatgpt-account-id']);
    if (path.endsWith('/snorlax/sidebar'))
      return Response.json({ items: catalog.map((item) => ({ gizmo: { gizmo: item } })) });
    if (path === '/backend-api/conversations')
      return Response.json({ items: ordinary, total: ordinary.length });
    if (path.includes('/gizmos/')) {
      projects.push(path.split('/')[3]);
      return Response.json({ items: members });
    }
    const conv = path.split('/').pop();
    reads.push(conv);
    return Response.json({
      id: conv,
      ...detailInfo[conv],
      current_node: 'm',
      mapping: {
        m: {
          id: 'm',
          message: {
            author: { role: 'user' },
            content: { content_type: 'text', parts: ['fixture'] },
          },
        },
      },
    });
  };
  return {
    reads,
    projects,
    writes,
    accounts,
    run: (
      chatgptSyncList,
      state = {},
      captureTypes = { recents: false, pinned: false, projects: false, agents: false },
    ) =>
      syncChatGPT({
        state,
        chatgptSyncList,
        captureTypes,
        push: async (_s, _a, items) => {
          writes.push(...items.map((c) => c.externalId));
          return items.length;
        },
      }),
  };
}

test('only selected verified project contents and ordinary IDs are fetched; categories do not broaden or disable scope', async () => {
  const ordinary = [
    { id: id(1) },
    { id: id(2) },
    { id: id(3), project_id: other },
    { id: id(4), is_pinned: true },
    { id: id(5), gizmo_id: 'g-agent' },
    { id: id(6), is_archived: true },
    { id: id(7), is_locked: true },
  ];
  const f = fixture({
    ordinary,
    members: [{ id: id(8) }, { id: id(9), is_deleted: true }, { id: id(10), is_hidden: true }],
  });
  await f.run(list([1, 3, 4, 5, 6, 7].map(id), [wanted]));
  assert.deepEqual(f.writes.sort(), [id(1), id(8)]);
  assert.deepEqual(f.reads.sort(), f.writes);
  assert.deepEqual(f.projects, [wanted]);
  assert.ok(f.accounts.every((a) => a === workspace));
});

test('empty/invalid list and an unmatched signed-in user or active workspace never fall back to category capture', async () => {
  for (const [scope, identity] of [
    [{ version: 9 }, {}],
    [list([]), {}],
    [list([id(1)]), { userId: 'someone-else' }],
    [list([id(1)]), { accountId: 'different' }],
  ]) {
    const f = fixture({ ordinary: [{ id: id(1) }], ...identity });
    await f.run(scope, {}, { recents: true, pinned: true, projects: true, agents: true });
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.reads, []);
    assert.deepEqual(f.accounts, []);
  }
});

test('stale queued IDs cannot resume under a changed list or user, even within the same workspace', async () => {
  const f = fixture({ ordinary: [{ id: id(1) }] });
  const stale = {
    pending: {
      signature: JSON.stringify([workspace]),
      groups: [
        {
          principalId: workspace,
          key: 'wrong',
          sourceId: 'chatgpt-web',
          adapter: 'chatgpt-web',
          ids: [id(3)],
          failed: [],
        },
      ],
    },
  };
  await f.run(list([id(1)]), stale);
  assert.deepEqual(f.writes, [id(1)]);
});

test('verified project membership cannot override contrary metadata at discovery or when the selected chat detail is read', async () => {
  const f = fixture({
    members: [
      { id: id(1) },
      { id: id(2) },
      { id: id(3), project_id: other },
      { id: id(4), is_pinned: true },
    ],
    detailInfo: {
      [id(1)]: { project_id: other },
      [id(2)]: { project_id: wanted, metadata: { folder_id: other } },
    },
  });
  await f.run(list([], [wanted]));
  assert.deepEqual(f.writes, [id(4)]);
  assert.ok(!f.reads.includes(id(3)));
});

test('saved projects explicitly marked locked or archived in the current catalog are not opened', async () => {
  const f = fixture({
    catalog: [
      { id: wanted, is_locked: true },
      { id: other, metadata: { is_archived: true } },
    ],
    members: [{ id: id(1) }],
  });
  await f.run(list([], [wanted, other]));
  assert.deepEqual(f.projects, []);
  assert.deepEqual(f.reads, []);
});
