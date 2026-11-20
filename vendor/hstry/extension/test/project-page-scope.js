import assert from 'node:assert/strict';
import test from 'node:test';
import { listChatGPTCatalog, syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';

test('ChatGPT discovery and manual capture use the selected tab account rather than browser default', async (t) => {
  const principalId = JSON.stringify(['pwa-user', 'pwa-account']);
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const path = new URL(url).pathname;
    if (path === '/api/auth/session') {
      return Response.json({
        user: { id: 'browser-user' },
        account: { id: 'browser-account' },
        accessToken: 'browser-token',
      });
    }
    const tab = init.credentials === 'omit' && init.headers.Authorization === 'Bearer pwa-token';
    if (path.includes('/accounts/check/')) {
      return Response.json({
        accounts: { a: { account: { id: tab ? 'pwa-account' : 'browser-account' } } },
      });
    }
    if (path.endsWith('/snorlax/sidebar')) return Response.json({ items: [] });
    if (path.endsWith('/conversations')) {
      return Response.json({ items: [{ id: tab ? 'pwa-chat' : 'browser-chat', title: 'A chat' }] });
    }
    assert.equal(path, '/backend-api/conversation/pwa-chat');
    assert.ok(tab);
    return Response.json({
      id: 'pwa-chat',
      current_node: 'm',
      mapping: {
        m: {
          id: 'm',
          message: {
            id: 'm',
            author: { role: 'user' },
            content: { content_type: 'text', parts: ['Fixture'] },
          },
        },
      },
    });
  });
  const oldChrome = globalThis.chrome;
  t.after(() => {
    globalThis.chrome = oldChrome;
  });
  globalThis.chrome = {
    tabs: {
      sendMessage: async (tabId, message) => {
        assert.equal(tabId, 123);
        assert.equal(message.type, 'GET_CHATGPT_TAB_SESSION');
        return {
          user: { id: 'pwa-user' },
          account: { id: 'pwa-account' },
          accessToken: 'pwa-token',
        };
      },
    },
  };
  const context = { tabId: 123, documentId: 'pwa-document' };
  const catalog = await listChatGPTCatalog(context);
  assert.equal(catalog.entries[0].principalId, principalId);
  assert.deepEqual(
    catalog.entries[0].conversations.map((c) => c.id),
    ['pwa-chat'],
  );
  const captured = [];
  await syncChatGPT({
    chatgptTab: context,
    selectedScope: { entries: [{ principalId, projectIds: [], conversationIds: ['pwa-chat'] }] },
    push: async (_source, _adapter, chats) =>
      captured.push(...chats.map((chat) => chat.externalId)),
  });
  assert.deepEqual(captured, ['pwa-chat']);
});

