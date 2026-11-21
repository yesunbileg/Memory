import {
  PROVIDERS,
  normalizeSettings,
  enterToggles,
  sameConnection,
  captureOutcome,
  captureProgress,
  statusForPairing,
  friendlyError,
  updateLocalSettings,
} from './ui.js';
import { parseConnectionFile, connectionPresentation } from './connection-file.js';
import { nonempty, scopeFor } from './popup-scope.js';

const el = (id) => document.getElementById(id);
const providers = Object.keys(PROVIDERS);
const names = Object.fromEntries(providers.map((p) => [p, PROVIDERS[p].name]));
const send = (message) => chrome.runtime.sendMessage(message);
const create = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
let settings = normalizeSettings(),
  scopes = {},
  status = {},
  activity = [],
  connected = false,
  checking = 0,
  pairTicket = 0,
  storageTicket = 0;
let syncPending = false,
  stopPending = false,
  stopAvailable = false,
  stopError = '',
  syncSince = 0,
  notice = '',
  writes = Promise.resolve();
let progressTimer;

function setSettings(open) {
  el('settings-panel').hidden = !open;
  el('list-panel').hidden = open;
  el('settings').setAttribute('aria-expanded', String(open));
  el('settings').textContent = open ? 'Done' : 'Settings';
}
el('settings').addEventListener('click', () => setSettings(el('settings-panel').hidden));
function updateCommand() {
  const family = el('browser-family').value || 'brave';
  el('setup-command').textContent =
    'ai-memory connect --extension-id ' + chrome.runtime.id + ' --browser ' + family;
}
el('browser-family').addEventListener('change', updateCommand);
el('version').textContent = 'AI Memory ' + chrome.runtime.getManifest().version;
updateCommand();

function summary() {
  const enabled = providers.filter((p) => settings.providers[p] && nonempty(scopes[p]));
  const runs = statusForPairing(status, settings);
  const progress = captureProgress(runs, settings.providers);
  clearTimeout(progressTimer);
  if (progress?.refreshAt) progressTimer = setTimeout(summary, progress.refreshAt - Date.now());
  const stopping = stopPending || Object.values(runs).some((entry) => entry.stopRequested);
  stopAvailable = Boolean(progress);
  el('provider-status').replaceChildren();
  for (const p of providers) {
    const state = runs[p],
      row = create('div', 'row'),
      desc = create('span', 'copy');
    const detail = state?.stopRequested
      ? 'Stopping…'
      : state?.running
        ? 'Syncing…'
        : state?.lastErrorCode
          ? friendlyError('', state.lastErrorCode)
          : state?.selectionWarning
            ? 'Some items could not be verified.'
            : state?.lastRunMs
              ? 'Last checked ' + new Date(state.lastRunMs).toLocaleTimeString()
              : 'No run for this archive.';
    desc.append(create('strong', '', names[p]), create('small', '', detail));
    const btn = create('button', '', 'Rescan');
    btn.setAttribute('aria-label', 'Rescan saved ' + names[p] + ' items');
    btn.disabled = !connected || !enabled.includes(p) || stopAvailable || syncPending || stopping;
    btn.addEventListener('click', () => requestSync(p));
    row.append(desc, btn);
    el('provider-status').append(row);
  }
  el('sync').disabled =
    stopping || (!stopAvailable && (!connected || !enabled.length || syncPending));
  el('sync').textContent = stopping
    ? 'Stopping…'
    : stopAvailable
      ? 'Stop sync'
      : syncPending
        ? 'Syncing…'
        : 'Sync now';
  el('schedule').textContent = settings.autoPaused
    ? 'Automatic sync paused'
    : 'Automatic sync every ' + settings.intervalMinutes + 'm';
  el('plan-hint').hidden = connected && enabled.length > 0;
  el('plan-hint').textContent = !connected
    ? 'Connect your archive in Settings to sync.'
    : providers.some((p) => nonempty(scopes[p]))
      ? 'Enable the selected provider in Settings to sync.'
      : 'Check a chat or project on its website to sync it.';
  const bar = el('sync-progress');
  bar.hidden = !progress && !syncPending;
  if (progress?.max) {
    bar.max = progress.max;
    bar.value = progress.value;
  } else bar.removeAttribute('value');
  el('notice').textContent =
    stopError ||
    (stopPending ? 'Stopping sync…' : '') ||
    progress?.text ||
    captureOutcome(runs, settings.providers, syncSince) ||
    notice;
}
async function readStorage() {
  const ticket = ++storageTicket;
  const data = await chrome.storage.local.get([
    'settings',
    'status',
    'providerSyncScopes',
    'chatgptSyncList',
    'activityLog',
  ]);
  if (ticket !== storageTicket) return;
  const next = normalizeSettings(data.settings);
  if (!sameConnection(settings, next)) {
    syncSince = 0;
    stopError = '';
    notice = '';
    connected = false;
  }
  settings = next;
  status = data.status || {};
  scopes = Object.fromEntries(
    providers.map((p) => [p, scopeFor(p, data.providerSyncScopes, data.chatgptSyncList).scope]),
  );
  activity = Array.isArray(data.activityLog) ? data.activityLog.slice(0, 5) : [];
  el('activity').replaceChildren();
  for (const record of activity)
    if (record && Object.hasOwn(names, record.provider))
      el('activity').append(
        create(
          'p',
          'muted',
          names[record.provider] +
            ' · ' +
            record.outcome +
            ' · ' +
            new Date(record.timestampMs).toLocaleString(),
        ),
      );
  if (!activity.length) el('activity').textContent = 'No runs recorded.';
  for (const p of providers) el('toggle-' + p).checked = settings.providers[p];
  el('auto-paused').checked = settings.autoPaused;
  if (document.activeElement !== el('interval')) el('interval').value = settings.intervalMinutes;
  summary();
}
function writeSettings(update) {
  const job = writes.then(async () => {
    if (!(await updateLocalSettings((prev) => update(normalizeSettings(prev))))) return false;
    await readStorage();
    await send({ type: 'settingsChanged' });
    return true;
  });
  writes = job.catch(() => {});
  return job;
}
function preference(input, update) {
  input.addEventListener('change', async () => {
    const patch = update();
    if (!patch) return;
    try {
      await writeSettings(patch);
    } catch {
      await readStorage();
      el('pair-status').textContent = 'Could not save this setting. Try again.';
    }
  });
}
for (const p of providers) {
  const toggle = el('toggle-' + p);
  enterToggles(toggle);
  preference(toggle, () => {
    const on = toggle.checked;
    return (prev) => ({ ...prev, providers: { ...prev.providers, [p]: on } });
  });
}
enterToggles(el('auto-paused'));
preference(el('auto-paused'), () => {
  const paused = el('auto-paused').checked;
  return (prev) => ({ ...prev, autoPaused: paused });
});
preference(el('interval'), () => {
  const n = Number(el('interval').value);
  if (!Number.isInteger(n) || n < 1 || n > 720) {
    el('interval').reportValidity();
    return;
  }
  return (prev) => ({ ...prev, intervalMinutes: n });
});

