// A07/A08: real provider parsing and checkpoint decisions, offline HTTP fixtures.
import assert from 'node:assert/strict';
import test from 'node:test';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';

const policy = { projects: false, pinned: false, agents: false, recents: true };
const watermark = 1_700_000_000_000;
const principal = 'synthetic-principal';
const key = 'syntheti';

async function capture(sync, detail, state, accountResponse) {
  const pushed = [];
  const reports = [];
  globalThis.fetch = async (input) => {
    const url = new URL(input);
    if (url.pathname === '/api/auth/session') return Response.json({ accessToken: 'synthetic' });
    if (url.pathname.includes('/accounts/check/'))
      return (
        accountResponse?.() ??
        Response.json({ accounts: { a: { account: { account_id: principal } } } })
      );
    if (url.pathname === '/backend-api/conversations')
      return Response.json({ items: [{ id: 'chat', update_time: 1_700_000_001 }], total: 1 });
    if (url.pathname === '/backend-api/conversation/chat') return Response.json(detail);
    if (url.pathname === '/api/organizations') return Response.json([{ uuid: principal }]);
    if (url.pathname === `/api/organizations/${principal}/chat_conversations`)
      return Response.json([{ uuid: 'chat', updated_at: 1_700_000_001 }]);
    if (url.pathname === `/api/organizations/${principal}/chat_conversations/chat`)
      return Response.json(detail);
    throw new Error(`Unexpected synthetic path ${url.pathname}`);
  };
  const result = await sync({
    state,
    captureTypes: policy,
    report: async (value) => reports.push(value),
    push: async (_source, _adapter, batch) => {
      pushed.push(...batch);
      return batch.length;
    },
  });
  return { result, pushed, reports };
}

for (const [name, sync, field, empty, malformed] of [
  [
    'ChatGPT',
    syncChatGPT,
    'accounts',
    { mapping: {}, current_node: null },
    [
      {},
      {
        mapping: {
          n: {
            message: {
              author: { role: 'user' },
              content: { content_type: 'unknown-visible-record', text: 'synthetic' },
            },
          },
        },
      },
      {
        mapping: {
          n: {
            message: {
              author: { role: 'user' },
              content: { content_type: 'text', parts: [{ type: 'unknown' }] },
            },
          },
        },
      },
      { mapping: null },
      { mapping: [] },
      { mapping: { n: {} } },
      { mapping: { n: { message: { author: { role: 'user' } } } } },
      {
        mapping: {
          n: { message: { author: { role: 'assistant' }, content: { content_type: 'text' } } },
        },
      },
    ],
  ],
  [
    'Claude',
    syncClaude,
    'orgs',
    { uuid: 'chat', chat_messages: [] },
    [
      {},
      {
        uuid: 'chat',
        chat_messages: [
          { sender: 'human', content: [{ type: 'future-visible-content', data: 'synthetic' }] },
        ],
      },
      {
        uuid: 'chat',
        chat_messages: [{ sender: 'human', content: [{ type: 'text', text: { unknown: true } }] }],
      },
      { uuid: 'chat', chat_messages: null },
      { uuid: 'chat', chat_messages: {} },
      { uuid: 'chat', chat_messages: [{}] },
      { uuid: 'chat', chat_messages: [{ sender: 'human' }] },
      { uuid: 'chat', chat_messages: [{ sender: 'assistant', content: {} }] },
    ],
  ],
]) {
  const state = { [field]: { [key]: { principalId: principal, lastSyncMs: watermark } } };
  for (const [index, detail] of malformed.entries()) {
    test(`${name} malformed HTTP 200 detail ${index} preserves retry instead of empty success`, async () => {
      const first = await capture(sync, detail, state);
      assert.equal(first.result.state[field][key].lastSyncMs, watermark);
      assert.match(first.result.selectionWarning ?? '', /incomplete/i);
      assert.deepEqual(first.pushed, []);
      const retry = await capture(sync, empty, first.result.state);
      assert.equal(retry.result.selectionWarning, null);
      assert.ok(retry.result.state[field][key].lastSyncMs > watermark);
      assert.equal(retry.result.selection.selected, 1, 'Recents retry must re-read the old detail');
    });
  }
  test(`${name} explicitly empty recognized detail completes`, async () => {
    const { result, pushed } = await capture(sync, empty, state);
    assert.equal(result.selectionWarning, null);
    assert.ok(result.state[field][key].lastSyncMs > watermark);
    assert.deepEqual(pushed, []);
  });
}

for (const response of [
  () => Response.json({ error: 'unavailable' }, { status: 500 }),
  () => Response.json({ unexpected: [] }),
]) {
  test('ChatGPT fallback warns and retains unavailable workspace retry state', async () => {
    const previous = { principalId: 'workspace-old', lastSyncMs: watermark, name: 'Workspace' };
    const { result, reports } = await capture(
      syncChatGPT,
      { mapping: {} },
      {
        accounts: { workspac: previous },
      },
      response,
    );
    assert.deepEqual(result.state.accounts.workspac, previous);
    assert.match(result.selectionWarning ?? '', /account.*incomplete|workspace.*unavailable/i);
    assert.equal(reports.at(-1).selectionWarning, result.selectionWarning);
    assert.equal(result.state.accounts.default.lastSyncMs, null);
  });
}
