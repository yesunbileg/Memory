import assert from 'node:assert/strict';
import { fetchJson, RateLimitedError } from '../lib/common.js';

let calls = 0;
globalThis.fetch = async () => {
  calls++;
  if (calls <= 2) return new Response('rl', { status: 429, headers: { 'retry-after': '0' } });
  return new Response(JSON.stringify({ ok: true, calls }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
};
const data = await fetchJson('https://example.com/x', {}, { retries: 5, baseDelayMs: 20 });
console.log(
  data.ok && data.calls === 3 ? 'PASS recovered after 2x 429' : `FAIL ${JSON.stringify(data)}`,
);

calls = 0;
globalThis.fetch = async () =>
  new Response('nope', { status: 429, headers: { 'retry-after': '0' } });
try {
  await fetchJson('https://example.com/y', {}, { retries: 2, baseDelayMs: 5 });
  console.log('FAIL should have thrown');
} catch (e) {
  console.log(
    e instanceof RateLimitedError
      ? 'PASS gives up as RateLimitedError'
      : `FAIL wrong error: ${e.name}`,
  );
}

const stopping = new AbortController();
let cancelledCalls = 0;
const backoff = fetchJson(
  'https://example.com/stop',
  { signal: stopping.signal },
  {
    fetcher: async () => {
      cancelledCalls++;
      return new Response('', { status: 429, headers: { 'retry-after': '60' } });
    },
  },
);
await Promise.resolve();
stopping.abort(new Error('Sync stopped.'));
await assert.rejects(
  Promise.race([backoff, new Promise((r) => setTimeout(r, 100))]),
  /Sync stopped/,
);
assert.equal(cancelledCalls, 1);
