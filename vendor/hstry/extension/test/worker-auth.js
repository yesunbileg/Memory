import assert from 'node:assert/strict';
import { checkConnection } from '../worker-auth.js';
const archiveId = 'a'.repeat(32),
  nativeHost = 'org.ai_memory.archive_' + archiveId;
const settings = { archiveId, nativeHost },
  calls = [];
const good = { status: 'ok', service: 'ai-memory', schema: 2, archive_instance_id: archiveId };
const sendNative = async (...args) => {
  calls.push(args);
  return good;
};
const token = 'synthetic-private-worker-token';
globalThis.fetch = () => {
  throw new Error('no HTTP check is permitted');
};
assert.equal((await checkConnection({}, { sendNative })).code, 'missing_token');
assert.equal(
  (await checkConnection({ token, port: 41770 }, { sendNative })).code,
  'legacy_pairing',
);
assert.equal(
  (await checkConnection({ ...settings, nativeHost: 'com.other.app' }, { sendNative })).code,
  'incompatible_server',
);
assert.equal(calls.length, 0);
assert.equal((await checkConnection(settings, { sendNative })).ok, true);
assert.deepEqual(calls, [[nativeHost, { op: 'check', archive_instance_id: archiveId }]]);
for (const [reply, code] of [
  [{}, 'incompatible_server'],
  [{ ...good, schema: 1 }, 'incompatible_server'],
  [{ ...good, service: 'other' }, 'incompatible_server'],
  [{ ...good, archive_instance_id: 'b'.repeat(32) }, 'archive_changed'],
  [{ error: 'unauthorized' }, 'unauthorized'],
]) {
  assert.equal((await checkConnection(settings, { sendNative: async () => reply })).code, code);
}
const offline = await checkConnection(settings, {
  sendNative: async () => {
    throw new Error(token);
  },
});
assert.equal(offline.code, 'offline');
assert.ok(!JSON.stringify(offline).includes(token));