async function check() {
  const ticket = ++checking,
    expected = settings;
  el('retry').disabled = true;
  let result;
  try {
    result = await send({ type: 'checkApi' });
  } catch {
    result = { ok: false, code: 'offline' };
  }
  if (ticket !== checking || !sameConnection(expected, settings)) return;
  const info = connectionPresentation(result);
  connected = result?.ok === true;
  el('api-title').textContent = connected ? 'Local archive connected' : info.label;
  el('api-detail').textContent = info.detail;
  el('retry').disabled = false;
  summary();
}
el('retry').addEventListener('click', check);
el('connection-file').addEventListener('change', (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  event.target.value = '';
  return pair(() => {
    if (file.size > 16384) throw Error('Choose a valid AI Memory connection file.');
    return file.text();
  });
});
el('verify-json').addEventListener('click', () => {
  const input = el('connection-json'),
    text = input.value;
  input.value = '';
  return pair(() => text);
});
async function pair(read) {
  const ticket = ++pairTicket,
    expected = settings;
  el('pair-status').textContent = 'Verifying local archive…';
  try {
    const candidate = parseConnectionFile(await read());
    const reply = await send({ type: 'verifyPairing', candidate });
    if (ticket !== pairTicket) return;
    if (!reply?.ok || reply.archiveId !== candidate.archiveId)
      throw Error('Could not verify this archive. The previous connection is unchanged.');
    const wrote = await writeSettings((prev) =>
      ticket === pairTicket && sameConnection(prev, expected)
        ? { ...prev, ...candidate, token: '' }
        : null,
    );
    if (ticket !== pairTicket) return;
    el('pair-status').textContent = wrote
      ? 'Connection verified and saved.'
      : 'Connection changed. Choose the file again.';
    if (wrote) await check();
  } catch (error) {
    if (ticket === pairTicket)
      el('pair-status').textContent = error.message || 'Could not connect to this archive.';
  }
}

// Mode belongs to the current top-level document, not extension storage. Reading it
// on popup open never starts discovery, enables a provider or changes a saved item.
let modePage = null,
  modeBusy = false;
async function activePage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!Number.isInteger(tab?.id) || tab.id < 0 || !tab.url) return null;
  const url = new URL(tab.url);
  const provider = providers.find(
    (p) => url.protocol === 'https:' && url.host === PROVIDERS[p].site,
  );
  return provider ? { tabId: tab.id, provider } : null;
}

