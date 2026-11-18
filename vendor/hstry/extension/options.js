import { eligibleCapture } from './providers/selection.js';
import {
  CAPTURE_TYPES,
  PROVIDERS,
  PROVIDER_CAPABILITIES,
  describeEffectivePlan,
  captureOutcome,
  statusForPairing,
  enterToggles,
  progressBar,
  resultChip,
  sameConnection,
  restoreControl,
  formatDuration,
  formatSyncBreakdown,
  formatTime,
  friendlyError,
  normalizeSettings,
  providerState,
} from './ui.js';
import { connectionPresentation, parseConnectionFile } from './connection-file.js';
import { CATEGORY_ICONS, PROVIDER_ICONS, UI_ICONS } from './icons.js';

const el = (id) => document.getElementById(id);
const typeCopy = {
  projects: ['Projects & folders', 'Chats organized inside a project or folder.'],
  pinned: ['Pinned & favorites', 'Conversations you’ve chosen to keep close.'],
  agents: ['GPTs & agents', 'Chats with a custom GPT or agent.'],
  recents: ['Ordinary Recents', 'Everyday chats outside your organized spaces.'],
};
let currentSettings = normalizeSettings();
let apiConnected = false;
let checking = false;
let connectionCheck = 0;
let pairingGeneration = 0;
let since = 0;
let lastStatus = {};
let starting = false;
let outcomeProviders = null;
let displayScope = '';
let statusRender = 0;
function updateCommand() {
  const selected = el('browser-family').value;
  const browser = ['brave', 'chromium', 'chrome'].includes(selected) ? selected : 'chrome';
  const id = /^[a-p]{32}$/.test(chrome.runtime.id) ? chrome.runtime.id : 'EXTENSION_ID';
  el('setup-command').textContent =
    `ai-memory --db /path/to/archive.sqlite connect --extension-id ${id} --browser ${browser}`;
}

function feedback(id, text, error = false) {
  const node = el(id);
  if (!node) return;
  node.textContent = text;
  node.className = `inline-feedback${error ? ' error' : ''}`;
}

function renderControls() {
  for (const [id, provider] of Object.entries(PROVIDERS)) {
    const caps = PROVIDER_CAPABILITIES[id];
    const row = document.createElement('label');
    row.className = 'provider-row';
    row.innerHTML = `<span class="provider-mark ${id}" aria-hidden="true">${PROVIDER_ICONS[id]}</span><span class="row-copy"><strong>${provider.name}</strong><small>${provider.site} · ${provider.description}</small></span><span class="switch"><input id="provider-${id}" type="checkbox" aria-label="Capture ${provider.name} chats" /></span>`;
    if (caps?.badges) {
      const badges = document.createElement('span');
      badges.className = 'capability-badges';
      for (const badge of caps.badges) {
        const tag = document.createElement('span');
        tag.className = `cap-badge${id === 'gemini' ? ' cap-warning' : ''}`;
        tag.textContent = badge.label;
        badges.appendChild(tag);
      }
      const copyNode = row.querySelector('small')?.parentElement;
      if (copyNode) copyNode.appendChild(badges);
    }
    el('provider-list').appendChild(row);
  }
  for (const id of Object.keys(CAPTURE_TYPES)) {
    const row = document.createElement('label');
    row.className = 'capture-row';
    row.innerHTML = `<input id="capture-${id}" type="checkbox" /><span class="category-icon" aria-hidden="true">${CATEGORY_ICONS[id]}</span><span class="row-copy"><strong>${typeCopy[id][0]}</strong><small>${typeCopy[id][1]}</small></span>${id === 'recents' ? '<span class="small-tag">OFF BY DEFAULT</span>' : ''}`;
    el('capture-list').appendChild(row);
  }
}

function readSettings() {
  return normalizeSettings({
    ...currentSettings,
    autoPaused: el('auto-paused').checked,
    account: el('account').value,
    intervalMinutes: el('interval').value,
    providers: Object.fromEntries(
      Object.keys(PROVIDERS).map((id) => [id, el(`provider-${id}`).checked]),
    ),
    captureTypes: Object.fromEntries(
      Object.keys(CAPTURE_TYPES).map((id) => [id, el(`capture-${id}`).checked]),
    ),
  });
}

