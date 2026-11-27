import { connectionPresentation } from './connection-file.js';
import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from './providers/index.js';
import {
  CAPTURE_TYPES,
  DEFAULT_CAPTURE_TYPES,
  normalizeCaptureTypes,
  sameCaptureTypes,
  eligibleCapture,
  supportedCaptureTypes,
} from './providers/selection.js';

export { DEFAULT_PROVIDER_SETTINGS, PROVIDERS };
export { CAPTURE_TYPES, DEFAULT_CAPTURE_TYPES };

export const PROVIDER_CAPABILITIES = Object.freeze({
  chatgpt: {
    loginUrl: 'https://chatgpt.com/',
    badges: [
      { key: 'projects', label: 'Projects' },
      { key: 'pinned', label: 'Pinned' },
      { key: 'agents', label: 'GPTs' },
      { key: 'recents', label: 'Recents' },
    ],
  },
  claude: {
    loginUrl: 'https://claude.ai/',
    badges: [
      { key: 'projects', label: 'Projects' },
      { key: 'pinned', label: 'Starred' },
      { key: 'recents', label: 'Recents' },
    ],
  },
  gemini: {
    loginUrl: 'https://gemini.google.com/app',
    badges: [{ key: 'recents', label: 'Recents only' }],
  },
});

export const DEFAULT_SETTINGS = Object.freeze({
  port: 41770,
  token: '',
  nativeHost: '',
  archiveId: '',
  account: 'browser-local',
  intervalMinutes: 15,
  autoPaused: false,
  providers: DEFAULT_PROVIDER_SETTINGS,
  captureTypes: DEFAULT_CAPTURE_TYPES,
});

export function normalizeSettings(values = {}, defaults = DEFAULT_SETTINGS) {
  values ??= {};
  const port = Number(values.port);
  const intervalMinutes = Number(values.intervalMinutes);
  return {
    port: Number.isInteger(port) && port >= 1 && port <= 65535 ? port : defaults.port,
    token: String(values.token ?? '').trim(),
    autoPaused: values.autoPaused === true,
    nativeHost: String(values.nativeHost ?? ''),
    archiveId: String(values.archiveId ?? ''),
    account:
      String(values.account ?? defaults.account ?? 'browser-local').trim() || 'browser-local',
    intervalMinutes:
      Number.isInteger(intervalMinutes) && intervalMinutes >= 1 && intervalMinutes <= 720
        ? intervalMinutes
        : defaults.intervalMinutes,
    providers: Object.fromEntries(
      Object.keys(PROVIDERS).map((id) => [
        id,
        Boolean(values.providers?.[id] ?? defaults.providers?.[id]),
      ]),
    ),
    captureTypes: normalizeCaptureTypes(values.captureTypes),
  };
}

export function formatTime(ms, now = Date.now()) {
  if (!ms) return 'Never';
  const elapsed = Math.max(0, now - ms);
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return 'Just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return rem ? `${mins}m ${rem}s` : `${mins}m`;
}

// Older profiles recorded unstructured loopback failures; newer workers record a code.
function isOffline(error, code) {
  const text = String(error ?? '');
  return (
    code === 'offline' ||
    text.includes('cannot reach hstry-api') ||
    text.includes('AI Memory is offline') ||
    text.includes('Failed to fetch')
  );
}

