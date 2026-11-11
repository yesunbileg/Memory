// Real provider modules: selected detail failures must not imply completeness.
import assert from 'node:assert/strict';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';
import { syncGemini } from '../providers/gemini.js';
const policy = { projects: false, pinned: false, agents: false, recents: true };
const rpc = (id, data) => `)]}'\n${JSON.stringify([['wrb.fr', id, JSON.stringify(data), null]])}\n`;
for (const [name, sync] of [
  ['ChatGPT', syncChatGPT],
  ['Claude', syncClaude],
  ['Gemini', syncGemini],
]) {
  const progress = [];
  globalThis.fetch = async (input) => {
    const url = new URL(input);
    if (url.pathname === '/api/auth/session') return Response.json({ accessToken: 'synthetic' });
    if (url.pathname.includes('/accounts/check/'))
      return Response.json({ accounts: { a: { account: { account_id: 'synthetic' } } } });
    if (url.pathname === '/backend-api/conversations')
      return Response.json({ items: [{ id: 'private-chat-id' }], total: 1 });
    if (url.pathname === '/api/organizations') return Response.json([{ uuid: 'synthetic-org' }]);
    if (url.pathname === '/api/organizations/synthetic-org/chat_conversations')
      return Response.json([{ uuid: 'private-chat-id' }]);
    if (url.pathname === '/app')
      return new Response('<script>{"SNlM0e":"synthetic","FdrFJe":"sid","cfb2h":"build"}</script>');
    if (url.searchParams.get('rpcids') === 'MaZiqc')
      return new Response(
        rpc('MaZiqc', [
          null,
          null,
          [['c_private-chat-id', 'Title', null, null, null, [1700000000, 0]]],
        ]),
      );
    return Response.json({ error: 'private detail failure' }, { status: 403 });
  };
  let pushed = 0;
  const result = await sync({
    state: {},
    captureTypes: policy,
    push: async () => {
      pushed++;
      return 0;
    },
    log: () => {},
    report: async (value) => progress.push(value),
  });
  assert.equal(pushed, 0);
  assert.match(
    result.selectionWarning ?? '',
    /could not|failed|incomplete/i,
    `${name} selected failures need a warning`,
  );
  assert.ok(!result.selectionWarning.includes('private-chat-id'));
  assert.equal(progress.at(-1).selectionWarning, result.selectionWarning);
  const cursor =
    name === 'ChatGPT'
      ? Object.values(result.state.accounts)[0].lastSyncMs
      : name === 'Claude'
        ? Object.values(result.state.orgs)[0].lastSyncMs
        : result.state.lastSyncMs;
  assert.equal(cursor, null);
  console.log(`PASS ${name} selected detail failure warns and retains retry watermark`);
}
