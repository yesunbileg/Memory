// AI Memory boundary: supported providers, local-only destination, required token.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from '../providers/index.js';

assert.deepEqual(Object.keys(PROVIDERS), ['chatgpt', 'claude', 'gemini']);
assert.deepEqual(DEFAULT_PROVIDER_SETTINGS, { chatgpt: false, claude: false, gemini: false });
const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'));
assert.deepEqual(manifest.host_permissions, [
  'https://chatgpt.com/*', 'https://chat.openai.com/*', 'https://claude.ai/*',
  'https://gemini.google.com/*', 'http://127.0.0.1/*',
]);

let settings = { port: 41770, token: '', providers: { chatgpt: true } };
let status = {};
let messageListener;
const writes = [];
globalThis.chrome = {
  storage: { local: {
    get: async key => key === 'settings' ? { settings } : { status },
    set: async value => { if (value.status) { status = structuredClone(value.status); writes.push(status); } },
  } },
  runtime: {
    onInstalled: { addListener() {} }, onStartup: { addListener() {} },
    onMessage: { addListener(listener) { messageListener = listener; } },
  },
  alarms: { onAlarm: { addListener() {} }, get: async () => null, create: async () => {} },
  action: { onClicked: { addListener() {} }, setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
};
const calls = [];
globalThis.fetch = async url => { calls.push(url); throw new Error('No request should occur without an ingest token'); };
const realWarn = console.warn;
console.warn = () => {};
await import('../background.js');
messageListener({ type: 'syncNow' }, {}, () => {});
for (let turn = 0; turn < 100; turn++) {
  await new Promise(resolve => setTimeout(resolve, 1));
  if (writes.some(state => state.chatgpt?.lastError)) break;
}
assert.equal(calls.length, 0);
assert.match(status.chatgpt.lastError, /token/i);
assert.equal(status.chatgpt.running, false);
console.warn = realWarn;
console.log('PASS local-only provider defaults and required ingest token');
