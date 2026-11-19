import { catalogRows, filterRows, idKey, savedRows, selectionFor } from './popup-picker.js';
import {
  normalizeSettings,
  enterToggles,
  sameConnection,
  captureOutcome,
  statusForPairing,
  friendlyError,
} from './ui.js';
import { parseConnectionFile, connectionPresentation } from './connection-file.js';
import { scopeFor } from './popup-scope.js';

const el = (id) => document.getElementById(id);
const names = { chatgpt: 'ChatGPT', claude: 'Claude', gemini: 'Gemini' };
const providers = Object.keys(names);
const send = (message) => chrome.runtime.sendMessage(message);
const create = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const size = (scope) =>
  (scope?.entries || []).reduce(
    (n, e) => n + (e.projectIds?.length || 0) + (e.conversationIds?.length || 0),
    0,
  );
const models = Object.fromEntries(
  providers.map((id) => [
    id,
    {
      pending: new Set(),
      kind: id === 'gemini' ? 'conversation' : 'all',
      query: '',
      limit: 50,
      savedLimit: 50,
    },
  ]),
);
let current = 'chatgpt',
  settings = normalizeSettings(),
  scopes = {},
  status = {},
  activity = [],
  connected = false,
  checking = 0,
  pairTicket = 0,
  storageTicket = 0;
let syncPending = false,
  syncSince = 0,
  notice = '',
  writes = Promise.resolve();

function setSettings(open) {
  el('settings-panel').hidden = !open;
  el('picker-view').hidden = open;
  el('settings').setAttribute('aria-expanded', String(open));
  el('settings').textContent = open ? 'Done' : 'Settings';
}
el('settings').addEventListener('click', () => setSettings(el('settings-panel').hidden));
function updateCommand() {
  const family = el('browser-family').value || 'chrome';
  el('setup-command').textContent =
    'ai-memory --db /path/to/archive.sqlite connect --extension-id ' +
    chrome.runtime.id +
    ' --browser ' +
    family;
}
el('browser-family').addEventListener('change', updateCommand);
el('version').textContent = 'AI Memory ' + chrome.runtime.getManifest().version;
updateCommand();

