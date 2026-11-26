// Execute the actual browser driver in a controlled synthetic browser environment.
// This proves client-side decisions; it cannot prove an atomic Google server CAS.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const driver = readFileSync(new URL('../../src/ai_memory/web_docs_driver.js', import.meta.url), 'utf8');
const id = 'synthetic-document-id-123456';
const origin = 'https://docs.google.com';
const address = `${origin}/document/d/${id}/save?sid=synthetic`;
const command = [{ ty: 'mlti', mts: [{ ty: 'is', ibi: 1, s: 'new' }] }];
const input = { action: 'write', id, transaction: 'tx-1', before: 'old-hash', after: 'new-hash', commands: command, recover: false };

function makeBrowser({ hash = input.before, revision = 7, recovery = false, pending = null, failRead = false, ack = 'normal', cache = true, cachedHash = input.before, bootstrapTimeout = false, signedOut = false } = {}) {
  const stats = { reads: 0, posts: [], closed: 0 };
  const remote = { hash, revision };
  const state = { memoryWebDocs: {} };
  const item = {
    knownHash: cachedHash, revision: 7, url: address, requestId: 0, pending,
    page: {
      isClosed: () => false,
      close: async () => { stats.closed++; },
      evaluate: async (_fn, args) => {
        stats.posts.push(args.body);
        if (ack === 'lost') throw new Error('response lost');
        if (ack === 'transformed') return { http: 200, metadata: { needsTransformOnClient: true, serverRevision: 8 }, revisionRanges: [[9,9]] };
        return { http: 200, metadata: { needsTransformOnClient: false, serverRevision: 7 }, revisionRanges: [[8,8]] };
      },
    },
  };
  if (cache) state.memoryWebDocs[id] = item;
  const context = {
    newPage: async () => {
      let closed = false;
      return {
        addInitScript: async () => {},
        waitForRequest: async () => {
          if (bootstrapTimeout) {
            bootstrapTimeout = false;
            throw Object.assign(new Error("Synthetic editor bootstrap stalled"), { name: "TimeoutError" });
          }
          return { url: () => `${origin}/document/d/${id}/docos/p/uc?id=${id}&sid=synthetic&token=synthetic` };
        },
        goto: async () => signedOut ? { status: () => 401 } : undefined,
        url: () => signedOut ? "https://accounts.google.com/signin" : origin,
        waitForLoadState: async () => {},
        locator: () => ({ waitFor: async () => {} }),
        evaluate: async (fn, args) => {
          if (args?.body) return item.page.evaluate(fn, args);
          stats.reads++;
          if (failRead) throw new Error('model unavailable');
          return { ...remote };
        },
        isClosed: () => closed,
        close: async () => { closed = true; stats.closed++; },
      };
    },
  };
  async function run(action = "write") {
    let result;
    await runInNewContext(`(async () => {\n${driver}\n})()`, {
      input: { ...input, action, recover: recovery }, state, context, URL, URLSearchParams, Buffer,
      console: { log: line => { result = JSON.parse(line.split('AI_MEMORY_DOCS_RESULT=')[1]); } },
    });
    return result;
  }
  return { run, stats, state, item };
}

test('release closes settled editor and export tabs, retaining an ambiguous write', async () => {
  const ordinary = makeBrowser();
  ordinary.state.memoryDocsExportPage = {
    isClosed: () => false,
    close: async () => { ordinary.stats.closed++; },
  };
  assert.deepEqual(await ordinary.run('release'), { status: 'released', pending: 0 });
  assert.equal(ordinary.stats.closed, 2);
  assert.equal(ordinary.state.memoryWebDocs[id], undefined);
  assert.equal(ordinary.state.memoryDocsExportPage, undefined);

  const uncertain = makeBrowser({ pending: { transaction: 'tx-1', body: 'pending-packet' } });
  assert.deepEqual(await uncertain.run('release'), { status: 'released', pending: 1 });
  assert.equal(uncertain.stats.closed, 0);
  assert.equal(uncertain.state.memoryWebDocs[id], uncertain.item);
});

