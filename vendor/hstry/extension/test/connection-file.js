import assert from 'node:assert/strict';
import { parseConnectionFile, connectionPresentation } from '../connection-file.js';
import { normalizeSettings } from '../ui.js';

const token = 'synthetic-private-connection-token';
const config = { schema: 1, application: 'ai-memory', host: '127.0.0.1', port: 41770, token };
assert.deepEqual(parseConnectionFile(JSON.stringify(config)), { port: 41770, token });
for (const patch of [
  { host: 'example.com' },
  { host: 'localhost.evil.test' },
  { schema: 2 },
  { application: 'other' },
  { port: 0 },
  { port: 65536 },
  { port: '41770' },
  { token: '' },
  { token: 'a'.repeat(5000) },
  { token: 'a'.repeat(513) },
  { token: `${token}\n` },
  { token: 'нууц'.repeat(8) },
]) {
  assert.throws(
    () => parseConnectionFile(JSON.stringify({ ...config, ...patch })),
    /connection file/i,
  );
}
assert.throws(() => parseConnectionFile('not JSON'), /connection file/i);
assert.throws(() => parseConnectionFile(' '.repeat(20_000)), /connection file/i);
assert.throws(() => parseConnectionFile('null'), /connection file/i);
assert.equal(connectionPresentation({ ok: true }).tone, 'success');
assert.equal(connectionPresentation({ ok: false, code: 'missing_token' }).tone, 'idle');
assert.match(connectionPresentation({ ok: false, code: 'unauthorized' }).detail, /file|token/i);
const unsafe = connectionPresentation({ ok: false, code: 'offline', error: token });
assert.ok(!JSON.stringify(unsafe).includes(token));
const settings = normalizeSettings(
  { port: 41770, intervalMinutes: 15, providers: { chatgpt: true } },
  { port: 41770, intervalMinutes: 15 },
);
assert.deepEqual(settings.captureTypes, {
  projects: true,
  pinned: true,
  agents: true,
  recents: false,
});
const selections = normalizeSettings(
  { ...settings, captureTypes: { projects: false, pinned: true, agents: false, recents: false } },
  settings,
);
assert.deepEqual(selections.captureTypes, {
  projects: false,
  pinned: true,
  agents: false,
  recents: false,
});
console.log(
  'PASS private loopback connection file validation, clear auth states, and capture settings migration',
);