async function persist() {
  if (el('interval').checkValidity && !el('interval').checkValidity()) {
    el('interval').reportValidity();
    throw new Error('Check the automatic interval.');
  }
  const next = readSettings();
  await chrome.storage.local.set({ settings: next });
  currentSettings = next;
  await chrome.runtime.sendMessage({ type: 'settingsChanged' });
  updateSelection();
}

function updateSelection() {
  const types = currentSettings.captureTypes;
  el('selection-summary').textContent = types.recents
    ? 'Ordinary Recents are included. Only the chat types you checked will be captured.'
    : 'Ordinary Recents stay out. A project or pinned chat is still included even when it also appears in Recents.';
  const plan = describeEffectivePlan(currentSettings.providers, types);
  if (el('effective-plan')) {
    el('effective-plan').textContent = plan.summary;
  }
  const selected = Object.values(types).some(Boolean);
  el('schedule-detail').textContent = currentSettings.autoPaused
    ? 'Automatic checks are paused. You can still capture manually.'
    : `Automatic checks run every ${currentSettings.intervalMinutes} minutes.`;
  el('gemini-selection-warning').hidden = !(
    currentSettings.providers.gemini &&
    !types.recents &&
    selected
  );
  el('sync-now').disabled =
    !apiConnected ||
    !plan.readyCount ||
    checking ||
    starting ||
    Object.values(lastStatus).some((entry) => entry.running);
}

async function checkConnection() {
  const generation = ++connectionCheck;
  const requested = currentSettings;
  checking = true;
  el('test-connection').setAttribute('aria-busy', 'true');
  el('connection-label').textContent = 'Checking…';
  el('connection-pill').className = 'status-pill checking';
  el('connection-dot').className = 'status-dot checking';
  updateSelection();
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: 'checkApi' });
  } catch {
    result = { ok: false, code: 'offline' };
  }
  if (generation !== connectionCheck || !sameConnection(requested, currentSettings)) return false;
  const state = connectionPresentation(result);
  const focused = document.activeElement;
  const inGuide = el('connection-guide').contains?.(focused);
  const inReady = el('connection-ready').contains?.(focused);
  apiConnected = result?.ok === true;
  el('connection-title').textContent = state.title;
  el('connection-result').textContent = state.detail;
  el('connection-label').textContent = state.label;
  el('connection-pill').className = `status-pill ${state.tone}`;
  el('connection-dot').className = `status-dot ${state.tone}`;
  el('connection-guide').hidden = apiConnected;
  el('connection-ready').hidden = !apiConnected;
  if (apiConnected && inGuide) el('change-connection').focus?.();
  if (!apiConnected && inReady) el('import-connection').focus?.();
  if (el('connection-ready-detail')) {
    el('connection-ready-detail').textContent = apiConnected
      ? `Verified local archive ${currentSettings.archiveId}. Selected chats stay on this computer.`
      : 'You’re all set. Your selected chats will be saved on this computer.';
  }
  el('test-connection').setAttribute('aria-busy', 'false');
  checking = false;
  updateSelection();
  await renderStatus();
  return apiConnected;
}

