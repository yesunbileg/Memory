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
import './page-picker-routes.js';

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
  connectionCode = null,
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
const syncDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

const rescanRows = Object.fromEntries(
  providers.map((provider) => {
    const row = create('div', 'row');
    const description = create('span', 'copy');
    const detail = create('small');
    const button = create('button', '', 'Rescan');
    description.append(create('strong', '', names[provider]), detail);
    button.setAttribute('aria-label', 'Rescan saved ' + names[provider] + ' items');
    button.addEventListener('click', () => {
      if (button.getAttribute('aria-disabled') !== 'true') return requestSync(provider);
    });
    row.append(description, button);
    el('provider-status').append(row);
    return [provider, { detail, button }];
  }),
);

function setText(id, value) {
  const node = el(id);
  if (node.textContent !== value) node.textContent = value;
}

function setSettings(open) {
  el('settings-panel').hidden = !open;
  el('list-panel').hidden = open;
  el('settings').setAttribute('aria-expanded', String(open));
  el('settings').textContent = open ? 'Done' : 'Settings';
  renderSyncPlan();
}
el('settings').addEventListener('click', () => setSettings(el('settings-panel').hidden));
el('connection-action').addEventListener('click', () => {
  setSettings(true);
  if (connectionCode === 'archive_changed') el('manual-connection').open = true;
  else if (['offline', 'missing_token', 'legacy_pairing', 'unauthorized'].includes(connectionCode))
    el('automatic-connection').open = true;
  el('retry').focus();
});
function updateCommand() {
  const family = el('browser-family').value || 'brave';
  el('setup-command').textContent =
    'ai-memory connect --extension-id ' + chrome.runtime.id + ' --browser ' + family;
  setText('copy-status', '');
}
el('browser-family').addEventListener('change', updateCommand);
el('copy-command').addEventListener('click', async () => {
  const command = el('setup-command');
  try {
    await navigator.clipboard.writeText(command.textContent);
    setText('copy-status', 'Command copied.');
  } catch {
    const range = document.createRange();
    range.selectNodeContents(command);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    setText('copy-status', 'Command selected. Copy it with your browser shortcut.');
  }
});
el('version').textContent = 'AI Memory ' + chrome.runtime.getManifest().version;
updateCommand();

function describeScope(provider) {
  let chats = 0,
    projects = 0,
    recents = false;
  for (const entry of scopes[provider].entries) {
    chats += entry.conversationIds.length;
    projects += entry.projectIds.length;
    recents ||= entry.includeRecents === true;
  }
  const items = [
    chats && `${chats} ${chats === 1 ? 'chat' : 'chats'}`,
    projects && `${projects} ${projects === 1 ? 'project' : 'projects'}`,
    recents && 'all Recents',
  ];
  return `${names[provider]} · ${items.filter(Boolean).join(', ')}`;
}

function renderSyncPlan() {
  const selected = providers.filter((provider) => nonempty(scopes[provider]));
  const enabled = selected.filter((provider) => settings.providers[provider]);
  const settingsOpen = !el('settings-panel').hidden;
  el('connection-action').hidden =
    connected || !connectionCode || connectionCode === 'untrusted_sender' || settingsOpen;
  const hint = !connected
    ? ''
    : enabled.length
      ? ''
      : selected.length
        ? 'Enable sync for ' +
          selected.map((provider) => names[provider]).join(', ') +
          (settingsOpen ? '.' : ' in Settings.')
        : settingsOpen
          ? 'Close Settings to choose chats.'
          : bulkPage
            ? 'Select chats below or use the sidebar checkboxes.'
            : 'Open ChatGPT, Claude, or Gemini to select chats.';
  setText('plan-hint', hint);
  el('plan-hint').hidden = !hint;
  const canSync = connected && enabled.length > 0;
  setText('sync-scope', canSync ? enabled.map(describeScope).join('\n') : '');
  el('sync-scope').hidden = !canSync;
  setText(
    'schedule',
    canSync
      ? settings.autoPaused
        ? 'Auto-sync paused'
        : `Auto-sync every ${settings.intervalMinutes} min`
      : '',
  );
  el('schedule').hidden = !canSync;
  return enabled;
}