export function providerState(
  entry,
  enabled = true,
  { apiConnected = false, captureTypes = null, providerId = null, chatgptSyncList } = {},
) {
  if (!enabled)
    return { tone: 'muted', label: 'Off', detail: 'Turn on to capture selected chat types.' };
  const types = captureTypes ? normalizeCaptureTypes(captureTypes) : null;
  if (
    apiConnected &&
    providerId &&
    types &&
    !eligibleCapture(providerId, types, chatgptSyncList) &&
    (Object.values(types).some(Boolean) || (providerId === 'chatgpt' && chatgptSyncList))
  ) {
    return {
      tone: 'warning',
      label: 'Selection unavailable',
      detail:
        providerId === 'chatgpt' && chatgptSyncList
          ? 'Your ChatGPT sync list is empty. Add items in the ChatGPT sidebar.'
          : providerId !== 'gemini'
            ? 'Choose a supported chat type or enable another website.'
            : 'Gemini cannot identify projects, pinned chats or agents yet. With Ordinary Recents off, Gemini chats stay out.',
    };
  }
  if (
    entry?.lastRunMs &&
    captureTypes &&
    !sameCaptureTypes(entry.capturePolicy, normalizeCaptureTypes(captureTypes))
  ) {
    return {
      tone: 'idle',
      label: 'Selection updated',
      detail: 'Capture again to apply your new checklist.',
    };
  }
  if (entry?.running) {
    const progress = entry.progress ?? {};
    if (progress.phase === 'discovering') {
      return {
        tone: 'syncing',
        label: 'Discovering',
        detail: 'Checking for changed conversations…',
      };
    }
    return {
      tone: 'syncing',
      label: 'Syncing',
      detail: progressDetail(progress),
    };
  }
  if (entry?.continuationPending || entry?.progress?.phase === 'queued') {
    return {
      tone: 'syncing',
      label: 'Continuing',
      detail: `${progressDetail(entry.progress ?? {})} · next batch queued`,
    };
  }
  if (!apiConnected)
    return {
      tone: 'muted',
      label: 'Waiting for connection',
      detail: 'Connect your computer to start saving chats.',
    };
  if (!entry?.lastRunMs)
    return {
      tone: 'idle',
      label: 'Ready to sync',
      detail: 'Selected chat types will appear here.',
    };
  if (isOffline(entry.lastError, entry.lastErrorCode)) {
    return {
      tone: 'idle',
      label: 'Ready to retry',
      detail: 'The API is connected now. Run sync again.',
    };
  }
  if (entry.lastError)
    return {
      tone: 'error',
      label: 'Needs attention',
      detail: friendlyError(entry.lastError, entry.lastErrorCode),
    };
  if (entry.selectionWarning)
    return { tone: 'warning', label: 'Some chats skipped', detail: entry.selectionWarning };
  if (entry.progress?.phase === 'complete') {
    const progress = entry.progress;
    const changed = progress.created + progress.updated;
    return {
      tone: 'success',
      label: 'Up to date',
      detail:
        changed > 0
          ? `${progress.created} new · ${progress.updated} updated · ${progress.detected} detected`
          : progress.accepted > 0
            ? `${progress.unchanged ?? progress.accepted} unchanged · ${progress.detected} detected`
            : (entry.selection?.skipped ?? 0) > 0
              ? `No new chats · ${entry.selection.skipped} outside your selection`
              : `No changes · ${progress.detected} checked`,
    };
  }
  return {
    tone: 'success',
    label: 'Up to date',
    detail: `${entry.lastCount ?? 0} conversation${entry.lastCount === 1 ? '' : 's'} synced last run.`,
  };
}

export function progressDetail(progress) {
  const detected = progress.detected ?? 0;
  const processed = progress.processed ?? 0;
  const parts = [`${detected} detected`, `${processed} processed`];
  if ((progress.created ?? 0) > 0) parts.push(`${progress.created} new`);
  if ((progress.updated ?? 0) > 0) parts.push(`${progress.updated} updated`);
  return parts.join(' · ');
}

export function friendlyError(error, code = null) {
  if (code === 'sync_stopped') return 'Sync stopped.';
  if (code === 'interrupted') return 'Sync interrupted. Resuming from the last saved batch.';
  const text = String(error ?? 'Unknown sync error');
  if (code === 'selection_changed' || text.includes('selection changed'))
    return 'Your selection changed. Capture again with your new checklist.';
  if (code === 'not_logged_in' || text.includes('not logged in') || text.includes('sign in'))
    return 'Open this provider and sign in, then sync again.';
  if (code === 'offline') return connectionPresentation({ code }).detail;
  if (isOffline(text, code)) return 'Start AI Memory on this computer, then try again.';
  // Reuse the pairing instructions shown in Settings, including after a legacy
  // bearer failure. Never interpolate raw errors into user-facing text.
  if (['legacy_pairing', 'archive_changed', 'incompatible_server', 'unauthorized'].includes(code)) {
    return connectionPresentation({ code }).detail;
  }
  if (text.includes('rejected the token') || text.includes('401'))
    return connectionPresentation({ code: 'legacy_pairing' }).detail;
  if (code === 'rate_limited' || text.includes('429') || text.includes('RateLimited')) {
    return 'The provider is rate-limiting requests. Wait a few minutes and retry.';
  }
  if (code === 'archive_unavailable') {
    return 'AI Memory could not write to the archive. Check its terminal and try again.';
  }
  if (code === 'network_error') {
    return 'Could not reach the provider website. Check your internet connection and retry.';
  }
  if (code === 'missing_token' || text.includes('token'))
    return 'Choose your connection file to finish setup.';
  return 'This provider could not finish. Open the site, check your sign-in, and try again.';
}