test('stale cached hash: fresh read rejects before POST', async () => {
  const b = makeBrowser({ hash: 'external-hash', revision: 8 });
  assert.deepEqual(await b.run(), { status: 'conflict' });
  assert.equal(b.stats.reads, 1);
  assert.equal(b.stats.posts.length, 0);
  assert.equal(b.item.pending, null);
});

test('same text but new remote revision: reject before POST', async () => {
  const b = makeBrowser({ revision: 8 });
  assert.deepEqual(await b.run(), { status: 'conflict' });
  assert.equal(b.stats.reads, 1);
  assert.equal(b.stats.posts.length, 0);
});

test('normal write: fresh read precedes exactly one POST and ACK advances cache', async () => {
  const b = makeBrowser();
  const result = await b.run();
  assert.equal(result.status, 'submitted');
  assert.equal(b.stats.reads, 1);
  assert.equal(b.stats.posts.length, 1);
  assert.equal(b.item.knownHash, input.after);
  assert.equal(b.item.revision, 8);
  assert.equal(b.item.pending, null);
  assert.equal(result.bytes, Buffer.byteLength(b.stats.posts[0]));
});

test('a stalled editor bootstrap retries the read, then sends only one write', async () => {
  const b = makeBrowser({ bootstrapTimeout: true, cache: false });
  assert.equal((await b.run()).status, 'submitted');
  assert.equal(b.stats.reads, 2);
  assert.equal(b.stats.posts.length, 1);
  assert.equal(b.stats.closed, 2);
});

test('read-only model checks do not wait for an editing session to open', async () => {
  const b = makeBrowser({ bootstrapTimeout: true });
  assert.deepEqual(await b.run('check'), { hash: input.before, revision: 7 });
  assert.equal(b.stats.reads, 1);
  assert.equal(b.stats.closed, 1);
});

test('a signed-out editor reports authentication before fetching or saving the model', async () => {
  const b = makeBrowser({ signedOut: true });
  assert.deepEqual(await b.run('check'), { status: 'auth_required' });
  assert.equal(b.stats.reads, 0);
  assert.equal(b.stats.posts.length, 0);
  assert.equal(b.stats.closed, 1);
});

test('lost ACK after commit: matching fresh hash reconciles without POST', async () => {
  const b = makeBrowser({ hash: input.after, revision: 8, recovery: true, pending: { transaction: input.transaction, body: 'old-packet' } });
  assert.deepEqual(await b.run(), { status: 'committed', bytes: 0 });
  assert.equal(b.stats.posts.length, 0);
  assert.equal(b.state.memoryWebDocs[id], undefined);
});

test('lost ACK before commit: retry reuses the exact pending packet', async () => {
  const b = makeBrowser({ recovery: true, pending: { transaction: input.transaction, body: 'original-packet' } });
  assert.equal((await b.run()).status, 'submitted');
  assert.deepEqual(b.stats.posts, ['original-packet']);
  assert.equal(b.item.pending, null);
});

test('lost session and uncommitted journal: do not invent a replacement packet', async () => {
  const b = makeBrowser({ recovery: true, cache: false });
  assert.deepEqual(await b.run(), { status: 'uncertain' });
  assert.equal(b.stats.reads, 2);
  assert.equal(b.stats.posts.length, 0);
});

test('failed fresh model read: classified as not sent, never POST', async () => {
  const b = makeBrowser({ failRead: true });
  assert.deepEqual(await b.run(), { status: 'not_sent' });
  assert.equal(b.stats.posts.length, 0);
});

test('server transformation ACK is not accepted as the new cached state', async () => {
  const b = makeBrowser({ ack: 'transformed' });
  const result = await b.run();
  assert.equal(result.status, 'submitted');
  assert.equal(result.ack.metadata.needsTransformOnClient, true);
  assert.equal(b.item.knownHash, input.before);
  assert.equal(b.item.revision, 7);
  assert.equal(b.item.pending.transaction, input.transaction);
});

test('another transaction cannot reuse a pending packet', async () => {
  const b = makeBrowser({ pending: { transaction: 'different', body: 'different-packet' } });
  assert.deepEqual(await b.run(), { status: 'uncertain' });
  assert.equal(b.stats.reads, 0);
  assert.equal(b.stats.posts.length, 0);
});