function renderActivityLog(entries) {
  const box = el('activity-log');
  if (!box) return;
  box.replaceChildren();
  const clearBtn = el('clear-log');
  if (clearBtn) clearBtn.disabled = entries.length === 0;
  if (entries.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'activity-empty';
    empty.textContent = 'No browser sync runs recorded yet.';
    box.appendChild(empty);
    return;
  }
  for (const run of entries.slice(0, 8)) {
    const row = document.createElement('div');
    row.className = 'log-row';
    const providerName = PROVIDERS[run.provider]?.name ?? run.provider;
    const counts = run.counts ?? {};
    const outcomeLabels = {
      success: 'Completed',
      queued: 'Queued batch',
      warning: 'Partial / skipped',
      error: 'Failed',
      cancelled: 'Selection changed',
    };
    const outcomeTone =
      run.outcome === 'success'
        ? 'success'
        : run.outcome === 'warning' || run.outcome === 'queued'
          ? 'warning'
          : run.outcome === 'error'
            ? 'error'
            : 'muted';

    const left = document.createElement('div');
    left.className = 'log-main';

    const title = document.createElement('strong');
    title.textContent = `${providerName} · ${outcomeLabels[run.outcome] ?? run.outcome}`;
    left.appendChild(title);

    const detail = document.createElement('span');
    detail.className = 'log-detail';
    if (run.outcome === 'error') {
      detail.textContent = friendlyError('', run.errorCode);
    } else {
      const parts = [
        `${counts.detected ?? 0} checked`,
        `${counts.created ?? 0} new`,
        `${counts.updated ?? 0} updated`,
        `${counts.unchanged ?? 0} unchanged`,
      ];
      if ((counts.skipped ?? 0) > 0) parts.push(`${counts.skipped} skipped`);
      if (run.durationMs) parts.push(formatDuration(run.durationMs));
      detail.textContent = parts.join(' · ');
    }
    left.appendChild(detail);

    const right = document.createElement('div');
    right.className = 'log-meta';
    const pill = document.createElement('span');
    pill.className = `mini-chip ${outcomeTone}`;
    pill.textContent =
      run.trigger === 'alarm' ? 'Auto' : run.trigger === 'full' ? 'Rescan' : 'Sync';
    const time = document.createElement('span');
    time.className = 'log-time';
    time.textContent = formatTime(run.timestampMs);
    right.appendChild(pill);
    right.appendChild(time);

    row.appendChild(left);
    row.appendChild(right);
    box.appendChild(row);
  }
}

async function renderStatus() {
  const ticket = ++statusRender;
  const stored = await chrome.storage.local.get(['status', 'activityLog']);
  const activityLog = Array.isArray(stored.activityLog) ? stored.activityLog : [];

  if (ticket !== statusRender) return;
  const status = statusForPairing(stored.status, currentSettings);
  const key = JSON.stringify([currentSettings.archiveId, currentSettings.account]);
  if (displayScope !== key) {
    displayScope = key;
    since = 0;
    outcomeProviders = null;
    feedback('capture-outcome', '');
  }
  const focused = document.activeElement?.id;
  const dynamic = /^(rescan|site|capture-one)-(.+)$/.exec(focused || '');
  lastStatus = status;
  updateSelection();
  const outcome = captureOutcome(status, outcomeProviders ?? currentSettings.providers, since);
  if (outcome) el('capture-outcome').textContent = outcome;
  const list = el('status-list');
  list.replaceChildren();
  const rescans = el('resync-list');
  rescans.replaceChildren();

  for (const [id, provider] of Object.entries(PROVIDERS)) {
    const entry = status[id];
    const enabled = Boolean(currentSettings.providers[id]);
    const state = providerState(entry, enabled, {
      apiConnected,
      captureTypes: currentSettings.captureTypes,
      providerId: id,
    });
    const breakdown = formatSyncBreakdown(entry);
    const caps = PROVIDER_CAPABILITIES[id];

    const item = document.createElement('div');
    item.className = `status-item${enabled ? '' : ' is-off'}`;
    item.innerHTML = `<span class="status-dot ${state.tone}"></span><div class="status-content"><strong>${provider.name} · ${state.label}</strong><p></p></div><span class="status-time"></span>`;
    const detail = item.querySelector('p');
    detail.textContent = state.detail;
    if (state.tone === 'error') detail.className = 'error-copy';
    if (state.tone === 'warning') detail.className = 'warning-copy';
    const durationText =
      entry?.durationMs && !entry?.running ? ` (${formatDuration(entry.durationMs)})` : '';
    item.querySelector('.status-time').textContent =
      `${formatTime(entry?.lastSuccessMs ?? entry?.lastRunMs)}${durationText}`;

    if (enabled && entry?.running) item.appendChild(progressBar(provider.name, breakdown.percent));

    if (enabled && (breakdown.chips.length > 0 || breakdown.categoryChips.length > 0)) {
      const chipsRow = document.createElement('div');
      chipsRow.className = 'status-chips';
      for (const chip of breakdown.chips) {
        chipsRow.appendChild(resultChip(chip.label, chip.tone));
      }
      for (const label of breakdown.categoryChips) {
        chipsRow.appendChild(resultChip(label, 'category'));
      }
      item.appendChild(chipsRow);
    }

    if (enabled && apiConnected && !entry?.running) {
      const actionBar = document.createElement('div');
      actionBar.className = 'status-actions';
      if (entry?.lastErrorCode === 'not_logged_in' && caps?.loginUrl) {
        const openLink = document.createElement('a');
        openLink.className = 'mini-action';
        openLink.id = `site-${id}`;
        openLink.href = caps.loginUrl;
        openLink.target = '_blank';
        openLink.rel = 'noopener noreferrer';
        openLink.innerHTML = `Open ${provider.site} ${UI_ICONS.external}`;
        actionBar.appendChild(openLink);
      }
      if (eligibleCapture(id, currentSettings.captureTypes)) {
        const syncOne = document.createElement('button');
        syncOne.type = 'button';
        syncOne.id = `capture-one-${id}`;
        syncOne.className = 'mini-action';
        syncOne.innerHTML = `${UI_ICONS.play} Sync ${provider.name}`;
        syncOne.addEventListener('click', () => startCapture('syncProvider', id));
        actionBar.appendChild(syncOne);
      }
      if (actionBar.children.length > 0) item.appendChild(actionBar);
    }

    list.appendChild(item);

    const row = document.createElement('div');
    row.className = 'resync-row';
    row.innerHTML = `<div><strong>${provider.name}</strong><small>Uses your current checklist</small></div><button class="button-secondary" type="button">Rescan selected chats</button>`;
    const button = row.querySelector('button');
    button.dataset.provider = id;
    button.id = `rescan-${id}`;
    button.disabled =
      !apiConnected ||
      !eligibleCapture(id, currentSettings.captureTypes) ||
      !enabled ||
      Boolean(entry?.running);
    button.setAttribute('aria-label', `${provider.name}: rescan selected chats`);
    rescans.appendChild(row);
  }

  renderActivityLog(activityLog);
  if (dynamic) restoreControl(focused, `provider-${dynamic[2]}`);
  if (focused === 'clear-log' && el('clear-log').disabled) restoreControl(focused, 'auto-paused');
  if (focused === 'sync-now' && el('sync-now').disabled) restoreControl(focused, 'auto-paused');
}

