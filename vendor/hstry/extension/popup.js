import {
  DEFAULT_CAPTURE_TYPES,
  DEFAULT_PROVIDER_SETTINGS,
  PROVIDERS,
  PROVIDER_CAPABILITIES,
  formatSyncBreakdown,
  describeEffectivePlan,
  captureOutcome,
  statusForPairing,
  enterToggles,
  progressSemantics,
  restoreControl,
  friendlyError,
  formatTime,
  normalizeSettings,
  providerState,
} from './ui.js';
import { connectionPresentation } from './connection-file.js';
import { PROVIDER_ICONS, UI_ICONS } from './icons.js';

const DEFAULTS = {
  port: 41770,
  token: '',
  account: 'browser-local',
  intervalMinutes: 15,
  providers: DEFAULT_PROVIDER_SETTINGS,
  captureTypes: DEFAULT_CAPTURE_TYPES,
};
const el = (id) => document.getElementById(id);
const filterLabels = {
  projects: 'Projects',
  pinned: 'Pinned',
  agents: 'GPTs & agents',
  recents: 'Recents',
};

let apiConnected = false;
let current = DEFAULTS;
let busy = false;
let connectionCheck = 0;
let since = 0;
let generation = 0;
let requestNotice = '';
let displayScope = '';

async function saveSettingsPatch(patch) {
  const { settings = {} } = await chrome.storage.local.get('settings');
  const next = normalizeSettings(
    {
      ...DEFAULTS,
      ...settings,
      ...patch,
      providers: {
        ...DEFAULT_PROVIDER_SETTINGS,
        ...settings.providers,
        ...(patch.providers ?? {}),
      },
      captureTypes: {
        ...current.captureTypes,
        ...(patch.captureTypes ?? {}),
      },
    },
    DEFAULTS,
  );
  current = next;
  await chrome.storage.local.set({ settings: next });
  try {
    await chrome.runtime.sendMessage({ type: 'settingsChanged' });
  } catch {
    // Worker may be waking up.
  }
  await render();
}

function renderFilters() {
  const container = el('quick-filters');
  container.replaceChildren();
  for (const [key, label] of Object.entries(filterLabels)) {
    const active = Boolean(current.captureTypes[key]);
    const chip = document.createElement('button');
    chip.id = `filter-${key}`;
    chip.type = 'button';
    chip.className = `filter-chip${active ? ' active' : ''}${key === 'recents' ? ' is-recents' : ''}`;
    chip.setAttribute('aria-pressed', String(active));
    chip.textContent = label;
    chip.addEventListener('click', () => {
      saveSettingsPatch({ captureTypes: { [key]: !active } });
    });
    container.appendChild(chip);
  }
}