function activeSaved(p) {
  if (models[p].catalog) return size(models[p].catalog.selectedScope);
  return size(scopes[p]);
}
function summary() {
  const enabled = providers.filter((p) => settings.providers[p] && activeSaved(p) > 0);
  const runs = statusForPairing(status, settings);
  const working = Object.entries(runs).some(([p, v]) => settings.providers[p] && v.running);
  el('enable-current').checked = settings.providers[current];
  el('enable-label').textContent = 'Enable ' + names[current] + ' sync';
  el('provider-status').replaceChildren();
  for (const p of providers) {
    const state = runs[p],
      row = create('div', 'row'),
      desc = create('span', 'copy');
    const detail = state?.running
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
    btn.disabled = !connected || !enabled.includes(p) || working || syncPending;
    btn.addEventListener('click', () => requestSync(p));
    row.append(desc, btn);
    el('provider-status').append(row);
  }
  el('sync').disabled = !connected || !enabled.length || syncPending || working;
  el('sync').textContent = syncPending || working ? 'Syncing…' : 'Sync now';
  el('schedule').textContent = settings.autoPaused
    ? 'Automatic sync paused'
    : 'Automatic sync every ' + settings.intervalMinutes + 'm';
  el('plan-hint').textContent = !connected
    ? 'Connect your archive in Settings to sync.'
    : enabled.length
      ? 'Saved items from ' + enabled.map((p) => names[p]).join(', ') + '.'
      : providers.some((p) => activeSaved(p))
        ? 'Enable a saved provider in Settings to sync.'
        : 'Add projects or chats to your sync list first.';
  el('notice').textContent = captureOutcome(runs, settings.providers, syncSince) || notice;
}
async function readStorage(updateCatalogs = false) {
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
  if (updateCatalogs)
    for (const p of providers) {
      const model = models[p];
      if (!model.catalog) continue;
      const owners = new Set(model.catalog.entries.map((e) => e.principalId));
      setScope(model, { entries: scopes[p].entries.filter((e) => owners.has(e.principalId)) });
      for (const key of model.savedKeys) model.pending.delete(key);
    }
  for (const p of providers) el('toggle-' + p).checked = settings.providers[p];
  el('auto-paused').checked = settings.autoPaused;
  if (document.activeElement !== el('interval')) el('interval').value = settings.intervalMinutes;
  if (updateCatalogs) draw();
  else summary();
}
function writeSettings(update) {
  const job = writes.then(async () => {
    const prev = normalizeSettings((await chrome.storage.local.get('settings')).settings);
    const next = update(prev);
    if (!next) return false;
    await chrome.storage.local.set({ settings: next });
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
enterToggles(el('enable-current'));
preference(el('enable-current'), () => {
  const p = current,
    on = el('enable-current').checked;
  return (prev) => ({ ...prev, providers: { ...prev.providers, [p]: on } });
});
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
  el('api-title').textContent = connected ? 'Connected to your computer' : info.label;
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

function copy(item) {
  const wrap = create('span', 'copy');
  wrap.append(
    create('strong', '', item.title),
    create(
      'small',
      '',
      (item.kind === 'project' ? 'Project' : 'Chat') + ' · ' + item.principalTitle,
    ),
  );
  return wrap;
}
function draw() {
  const p = current,
    m = models[p],
    cat = m.catalog;
  for (const id of providers) el('tab-' + id).setAttribute('aria-pressed', String(id === p));
  el('catalog-heading').textContent = names[p] + ' catalog';
  el('filters').hidden = !cat;
  el('picker-search').value = m.query;
  el('picker-kind').value = m.kind;
  el('project-filter').hidden = p === 'gemini';
  el('picker-kind').disabled = p === 'gemini';
  el('load-catalog').disabled = !!m.busy;
  el('load-catalog').textContent =
    m.busy === 'load' ? 'Loading…' : cat ? 'Reload items' : 'Load items';
  el('catalog-items').replaceChildren();
  el('saved-items').replaceChildren();
  el('catalog-note').textContent =
    m.busy === 'load'
      ? 'Reading titles and IDs…'
      : !cat
        ? 'Load ' +
          (p === 'gemini' ? 'chat titles' : 'projects and chat titles') +
          ' from your signed-in ' +
          names[p] +
          ' account.'
        : '';
  el('saved-heading').textContent =
    'Saved list · ' + names[p] + (cat ? ' (' + m.saved.length + ')' : '');
  el('saved-note').textContent = !cat
    ? 'Load items to review the saved list for your signed-in account.'
    : m.saved.length
      ? 'Removing an item stops future syncs; it does not delete the local copy.'
      : 'No saved items for this account.';
  el('show-more').hidden = true;
  el('more-saved').hidden = true;
  if (cat) {
    const filtered = filterRows(m.rows, m.query, m.kind);
    const count = filtered.length;
    el('catalog-note').textContent =
      (cat.incomplete ? 'Partial catalog. ' : '') +
      (count
        ? count + ' matching item' + (count === 1 ? '' : 's')
        : m.rows.length
          ? 'No matches. Change your search or filter.'
          : 'No items available for this account.');
    if (cat.warning) el('catalog-note').textContent += ' ' + cat.warning;
    for (const item of filtered.slice(0, m.limit)) {
      const key = idKey(item),
        saved = m.savedKeys.has(key),
        row = create('div', 'row'),
        label = create('label'),
        box = create('input');
      box.type = 'checkbox';
      box.dataset.kind = item.kind;
      box.dataset.id = item.id;
      box.checked = saved || m.pending.has(key);
      box.disabled = saved || !!m.busy;
      label.append(box, copy(item));
      row.append(label);
      if (saved) row.append(create('small', 'muted', 'Saved'));
      box.addEventListener('change', () => {
        if (box.checked) m.pending.add(key);
        else m.pending.delete(key);
        m.note = '';
        updateActions(m);
      });
      el('catalog-items').append(row);
    }
    el('show-more').hidden = count <= m.limit;
    for (const item of m.saved.slice(0, m.savedLimit)) {
      const row = create('div', 'row'),
        button = create('button', '', 'Remove');
      button.dataset.id = item.id;
      button.dataset.kind = item.kind;
      button.disabled = !!m.busy;
      button.setAttribute('aria-label', 'Remove ' + item.title + ' from sync list');
      button.addEventListener('click', () => edit(p, item));
      row.append(copy(item), button);
      el('saved-items').append(row);
    }
    el('more-saved').hidden = m.saved.length <= m.savedLimit;
  }
  updateActions(m);
  summary();
}
function updateActions(m) {
  el('pick-count').textContent = m.pending.size + ' selected';
  el('add-selection').disabled = !m.pending.size || !!m.busy;
  el('picker-status').textContent = m.note || '';
}
function setScope(m, scope) {
  m.catalog.selectedScope = scope || { entries: [] };
  m.saved = savedRows(m.catalog.selectedScope, m.catalog, m.known);
  m.savedKeys = new Set(m.saved.map(idKey));
}
function reset(m) {
  m.catalog = null;
  m.saved = [];
  m.savedKeys = new Set();
  m.rows = [];
  m.known = new Map();
  m.pending.clear();
  m.limit = m.savedLimit = 50;
}
function redraw(p) {
  if (p === current) draw();
}
async function load(p, after = '') {
  const m = models[p];
  if (m.busy) return;
  m.busy = 'load';
  reset(m);
  m.note = '';
  redraw(p);
  try {
    const result = await send({ type: 'LIST_SYNC_CATALOG', provider: p });
    if (!result?.ok || !result.catalogToken || !Array.isArray(result.entries))
      throw Error(
        result?.error ||
          'Could not load items. Sign in to ' + names[p] + ' in this browser, then retry.',
      );
    m.catalog = result;
    m.rows = catalogRows(result);
    m.known = new Map(m.rows.map((item) => [idKey(item), item]));
    setScope(m, result.selectedScope);
    m.note = after;
  } catch (error) {
    reset(m);
    m.note = error.message || 'Could not load items.';
  } finally {
    m.busy = '';
    redraw(p);
  }
}
async function edit(p, remove) {
  const m = models[p];
  if (m.busy || !m.catalog || (!remove && !m.pending.size)) return;
  // Add only new picks. The worker merges them with saved (including unseen)
  // items for their original identities. Removal is a separate explicit action.
  const rows = remove ? [remove] : m.rows.filter((item) => m.pending.has(idKey(item)));
  m.busy = 'save';
  m.note = 'Saving…';
  redraw(p);
  let reload = false;
  try {
    const result = await send({
      type: remove ? 'REMOVE_SYNC_SELECTION' : 'SAVE_SYNC_SELECTION',
      provider: p,
      catalogToken: m.catalog.catalogToken,
      selection: selectionFor(rows),
    });
    if (!result?.ok) {
      reload = ['catalog_expired', 'principal_changed'].includes(result?.code);
      throw Error(result?.error || 'Could not save. Try again.');
    }
    setScope(m, result.selectedScope);
    if (!remove) m.pending.clear();
    m.note = remove
      ? 'Removed from the sync list.'
      : 'Added to the sync list. No chats were synced.';
  } catch (error) {
    m.note = error.message || 'Could not save. Try again.';
  } finally {
    m.busy = '';
    if (reload) await load(p, 'Account or catalog changed. Check your choices again.');
    else redraw(p);
  }
}
for (const id of providers)
  el('tab-' + id).addEventListener('click', () => {
    current = id;
    draw();
  });
el('load-catalog').addEventListener('click', () => load(current));
el('picker-search').addEventListener('input', (e) => {
  models[current].query = e.target.value;
  models[current].limit = 50;
  draw();
});
el('picker-kind').addEventListener('change', (e) => {
  models[current].kind = e.target.value;
  models[current].limit = 50;
  draw();
});
el('show-more').addEventListener('click', () => {
  models[current].limit += 50;
  draw();
});
el('more-saved').addEventListener('click', () => {
  models[current].savedLimit += 50;
  draw();
});
el('add-selection').addEventListener('click', () => edit(current));
el('sync').addEventListener('click', () => requestSync());
async function requestSync(provider) {
  if (
    el('sync').disabled ||
    syncPending ||
    (provider && (!settings.providers[provider] || !activeSaved(provider)))
  )
    return;
  syncPending = true;
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
  await readStorage(!!(changes.providerSyncScopes || changes.chatgptSyncList));
  if (repair) await check();
});
await readStorage();
draw();
await check();