async function importFileObject(file) {
  if (!file) return;
  const generation = ++pairingGeneration;
  let saved = false;
  const previousConnection = currentSettings;
  try {
    if (file.size > 16_384) throw new Error();
    const candidate = parseConnectionFile(await file.text());
    const verified = await chrome.runtime.sendMessage({ type: 'verifyPairing', candidate });
    if (generation !== pairingGeneration || !sameConnection(previousConnection, currentSettings))
      return;
    if (!verified?.ok || verified.archiveId !== candidate.archiveId) throw new Error();
    const next = { ...readSettings(), ...candidate, token: '' };
    await chrome.storage.local.set({ settings: next });
    saved = true;
    currentSettings = next;
    await chrome.runtime.sendMessage({ type: 'settingsChanged' });
    await checkConnection();
    if (generation !== pairingGeneration) return;
    feedback('import-result', 'Local archive paired.');
    if (apiConnected && document.activeElement === document.body) el('change-connection').focus?.();
  } catch {
    if (generation !== pairingGeneration) return;
    feedback(
      'import-result',
      saved
        ? 'The verified pairing was saved. Check the connection again.'
        : 'Could not verify or save that file. Choose the matching native connection JSON. Your previous connection was not replaced.',
      true,
    );
  }
}

async function startCapture(type = 'syncNow', provider = null) {
  const enabled = provider
    ? { [provider]: currentSettings.providers[provider] }
    : currentSettings.providers;
  if (
    starting ||
    !apiConnected ||
    !describeEffectivePlan(enabled, currentSettings.captureTypes).readyCount
  )
    return;
  starting = true;
  const buttonId = document.activeElement?.id;
  since = Date.now();
  outcomeProviders = enabled;
  el('capture-outcome').textContent = 'Requesting local capture…';
  updateSelection();
  if (buttonId === 'sync-now' && el('sync-now').disabled) restoreControl(buttonId, 'auto-paused');
  try {
    const result = await chrome.runtime.sendMessage({ type, ...(provider ? { provider } : {}) });
    if (!result?.ok) feedback('capture-outcome', friendlyError('', result?.code), true);
    await renderStatus();
  } catch {
    feedback(
      'capture-outcome',
      'Could not start capture. Check the local archive connection.',
      true,
    );
  } finally {
    starting = false;
    updateSelection();
  }
}