async function render() {
  const ticket = ++generation;
  const { settings = {}, status: rawStatus } = await chrome.storage.local.get([
    'settings',
    'status',
  ]);
  if (ticket !== generation) return;
  const focused = document.activeElement?.id;
  current = normalizeSettings(
    {
      ...DEFAULTS,
      ...settings,
      providers: { ...DEFAULT_PROVIDER_SETTINGS, ...settings.providers },
    },
    DEFAULTS,
  );

  const status = statusForPairing(rawStatus, current);
  const key = JSON.stringify([current.archiveId, current.account]);
  if (displayScope !== key) {
    displayScope = key;
    since = 0;
    requestNotice = '';
  }
  el('selection-summary').textContent =
    Object.entries(current.captureTypes)
      .filter(([, value]) => value)
      .map(([key]) => filterLabels[key])
      .join(' · ') || 'No chat types selected';
  el('recents-note').textContent = current.captureTypes.recents
    ? 'Ordinary Recents included'
    : 'Ordinary Recents stay out';
  el('schedule').textContent = current.autoPaused
    ? 'Automatic checks paused'
    : `Every ${current.intervalMinutes}m`;

  renderFilters();

  const providersBox = el('providers');
  providersBox.replaceChildren();
  let anyRunning = false;

  for (const [id, provider] of Object.entries(PROVIDERS)) {
    const entry = status[id];
    const enabled = Boolean(current.providers[id]);
    const state = providerState(entry, enabled, {
      apiConnected,
      captureTypes: current.captureTypes,
      providerId: id,
    });
    if (entry?.running) anyRunning = true;
    const caps = PROVIDER_CAPABILITIES[id];
    const breakdown = formatSyncBreakdown(entry);
    const card = document.createElement('article');
    card.className = `popup-provider${enabled ? '' : ' is-off'}`;

    const top = document.createElement('div');
    top.className = 'popup-provider-top';
    top.innerHTML = `
      <span class="provider-mark ${id}" aria-hidden="true">${PROVIDER_ICONS[id]}</span>
      <div class="popup-provider-copy">
        <div class="popup-provider-title">
          <h2>${provider.name}</h2>
          <span class="status-dot ${state.tone}" title="${state.label}"></span>
          <span class="popup-state-label ${state.tone}">${state.label}</span>
        </div>
        <p class="popup-provider-detail"></p>
      </div>
      <label class="switch switch-sm" title="Enable ${provider.name}">
        <input type="checkbox" aria-label="Enable ${provider.name}" />
      </label>
    `;

    const detailEl = top.querySelector('.popup-provider-detail');
    detailEl.textContent = enabled ? state.detail : `${provider.site} · Off`;
    if (state.tone === 'error') detailEl.classList.add('error-copy');
    if (state.tone === 'warning') detailEl.classList.add('warning-copy');

    const toggle = top.querySelector('input');
    toggle.id = `toggle-${id}`;
    toggle.setAttribute('role', 'switch');
    enterToggles(toggle);
    toggle.checked = enabled;
    toggle.addEventListener('change', () => {
      saveSettingsPatch({ providers: { [id]: toggle.checked } });
    });

    card.appendChild(top);

    if (enabled && entry?.running) {
      const bar = document.createElement('div');
      bar.className = `progress-track${breakdown.percent === null ? ' indeterminate' : ''}`;
      progressSemantics(bar, provider.name, breakdown.percent);
      const fill = document.createElement('div');
      fill.className = 'progress-fill';
      if (breakdown.percent !== null) fill.style.width = `${breakdown.percent}%`;
      bar.appendChild(fill);
      card.appendChild(bar);
    }

    const showMetaRow =
      enabled &&
      (entry?.lastRunMs || breakdown.chips.length > 0 || entry?.lastErrorCode === 'not_logged_in');
    if (showMetaRow) {
      const meta = document.createElement('div');
      meta.className = 'popup-provider-meta';

      const left = document.createElement('div');
      left.className = 'popup-meta-chips';
      if (entry?.lastRunMs && !entry?.running) {
        const timeSpan = document.createElement('span');
        timeSpan.className = 'meta-time';
        timeSpan.textContent = formatTime(entry.lastSuccessMs ?? entry.lastRunMs);
        left.appendChild(timeSpan);
      }
      for (const chip of breakdown.chips.slice(0, 2)) {
        const badge = document.createElement('span');
        badge.className = `mini-chip ${chip.tone}`;
        badge.textContent = chip.label;
        left.appendChild(badge);
      }
      meta.appendChild(left);

      const actions = document.createElement('div');
      actions.className = 'popup-meta-actions';
      if (entry?.lastErrorCode === 'not_logged_in' && caps?.loginUrl) {
        const link = document.createElement('a');
        link.className = 'mini-action';
        link.id = `signin-${id}`;
        link.href = caps.loginUrl;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.innerHTML = `Open site ${UI_ICONS.external}`;
        actions.appendChild(link);
      }
      meta.appendChild(actions);
      card.appendChild(meta);
    }

    providersBox.appendChild(card);
  }

  const plan = describeEffectivePlan(current.providers, current.captureTypes);
  const syncBtn = el('sync');
  el('plan-hint').textContent = apiConnected && !plan.readyCount ? plan.summary : '';
  syncBtn.disabled = busy || anyRunning || (apiConnected && !plan.readyCount);
  if (busy || anyRunning) {
    syncBtn.innerHTML = `${UI_ICONS.refresh} Capture in progress…`;
  } else if (!apiConnected) {
    syncBtn.innerHTML = `Connect your computer ${UI_ICONS.arrowRight}`;
  } else {
    syncBtn.innerHTML = `${UI_ICONS.play} Capture selected chats`;
  }
  const outcome = captureOutcome(status, current.providers, since);
  el('notice').textContent =
    outcome || requestNotice || 'No local capture reported for the current sites.';
  if (/^(toggle|filter|signin)-/.test(focused || '')) {
    restoreControl(
      focused,
      focused.startsWith('signin-') ? focused.replace('signin-', 'toggle-') : 'settings',
    );
  } else if (focused === 'sync' && syncBtn.disabled) restoreControl(focused, 'settings');
}

async function checkApi() {
  const generation = ++connectionCheck;
  const requested = current;
  el('api-dot').className = 'status-dot checking';
  el('retry').setAttribute('aria-busy', 'true');
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: 'checkApi' });
  } catch {
    result = { ok: false, code: 'offline' };
  }
  if (
    generation !== connectionCheck ||
    requested.nativeHost !== current.nativeHost ||
    requested.archiveId !== current.archiveId ||
    requested.token !== current.token
  )
    return false;
  apiConnected = result?.ok === true;
  const state = connectionPresentation(result);
  el('api-title').textContent = apiConnected ? 'Connected to your computer' : state.label;
  el('api-detail').textContent = apiConnected
    ? 'Verified local archive · SQLite on this computer'
    : state.detail;
  el('api-dot').className = `status-dot ${state.tone}`;
  el('connection-banner').className =
    `popup-connection is-${apiConnected ? 'connected' : state.tone}`;
  el('retry').setAttribute('aria-busy', 'false');
  await render();
  return apiConnected;
}

el('sync').addEventListener('click', async () => {
  if (busy) return;
  if (!apiConnected) {
    await chrome.runtime.openOptionsPage();
    return;
  }
  if (
    !describeEffectivePlan(current.providers, current.captureTypes).readyCount ||
    el('sync').disabled
  )
    return;
  since = Date.now();
  requestNotice = 'Requesting local capture…';
  busy = true;
  await render();
  try {
    const result = await chrome.runtime.sendMessage({ type: 'syncNow' });
    requestNotice = result?.ok
      ? 'Capture requested. Waiting for local progress.'
      : friendlyError('', result?.code);
  } catch {
    requestNotice = 'Check your connection in settings and try again.';
  } finally {
    busy = false;
    await render();
  }
});

for (const id of ['settings', 'edit-selection', 'open-setup']) {
  el(id)?.addEventListener('click', () => chrome.runtime.openOptionsPage());
}
el('retry').addEventListener('click', checkApi);

chrome.storage.onChanged.addListener(async (changes) => {
  const previous = current;
  const values = changes.settings?.newValue;
  const authChanged =
    values &&
    ((values.nativeHost ?? '') !== previous.nativeHost ||
      (values.archiveId ?? '') !== previous.archiveId ||
      (values.token ?? '') !== previous.token);
  if (authChanged) {
    connectionCheck++;
    apiConnected = false;
  }
  if (changes.status || changes.settings) await render();
  if (authChanged) await checkApi();
});

await render();
await checkApi();
