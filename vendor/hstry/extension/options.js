import { CAPTURE_TYPES, DEFAULT_CAPTURE_TYPES, DEFAULT_PROVIDER_SETTINGS, PROVIDERS, formatTime, normalizeSettings, providerState } from './ui.js';
import { connectionPresentation, parseConnectionFile } from './connection-file.js';

const DEFAULTS = { port: 41770, token: '', account: 'browser-local', intervalMinutes: 15, providers: DEFAULT_PROVIDER_SETTINGS, captureTypes: DEFAULT_CAPTURE_TYPES };
const el = id => document.getElementById(id);
const marks = { chatgpt: '◎', claude: '✳︎', gemini: '✦' };
const typeCopy = {
  projects: ['Projects & folders', 'Chats organized inside a project or folder.'],
  pinned: ['Pinned & favorites', 'Conversations you’ve chosen to keep close.'],
  agents: ['GPTs & agents', 'Chats with a custom GPT or agent.'],
  recents: ['Ordinary Recents', 'Everyday chats outside your organized spaces.'],
};
let currentSettings = DEFAULTS;
let apiConnected = false;
let checking = false;
let connectionCheck = 0;

function sameConnection(a, b) { return a.port === b.port && a.token === b.token; }

function feedback(id, text, error = false) {
  el(id).textContent = text;
  el(id).className = `inline-feedback${error ? ' error' : ''}`;
}

function renderControls() {
  for (const [id, provider] of Object.entries(PROVIDERS)) {
    const row = document.createElement('label');
    row.className = 'provider-row';
    row.innerHTML = `<span class="provider-mark ${id}" aria-hidden="true">${marks[id]}</span><span class="row-copy"><strong>${provider.name}</strong><small>${provider.site}</small></span><span class="switch"><input id="provider-${id}" type="checkbox" aria-label="Capture ${provider.name} chats" /></span>`;
    el('provider-list').appendChild(row);
  }
  for (const id of Object.keys(CAPTURE_TYPES)) {
    const row = document.createElement('label');
    row.className = 'capture-row';
    row.innerHTML = `<input id="capture-${id}" type="checkbox" /><span class="row-copy"><strong>${typeCopy[id][0]}</strong><small>${typeCopy[id][1]}</small></span>${id === 'recents' ? '<span class="small-tag">OFF BY DEFAULT</span>' : ''}`;
    el('capture-list').appendChild(row);
  }
}

function readSettings() {
  return normalizeSettings({
    port: el('port').value, token: el('token').value, account: el('account').value,
    intervalMinutes: el('interval').value,
    providers: Object.fromEntries(Object.keys(PROVIDERS).map(id => [id, el(`provider-${id}`).checked])),
    captureTypes: Object.fromEntries(Object.keys(CAPTURE_TYPES).map(id => [id, el(`capture-${id}`).checked])),
  }, DEFAULTS);
}

async function persist() {
  if (el('port').checkValidity && !el('port').checkValidity()) {
    el('port').reportValidity();
    throw new Error('Check the port number in manual settings.');
  }
  currentSettings = readSettings();
  await chrome.storage.local.set({ settings: currentSettings });
  await chrome.runtime.sendMessage({ type: 'settingsChanged' });
  updateSelection();
}

function updateSelection() {
  const types = currentSettings.captureTypes;
  el('selection-summary').textContent = types.recents
    ? 'Ordinary Recents are included. Only the chat types you checked will be captured.'
    : 'Ordinary Recents stay out. A project or pinned chat is still included even when it also appears in Recents.';
  const selected = Object.values(types).some(Boolean);
  const enabled = Object.values(currentSettings.providers).some(Boolean);
  el('sync-now').disabled = !apiConnected || !selected || !enabled || checking;
}

