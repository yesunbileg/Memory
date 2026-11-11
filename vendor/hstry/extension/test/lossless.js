// AI Memory regression: real providers, synthetic network fixtures, no login.
import assert from 'node:assert/strict';
import { syncChatGPT } from '../providers/chatgpt.js';
import { syncClaude } from '../providers/claude.js';
import { syncGemini } from '../providers/gemini.js';

const raw = "  Сайн уу\n```py\n  print('漢字 🦦')\n```\n\t";
const answer = '  Visible answer.\n';
const detail = {
  title: 'Synthetic ChatGPT', current_node: 'a', mapping: {
    s: { id: 's', parent: null, message: { author: { role: 'system' }, content: { content_type: 'text', parts: ['DO NOT IMPORT'] } } },
    u: { id: 'u', parent: 's', message: { id: 'chatgpt-user', author: { role: 'user' }, content: { content_type: 'text', parts: [raw.slice(0, 8), raw.slice(8)] } } },
    h: { id: 'h', parent: 'u', message: { id: 'hidden', author: { role: 'assistant' }, metadata: { is_visually_hidden_from_conversation: true }, content: { content_type: 'text', parts: ['DO NOT IMPORT'] } } },
    r: { id: 'r', parent: 'h', message: { id: 'reasoning', author: { role: 'assistant' }, channel: 'analysis', content: { content_type: 'thoughts', thoughts: [{ content: 'DO NOT IMPORT' }] } } },
    t: { id: 't', parent: 'r', message: { id: 'tool', author: { role: 'assistant' }, recipient: 'python', content: { content_type: 'code', text: 'DO NOT IMPORT' } } },
    a: { id: 'a', parent: 't', message: { id: 'chatgpt-answer', author: { role: 'assistant' }, content: { content_type: 'text', parts: [answer] }, metadata: { private_prompt: 'DO NOT IMPORT' } } },
  },
};
const rpc = (id, data) => `)]}'\n${JSON.stringify([['wrb.fr', id, JSON.stringify(data), null]])}\n`;
globalThis.fetch = async (url, init = {}) => {
  const parsed = new URL(url);
  let fixture;
  if (parsed.hostname === 'chatgpt.com') {
    if (parsed.pathname === '/api/auth/session') fixture = { accessToken: 'synthetic-token' };
    else if (parsed.pathname.includes('/accounts/check/')) fixture = { accounts: { default: { account: { account_id: 'synthetic-account', structure: 'personal' } } } };
    else if (parsed.pathname === '/backend-api/conversations') fixture = { total: 1, items: [{ id: 'synthetic-chatgpt' }] };
    else if (parsed.pathname === '/backend-api/conversation/synthetic-chatgpt') fixture = detail;
  } else if (parsed.hostname === 'claude.ai') {
    if (parsed.pathname === '/api/organizations') fixture = [{ uuid: 'synthetic-org' }];
    else if (parsed.pathname.endsWith('/chat_conversations')) fixture = [{ uuid: 'synthetic-claude' }];
    else if (parsed.pathname.endsWith('/synthetic-claude')) fixture = {
      uuid: 'synthetic-claude', name: 'Synthetic Claude', chat_messages: [
        { uuid: 'claude-user', sender: 'human', content: [{ type: 'text', text: raw.slice(0, 8) }, { type: 'text', text: raw.slice(8) }] },
        { uuid: 'claude-answer', sender: 'assistant', content: [{ type: 'thinking', thinking: 'DO NOT IMPORT' }, { type: 'tool_use', id: 'tool-1', name: 'shell', input: 'DO NOT IMPORT' }, { type: 'tool_result', tool_use_id: 'tool-1', content: 'DO NOT IMPORT' }, { type: 'text', text: answer }] },
      ],
    };
  } else if (parsed.hostname === 'gemini.google.com') {
    if (parsed.pathname === '/app') return new Response('<script>{"SNlM0e":"csrf","FdrFJe":"sid","cfb2h":"build"}</script>');
    const id = parsed.searchParams.get('rpcids');
    if (id === 'MaZiqc') return new Response(rpc(id, [null, null, [['c_synthetic-gemini', 'Synthetic Gemini', null, null, null, null]]]));
    if (id === 'hNvQHb') return new Response(rpc(id, [[['gemini-native-turn', null, [[raw]], [[[null, [answer]]]]]], null]));
  }
  if (fixture !== undefined) return Response.json(fixture);
  throw new Error(`Unexpected synthetic request: ${url}`);
};
const packets = [];
const push = async (source, adapter, conversations) => {
  packets.push({ source, adapter, conversations });
  return conversations.length;
};
for (const sync of [syncChatGPT, syncClaude, syncGemini]) {
  const result = await sync({ state: {}, captureTypes: { projects: false, pinned: false, agents: false, recents: true }, push, log: message => { throw new Error(message); } });
  assert.equal(result.conversations, 1);
}
for (const packet of packets) {
  const conversation = packet.conversations[0];
  assert.equal(conversation.createdAt, null);
  assert.deepEqual(conversation.messages.map(m => m.content), [raw, answer]);
  assert.ok(conversation.messages.every(m => m.createdAt === null));
  assert.ok(conversation.messages.every(m => m.metadata?.id));
  assert.ok(!JSON.stringify(conversation).includes('DO NOT IMPORT'));
}
if (process.argv.includes('--json')) console.log(JSON.stringify(packets));
else console.log('PASS provider raw text, native IDs, visibility, and unknown metadata');
