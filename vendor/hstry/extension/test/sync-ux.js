import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  describeEffectivePlan,
  formatDuration,
  formatSyncBreakdown,
  friendlyError,
  providerState,
} from '../ui.js';
import { PROVIDERS } from '../providers/index.js';

// 1. Offline recovery bug fix & friendly errors
assert.equal(
  providerState(
    {
      lastRunMs: 1_700_000_000_000,
      lastErrorCode: 'offline',
      lastError: 'AI Memory is offline. Run ai-memory connect and keep its terminal open.',
    },
    true,
    { apiConnected: true },
  ).label,
  'Ready to retry',
);
assert.match(friendlyError('', 'not_logged_in'), /sign in/i);
assert.match(friendlyError('', 'rate_limited'), /rate-limiting/i);
assert.match(friendlyError('', 'archive_unavailable'), /archive/i);
assert.match(friendlyError('', 'network_error'), /internet/i);

// 2. Effective capture plan preview
const emptyPlan = describeEffectivePlan({}, { projects: true, pinned: true, agents: true });
assert.equal(emptyPlan.readyCount, 0);
assert.match(emptyPlan.summary, /Turn on at least one website/i);

const geminiOnlyBlocked = describeEffectivePlan(
  { chatgpt: true, claude: false, gemini: true },
  { projects: true, pinned: true, agents: true, recents: false },
);
assert.equal(geminiOnlyBlocked.readyCount, 1);
assert.equal(geminiOnlyBlocked.items.find((i) => i.id === 'gemini').willCapture, false);
assert.match(
  geminiOnlyBlocked.items.find((i) => i.id === 'gemini').note,
  /turn on Ordinary Recents/i,
);

const geminiIncluded = describeEffectivePlan(
  { gemini: true },
  { projects: true, pinned: false, agents: false, recents: true },
);
assert.equal(geminiIncluded.readyCount, 1);
assert.equal(geminiIncluded.items[0].willCapture, true);

// 3. Breakdown & duration formatting
const breakdown = formatSyncBreakdown({
  running: true,
  progress: { detected: 20, processed: 10, created: 4, updated: 2 },
  selection: {
    skipped: 8,
    unknown: 1,
    byType: { projects: 5, pinned: 1, agents: 0, recents: 0 },
  },
});
assert.equal(breakdown.percent, 50);
assert.deepEqual(breakdown.chips, [
  { label: '+4 new', tone: 'success' },
  { label: '2 updated', tone: 'info' },
  { label: '8 skipped', tone: 'muted' },
  { label: '1 unclassified', tone: 'warning' },
]);
assert.deepEqual(breakdown.categoryChips, ['Projects: 5', 'Pinned: 1']);
assert.equal(formatDuration(450), '450ms');
assert.equal(formatDuration(3400), '3.4s');
assert.equal(formatDuration(75_000), '1m 15s');

// 4. Single-provider sync & safe activityLog in background.js
const token = 'synthetic-ux-private-token-123456';
const archiveId = 'a'.repeat(32),
  nativeHost = 'org.ai_memory.archive_' + archiveId;
let settings = {
  archiveId,
  nativeHost,
  port: 41770,
  token,
  providers: { chatgpt: true, claude: true, gemini: false },
  captureTypes: { projects: true, pinned: true, agents: true, recents: false },
};
let status = {};
let activityLog = [];
let listener;
globalThis.chrome = {
  storage: {
    local: {
      get: async (keys) => {
        const list = typeof keys === 'string' ? [keys] : keys;
        const out = {};
        for (const k of list) {
          if (k === 'settings') out.settings = structuredClone(settings);
          if (k === 'status') out.status = structuredClone(status);
          if (k === 'activityLog') out.activityLog = structuredClone(activityLog);
        }
        return out;
      },
      set: async (values) => {
        if (values.settings) settings = structuredClone(values.settings);
        if (values.status) status = structuredClone(values.status);
        if (values.activityLog) activityLog = structuredClone(values.activityLog);
      },
    },
  },
  runtime: {
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: {
      addListener(fn) {
        listener = fn;
      },
    },
  },
  alarms: { onAlarm: { addListener() {} }, get: async () => null, create: async () => {} },
  action: {
    onClicked: { addListener() {} },
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
  },
};