async function checkConnection() {
  const generation = ++connectionCheck;
  const requested = { port: currentSettings.port, token: currentSettings.token };
  checking = true;
  el('test-connection').disabled = true;
  el('connection-label').textContent = 'Checking…';
  el('connection-pill').className = 'status-pill checking';
  el('connection-dot').className = 'status-dot checking';
  updateSelection();
  let result;
  try { result = await chrome.runtime.sendMessage({ type: 'checkApi' }); }
  catch { result = { ok: false, code: 'offline' }; }
  if (generation !== connectionCheck || !sameConnection(requested, currentSettings)) return false;
  const state = connectionPresentation(result);
  apiConnected = result?.ok === true;
  el('connection-title').textContent = state.title;
  el('connection-result').textContent = state.detail;
  el('connection-label').textContent = state.label;
  el('connection-pill').className = `status-pill ${state.tone}`;
  el('connection-dot').className = `status-dot ${state.tone}`;
  el('connection-guide').hidden = apiConnected;
  el('connection-ready').hidden = !apiConnected;
  el('test-connection').disabled = false;
  checking = false;
  updateSelection();
  await renderStatus();
  return apiConnected;
}

async function renderStatus() {
  const { status = {} } = await chrome.storage.local.get('status');
  const list = el('status-list');
  list.replaceChildren();
  const rescans = el('resync-list');
  rescans.replaceChildren();
  for (const [id, provider] of Object.entries(PROVIDERS)) {
    const entry = status[id];
    const state = providerState(entry, currentSettings.providers[id], { apiConnected, captureTypes: currentSettings.captureTypes });
    const item = document.createElement('div');
    item.className = 'status-item';
    item.innerHTML = `<span class="status-dot ${state.tone}"></span><div class="status-content"><strong>${provider.name} · ${state.label}</strong><p></p></div><span class="status-time"></span>`;
    const detail = item.querySelector('p');
    detail.textContent = state.detail;
    if (state.tone === 'error') detail.className = 'error-copy';
    if (state.tone === 'warning') detail.className = 'warning-copy';
    item.querySelector('.status-time').textContent = formatTime(entry?.lastSuccessMs ?? entry?.lastRunMs);
    list.appendChild(item);
    const row = document.createElement('div');
    row.className = 'resync-row';
    row.innerHTML = `<div><strong>${provider.name}</strong><small>Uses your current checklist</small></div><button class="button-secondary" type="button">Rescan selected chats</button>`;
    const button = row.querySelector('button');
    button.dataset.provider = id;
    button.disabled = !apiConnected || !Object.values(currentSettings.captureTypes).some(Boolean) || !currentSettings.providers[id] || Boolean(entry?.running);
    button.setAttribute('aria-label', `${provider.name}: rescan selected chats`);
    rescans.appendChild(row);
  }
}

async function capture() {
  try {
    await persist();
    if (!(await checkConnection())) return;
    if (!Object.values(currentSettings.providers).some(Boolean) || !Object.values(currentSettings.captureTypes).some(Boolean)) return;
    const response = await chrome.runtime.sendMessage({ type: 'syncNow' });
    feedback('saved', response?.ok ? 'Capturing your selected chats. You can close this page.' : 'Capture did not start. Check your connection.', !response?.ok);
    await renderStatus();
  } catch { feedback('saved', 'Could not start capture. Check your connection and try again.', true); }
}