async function updateListMode(toggle = false) {
  if (modeBusy || (toggle && !modePage)) return;
  const button = el('list-mode');
  modeBusy = true;
  button.setAttribute('aria-busy', 'true');
  let page,
    next = null;
  let hint = 'Open ChatGPT, Claude, or Gemini in this tab to use list mode.';
  try {
    page = await activePage();
    if (page) {
      const name = names[page.provider];
      const change =
        toggle && modePage?.tabId === page.tabId && modePage.provider === page.provider;
      // If the active tab changed, read its state first. Do not apply the other tab’s click.
      const message = change
        ? { type: 'SET_PAGE_LIST_MODE', enabled: !modePage.enabled }
        : { type: 'GET_PAGE_LIST_MODE' };
      if (change)
        el('mode-status').textContent = message.enabled
          ? 'Checking the list on ' + name + '…'
          : 'Hiding checkboxes…';
      const reply = await chrome.tabs.sendMessage(page.tabId, message, { frameId: 0 });
      if (
        reply?.ok === true &&
        reply.provider === page.provider &&
        typeof reply.enabled === 'boolean'
      ) {
        next = { ...page, enabled: reply.enabled };
        const items = page.provider === 'gemini' ? 'chats' : 'chats or whole projects';
        hint = next.enabled
          ? 'Choose chats in the sidebar.'
          : 'Show checkboxes beside ' + items + ' on ' + name + '.';
      } else if (reply?.ok === false && reply.code === 'page_unavailable') {
        hint = 'Open the ' + name + ' chat list in this tab to use list mode.';
      } else if (reply?.ok === false && reply.code === 'page_unverified') {
        next = { ...page, enabled: false };
        const item = page.provider === 'gemini' ? 'chat' : 'chat or project';
        hint = `Cannot verify this ${item}. Open the ${name} home page and try list mode there.`;
      } else if (change && reply?.ok === false) {
        next = typeof reply.enabled === 'boolean' ? { ...page, enabled: reply.enabled } : modePage;
        hint =
          reply.code === 'not_logged_in'
            ? 'Sign in to ' + name + ', then try list mode again.'
            : reply.code === 'catalog_unavailable'
              ? 'Could not read the list on ' + name + '. Check that you are signed in and retry.'
              : 'Could not change list mode on ' + name + '. Try again.';
      } else {
        throw Error('page unavailable');
      }
    }
  } catch {
    if (page) hint = 'Reload this ' + names[page.provider] + ' tab, then reopen AI Memory.';
  }
  modePage = next;
  button.disabled = !next;
  button.setAttribute('aria-pressed', String(next?.enabled === true));
  button.setAttribute('aria-busy', 'false');
  el('mode-state').textContent = next?.enabled ? 'On' : 'Off';
  el('mode-site').textContent = page ? names[page.provider] : 'Current tab';
  el('mode-status').textContent = hint;
  modeBusy = false;
}
el('list-mode').addEventListener('click', () => updateListMode(true));

el('sync').addEventListener('click', () => (stopAvailable ? requestStop() : requestSync()));
async function requestStop() {
  if (el('sync').disabled) return;
  stopError = '';
  stopPending = true;
  summary();
  try {
    const result = await send({ type: 'stopSync' });
    if (!result?.ok) throw new Error();
    syncSince = 0;
  } catch {
    stopError = 'Could not stop sync. Try again.';
  } finally {
    stopPending = false;
    await readStorage();
  }
}

async function requestSync(provider) {
  if (
    el('sync').disabled ||
    syncPending ||
    (provider && (!settings.providers[provider] || !nonempty(scopes[provider])))
  )
    return;
  syncPending = true;
  stopError = '';
  syncSince = Date.now();
  notice = 'Requesting local sync…';
  summary();
  try {
    const result = await send(
      provider ? { type: 'fullSyncProvider', provider } : { type: 'syncNow' },
    );
    notice = result?.ok ? 'Sync requested.' : friendlyError('', result?.code);
  } catch {
    notice = 'Could not sync. Check the local connection and retry.';
  } finally {
    syncPending = false;
    await readStorage();
  }
}
chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area && area !== 'local') return;
  if (
    !changes.settings &&
    !changes.status &&
    !changes.providerSyncScopes &&
    !changes.chatgptSyncList &&
    !changes.activityLog
  )
    return;
  const repair =
    changes.settings && !sameConnection(settings, normalizeSettings(changes.settings.newValue));
  if (repair) {
    ++checking;
    connected = false;
  }
  await readStorage();
  if (repair) await check();
});
await Promise.all([updateListMode(), readStorage().then(check)]);