export function describeEffectivePlan(providers = {}, captureTypes = {}, chatgptSyncList) {
  const policy = normalizeCaptureTypes(captureTypes);
  const selectedKeys = Object.entries(policy)
    .filter(([, enabled]) => enabled)
    .map(([key]) => key);
  const labels = {
    projects: 'Projects',
    pinned: 'Pinned',
    agents: 'GPTs & agents',
    recents: 'Ordinary Recents',
  };
  const enabledProviders = Object.keys(PROVIDERS).filter((id) => Boolean(providers?.[id]));
  if (enabledProviders.length === 0) {
    return {
      readyCount: 0,
      summary: 'Turn on at least one website under Sources to start capturing.',
      items: [],
    };
  }
  if (selectedKeys.length === 0 && !(chatgptSyncList && enabledProviders.includes('chatgpt'))) {
    return {
      readyCount: 0,
      summary: 'Select at least one chat type above before starting capture.',
      items: [],
    };
  }
  const items = enabledProviders.map((id) => {
    const caps = supportedCaptureTypes[id] ?? [];
    const matched = selectedKeys.filter((key) => caps.includes(key));
    const unsupported = selectedKeys.filter((key) => !caps.includes(key));
    const name = PROVIDERS[id].name;
    if (id === 'chatgpt' && chatgptSyncList) {
      const willCapture = eligibleCapture(id, policy, chatgptSyncList);
      return {
        id,
        name,
        willCapture,
        note: willCapture
          ? 'ChatGPT uses only saved sidebar items for the current user and workspace.'
          : 'ChatGPT sync list is empty; no ChatGPT chats will be captured.',
      };
    }
    if (!selectedKeys.length)
      return { id, name, willCapture: false, note: name + ': no chat types selected.' };
    if (!eligibleCapture(id, policy)) {
      return {
        id,
        name,
        willCapture: false,
        note: `${name}: skips all chats (${unsupported.map((k) => labels[k]).join(', ')} not supported — turn on Ordinary Recents to include).`,
      };
    }
    return {
      id,
      name,
      willCapture: true,
      note: `${name}: captures ${matched.map((k) => labels[k]).join(', ')}${unsupported.length ? ` (${unsupported.map((k) => labels[k]).join(', ')} unavailable)` : ''}.`,
    };
  });
  const readyCount = items.filter((item) => item.willCapture).length;
  const summary = items.map((item) => item.note).join(' ');
  return { readyCount, summary, items };
}

export function formatSyncBreakdown(entry) {
  if (!entry) return { chips: [], categoryChips: [], percent: null };
  const progress = entry.progress ?? {};
  const selection = entry.selection ?? {};
  const chips = [];
  if ((progress.created ?? 0) > 0)
    chips.push({ label: `+${progress.created} new`, tone: 'success' });
  if ((progress.updated ?? 0) > 0)
    chips.push({ label: `${progress.updated} updated`, tone: 'info' });
  if ((progress.unchanged ?? 0) > 0)
    chips.push({ label: `${progress.unchanged} unchanged`, tone: 'muted' });
  if ((selection.skipped ?? 0) > 0)
    chips.push({ label: `${selection.skipped} skipped`, tone: 'muted' });
  if ((selection.unknown ?? 0) > 0)
    chips.push({ label: `${selection.unknown} unclassified`, tone: 'warning' });
  const byType = selection.byType ?? {};
  const typeLabels = {
    projects: 'Projects',
    pinned: 'Pinned',
    agents: 'GPTs/Agents',
    recents: 'Recents',
  };
  const categoryChips = Object.entries(typeLabels)
    .filter(([key]) => (byType[key] ?? 0) > 0)
    .map(([key, label]) => `${label}: ${byType[key]}`);
  const detected = progress.detected ?? 0;
  const processed = progress.processed ?? 0;
  const percent =
    entry.running && detected > 0
      ? Math.max(0, Math.min(100, Math.round((processed / detected) * 100)))
      : null;
  return { chips, categoryChips, percent };
}

