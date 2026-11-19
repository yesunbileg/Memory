import assert from 'node:assert/strict';
import test from 'node:test';
import * as gpt from '../providers/chatgpt.js';
import * as claude from '../providers/claude.js';

function fixture(provider, { user = 'user', unauth = false, moved = false, changed = false } = {}) {
  let logins = 0;
  const calls = [],
    reads = [],
    pushed = [];
  const workspace = 'work',
    project = 'project',
    hidden = 'hidden-project',
    child = 'chat-in-project',
    plain = 'ordinary';
  const principalId = JSON.stringify([user, workspace]);
  const chat = (id, title, more = {}) =>
    provider === 'chatgpt' ? { id, title, ...more } : { uuid: id, name: title, ...more };
  const req = async (url, init = {}) => {
    const path = new URL(url).pathname;
    calls.push(path);
    let value;
    if (path === '/api/auth/session' || path === '/api/account') {
      logins++;
      const id = changed && logins > 1 ? 'another-user' : user;
      value =
        provider === 'chatgpt'
          ? { accessToken: 'PRIVATE_TOKEN', account: { id: workspace }, user: { id } }
          : { uuid: id, memberships: [{ organization: { uuid: workspace } }] };
    } else if (path.includes('accounts/check'))
      value = { accounts: { one: { account: { account_id: workspace, name: 'Workspace' } } } };
    else if (path === '/api/organizations') value = [{ uuid: workspace, name: 'Workspace' }];
    else if (path.endsWith('/sidebar'))
      value = {
        items: [
          { gizmo: { gizmo: { id: project, display: { name: 'Group' } } } },
          { gizmo: { gizmo: { id: hidden, name: 'HIDDEN TITLE', is_locked: true } } },
        ],
      };
    else if (path.endsWith('/projects'))
      value = [
        { uuid: project, name: 'Group' },
        { uuid: hidden, name: 'HIDDEN TITLE', is_locked: true },
      ];
    else if (
      path.includes('/gizmos/project/conversations') ||
      path.includes('/projects/project/conversations_v2')
    )
      value = provider === 'chatgpt' ? { items: [chat(child, 'Child')] } : [chat(child, 'Child')];
    else if (path === '/backend-api/conversations' || path.endsWith('/chat_conversations_v2')) {
      if (unauth) return new Response('{}', { status: 403 });
      const items = [
        chat(plain, 'Ordinary', { snippet: 'PRIVATE SNIPPET' }),
        chat('archived', 'ARCHIVED TITLE', { is_archived: true }),
        chat('arch-project', 'HIDDEN TITLE', { project_id: hidden }),
      ];
      value = provider === 'chatgpt' ? { items } : items;
    } else if (path.includes('/conversation/') || path.includes('/chat_conversations/')) {
      const id = path.split('/').at(-1);
      reads.push(id);
      value =
        provider === 'chatgpt'
          ? {
              id,
              current_node: 'm',
              mapping: {
                m: {
                  id: 'm',
                  message: {
                    id: 'm',
                    author: { role: 'user' },
                    content: { content_type: 'text', parts: ['visible'] },
                  },
                },
              },
            }
          : { uuid: id, chat_messages: [{ uuid: 'm', sender: 'human', text: 'visible' }] };
      if (moved) value.project_uuid = hidden;
    } else throw Error('unexpected listing');
    return Response.json(value);
  };
  const entry = { principalId, projectIds: [], conversationIds: [child] };
  return {
    req,
    calls,
    reads,
    pushed,
    principalId,
    child,
    plain,
    entry,
    options: {
      selectedScope: { entries: [entry] },
      push: async (_source, _adapter, batch) => {
        pushed.push(...batch.map((c) => c.externalId));
        return batch.length;
      },
    },
  };
}

