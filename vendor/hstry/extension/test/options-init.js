// Smoke test that the options module creates dynamic controls before wiring them.
// Usage: bun extension/test/options-init.js

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

export const elements = new Map();
export const storageListeners = [];

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.dataset = {};
    this.className = '';
    this.textContent = '';
    this.children = [];
    this.queries = new Map();
    this.listeners = new Map();
    if (id) elements.set(id, this);
  }

  set innerHTML(html) {
    for (const match of html.matchAll(/id="([^"]+)"/g)) new FakeElement(match[1]);
    for (const selector of ['small', '.provider-domain', 'p', 'strong', 'button', '.status-time']) {
      this.queries.set(selector, new FakeElement());
    }
  }

  querySelector(selector) {
    return this.queries.get(selector) ?? null;
  }

  replaceChildren() {
    this.children = [];
  }

  appendChild(child) {
    this.children.push(child);
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  setAttribute(name, value) {
    this[name] = value;
  }
}

const html = await readFile(new URL('../options.html', import.meta.url), 'utf8');
for (const match of html.matchAll(/id="([^"]+)"/g)) new FakeElement(match[1]);

globalThis.document = {
  getElementById: id => elements.get(id) ?? null,
  createElement: () => new FakeElement(),
};
globalThis.chrome = {
  runtime: {
    sendMessage: async message =>
      message.type === 'checkApi' ? { ok: false, code: 'missing_token' } : { ok: true },
  },
  storage: {
    local: {
      get: async () => ({ settings: undefined, status: {} }),
      set: async () => {},
    },
    onChanged: { addListener: listener => storageListeners.push(listener) },
  },
};

await import(`../options.js?options-init-test=${Date.now()}`);

for (const provider of ['chatgpt', 'claude', 'gemini']) {
  const checkbox = elements.get(`provider-${provider}`);
  assert.ok(checkbox, `${provider} checkbox was created`);
  assert.ok(checkbox.listeners.has('change'), `${provider} change listener was registered`);
}

for (const type of ['projects', 'pinned', 'agents', 'recents']) {
  const checkbox = elements.get(`capture-${type}`);
  assert.ok(checkbox, `${type} checkbox exists`);
  assert.ok(checkbox.listeners.has('change'), `${type} saves on change`);
  assert.equal(checkbox.checked, type !== 'recents');
}
assert.equal(elements.get('connection-label').textContent, 'Not connected');
assert.equal(elements.get('sync-now').disabled, true);
assert.equal(elements.get('connection-guide').hidden, false);
assert.ok(elements.get('import-connection').listeners.has('click'));
console.log('PASS guided options initializes providers, category checklist, connection import and honest missing-token state');