// Status is keyed by provider in profile storage, not by archive. Do not
// attribute an older site's counts to a newly paired archive or renamed profile.
export function statusForPairing(status, settings) {
  if (!settings.archiveId) return {};
  return Object.fromEntries(
    Object.entries(status ?? {}).filter(([provider, entry]) => {
      if (typeof entry?.captureScope !== 'string') return false;
      try {
        const scope = JSON.parse(entry.captureScope);
        return (
          Array.isArray(scope) &&
          scope.length === 5 &&
          scope[0] === settings.archiveId &&
          scope[1] === provider &&
          scope[3] === settings.account
        );
      } catch {
        return false;
      }
    }),
  );
}

// The receiver acknowledges local capture. NotebookLM publication has its own status.
export function captureProgress(status, providers, since = 0) {
  const active = Object.entries(status)
    .filter(
      ([id, entry]) =>
        providers[id] && entry.lastRunMs >= since && (entry.running || entry.continuationPending),
    )
    .map(([, entry]) => entry);
  if (!active.length) return null;
  let checked = 0,
    selected = 0,
    saved = 0,
    accounts = 0,
    accountsChecked = 0;
  for (const { progress = {}, selection } of active) {
    checked += (progress.processed ?? 0) + (progress.alreadyCurrent ?? 0);
    selected += selection?.selected ?? progress.selection?.selected ?? 0;
    saved += progress.accepted ?? 0;
    accounts += progress.accountsTotal ?? 0;
    accountsChecked += progress.accountsChecked ?? 0;
  }
  const running = active.some((entry) => entry.running);
  const stopping = active.some((entry) => entry.stopRequested);
  const recovering = active.some((entry) => entry.progress?.phase === 'recovering');
  const discovering = active.some(
    (entry) => entry.running && entry.progress?.phase === 'discovering',
  );
  const latestProgress = Math.max(
    0,
    ...active.filter((entry) => entry.running).map((entry) => entry.progressAtMs || 0),
  );
  const waitAt = !stopping && latestProgress ? latestProgress + 35_000 : 0;
  const now = Date.now();
  const chatMax = !discovering && selected > 0 && accounts <= 1 ? selected : null;
  const max = accounts > 1 ? accounts : chatMax;
  let stage = 'Capturing selected chats…';
  if (stopping) stage = 'Stopping sync…';
  else if (recovering) stage = 'Sync interrupted. Resuming from the last saved batch…';
  else if (!running) stage = 'Next batch queued.';
  else if (waitAt && waitAt <= now) stage = 'Waiting for sync progress…';
  else if (discovering) stage = 'Checking selected accounts…';
  const counts = [
    chatMax
      ? `${Math.min(checked, chatMax)}/${chatMax} checked`
      : checked > 0
        ? `${checked} checked`
        : null,
    chatMax || checked || saved ? `${saved} saved` : null,
    accounts > 1 ? `${accountsChecked}/${accounts} accounts checked` : null,
  ].filter(Boolean);
  return {
    value: accounts > 1 ? accountsChecked : chatMax ? Math.min(checked, chatMax) : null,
    max,
    text: [stage, counts.join(' · ')].filter(Boolean).join(' '),
    ...(waitAt > now ? { refreshAt: waitAt } : {}),
  };
}

