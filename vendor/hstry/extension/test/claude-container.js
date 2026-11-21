// Container-aware Claude reads: every API request for a selected tab must be
// issued from that tab, never from the worker's default cookie context.

import assert from 'node:assert/strict';
import test from 'node:test';
import { claudePrincipalIds, listClaudeCatalog, syncClaude } from '../providers/claude.js';

const now = new Date(Date.now() - 3_600_000).toISOString();
const context = { tabId: 77, documentId: 'claude-document' };
const principalId = JSON.stringify(['user', 'work']);

const reply = (value, url, status = 200) => ({
  status,
  url,
  body: JSON.stringify(value),
  retryAfter: null,
});

const account = { uuid: 'user', memberships: [{ organization: { uuid: 'work' } }] };
const organizations = [{ uuid: 'work', name: 'Workspace' }];
const projects = [{ uuid: 'project', name: 'Group' }];
const members = [{ uuid: 'child', name: 'Child' }];
const chats = [{ uuid: 'plain', name: 'Ordinary' }];

function responseFor(url) {
  const { pathname } = new URL(url);
  if (pathname === '/api/account') return reply(account, url);
  if (pathname === '/api/organizations') return reply(organizations, url);
  if (pathname === '/api/organizations/work/projects') return reply(projects, url);
  if (pathname === '/api/organizations/work/projects/project/conversations_v2')
    return reply(members, url);
  if (pathname === '/api/organizations/work/chat_conversations_v2') return reply(chats, url);
  const detail = pathname.match(/^\/api\/organizations\/work\/chat_conversations\/([^/]+)$/);
  if (detail)
    return reply(
      {
        uuid: detail[1],
        name: 'Ordinary',
        created_at: now,
        updated_at: now,
        chat_messages: [
          { sender: 'human', created_at: now, content: [{ type: 'text', text: 'hello' }] },
          { sender: 'assistant', created_at: now, content: [{ type: 'text', text: 'response' }] },
        ],
      },
      url,
    );
  throw new Error('Unexpected Claude request ' + url);
}

function tabFixture(t) {
  const sent = [];
  const oldChrome = globalThis.chrome;
  t.after(() => {
    globalThis.chrome = oldChrome;
  });
  t.mock.method(globalThis, 'fetch', async (url) => {
    throw new Error('Default fetch must not be used for ' + url);
  });
  globalThis.chrome = {
    tabs: {
      sendMessage: async (tabId, message, options) => {
        sent.push({ tabId, message, options });
        assert.equal(message.type, 'FETCH_PROVIDER_TAB');
        assert.equal(tabId, context.tabId);
        assert.equal(options?.frameId, 0);
        assert.equal(options?.documentId, context.documentId);
        assert.equal(message.method, 'GET');
        return responseFor(message.url);
      },
    },
  };
  return sent;
}

const paths = (sent) => sent.map(({ message }) => new URL(message.url).pathname);

test('Claude catalog reads account and every catalog page from the selected tab', async (t) => {
  const sent = tabFixture(t);
  const catalog = await listClaudeCatalog(context);

  assert.deepEqual(catalog.entries, [
    {
      principalId,
      title: 'Workspace',
      projects: [{ id: 'project', title: 'Group' }],
      conversations: [
        { id: 'plain', title: 'Ordinary' },
        { id: 'child', title: 'Child', projectId: 'project' },
      ],
    },
  ]);
  assert.ok(paths(sent).includes('/api/account'));
  assert.ok(paths(sent).includes('/api/organizations'));
  assert.ok(paths(sent).includes('/api/organizations/work/projects'));
  assert.ok(paths(sent).includes('/api/organizations/work/projects/project/conversations_v2'));
  assert.ok(paths(sent).includes('/api/organizations/work/chat_conversations_v2'));
});

test('Claude principal discovery uses the selected tab', async (t) => {
  const sent = tabFixture(t);
  assert.deepEqual(await claudePrincipalIds(context), [principalId]);
  assert.deepEqual(paths(sent), ['/api/account', '/api/organizations']);
});

test('Claude explicit selection reads, re-verifies ownership, and pushes in the tab', async (t) => {
  const sent = tabFixture(t);
  const pushed = [];
  const result = await syncClaude({
    tabContext: context,
    selectedScope: {
      entries: [{ principalId, projectIds: [], conversationIds: ['plain'] }],
    },
    push: async (_source, _adapter, batch) => {
      pushed.push(...batch.map((chat) => chat.externalId));
      return batch.length;
    },
  });

  assert.equal(result.conversations, 1);
  assert.deepEqual(pushed, ['plain']);
  const requested = paths(sent);
  assert.ok(requested.filter((path) => path === '/api/account').length >= 2);
  assert.ok(requested.includes('/api/organizations'));
  assert.ok(requested.includes('/api/organizations/work/projects'));
  assert.ok(requested.includes('/api/organizations/work/chat_conversations_v2'));
  assert.ok(requested.includes('/api/organizations/work/chat_conversations/plain'));
});

test('Claude without a tab context keeps the default fetch path', async (t) => {
  let fetched = 0;
  t.mock.method(globalThis, 'fetch', async (url) => {
    fetched++;
    const { pathname } = new URL(url);
    if (pathname === '/api/account') return Response.json(account);
    if (pathname === '/api/organizations') return Response.json(organizations);
    if (pathname === '/api/organizations/work/projects') return Response.json(projects);
    if (pathname === '/api/organizations/work/projects/project/conversations_v2')
      return Response.json(members);
    if (pathname === '/api/organizations/work/chat_conversations_v2') return Response.json(chats);
    throw new Error('Unexpected Claude request ' + url);
  });
  const oldChrome = globalThis.chrome;
  t.after(() => {
    globalThis.chrome = oldChrome;
  });
  const sent = [];
  globalThis.chrome = {
    tabs: {
      sendMessage: async () => {
        sent.push(1);
        throw new Error('A tab fetch requires an explicit context');
      },
    },
  };

  const catalog = await listClaudeCatalog();
  assert.equal(catalog.entries[0].principalId, principalId);
  assert.ok(fetched > 0);
  assert.deepEqual(sent, []);
});