function summary() {
  const enabled = renderSyncPlan();
  const runs = statusForPairing(status, settings);
  const progress = captureProgress(runs, settings.providers);
  clearTimeout(progressTimer);
  if (progress?.refreshAt) progressTimer = setTimeout(summary, progress.refreshAt - Date.now());
  const stopping = stopPending || Object.values(runs).some((entry) => entry.stopRequested);
  stopAvailable = Boolean(progress);
  for (const [provider, { detail, button }] of Object.entries(rescanRows)) {
    const state = runs[provider];
    const text = state?.stopRequested
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
    if (detail.textContent !== text) detail.textContent = text;
    button.setAttribute(
      'aria-disabled',
      String(!connected || !enabled.includes(provider) || stopAvailable || syncPending || stopping),
    );
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
  const bar = el('sync-progress');
  bar.hidden = !progress && !syncPending;
  if (progress?.max) {
    bar.max = progress.max;
    bar.value = progress.value;
  } else bar.removeAttribute('value');
  const pending = stopError || (stopPending ? 'Stopping sync…' : '') || progress?.text;
  const outcome = pending ? null : captureOutcome(runs, settings.providers, syncSince);
  const lastRun =
    !syncSince && typeof outcome?.counts === 'string'
      ? Math.max(
          0,
          ...providers
            .filter((provider) => settings.providers[provider])
            .map((provider) => runs[provider]?.lastRunMs)
            .filter(Number.isFinite),
        )
      : 0;
  const lines = {
    'last-sync': lastRun > 0 ? `Last sync · ${syncDate.format(lastRun)}` : '',
    'notice-label': pending || outcome?.label || notice,
    'notice-counts': outcome?.counts ? ' · ' + outcome.counts : '',
    'notice-actions': outcome?.actions || '',
  };
  for (const [id, text] of Object.entries(lines)) setText(id, text);
  renderBulk();
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
    connectionCode = null;
    setText('api-title', 'Checking connection…');
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
      setText('preferences-status', '');
    } catch {
      await readStorage();
      setText('preferences-status', 'Could not save this setting. Try again.');
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
  el('retry').setAttribute('aria-disabled', 'true');
  setText('api-title', 'Checking connection…');
  let result;
  try {
    result = await send({ type: 'checkApi' });
  } catch {
    result = { ok: false, code: 'offline' };
  }
  if (ticket !== checking || !sameConnection(expected, settings)) return;
  const info = connectionPresentation(result);
  connected = result?.ok === true;
  connectionCode = connected ? null : result?.code || 'offline';
  setText(
    'api-title',
    connected
      ? 'Local archive connected'
      : info.label === 'Offline'
        ? 'Archive offline'
        : info.label,
  );
  el('api-detail').textContent = info.detail;
  el('retry').setAttribute('aria-disabled', 'false');
  summary();
}
el('retry').addEventListener('click', () => {
  if (el('retry').getAttribute('aria-disabled') !== 'true') return check();
});
el('connection-file').addEventListener('change', (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  event.target.value = '';
  return pair('file', () => {
    if (file.size > 16384) throw Error('Choose a valid AI Memory connection file.');
    return file.text();
  });
});
el('verify-json').addEventListener('click', () => {
  const text = el('connection-json').value;
  return pair('json', () => text, text);
});
let pairingField = null;
el('connection-json').addEventListener('input', () => {
  if (pairingField === 'json') {
    ++pairTicket;
    pairingField = null;
  }
  el('connection-json').removeAttribute('aria-invalid');
  el('json-status').removeAttribute('data-error');
  setText('json-status', '');
});
async function pair(field, read, submitted) {
  const ticket = ++pairTicket,
    expected = settings;
  pairingField = field;
  for (const kind of ['file', 'json']) {
    el('connection-' + kind).removeAttribute('aria-invalid');
    el(kind + '-status').removeAttribute('data-error');
    setText(kind + '-status', '');
  }
  const feedback = field + '-status';
  setText(feedback, 'Verifying local archive…');
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
    setText(
      feedback,
      wrote
        ? 'Connection verified and saved.'
        : 'Connection changed. Verify this connection again.',
    );
    if (wrote && field === 'json' && el('connection-json').value === submitted)
      el('connection-json').value = '';
    if (wrote) await check();
  } catch (error) {
    if (ticket === pairTicket) {
      const input = el('connection-' + field);
      input.setAttribute('aria-invalid', 'true');
      el(feedback).setAttribute('data-error', 'true');
      setText(feedback, error.message || 'Could not connect to this archive.');
      if (field === 'json' && document.activeElement === el('verify-json')) input.focus();
    }
  } finally {
    if (ticket === pairTicket) pairingField = null;
  }
}