renderControls();
for (const id of [
  ...Object.keys(PROVIDERS).map((id) => `provider-${id}`),
  ...Object.keys(CAPTURE_TYPES).map((id) => `capture-${id}`),
  'account',
  'interval',
  'auto-paused',
]) {
  el(id).addEventListener('change', async () => {
    try {
      await persist();
      feedback('saved', 'Your selection is saved.');
      await renderStatus();
    } catch {
      feedback('saved', 'Could not save. Check your settings and try again.', true);
    }
  });
}
el('test-connection').addEventListener('click', checkConnection);
el('sync-now').addEventListener('click', () => startCapture());
el('browser-family').addEventListener('change', updateCommand);
updateCommand();
for (const id of Object.keys(PROVIDERS)) enterToggles(el(`provider-${id}`));
enterToggles(el('auto-paused'));
el('copy-command').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(el('setup-command').textContent);
    feedback('import-result', 'Command copied. Run it in your Memory folder.');
  } catch {
    feedback('import-result', 'Select and copy the command above.');
  }
});
el('import-connection').addEventListener('click', () => el('connection-file').click());
el('change-connection').addEventListener('click', () => {
  el('connection-guide').hidden = false;
  el('connection-ready').hidden = true;
  el('import-connection').focus();
});
el('connection-file').addEventListener('change', async (event) => {
  const file = event.target.files?.[0];
  try {
    await importFileObject(file);
  } finally {
    event.target.value = '';
  }
});

const dropzone = el('dropzone');
if (dropzone) {
  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropzone.className = 'dropzone is-dragging';
  });
  dropzone.addEventListener('dragleave', () => {
    dropzone.className = 'dropzone';
  });
  dropzone.addEventListener('drop', async (event) => {
    event.preventDefault();
    dropzone.className = 'dropzone';
    const file = event.dataTransfer?.files?.[0];
    await importFileObject(file);
  });
}

el('clear-log')?.addEventListener('click', async () => {
  try {
    await chrome.runtime.sendMessage({ type: 'clearActivityLog' });
    await renderStatus();
  } catch {
    // Ignore when worker is inactive.
  }
});

el('resync-list').addEventListener('click', async (event) => {
  const button = event.target.closest?.('button[data-provider]');
  if (!button || button.disabled) return;
  await startCapture('fullSyncProvider', button.dataset.provider);
});
chrome.storage.onChanged.addListener((changes) => {
  if (changes.settings?.newValue) {
    const values = changes.settings.newValue;
    const next = normalizeSettings(values);
    const authChanged = !sameConnection(currentSettings, next);
    showSettings(next);
    if (authChanged) {
      connectionCheck++;
      apiConnected = false;
      checkConnection();
    } else {
      updateSelection();
      renderStatus();
    }
  }
  if (changes.status || changes.activityLog) renderStatus();
});
function showSettings(settings) {
  currentSettings = settings;
  el('account').value = settings.account;
  el('auto-paused').checked = settings.autoPaused;
  el('interval').value = settings.intervalMinutes;
  for (const id of Object.keys(PROVIDERS)) el(`provider-${id}`).checked = settings.providers[id];
  for (const id of Object.keys(CAPTURE_TYPES))
    el(`capture-${id}`).checked = settings.captureTypes[id];
}

showSettings(normalizeSettings((await chrome.storage.local.get('settings')).settings));
const version = chrome.runtime.getManifest?.().version;
el('app-version').textContent = version ? `v${version}` : 'Extension';
updateSelection();
await checkConnection();