renderControls();
for (const id of [...Object.keys(PROVIDERS).map(id => `provider-${id}`), ...Object.keys(CAPTURE_TYPES).map(id => `capture-${id}`), 'account', 'interval']) {
  el(id).addEventListener('change', async () => {
    try { await persist(); feedback('saved', 'Your selection is saved.'); await renderStatus(); }
    catch { feedback('saved', 'Could not save. Check your settings and try again.', true); }
  });
}
el('save').addEventListener('click', async () => {
  try { await persist(); await checkConnection(); }
  catch { feedback('import-result', 'Check the port and connection token.', true); }
});
el('test-connection').addEventListener('click', async () => {
  try { await persist(); await checkConnection(); }
  catch { feedback('import-result', 'Check your manual connection settings.', true); }
});
el('sync-now').addEventListener('click', capture);
el('copy-command').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText('.venv/bin/ai-memory connect'); feedback('import-result', 'Command copied. Run it in your Memory folder.'); }
  catch { feedback('import-result', 'Select and copy the command above.'); }
});
el('import-connection').addEventListener('click', () => el('connection-file').click());
el('change-connection').addEventListener('click', () => { el('connection-guide').hidden = false; el('connection-ready').hidden = true; el('import-connection').focus(); });
el('connection-file').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    if (file.size > 16_384) throw new Error('Invalid connection file');
    const connection = parseConnectionFile(await file.text());
    el('port').value = connection.port;
    el('token').value = connection.token;
    await persist();
    const connected = await checkConnection();
    feedback('import-result', connected ? 'Connection file verified. You’re ready.' : 'File imported. Keep the local app running and check the connection again.', !connected);
  } catch { feedback('import-result', 'Choose extension-connection.json created by AI Memory on this computer.', true); }
  finally { event.target.value = ''; }
});
el('resync-list').addEventListener('click', async event => {
  const button = event.target.closest?.('button[data-provider]');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    await persist();
    if (!(await checkConnection())) return;
    const response = await chrome.runtime.sendMessage({ type: 'fullSyncProvider', provider: button.dataset.provider });
    feedback('resync-result', response?.ok ? 'Rescanning only your selected chat types.' : 'Rescan did not start. Check the source and try again.', !response?.ok);
  } catch { feedback('resync-result', 'Could not start the rescan. Try again.', true); }
  finally { await renderStatus(); }
});
chrome.storage.onChanged.addListener(changes => {
  if (changes.settings?.newValue) {
    const values = changes.settings.newValue;
    const next = normalizeSettings({ ...DEFAULTS, ...values, providers: { ...DEFAULT_PROVIDER_SETTINGS, ...values.providers } }, DEFAULTS);
    const authChanged = !sameConnection(currentSettings, next);
    currentSettings = next;
    for (const id of ['port', 'token', 'account']) el(id).value = next[id];
    el('interval').value = next.intervalMinutes;
    for (const id of Object.keys(PROVIDERS)) el(`provider-${id}`).checked = next.providers[id];
    for (const id of Object.keys(CAPTURE_TYPES)) el(`capture-${id}`).checked = next.captureTypes[id];
    if (authChanged) {
      connectionCheck++;
      apiConnected = false;
      checkConnection();
    } else { updateSelection(); renderStatus(); }
  }
  if (changes.status) renderStatus();
});
for (const id of ['port', 'token']) el(id).addEventListener('input', () => {
  connectionCheck++;
  apiConnected = false;
  checking = false;
  el('connection-title').textContent = 'Verify your connection';
  el('connection-result').textContent = 'Save & verify your updated connection settings.';
  el('connection-label').textContent = 'Not verified';
  el('connection-pill').className = 'status-pill idle';
  el('connection-dot').className = 'status-dot idle';
  el('test-connection').disabled = false;
  updateSelection();
  renderStatus();
});

const { settings = {} } = await chrome.storage.local.get('settings');
currentSettings = normalizeSettings({ ...DEFAULTS, ...settings, providers: { ...DEFAULT_PROVIDER_SETTINGS, ...settings.providers } }, DEFAULTS);
for (const id of ['port', 'token', 'account']) el(id).value = currentSettings[id];
el('interval').value = currentSettings.intervalMinutes;
for (const id of Object.keys(PROVIDERS)) el(`provider-${id}`).checked = currentSettings.providers[id];
for (const id of Object.keys(CAPTURE_TYPES)) el(`capture-${id}`).checked = currentSettings.captureTypes[id];
el('app-version').textContent = `AI Memory ${chrome.runtime.getManifest?.().version ?? ''}`.trim();
updateSelection();
await checkConnection();