// Mode belongs to the current top-level document, not extension storage. Reading it
// on popup open never starts discovery, enables a provider or changes a saved item.
let modePage = null,
  modeBusy = false;
let bulkPage = null,
  bulkBusy = false,
  bulkChoice = null,
  bulkResult = null,
  bulkUndo = null,
  bulkNotice = '';
async function activePage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!Number.isInteger(tab?.id) || tab.id < 0 || !tab.url) return null;
  const url = new URL(tab.url);
  const provider = providers.find(
    (p) => url.protocol === 'https:' && url.host === PROVIDERS[p].site,
  );
  return provider ? { tabId: tab.id, provider, url: tab.url } : null;
}

const sameTab = (a, b) => a?.tabId === b?.tabId && a?.url === b?.url;
function setBulkPage(page) {
  const next = globalThis.aiMemoryPageRoutes.pageContext(page?.url) ? page : null;
  if (!sameTab(next, bulkPage)) {
    bulkChoice = null;
    bulkResult = null;
    bulkUndo = null;
    bulkNotice = '';
  }
  bulkPage = next;
  renderSyncPlan();
  renderBulk();
}

function renderBulk() {
  el('bulk-controls').hidden = !bulkPage;
  if (!bulkPage) return;
  const { provider } = bulkPage;
  const title = bulkResult?.title;
  el('bulk-account').textContent =
    title && title !== names[provider] ? `${names[provider]} · ${title}` : names[provider];
  const workspace = el('bulk-workspace');
  el('bulk-workspace-field').hidden = !bulkChoice;
  workspace.disabled = bulkBusy;
  el('bulk-select').disabled = bulkBusy || (bulkChoice?.action === 'select' && !workspace.value);
  el('bulk-clear').disabled = bulkBusy || (bulkChoice?.action === 'clear' && !workspace.value);
  const target = bulkChoice?.action === 'clear' ? workspace.selectedOptions?.[0]?.textContent : '';
  el('bulk-clear').setAttribute(
    'aria-label',
    target && workspace.value
      ? `Clear workspace selection for ${target}`
      : 'Clear workspace selection',
  );
  el('bulk-undo').hidden = !bulkUndo;
  el('bulk-undo').disabled = bulkBusy;
  setText('bulk-status', bulkNotice);
  el('bulk-enable').hidden = !nonempty(scopes[provider]) || settings.providers[provider];
  el('bulk-enable').disabled = bulkBusy;
  el('bulk-enable').textContent = `Enable ${names[provider]} sync`;
}

