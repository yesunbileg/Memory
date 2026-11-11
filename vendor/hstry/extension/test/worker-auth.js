// Simulated loopback HTTP: authentication, identity, and privacy at the worker boundary.
import assert from 'node:assert/strict';
import { checkConnection } from '../worker-auth.js';

const settings = { port: 41770, token: 'synthetic-private-ingest-token-123456' };
let calls = [];
const fetchImpl = async (url, options) => {
  calls.push({ url, options });
  return new Response(JSON.stringify({ status: 'ok', service: 'ai-memory', schema: 1 }));
};
const missing = await checkConnection({ ...settings, token: '' }, { fetchImpl });
assert.equal(missing.ok, false);
assert.equal(missing.code, 'missing_token');
assert.equal(calls.length, 0);

const valid = await checkConnection(settings, { fetchImpl });
assert.equal(valid.ok, true);
assert.equal(calls.length, 1);
assert.equal(calls[0].url, 'http://127.0.0.1:41770/auth/check');
assert.equal(calls[0].options.method, 'POST');
assert.equal(calls[0].options.headers.Authorization, `Bearer ${settings.token}`);
assert.equal(calls[0].options.body, '{}');
assert.equal(calls[0].options.redirect, 'error');
assert.equal(JSON.stringify(valid).includes(settings.token), false);

for (const status of [401, 403]) {
  const unauthorized = await checkConnection(settings, { fetchImpl: async () => new Response('', { status }) });
  assert.equal(unauthorized.ok, false);
  assert.equal(unauthorized.code, 'unauthorized');
}
for (const body of [{ status: 'ok' }, { status: 'ok', service: 'other-app', schema: 1 },
  { status: 'ok', service: 'ai-memory', schema: 2 }]) {
  const wrongApp = await checkConnection(settings, { fetchImpl: async () => new Response(JSON.stringify(body)) });
  assert.equal(wrongApp.ok, false);
  assert.equal(wrongApp.code, 'incompatible_server');
}
const wrongRoute = await checkConnection(settings, { fetchImpl: async () => new Response('', { status: 404 }) });
assert.equal(wrongRoute.code, 'incompatible_server');
const offline = await checkConnection(settings, { fetchImpl: async () => {
  throw new Error(`Unsafe error contains ${settings.token}`);
} });
assert.equal(offline.code, 'offline');
assert.equal(JSON.stringify(offline).includes(settings.token), false);

calls = [];
const tampered = await checkConnection({ ...settings, port: '41770@evil.example' }, { fetchImpl });
assert.equal(tampered.ok, false);
assert.equal(calls.length, 0);
console.log('PASS authenticated loopback connection checks and sanitized failure codes');