for (const [provider, mod, catalog, sync] of [
  ['chatgpt', gpt, 'listChatGPTCatalog', 'syncChatGPT'],
  ['claude', claude, 'listClaudeCatalog', 'syncClaude'],
]) {
  test(
    provider + ' metadata catalog returns only verified active names and native group IDs',
    async (t) => {
      const f = fixture(provider);
      t.mock.method(globalThis, 'fetch', f.req);
      assert.equal(typeof mod[catalog], 'function');
      const result = await mod[catalog]();
      assert.deepEqual(result.entries, [
        {
          principalId: f.principalId,
          title: 'Workspace',
          projects: [{ id: 'project', title: 'Group' }],
          conversations: [
            { id: f.plain, title: 'Ordinary' },
            { id: f.child, title: 'Child', projectId: 'project' },
          ],
        },
      ]);
      assert.deepEqual(f.reads, []);
      assert.ok(!JSON.stringify(result).match(/PRIVATE|ARCHIVED|HIDDEN/));
    },
  );
  test(
    provider + ' saves a single project member without selecting its whole project',
    async (t) => {
      const f = fixture(provider);
      t.mock.method(globalThis, 'fetch', f.req);
      await mod[sync](f.options);
      assert.deepEqual(f.reads, [f.child]);
      assert.deepEqual(f.pushed, [f.child]);
    },
  );
  test(provider + ' never reuses another user selection within the same workspace', async (t) => {
    const f = fixture(provider, { user: 'other-user' });
    t.mock.method(globalThis, 'fetch', f.req);
    f.entry.principalId = JSON.stringify(['user', 'work']);
    await mod[sync](f.options);
    assert.deepEqual(f.reads, []);
  });
  test(
    provider + ' unreadable catalog cannot leak titles or fall back to detail calls',
    async (t) => {
      const f = fixture(provider, { unauth: true });
      t.mock.method(globalThis, 'fetch', f.req);
      await assert.rejects(mod[catalog]());
      assert.deepEqual(f.reads, []);
    },
  );
  test(
    provider + ' account switch during catalog does not return the previous account names',
    async (t) => {
      const f = fixture(provider, { changed: true });
      t.mock.method(globalThis, 'fetch', f.req);
      await assert.rejects(mod[catalog]());
      assert.deepEqual(f.reads, []);
    },
  );
  test(
    provider + ' skips a selected project chat after it moves into an unavailable project',
    async (t) => {
      const f = fixture(provider, { moved: true });
      t.mock.method(globalThis, 'fetch', f.req);
      await mod[sync](f.options);
      assert.deepEqual(f.pushed, []);
    },
  );
}

for (const [provider, sync] of [
  ['chatgpt', gpt.syncChatGPT],
  ['claude', claude.syncClaude],
]) {
  test(
    provider + ' project-only capture does not enumerate ordinary or unrelated project chats',
    async (t) => {
      const f = fixture(provider);
      t.mock.method(globalThis, 'fetch', f.req);
      f.entry.projectIds = ['project'];
      f.entry.conversationIds = [];
      const result = await sync(f.options);
      assert.deepEqual(f.pushed, [f.child]);
      assert.equal(result.selection.selected, 1);
      assert.ok(
        !f.calls.some(
          (p) => p === '/backend-api/conversations' || p.endsWith('/chat_conversations_v2'),
        ),
      );
    },
  );
}

test('ChatGPT accepts native conversation_id but never a conflicting detail identity', async (t) => {
  const f = fixture('chatgpt');
  let conflict = false;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const response = await f.req(url, init);
    if (!new URL(url).pathname.startsWith('/backend-api/conversation/')) return response;
    const obj = await response.json();
    obj.conversation_id = conflict ? 'different' : obj.id;
    if (!conflict) delete obj.id;
    return Response.json(obj);
  });
  await gpt.syncChatGPT(f.options);
  assert.deepEqual(f.pushed, [f.child]);
  conflict = true;
  await gpt.syncChatGPT(f.options);
  assert.deepEqual(f.pushed, [f.child]);
});

