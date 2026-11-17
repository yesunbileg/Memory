// Native principal guards prevent collisions in shortened source labels.
import assert from 'node:assert/strict';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';
const policy = { projects: false, pinned: false, agents: false, recents: true };
const current = 'sameprefix-current',
  previous = 'sameprefix-previous';
let pushed = 0;
globalThis.fetch = async (input) => {
  const url = new URL(input);
  if (url.pathname === '/api/auth/session') return Response.json({ accessToken: 'synthetic' });
  if (url.pathname.includes('/accounts/check/'))
    return Response.json({ accounts: { a: { account: { account_id: current } } } });
  if (url.pathname === '/backend-api/conversations')
    return Response.json({
      items: [
        {
          id: 'history',
          update_time: 1700000000,
          project_id: null,
          gizmo_id: null,
          is_starred: false,
        },
      ],
      total: 1,
    });
  if (url.pathname === '/backend-api/conversation/history')
    return Response.json({
      id: 'history',
      mapping: {
        m: {
          id: 'm',
          parent: null,
          message: {
            id: 'm',
            author: { role: 'user' },
            content: { content_type: 'text', parts: ['old visible text'] },
          },
        },
      },
      current_node: 'm',
    });
  if (url.pathname === '/api/organizations') return Response.json([{ uuid: current }]);
  if (url.pathname === `/api/organizations/${current}/chat_conversations`)
    return Response.json([
      {
        uuid: 'history',
        updated_at: '2023-11-14T00:00:00Z',
        project_uuid: null,
        is_starred: false,
      },
    ]);
  if (url.pathname === `/api/organizations/${current}/chat_conversations/history`)
    return Response.json({
      uuid: 'history',
      chat_messages: [{ uuid: 'm', sender: 'human', text: 'old visible text' }],
    });
  return Response.json({}, { status: 404 });
};
for (const [sync, field] of [
  [syncChatGPT, 'accounts'],
  [syncClaude, 'orgs'],
]) {
  pushed = 0;
  const result = await sync({
    state: { [field]: { samepref: { principalId: previous, lastSyncMs: 2000000000000 } } },
    captureTypes: policy,
    push: async (_s, _a, conversations) => {
      pushed += conversations.length;
      return conversations.length;
    },
  });
  assert.equal(pushed, 1, `${field}: old principal watermark must not omit new principal history`);
  assert.equal(result.state[field].samepref.principalId, current);
}
console.log('PASS native principal checkpoint collision isolation');
