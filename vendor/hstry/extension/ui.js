import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from './providers/index.js';
import {
  CAPTURE_TYPES,
  DEFAULT_CAPTURE_TYPES,
  normalizeCaptureTypes,
} from './providers/selection.js';

export { DEFAULT_PROVIDER_SETTINGS, PROVIDERS };
export { CAPTURE_TYPES, DEFAULT_CAPTURE_TYPES };

export const PROVIDER_CAPABILITIES = Object.freeze({
  chatgpt: {
    projects: true,
    pinned: true,
    agents: true,
    recents: true,
    loginUrl: 'https://chatgpt.com/',
    badges: [
      { key: 'projects', label: 'Projects' },
      { key: 'pinned', label: 'Pinned' },
      { key: 'agents', label: 'GPTs' },
      { key: 'recents', label: 'Recents' },
    ],
  },
  claude: {
    projects: true,
    pinned: true,
    agents: false,
    recents: true,
    loginUrl: 'https://claude.ai/',
    badges: [
      { key: 'projects', label: 'Projects' },
      { key: 'pinned', label: 'Starred' },
      { key: 'recents', label: 'Recents' },
    ],
  },
  gemini: {
    projects: false,
    pinned: false,
    agents: false,
    recents: true,
    loginUrl: 'https://gemini.google.com/app',
    badges: [{ key: 'recents', label: 'Recents only' }],
  },
});

export function normalizeSettings(values, defaults) {
  const port = Number(values.port);
  const intervalMinutes = Number(values.intervalMinutes);
  return {
    port: Number.isInteger(port) && port >= 1 && port <= 65535 ? port : defaults.port,
    token: String(values.token ?? '').trim(),
    account:
      String(values.account ?? defaults.account ?? 'browser-local').trim() || 'browser-local',
    intervalMinutes:
      Number.isInteger(intervalMinutes) && intervalMinutes >= 1 && intervalMinutes <= 720
        ? intervalMinutes
        : defaults.intervalMinutes,
    providers: Object.fromEntries(
      Object.keys(PROVIDERS).map((id) => [id, Boolean(values.providers?.[id])]),
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

export function providerState(
  entry,
  enabled = true,
  { apiConnected = false, captureTypes = null, providerId = null } = {},
) {
  if (!enabled)
    return { tone: 'muted', label: 'Off', detail: 'Turn on to capture selected chat types.' };
  const types = captureTypes ? normalizeCaptureTypes(captureTypes) : null;
  if (
    apiConnected &&
    providerId === 'gemini' &&
    types &&
    !types.recents &&
    (types.projects || types.pinned || types.agents)
  ) {
    return {
      tone: 'warning',
      label: 'Selection unavailable',
      detail:
        'Gemini cannot identify projects, pinned chats or agents yet. With Ordinary Recents off, Gemini chats stay out.',
    };
  }
  if (
    entry?.lastRunMs &&
    captureTypes &&
    JSON.stringify(entry.capturePolicy) !== JSON.stringify(normalizeCaptureTypes(captureTypes))
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
  if (
    apiConnected &&
    (entry.lastErrorCode === 'offline' ||
      (entry.lastError &&
        (entry.lastError.includes('cannot reach hstry-api') ||
          entry.lastError.includes('AI Memory is offline') ||
          entry.lastError.includes('Failed to fetch'))))
  ) {
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
            ? `${progress.accepted} accepted · ${progress.detected} detected`
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
  const text = String(error ?? 'Unknown sync error');
  if (code === 'selection_changed' || text.includes('selection changed'))
    return 'Your selection changed. Capture again with your new checklist.';
  if (code === 'not_logged_in' || text.includes('not logged in') || text.includes('sign in'))
    return 'Open this provider and sign in, then sync again.';
  if (
    code === 'offline' ||
    text.includes('cannot reach hstry-api') ||
    text.includes('AI Memory is offline') ||
    text.includes('Failed to fetch')
  ) {
    return 'Start AI Memory on this computer, then try again.';
  }
  if (code === 'unauthorized' || text.includes('rejected the token') || text.includes('401')) {
    return 'The ingest token does not match the local API.';
  }
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

export function describeEffectivePlan(providers = {}, captureTypes = {}) {
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
  if (selectedKeys.length === 0) {
    return {
      readyCount: 0,
      summary: 'Select at least one chat type above before starting capture.',
      items: [],
    };
  }
  const items = enabledProviders.map((id) => {
    const caps = PROVIDER_CAPABILITIES[id] ?? {};
    const matched = selectedKeys.filter((key) => caps[key]);
    const unsupported = selectedKeys.filter((key) => !caps[key]);
    const name = PROVIDERS[id].name;
    if (matched.length === 0) {
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
      ? Math.max(6, Math.min(100, Math.round((processed / detected) * 100)))
      : entry.running
        ? null
        : null;
  return { chips, categoryChips, percent };
}