for (const [provider, list] of [
  ['chatgpt', gpt.listChatGPTCatalog],
  ['claude', claude.listClaudeCatalog],
]) {
  test(
    provider +
      ' retains verified project members if another project cannot be read, without exposing its title or ambiguous Recents',
    async (t) => {
      const f = fixture(provider);
      t.mock.method(globalThis, 'fetch', async (url, init) => {
        const path = new URL(url).pathname;
        if (path.includes('/broken/conversations')) return new Response('{}', { status: 403 });
        const resp = await f.req(url, init);
        if (path.endsWith('/sidebar')) {
          const json = await resp.json();
          json.items.unshift({ gizmo: { gizmo: { id: 'broken', name: 'PRIVATE FORBIDDEN' } } });
          return Response.json(json);
        }
        if (path.endsWith('/projects')) {
          const json = await resp.json();
          json.unshift({ uuid: 'broken', name: 'PRIVATE FORBIDDEN' });
          return Response.json(json);
        }
        return resp;
      });
      const catalog = await list();
      assert.equal(catalog.incomplete, true);
      assert.deepEqual(
        catalog.entries[0].projects.map((p) => p.id),
        ['project'],
      );
      assert.deepEqual(
        catalog.entries[0].conversations.map((c) => c.id),
        [f.child],
      );
      assert.ok(!JSON.stringify(catalog).includes('FORBIDDEN'));
      assert.deepEqual(f.reads, []);
    },
  );
}

test('Claude does not fetch a selected chat using newly switched users cookies', async (t) => {
  const f = fixture('claude');
  let switched = false;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    if (switched && new URL(url).pathname === '/api/account')
      return Response.json({ uuid: 'new-user', memberships: [{ organization: { uuid: 'work' } }] });
    return f.req(url, init);
  });
  f.options.register = async () => {
    switched = true;
  };
  await claude.syncClaude(f.options).catch(() => {});
  assert.deepEqual(f.reads, []);
  assert.deepEqual(f.pushed, []);
});

for (const [provider, sync] of [
  ['chatgpt', gpt.syncChatGPT],
  ['claude', claude.syncClaude],
]) {
  test(
    provider + ' account change immediately before detail aborts without advancing the ID queue',
    async (t) => {
      const f = fixture(provider);
      let switched = false;
      t.mock.method(globalThis, 'fetch', async (url, init) => {
        const path = new URL(url).pathname;
        if (switched && path === '/api/auth/session')
          return Response.json({
            accessToken: 'changed-token',
            user: { id: 'other-user' },
            account: { id: 'work' },
          });
        if (switched && path === '/api/account')
          return Response.json({
            uuid: 'other-user',
            memberships: [{ organization: { uuid: 'work' } }],
          });
        return f.req(url, init);
      });
      f.options.register = async () => {
        switched = true;
      };
      const checkpoints = [];
      f.options.checkpoint = async (state) => checkpoints.push(structuredClone(state));
      await assert.rejects(sync(f.options));
      assert.deepEqual(f.reads, []);
      assert.deepEqual(f.pushed, []);
      assert.ok(
        checkpoints.every(
          (s) =>
            s.pending && s.pending.processed === 0 && s.pending.groups[0].ids.includes(f.child),
        ),
      );
    },
  );
}

test('Claude omits an inaccessible organization without discarding a separately verified workspace', async (t) => {
  const f = fixture('claude');
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const path = new URL(url).pathname;
    if (path === '/api/account')
      return Response.json({
        uuid: 'user',
        memberships: ['blocked', 'work'].map((uuid) => ({ organization: { uuid } })),
      });
    if (path === '/api/organizations')
      return Response.json([
        { uuid: 'blocked', name: 'PRIVATE ORG TITLE' },
        { uuid: 'work', name: 'Workspace' },
      ]);
    if (path.includes('/organizations/blocked/')) return new Response('{}', { status: 403 });
    return f.req(url, init);
  });
  const result = await claude.listClaudeCatalog();
  assert.deepEqual(
    result.entries.map((e) => e.principalId),
    [f.principalId],
  );
  assert.equal(result.incomplete, true);
  assert.ok(!JSON.stringify(result).includes('PRIVATE ORG TITLE'));
});
