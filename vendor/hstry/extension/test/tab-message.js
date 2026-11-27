import assert from 'node:assert/strict';
import test from 'node:test';
import { sendProviderMessage } from '../lib/tab-message.js';

function fixture(t, url = 'https://chatgpt.com/') {
  const calls = { send: 0, inject: 0 };
  let ready = false;
  const old = globalThis.chrome;
  t.after(() => {
    globalThis.chrome = old;
  });
  globalThis.chrome = {
    tabs: {
      get: async () => ({ url, discarded: false }),
      sendMessage: async (tabId, message, target) => {
        calls.send++;
        assert.equal(tabId, 7);
        assert.equal(target.frameId, 0);
        if (!ready) throw Error('Could not establish connection. Receiving end does not exist.');
        return { ok: true };
      },
    },
    scripting: {
      executeScript: async ({ target, files, world }) => {
        calls.inject++;
        assert.deepEqual(target, { tabId: 7, frameIds: [0] });
        assert.deepEqual(files, ['page-picker-routes.js', 'page-picker.js']);
        assert.equal(world, 'ISOLATED');
        await new Promise((resolve) => setTimeout(resolve, 5));
        ready = true;
      },
    },
  };
  return calls;
}
const ctx = { tabId: 7, provider: 'chatgpt', slot: null };
const message = { type: 'GET_CHATGPT_TAB_SESSION' };

test('missing content script recovers once and retries', async (t) => {
  const calls = fixture(t);
  assert.deepEqual(await sendProviderMessage(ctx, message), { ok: true });
  assert.deepEqual(calls, { send: 2, inject: 1 });
});
test('concurrent missing receivers share one recovery', async (t) => {
  const calls = fixture(t);
  await Promise.all([sendProviderMessage(ctx, message), sendProviderMessage(ctx, message)]);
  assert.deepEqual(calls, { send: 4, inject: 1 });
});
test('navigation to another provider cannot authorize recovery', async (t) => {
  const calls = fixture(t, 'https://claude.ai/new');
  await assert.rejects(sendProviderMessage(ctx, message), /Provider tab changed/);
  assert.equal(calls.inject, 0);
});
test('unrelated failure is not retried', async (t) => {
  const calls = fixture(t);
  chrome.tabs.sendMessage = async () => {
    throw Error('Unknown failure');
  };
  await assert.rejects(sendProviderMessage(ctx, message), /Unknown failure/);
  assert.equal(calls.inject, 0);
});