// Exercise the provider adapters through their actual cursor and offset APIs. The second
// membership page has no corresponding sidebar or ordinary history rows.
function fixture(t, provider, sync, unrelatedProjects = 0) {
  const gpt = provider === 'ChatGPT';
  const principalId = JSON.stringify(['user', 'workspace']);
  const names = [...Array.from({ length: unrelatedProjects }, (_, i) => 'other-' + i), 'chosen'];
  const row = (id, title, flags = {}) =>
    gpt ? { id, title, ...flags } : { uuid: id, name: title, ...flags };
  const first = [
    row('overlap', 'Checked independently'),
    row('locked-at-read', 'Still in the list'),
    ...Array.from({ length: 48 }, (_, i) => row('excluded-' + i, null, { is_locked: true })),
  ];
  const second = [
    row('overlap', 'Also on an overlapping page'),
    row('no-title', null),
    row('offscreen', 'Only on page two'),
    row('locked-no-title', null, { metadata: { is_locked: true } }),
    row('wrong-parent', null, { project_id: 'unavailable-project' }),
  ];
  const pages = [],
    reads = [],
    sent = [];
  t.mock.method(globalThis, 'fetch', async (url) => {
    const { pathname: path, searchParams } = new URL(url);
    if (path === '/api/auth/session')
      return Response.json({
        user: { id: 'user' },
        account: { id: 'workspace' },
        accessToken: 'test',
      });
    if (path.includes('/accounts/check/'))
      return Response.json({ accounts: { current: { account: { account_id: 'workspace' } } } });
    if (path === '/api/account')
      return Response.json({
        uuid: 'user',
        memberships: [{ organization: { uuid: 'workspace' } }],
      });
    if (path === '/api/organizations') return Response.json([{ uuid: 'workspace', name: 'Work' }]);
    if (path.endsWith('/snorlax/sidebar'))
      return Response.json({
        items: names.map((id) => ({ gizmo: { gizmo: { id, name: id } } })),
        cursor: null,
      });
    if (path.endsWith('/projects')) return Response.json(names.map((id) => row(id, id)));
    const members = path.match(/\/(?:projects|gizmos)\/([^/]+)\/conversations(?:_v2)?$/);
    if (members) {
      const id = members[1];
      if (id !== 'chosen') return Response.json(gpt ? { items: [], cursor: null } : { data: [] });
      const page = searchParams.get(gpt ? 'cursor' : 'offset');
      assert.equal(searchParams.get('limit'), '50');
      pages.push(page);
      if (page === '0')
        return Response.json(gpt ? { items: first, cursor: 'next-page' } : { data: first });
      assert.equal(page, gpt ? 'next-page' : '50');
      return Response.json(gpt ? { items: second, cursor: null } : { data: second });
    }
    if (path === '/backend-api/conversations' || path.endsWith('/chat_conversations_v2')) {
      const recent = [
        row('overlap', 'Again in Recents', { project_id: 'chosen' }),
        row('standalone', 'Direct'),
        row('unselected', 'Not checked'),
        row('pinned', 'Pinned', { is_pinned: true }),
        row('moved', 'Was in Recents'),
      ];
      return Response.json(gpt ? { items: recent, total: recent.length } : { data: recent });
    }
    const detail = path.match(/\/(?:conversation|chat_conversations)\/([^/]+)$/);
    if (!detail) throw new Error('Unexpected fixture request ' + path);
    const id = detail[1];
    reads.push(id);
    const visible = {
      id: 'm',
      author: { role: 'user' },
      content: { content_type: 'text', parts: ['Visible'] },
    };
    const item = gpt
      ? { id, current_node: 'm', mapping: { m: { id: 'm', parent: null, message: visible } } }
      : { uuid: id, chat_messages: [{ uuid: 'm', sender: 'human', text: 'Visible' }] };
    if (id === 'locked-at-read') item.is_locked = true;
    if (id === 'moved') item.is_pinned = true;
    return Response.json(item);
  });
  const run = (projectIds, conversationIds, state = {}, includeRecents = false) =>
    sync({
      state,
      selectedScope: {
        entries: [
          { principalId, projectIds, conversationIds, ...(includeRecents && { includeRecents }) },
        ],
      },
      push: async (_source, _adapter, batch) => {
        sent.push(...batch.map((c) => c.externalId));
        return batch.length;
      },
    });
  return { run, sent, reads, pages };
}

test('ChatGPT bulk Recents discovers current ordinary chats and excludes projects, pins, and chats that moved', async (t) => {
  const f = fixture(t, 'ChatGPT', syncChatGPT);
  await f.run([], [], {}, true);
  assert.deepEqual(f.reads.toSorted(), ['moved', 'standalone', 'unselected']);
  assert.deepEqual(f.sent.toSorted(), ['standalone', 'unselected']);
});

for (const [provider, sync] of [
  ['ChatGPT', syncChatGPT],
  ['Claude', syncClaude],
]) {
  test(
    provider +
      ' expands a selected project past visible rows without duplicating direct selections',
    async (t) => {
      const f = fixture(t, provider, sync);
      const direct = ['overlap', 'standalone'];
      const initial = await f.run(['chosen'], direct);
      assert.deepEqual(f.pages, provider === 'ChatGPT' ? ['0', 'next-page'] : ['0', '50']);
      assert.deepEqual(f.sent.toSorted(), ['no-title', 'offscreen', 'overlap', 'standalone']);
      assert.deepEqual(f.reads.toSorted(), [
        'locked-at-read',
        'no-title',
        'offscreen',
        'overlap',
        'standalone',
      ]);
      assert.equal(initial.selectionWarning, null);

      // Unchecking the project leaves the independent chat picks intact.
      f.sent.length = f.reads.length = 0;
      await f.run([], direct, initial.state);
      assert.deepEqual(f.reads.toSorted(), ['overlap', 'standalone']);
      assert.deepEqual(f.sent.toSorted(), ['overlap', 'standalone']);
    },
  );

  test(
    provider + ' verifies a chosen project before unrelated membership exhausts the scan',
    async (t) => {
      const f = fixture(t, provider, sync, 201);
      const result = await f.run(['chosen'], ['overlap']);
      assert.deepEqual(f.sent.toSorted(), ['no-title', 'offscreen', 'overlap']);
      assert.ok(result.selectionWarning, 'unverified entries must still be reported');
    },
  );
}