export function captureOutcome(status, providers, since = 0) {
  const runs = Object.entries(status).filter(
    ([id, entry]) => providers[id] && entry?.lastRunMs && entry.lastRunMs >= since,
  );
  if (!runs.length) return null;
  if (runs.some(([, entry]) => entry.running)) return { label: 'Capturing selected chats…' };
  if (runs.some(([, entry]) => entry.continuationPending)) return { label: 'Next batch queued' };
  const totals = { created: 0, updated: 0, unchanged: 0, alreadyCurrent: 0, skipped: 0 };
  const problems = [];
  const missingTabs = [];
  let missingTabCount = 0;
  let stopped = false;
  let complete = 0;
  for (const [id, entry] of runs) {
    for (const key of ['created', 'updated', 'unchanged', 'alreadyCurrent'])
      totals[key] += entry.progress?.[key] ?? 0;
    totals.skipped += entry.selection?.skipped ?? 0;
    if (entry.lastErrorCode === 'sync_stopped') stopped = true;
    else if (entry.lastErrorCode || entry.lastError || entry.selectionWarning) {
      const name = PROVIDERS[id]?.name ?? 'Website';
      const unavailable = entry.progress?.accountsUnavailable ?? 0;
      if (entry.lastErrorCode === 'not_logged_in') problems.push(`Sign in · ${name}`);
      else if (unavailable > 0) {
        missingTabCount += unavailable;
        missingTabs.push(unavailable === 1 ? name : `${name} (${unavailable} accounts)`);
        if (entry.progress?.tabBridgeFailures > 0) problems.push(`Tab connection failed · ${name}`);
      } else if (entry.selectionWarning) {
        const warning = entry.selectionWarning;
        const label = /could not be read|capture is incomplete|receipt incomplete/i.test(warning)
          ? 'Incomplete capture'
          : /discovery is incomplete/i.test(warning)
            ? 'Incomplete chat list'
            : 'Unverified chats';
        problems.push(`${label} · ${name}`);
      } else problems.push(`${name} · ${friendlyError('', entry.lastErrorCode)}`);
    } else complete++;
  }
  if (missingTabCount) {
    const tab = missingTabCount === 1 ? 'tab' : 'tabs';
    problems.unshift(`Open signed-in ${tab} · ${missingTabs.join(', ')}`);
  }
  const { created, updated, unchanged, alreadyCurrent, skipped } = totals;
  const current = unchanged + alreadyCurrent;
  const counts = [
    created > 0 && `${created} new`,
    updated > 0 && `${updated} updated`,
    current > 0 && `${current} unchanged`,
    skipped > 0 && `${skipped} skipped`,
  ]
    .filter(Boolean)
    .join(' · ');
  let label = 'No matching chats';
  if (stopped) label = 'Sync stopped';
  else if (problems.length)
    label = complete || created + updated + current > 0 ? 'Partial sync' : 'Sync incomplete';
  else if (created + updated > 0) label = 'Done';
  else if (current > 0) label = 'Up to date';
  return { label, counts, actions: problems.join('\n') };
}

export function enterToggles(input) {
  // Space is native checkbox activation. Enter should activate once, including
  // when assistive technology follows keyboard focus on the switch.
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.repeat) {
      event.preventDefault();
      input.click();
    }
  });
}

// Keep the visible fill and the announced percentage in agreement on both surfaces.
export function progressBar(name, percent) {
  const bar = document.createElement('div');
  bar.className = `progress-track${percent === null ? ' indeterminate' : ''}`;
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', `${name} local capture`);
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  const fill = document.createElement('div');
  fill.className = 'progress-fill';
  if (percent !== null) {
    bar.setAttribute('aria-valuenow', String(percent));
    fill.style.width = `${percent}%`;
  }
  bar.appendChild(fill);
  return bar;
}

export function resultChip(label, tone) {
  const chip = document.createElement('span');
  chip.className = `mini-chip ${tone}`;
  chip.textContent = label;
  return chip;
}

export function sameConnection(previous, next) {
  return (
    previous.nativeHost === next.nativeHost &&
    previous.archiveId === next.archiveId &&
    previous.token === next.token
  );
}

export async function updateLocalSettings(update) {
  const write = async () => {
    const { settings } = await chrome.storage.local.get('settings');
    const next = update(settings ?? {});
    if (!next) return false;
    await chrome.storage.local.set({ settings: next });
    return true;
  };
  return navigator.locks ? navigator.locks.request('ai-memory-settings', write) : write();
}

// Rebuilt controls have stable IDs. If a focused action no longer exists,
// continue at its provider switch (or another visible, actionable control).
export function restoreControl(id, fallback) {
  if (!id) return;
  const first = document.getElementById(id);
  const target =
    first && !first.disabled && !first.closest?.('[hidden]')
      ? first
      : document.getElementById(fallback);
  if (target && document.activeElement !== target) target.focus?.({ preventScroll: true });
}
