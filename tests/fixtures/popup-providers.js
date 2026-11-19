// Inject at the network boundary of the installed MV3 worker. No account, cookies or network.
(() => {
  const project = 'g-p-11111111111141118111111111111111';
  const member = '33333333-3333-4333-8333-333333333333';
  const claude = '44444444-4444-4444-8444-444444444444';
  const spare = '55555555-5555-4555-8555-555555555555';
  const org = '66666666-6666-4666-8666-666666666666';
  const claudeProject = '77777777-7777-4777-8777-777777777777';
  const gemini = 'abcdef123456abcd';
  const rpc = (id, value) =>
    new Response(")]}'\n" + JSON.stringify([['wrb.fr', id, JSON.stringify(value), null]]) + '\n');
  const result = (value, status = 200) => Response.json(value, { status });
  self.qa = { detail: [], requests: [], unexpected: [], signedIn: false };
  self.fetch = async (input, init = {}) => {
    const u = new URL(input),
      path = u.pathname;
    self.qa.requests.push(u.hostname + path);
    if (u.hostname === 'chatgpt.com') {
      if (path === '/api/auth/session')
        return self.qa.signedIn
          ? result({
              user: { id: 'fixture-user' },
              account: { id: 'fixture-account' },
              accessToken: 'synthetic-token',
            })
          : result({}, 401);
      if (path.includes('/accounts/check/'))
        return result({
          accounts: { a: { account: { account_id: 'fixture-account', structure: 'personal' } } },
        });
      if (path === '/backend-api/conversations')
        return result({
          total: 1,
          items: [
            {
              id: spare,
              title: 'Unselected GPT chat',
              is_pinned: false,
              gizmo_id: null,
              project_id: null,
            },
          ],
        });
      if (path.endsWith('/snorlax/sidebar'))
        return result({
          items: [{ gizmo: { gizmo: { id: project, display: { name: 'Selected GPT project' } } } }],
          cursor: null,
        });
      if (path === '/backend-api/gizmos/' + project + '/conversations')
        return result({
          items: [{ id: member, title: 'Project member', project_id: project }],
          cursor: null,
        });
      if (path.startsWith('/backend-api/conversation/')) {
        const id = path.split('/').pop();
        self.qa.detail.push(id);
        return result({
          id,
          title: 'Fixture ' + id,
          current_node: 'm',
          mapping: {
            m: {
              id: 'm',
              parent: null,
              message: {
                id: 'm-' + id,
                author: { role: 'user' },
                content: { content_type: 'text', parts: ['Synthetic GPT message'] },
              },
            },
          },
        });
      }
    }
    if (u.hostname === 'claude.ai') {
      if (path === '/api/account')
        return result({
          uuid: '99999999-9999-4999-8999-999999999999',
          memberships: [{ organization: { uuid: org } }],
        });
      if (path === '/api/organizations')
        return result([{ uuid: org, name: 'Fixture workspace', capabilities: ['claude_pro'] }]);
      if (path === '/api/organizations/' + org + '/projects')
        return result([
          { uuid: claudeProject, name: 'Unselected Claude project', archived_at: null },
        ]);
      if (path === '/api/organizations/' + org + '/projects/' + claudeProject + '/conversations_v2')
        return result({ data: [] });
      if (path === '/api/organizations/' + org + '/chat_conversations_v2')
        return result(
          [claude, spare].map((id, i) => ({
            uuid: id,
            name: i ? 'Unselected Claude chat' : 'Selected Claude chat',
            project_uuid: null,
            is_starred: false,
          })),
        );
      if (path.startsWith('/api/organizations/' + org + '/chat_conversations/')) {
        const id = path.split('/').pop();
        self.qa.detail.push(id);
        return result({
          uuid: id,
          name: 'Fixture ' + id,
          chat_messages: [
            {
              uuid: 'm-' + id,
              sender: 'human',
              content: [{ type: 'text', text: 'Synthetic Claude message' }],
            },
          ],
        });
      }
    }
    if (u.hostname === 'gemini.google.com') {
      if (path === '/app') {
        const r = new Response(
          '<script>{"SNlM0e":"synthetic-csrf","FdrFJe":"synthetic-session","cfb2h":"test-build"}</script>',
        );
        Object.defineProperty(r, 'url', { value: 'https://gemini.google.com/u/0/app' });
        return r;
      }
      const id = u.searchParams.get('rpcids');
      if (id === 'MaZiqc')
        return rpc(id, [
          null,
          null,
          [gemini, 'ffffffffffffffff'].map((v, i) => [
            'c_' + v,
            i ? 'Unselected Gemini chat' : 'Selected Gemini chat',
            null,
            null,
            null,
            [1700000000, 0],
          ]),
        ]);
      if (id === 'hNvQHb') {
        const [raw] = JSON.parse(JSON.parse(init.body.get('f.req'))[0][0][1]);
        self.qa.detail.push(raw.replace(/^c_/, ''));
        return rpc(id, [[[raw + '-turn', null, [['Synthetic Gemini message']], null]], null]);
      }
    }
    self.qa.unexpected.push(u.hostname + path);
    return result({ error: 'unexpected fixture endpoint' }, 404);
  };
})();