const originalChatgptSync = PROVIDERS.chatgpt.sync;
const originalClaudeSync = PROVIDERS.claude.sync;
let claudeSynced = false;
PROVIDERS.chatgpt.sync = async ({ push, report }) => {
  const selection = {
    selected: 1,
    skipped: 2,
    unknown: 0,
    byType: { projects: 1, pinned: 0, agents: 0, recents: 0 },
  };
  await report({ detected: 3, processed: 1, selection });
  await push('chatgpt-web', 'chatgpt-web', [{ externalId: 'secret-chat-id', messages: [] }]);
  return { state: { ok: true }, conversations: 1, selection, selectionWarning: null };
};
PROVIDERS.claude.sync = async () => {
  claudeSynced = true;
  return { state: {}, conversations: 0 };
};
chrome.runtime.sendNativeMessage = async (host, packet) => {
  assert.equal(host, nativeHost);
  if (packet.op === 'check')
    return { status: 'ok', service: 'ai-memory', schema: 2, archive_instance_id: archiveId };
  return { conversations: 1, created: 1, updated: 0, unchanged: 0 };
};
await import(`../background.js?ux-test=${Date.now()}`);
const send = (msg) =>
  new Promise((resolve) => {
    listener(msg, {}, resolve);
  });

const singleResp = await send({ type: 'syncProvider', provider: 'chatgpt' });
assert.equal(singleResp.ok, true);
for (let i = 0; i < 100 && status.chatgpt?.running; i++) {
  await new Promise((r) => setTimeout(r, 2));
}
assert.equal(claudeSynced, false, 'syncProvider must only sync the requested provider');
assert.equal(activityLog.length, 1);
assert.equal(activityLog[0].provider, 'chatgpt');
assert.equal(activityLog[0].outcome, 'success');
assert.equal(activityLog[0].counts.created, 1);
assert.equal(activityLog[0].counts.skipped, 2);
assert.equal(JSON.stringify(activityLog).includes(token), false);
assert.equal(JSON.stringify(activityLog).includes('secret-chat-id'), false);

await send({ type: 'clearActivityLog' });
assert.equal(activityLog.length, 0);
PROVIDERS.chatgpt.sync = originalChatgptSync;
PROVIDERS.claude.sync = originalClaudeSync;

// 5. Popup interactive toggles & quick filters
const popupHtml = await readFile(new URL('../popup.html', import.meta.url), 'utf8');
const popupElements = new Map();
class PopupElement {
  constructor(id = '') {
    this.id = id;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.className = '';
    this.textContent = '';
    this.children = [];
    this.listeners = new Map();
    this.classList = {
      add: (c) => {
        this.className = `${this.className} ${c}`.trim();
      },
    };
    if (id) popupElements.set(id, this);
  }
  set innerHTML(_html) {}
  querySelector(selector) {
    if (!this._q) this._q = new Map();
    if (!this._q.has(selector)) this._q.set(selector, new PopupElement());
    return this._q.get(selector);
  }
  replaceChildren() {
    this.children = [];
  }
  appendChild(child) {
    this.children.push(child);
  }
  addEventListener(type, fn) {
    this.listeners.set(type, fn);
  }
  setAttribute(k, v) {
    this[k] = v;
  }
}
for (const match of popupHtml.matchAll(/id="([^"]+)"/g)) new PopupElement(match[1]);
globalThis.document = {
  getElementById: (id) => popupElements.get(id) ?? null,
  createElement: () => new PopupElement(),
};
const popupSent = [];
globalThis.chrome.storage.onChanged = { addListener() {} };
globalThis.chrome.runtime.sendMessage = async (msg) => {
  popupSent.push(msg.type);
  return msg.type === 'checkApi' ? { ok: true } : { ok: true };
};
globalThis.chrome.runtime.openOptionsPage = async () => {};

await import(`../popup.js?ux-test=${Date.now()}`);
assert.equal(popupElements.get('api-title').textContent, 'Connected to your computer');
assert.equal(popupElements.get('providers').children.length, 3);
assert.equal(popupElements.get('quick-filters').children.length, 4);

// Toggle Recents chip in popup
const recentsChip = popupElements.get('quick-filters').children[3];
recentsChip.listeners.get('click')();
for (let i = 0; i < 50 && !popupSent.includes('settingsChanged'); i++) {
  await new Promise((r) => setTimeout(r, 2));
}
assert.equal(settings.captureTypes.recents, true);
assert.ok(popupSent.includes('settingsChanged'));

console.log(
  'PASS web sync UX: offline recovery, capability plans, single-provider sync, safe log, popup controls',
);