async function changeBulk(action) {
  if (!bulkPage || bulkBusy || (action === 'undo' && !bulkUndo)) return;
  const page = bulkPage;
  const { tabId, provider } = page;
  const workspace = el('bulk-workspace');
  const origin = el('bulk-' + action);
  const hadFocus = document.activeElement === origin;
  let focusAfter = origin;
  let choice = {};
  if (action === 'undo') choice = { token: bulkUndo };
  else if (bulkChoice?.action === action && workspace.value)
    choice = { token: bulkChoice.token, index: Number(workspace.value) };
  bulkBusy = true;
  bulkChoice = null;
  if (action !== 'undo') bulkUndo = null;
  bulkNotice = action === 'select' ? 'Checking available chats…' : 'Checking the workspace…';
  renderBulk();
  try {
    const current = await activePage();
    if (!sameTab(page, current)) return setBulkPage(current);
    const reply = await send({
      type: 'changeBulkSelection',
      tabId,
      provider,
      url: page.url,
      action,
      ...choice,
    });
    const latest = await activePage();
    if (!sameTab(page, latest)) return setBulkPage(latest);
    if (!reply?.ok) {
      if (
        action === 'undo' &&
        ['catalog_expired', 'selection_changed', 'principal_changed'].includes(reply?.code)
      ) {
        bulkUndo = null;
        focusAfter = el('bulk-clear');
      }
      bulkNotice =
        reply?.code === 'catalog_expired' && action === 'undo'
          ? 'Undo expired.'
          : reply?.error || 'Could not update the selection. Try again.';
    } else if (Array.isArray(reply.choices)) {
      bulkChoice = { token: reply.token, action };
      const placeholder = create('option', '', 'Choose a workspace');
      placeholder.value = '';
      workspace.replaceChildren(
        placeholder,
        ...reply.choices.map(({ title, index }) => {
          const option = create('option', '', title);
          option.value = String(index);
          return option;
        }),
      );
      workspace.value = '';
      focusAfter = workspace;
      bulkNotice =
        action === 'select'
          ? 'Choose a workspace, then select all chats.'
          : 'Choose a workspace, then clear its selection.';
    } else {
      bulkResult = reply;
      const target = reply.title || names[provider] + ' workspace';
      if (action === 'clear') {
        bulkUndo = reply.undoToken || null;
        bulkNotice = `Selection cleared · ${target}`;
        if (bulkUndo) focusAfter = el('bulk-undo');
      } else if (action === 'undo') {
        bulkUndo = null;
        bulkNotice = `Selection restored · ${target}`;
        focusAfter = el('bulk-clear');
      } else {
        bulkNotice = `${reply.count} ${reply.count === 1 ? 'chat' : 'chats'} selected.`;
      }
      await readStorage();
    }
  } catch {
    bulkNotice = 'Could not update the selection. Reopen AI Memory and retry.';
  } finally {
    bulkBusy = false;
    renderBulk();
    if (
      hadFocus &&
      sameTab(page, bulkPage) &&
      !el('list-panel').hidden &&
      (document.activeElement === origin || document.activeElement === document.body)
    )
      focusAfter.focus();
  }
}

el('bulk-select').addEventListener('click', () => changeBulk('select'));
el('bulk-clear').addEventListener('click', () => changeBulk('clear'));
el('bulk-undo').addEventListener('click', () => changeBulk('undo'));
el('bulk-workspace').addEventListener('change', renderBulk);
el('bulk-enable').addEventListener('click', async () => {
  if (!bulkPage || bulkBusy) return;
  const page = bulkPage;
  bulkBusy = true;
  renderBulk();
  try {
    const current = await activePage();
    if (!sameTab(page, current)) return setBulkPage(current);
    const ok = await writeSettings((prev) => ({
      ...prev,
      providers: { ...prev.providers, [page.provider]: true },
    }));
    if (!ok) throw Error();
  } catch {
    bulkNotice = 'Could not enable sync. Try again.';
  } finally {
    bulkBusy = false;
    renderBulk();
  }
});

async function updateListMode(toggle = false) {
  if (modeBusy || (toggle && !modePage)) return;
  const button = el('list-mode');
  modeBusy = true;
  button.setAttribute('aria-busy', 'true');
  let page,
    next = null;
  let hint = 'List mode is unavailable on this tab.';
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
  setBulkPage(page);
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
