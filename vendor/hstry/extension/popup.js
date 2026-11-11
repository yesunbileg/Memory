import {
  DEFAULT_CAPTURE_TYPES,
  DEFAULT_PROVIDER_SETTINGS,
  PROVIDERS,
  normalizeSettings,
  providerState,
} from './ui.js';
import { connectionPresentation } from './connection-file.js';

const DEFAULTS = {
  port: 41770,
  token: '',
  intervalMinutes: 15,
  providers: DEFAULT_PROVIDER_SETTINGS,
  captureTypes: DEFAULT_CAPTURE_TYPES,
};
const el = (id) => document.getElementById(id);
const marks = { chatgpt: '◎', claude: '✳︎', gemini: '✦' };
let apiConnected = false;
let current = DEFAULTS;
let busy = false;
let connectionCheck = 0;

async function render() {
  const { settings = {}, status = {} } = await chrome.storage.local.get(['settings', 'status']);
  current = normalizeSettings(
    {
      ...DEFAULTS,
      ...settings,
      providers: { ...DEFAULT_PROVIDER_SETTINGS, ...settings.providers },
    },
    DEFAULTS,
  );
  const names = {
    projects: 'Projects',
    pinned: 'Pinned',
    agents: 'GPTs & agents',
    recents: 'Recents',
  };
  el('selection-summary').textContent =
    Object.entries(current.captureTypes)
      .filter(([, value]) => value)
      .map(([key]) => names[key])
      .join(' · ') || 'No chat types selected';
  el('recents-note').textContent = current.captureTypes.recents
    ? 'Ordinary Recents included'
    : 'Ordinary Recents stay out';
  el('schedule').textContent = `Checks every ${current.intervalMinutes} min`;
  el('providers').replaceChildren();
  for (const [id, provider] of Object.entries(PROVIDERS)) {
    const state = providerState(status[id], current.providers[id], {
      apiConnected,
      captureTypes: current.captureTypes,
      providerId: id,
    });
    const row = document.createElement('article');
    row.className = 'popup-provider';
    row.innerHTML = `<span class="provider-mark ${id}" aria-hidden="true">${marks[id]}</span><div class="popup-provider-copy"><h2>${provider.name}</h2><p></p></div><span class="status-dot ${state.tone}"></span>`;
    row.querySelector('p').textContent =
      state.label === 'Off' ? 'Off · enable in settings' : state.detail;
    if (state.tone === 'error') row.querySelector('p').className = 'error-copy';
    if (state.tone === 'warning') row.querySelector('p').className = 'warning-copy';
    el('providers').appendChild(row);
  }
  const selected =
    Object.values(current.providers).some(Boolean) &&
    Object.values(current.captureTypes).some(Boolean);
  el('sync').textContent = busy
    ? 'Starting capture…'
    : !apiConnected
      ? 'Connect your computer'
      : selected
        ? 'Capture selected chats'
        : 'Choose what to capture';
  el('sync').disabled = busy;
}

async function checkApi() {
  const generation = ++connectionCheck;
  const requested = { port: current.port, token: current.token };
  el('api-dot').className = 'status-dot checking';
  el('retry').disabled = true;
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: 'checkApi' });
  } catch {
    result = { ok: false, code: 'offline' };
  }
  if (
    generation !== connectionCheck ||
    requested.port !== current.port ||
    requested.token !== current.token
  )
    return false;
  apiConnected = result?.ok === true;
  const state = connectionPresentation(result);
  el('api-title').textContent = apiConnected ? 'Connected to your computer' : state.label;
  el('api-detail').textContent = apiConnected
    ? 'Your selected chats stay on this computer.'
    : state.detail;
  el('api-dot').className = `status-dot ${state.tone}`;
  el('retry').disabled = false;
  await render();
  return apiConnected;
}

el('sync').addEventListener('click', async () => {
  if (busy) return;
  const selected =
    Object.values(current.providers).some(Boolean) &&
    Object.values(current.captureTypes).some(Boolean);
  if (!apiConnected || !selected) {
    await chrome.runtime.openOptionsPage();
    return;
  }
  busy = true;
  await render();
  try {
    const result = await chrome.runtime.sendMessage({ type: 'syncNow' });
    el('notice').textContent = result?.ok
      ? 'Capturing your selection. You can close this popup.'
      : 'Could not start. Check your connection in settings.';
  } catch {
    el('notice').textContent = 'Check your connection in settings and try again.';
  } finally {
    busy = false;
    await render();
  }
});
for (const id of ['settings', 'edit-selection'])
  el(id).addEventListener('click', () => chrome.runtime.openOptionsPage());
el('retry').addEventListener('click', checkApi);
chrome.storage.onChanged.addListener(async (changes) => {
  const previous = { port: current.port, token: current.token };
  const values = changes.settings?.newValue;
  const authChanged =
    values && (Number(values.port) !== previous.port || values.token !== previous.token);
  if (authChanged) {
    connectionCheck++;
    apiConnected = false;
  }
  if (changes.status || changes.settings) await render();
  if (authChanged) await checkApi();
});
await render();
await checkApi();
