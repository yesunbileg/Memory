// Poll selected AI conversations and save them to the local AI Memory receiver.

import { NotLoggedInError, RateLimitedError } from './lib/common.js';
import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from './providers/index.js';
import {
  DEFAULT_CAPTURE_TYPES,
  normalizeCaptureTypes,
  eligibleCapture,
} from './providers/selection.js';
import { normalizeSettings } from './ui.js';
import { checkConnection } from './worker-auth.js';
import { requestGroups } from './transport.js';

const DEFAULT_SETTINGS = {
  port: 41770,
  token: '',
  account: 'browser-local',
  intervalMinutes: 15,
  providers: DEFAULT_PROVIDER_SETTINGS,
  captureTypes: DEFAULT_CAPTURE_TYPES,
};

const ALARM_NAME = 'hstry-sync';
const CONTINUE_ALARM_PREFIX = 'hstry-sync-continue-';
const MAX_ACTIVITY_LOG = 12;
const activeProviders = new Map();
let profilePromise;
function profileNamespace() {
  return (profilePromise ??= (async () => {
    const { profileNamespace: id } = await chrome.storage.local.get('profileNamespace');
    if (id) return id;
    const fresh = crypto.randomUUID();
    await chrome.storage.local.set({ profileNamespace: fresh });
    return fresh;
  })().catch((err) => {
    profilePromise = null;
    throw err;
  }));
}
let statusWriteQueue = Promise.resolve();
let activityWriteQueue = Promise.resolve();
let captureEpoch = 0;
let captureKey = null;

function observeSettings(settings) {
  const key = JSON.stringify([
    settings.nativeHost,
    settings.archiveId,
    settings.port,
    settings.token,
    settings.account,
    settings.providers,
    settings.captureTypes,
  ]);
  if (key !== captureKey) {
    captureKey = key;
    captureEpoch++;
  }
  return captureEpoch;
}

async function assertCurrentCapture(settings, epoch) {
  observeSettings(await getSettings());
  if (epoch !== captureEpoch) throw localError('selection_changed');
}

async function getSettings() {
  const { settings } = await chrome.storage.local.get('settings');
  const normalized = normalizeSettings(
    {
      ...DEFAULT_SETTINGS,
      ...settings,
      providers: { ...DEFAULT_SETTINGS.providers, ...settings?.providers },
    },
    DEFAULT_SETTINGS,
  );
  return {
    ...normalized,
    nativeHost: settings?.nativeHost,
    archiveId: settings?.archiveId,
    autoPaused: settings?.autoPaused === true,
    captureTypes: normalizeCaptureTypes(settings?.captureTypes),
  };
}

async function getStatus() {
  const { status } = await chrome.storage.local.get('status');
  return status ?? {};
}

async function setStatus(status) {
  await chrome.storage.local.set({ status });
}

function setStatusEntry(name, entry) {
  statusWriteQueue = statusWriteQueue
    .catch(() => {})
    .then(async () => {
      const status = await getStatus();
      status[name] = entry;
      await setStatus(status);
    });
  return statusWriteQueue;
}

function appendActivityLog(record) {
  activityWriteQueue = activityWriteQueue
    .then(async () => {
      const data = await chrome.storage.local.get('activityLog');
      const existing = Array.isArray(data?.activityLog) ? data.activityLog : [];
      const next = [record, ...existing].slice(0, MAX_ACTIVITY_LOG);
      await chrome.storage.local.set({ activityLog: next });
    })
    .catch(() => {});
  return activityWriteQueue;
}

async function clearInterruptedRuns() {
  const status = await getStatus();
  let changed = false;
  for (const entry of Object.values(status)) {
    if (!entry?.running) continue;
    entry.running = false;
    entry.continuationPending = true;
    entry.nextRetryMs ??= Date.now() + 30_000;
    entry.lastError = 'Previous sync was interrupted. Run it again.';
    entry.lastErrorCode = 'interrupted';
    entry.progress = { ...entry.progress, phase: 'failed' };
    changed = true;
  }
  if (changed) await setStatus(status);
}

const startupReady = clearInterruptedRuns()
  .then(ensureAlarm)
  .catch(() => {});

function makePush(
  settings,
  onResult = async () => {},
  beforePush = async () => {},
  archiveId = null,
) {
  return async function push(sourceId, adapter, conversations) {
    if (!settings.nativeHost || !archiveId) throw localError('missing_token');
    // Preflight the entire provider batch before sending any part of it.
    const groups = requestGroups(sourceId, adapter, settings.account, conversations, archiveId);
    for (const group of groups) {
      let created = 0,
        updated = 0,
        unchanged = 0;
      for (const body of group.bodies) {
        await beforePush();
        let data;
        try {
          data = await chrome.runtime.sendNativeMessage(settings.nativeHost, JSON.parse(body));
        } catch {
          throw localError('offline');
        }
        if (data?.error)
          throw localError(
            Object.hasOwn(LOCAL_ERRORS, data.error) ? data.error : 'archive_unavailable',
          );
        const counts = [data?.created, data?.updated, data?.unchanged];
        if (
          data?.conversations !== group.count ||
          !counts.every((n) => Number.isSafeInteger(n) && n >= 0) ||
          counts.reduce((a, b) => a + b, 0) !== group.count
        )
          throw localError('archive_unavailable');
        created += data?.created ?? 0;
        updated += data?.updated ?? 0;
        unchanged += data?.unchanged ?? 0;
      }
      // A chunked snapshot is still one logical conversation receipt.
      created = Math.min(group.count, created);
      updated = Math.min(group.count - created, updated);
      unchanged = Math.min(group.count - created - updated, unchanged);
      await onResult({ accepted: group.count, created, updated, unchanged });
    }
    return conversations.length;
  };
}

function makeRegister(_settings) {
  // AI Memory registers no remote sources. /ingest carries provider identity;
  // uploaded NotebookLM sources are created only by the separately approved sync.
  return async (source, adapter) => ({ source, adapter });
}

async function registerEnabledSources(settings) {
  settings ??= await getSettings();
  const register = makeRegister(settings);
  const results = {};
  for (const [id, provider] of Object.entries(PROVIDERS)) {
    if (!settings.providers[id]) continue;
    try {
      results[id] = await register(provider.sourceId, provider.adapter);
    } catch (error) {
      results[id] = { error: error.message };
    }
  }
  return results;
}

async function checkApi() {
  return checkConnection(await getSettings(), { requireArchive: true });
}

const LOCAL_ERRORS = {
  storage_unavailable: 'Browser storage could not save capture progress. Free space and retry.',
  missing_token: 'Connect AI Memory using your private connection file first.',
  unauthorized: 'This connection file does not match the running AI Memory receiver.',
  offline: 'AI Memory is offline. Run ai-memory connect and keep its terminal open.',
  archive_changed: 'The receiving archive changed during capture. Capture again to replay safely.',
  archive_unavailable: 'AI Memory could not save this batch. Check its terminal and retry.',
  body_too_large:
    'A conversation item is too large for the local receiver. Split the source item before retrying.',
  selection_changed: 'Your selection changed. Capture again to use the updated settings.',
};

function localError(code) {
  return Object.assign(new Error(LOCAL_ERRORS[code]), { code });
}

function safeSyncFailure(error) {
  if (Object.hasOwn(LOCAL_ERRORS, error?.code))
    return { code: error.code, error: LOCAL_ERRORS[error.code] };
  if (error instanceof NotLoggedInError || error?.name === 'NotLoggedInError')
    return { code: 'not_logged_in', error: 'Open this provider and sign in, then sync again.' };
  if (error instanceof RateLimitedError || error?.name === 'RateLimitedError')
    return {
      code: 'rate_limited',
      error: 'This provider is busy. Wait a few minutes and try again.',
    };
  if (error instanceof TypeError && /fetch|network/i.test(error.message))
    return {
      code: 'network_error',
      error: 'Could not reach the provider website. Check your internet connection and retry.',
    };
  return {
    code: 'provider_error',
    error: 'This provider could not finish syncing. Try again in a moment.',
  };
}

async function ensureAlarm() {
  const settings = await getSettings();
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing || existing.periodInMinutes !== settings.intervalMinutes) {
    await chrome.alarms.create(ALARM_NAME, {
      periodInMinutes: settings.intervalMinutes,
      delayInMinutes: 1,
    });
  }
  const status = await getStatus();
  for (const [name, entry] of Object.entries(status)) {
    if (
      !settings.providers[name] ||
      !eligibleCapture(name, settings.captureTypes) ||
      (!entry?.continuationPending && !entry?.nextRetryMs) ||
      (settings.autoPaused && !entry.manualContinuation)
    )
      continue;
    if (!(await chrome.alarms.get(CONTINUE_ALARM_PREFIX + name)))
      await scheduleContinuation(name, entry.nextRetryMs);
  }
}

async function scheduleContinuation(providerName, deadline = 0) {
  await chrome.alarms.create(`${CONTINUE_ALARM_PREFIX}${providerName}`, {
    when: Math.max(Date.now() + 30_000, deadline),
  });
}

async function runSync(
  trigger,
  { onlyProvider = null, full = false, onStarted = async () => {} } = {},
) {
  await startupReady;
  const settings = await getSettings();
  const epoch = observeSettings(settings);
  if (!Object.values(settings.captureTypes).some(Boolean))
    return {
      ok: false,
      code: 'no_capture_types',
      error: 'Choose at least one chat type before capturing.',
    };
  const status = await getStatus();
  if (
    settings.autoPaused &&
    (trigger === 'alarm' ||
      (trigger === 'continuation' && !status[onlyProvider]?.manualContinuation))
  )
    return { ok: false, code: 'paused' };
  if (
    trigger === 'continuation' &&
    !status[onlyProvider]?.continuationPending &&
    !status[onlyProvider]?.nextRetryMs
  )
    return { ok: false, code: 'stale_alarm' };
  const eligible = Object.entries(PROVIDERS).filter(
    ([name]) =>
      settings.providers[name] &&
      eligibleCapture(name, settings.captureTypes) &&
      (!onlyProvider || name === onlyProvider),
  );
  const selected = eligible.filter(([name]) => !activeProviders.has(name));
  if (selected.length === 0)
    return {
      ok: false,
      code: eligible.length ? 'already_syncing' : 'no_enabled_providers',
      error: eligible.length
        ? 'This provider is already syncing.'
        : 'Choose a provider in settings before syncing.',
    };
  const owner = Symbol();
  for (const [name] of selected) activeProviders.set(name, owner);
  const release = (name) => {
    if (activeProviders.get(name) === owner) activeProviders.delete(name);
  };
  try {
    await chrome.action.setBadgeBackgroundColor({ color: '#007aff' });
    await chrome.action.setBadgeText({ text: '…' });

    // Verify local credentials before any provider makes remote account or chat reads.
    const connection = await checkConnection(settings, { requireArchive: true });
    if (!connection.ok) {
      for (const [name] of selected) {
        await setStatusEntry(name, {
          ...(status[name] ?? {}),
          lastRunMs: Date.now(),
          trigger,
          running: false,
          continuationPending: false,
          lastError: connection.error,
          lastErrorCode: connection.code,
          progress: {
            phase: 'failed',
            detected: 0,
            processed: 0,
            accepted: 0,
            created: 0,
            updated: 0,
            unchanged: 0,
          },
        });
        release(name);
      }
      await chrome.action.setBadgeText({ text: '!' });
      await chrome.action.setBadgeBackgroundColor({ color: '#cc3333' });
      return connection;
    }

    let anyError = false;
    let startedReported = false;
    const profile = await profileNamespace();
    const settled = await Promise.allSettled(
      selected.map(async ([name, provider]) => {
        const startedAt = Date.now();
        const captureScope = connection.archiveId
          ? JSON.stringify([
              connection.archiveId,
              name,
              profile,
              settings.account,
              settings.captureTypes,
            ])
          : null;
        const scopeChanged = !captureScope || status[name]?.captureScope !== captureScope;
        if (
          (trigger === 'alarm' || trigger === 'continuation') &&
          !scopeChanged &&
          (status[name]?.lastErrorCode === 'body_too_large' ||
            status[name]?.nextRetryMs > Date.now())
        ) {
          // Permanent payload failures need an explicit retry or changed scope,
          // not an endless sequence of identical scheduled 413 requests.
          anyError = true;
          if (status[name]?.nextRetryMs > Date.now())
            await scheduleContinuation(name, status[name].nextRetryMs);
          release(name);
          return;
        }
        const policyChanged =
          !status[name]?.capturePolicy ||
          JSON.stringify(status[name].capturePolicy) !== JSON.stringify(settings.captureTypes);
        const continuing =
          !full && !scopeChanged && !policyChanged && Boolean(status[name]?.continuationPending);
        const entry = {
          ...(status[name] ?? {}),
          ...((full && name === onlyProvider) || policyChanged || scopeChanged
            ? { state: {} }
            : {}),
          ...(policyChanged || scopeChanged
            ? { lastSuccessMs: null, lastCount: null, selection: null, selectionWarning: null }
            : {}),
          capturePolicy: { ...settings.captureTypes },
          captureScope,
          manualContinuation:
            trigger === 'manual' ||
            trigger === 'full' ||
            (trigger === 'continuation' && status[name]?.manualContinuation),
          continuationPending: false,
          lastRunMs: startedAt,
          trigger,
          running: true,
          progress: {
            ...((continuing && status[name]?.progress) || {}),
            phase: 'discovering',
            detected: 0,
            processed: 0,
            accepted: continuing ? (status[name]?.progress?.accepted ?? 0) : 0,
            created: continuing ? (status[name]?.progress?.created ?? 0) : 0,
            updated: continuing ? (status[name]?.progress?.updated ?? 0) : 0,
            unchanged: continuing ? (status[name]?.progress?.unchanged ?? 0) : 0,
          },
        };
        await setStatusEntry(name, entry);
        if (!startedReported) {
          startedReported = true;
          await onStarted();
        }
        const report = async (progress) => {
          entry.progress = { ...entry.progress, ...progress };
          if (Object.hasOwn(progress, 'selection')) entry.selection = progress.selection;
          if (Object.hasOwn(progress, 'selectionWarning'))
            entry.selectionWarning = progress.selectionWarning;
          await setStatusEntry(name, entry);
        };
        const push = makePush(
          settings,
          async (result) => {
            await report({
              accepted: entry.progress.accepted + result.accepted,
              created: entry.progress.created + result.created,
              updated: entry.progress.updated + result.updated,
              unchanged: (entry.progress.unchanged ?? 0) + result.unchanged,
            });
          },
          () => assertCurrentCapture(settings, epoch),
          connection.archiveId,
        );
        const register = makeRegister(settings);
        try {
          await assertCurrentCapture(settings, epoch);
          const result = await provider.sync({
            state: entry.state ?? {},
            captureTypes: { ...settings.captureTypes },
            push,
            register,
            report,
            guard: () => assertCurrentCapture(settings, epoch),
            checkpoint: async (state) => {
              await assertCurrentCapture(settings, epoch);
              entry.state = state;
              entry.continuationPending = Boolean(state?.pending || state?.discovery);
              await setStatusEntry(name, entry);
            },
            // Provider diagnostics can contain account or conversation text.
            log: () => {},
          });
          await assertCurrentCapture(settings, epoch);
          entry.state = result.state;
          entry.selection = Object.hasOwn(result, 'selection')
            ? result.selection
            : (entry.progress.selection ?? null);
          entry.selectionWarning = Object.hasOwn(result, 'selectionWarning')
            ? result.selectionWarning
            : (entry.progress.selectionWarning ?? null);
          if (!entry.selectionWarning) entry.lastSuccessMs = Date.now();
          else anyError = true;
          entry.lastCount = entry.progress.accepted || result.conversations;
          entry.lastError = null;
          entry.lastErrorCode = null;
          entry.durationMs = Math.max(0, Date.now() - startedAt);
          entry.running = false;
          entry.continuationPending = Boolean(result.hasMore);
          entry.progress = {
            ...entry.progress,
            phase: result.hasMore ? 'queued' : entry.selectionWarning ? 'attention' : 'complete',
            selection: entry.selection,
            selectionWarning: entry.selectionWarning,
          };
          entry.nextRetryMs = null;
          if (result.hasMore) {
            await setStatusEntry(name, entry);
            await scheduleContinuation(name);
          }
          await appendActivityLog({
            id: `${name}-${startedAt}`,
            timestampMs: Date.now(),
            provider: name,
            trigger,
            outcome: result.hasMore ? 'queued' : entry.selectionWarning ? 'warning' : 'success',
            errorCode: null,
            durationMs: entry.durationMs,
            counts: {
              detected: entry.progress.detected ?? 0,
              processed: entry.progress.processed ?? 0,
              created: entry.progress.created ?? 0,
              updated: entry.progress.updated ?? 0,
              unchanged: entry.progress.unchanged ?? 0,
              skipped: entry.selection?.skipped ?? 0,
              unknown: entry.selection?.unknown ?? 0,
            },
          });
        } catch (err) {
          anyError = true;
          const failure = safeSyncFailure(err);
          entry.lastError = failure.error;
          entry.lastErrorCode = failure.code;
          entry.nextRetryMs = [
            'rate_limited',
            'network_error',
            'provider_error',
            'archive_unavailable',
            'offline',
          ].includes(failure.code)
            ? Date.now() + (failure.code === 'rate_limited' ? 300_000 : 60_000)
            : null;
          entry.durationMs = Math.max(0, Date.now() - startedAt);
          entry.running = false;
          entry.continuationPending = false;
          entry.progress = {
            ...entry.progress,
            phase: failure.code === 'selection_changed' ? 'cancelled' : 'failed',
          };
          await appendActivityLog({
            id: `${name}-${startedAt}`,
            timestampMs: Date.now(),
            provider: name,
            trigger,
            outcome: failure.code === 'selection_changed' ? 'cancelled' : 'error',
            errorCode: failure.code,
            durationMs: entry.durationMs,
            counts: {
              detected: entry.progress.detected ?? 0,
              processed: entry.progress.processed ?? 0,
              created: entry.progress.created ?? 0,
              updated: entry.progress.updated ?? 0,
              unchanged: entry.progress.unchanged ?? 0,
              skipped: entry.selection?.skipped ?? 0,
              unknown: entry.selection?.unknown ?? 0,
            },
          });
        } finally {
          release(name);
          await setStatusEntry(name, entry);
          if (entry.nextRetryMs) await scheduleContinuation(name, entry.nextRetryMs);
        }
      }),
    );

    anyError ||= settled.some((s) => s.status === 'rejected');
    await chrome.action.setBadgeText({
      text: anyError ? '!' : activeProviders.size > 0 ? '…' : '',
    });
    if (anyError) {
      await chrome.action.setBadgeBackgroundColor({ color: '#cc3333' });
    }
    return settled.every((s) => s.status === 'rejected')
      ? { ok: false, error: 'Local storage is unavailable. Retry capture.' }
      : { ok: true };
  } finally {
    for (const [name] of selected) release(name);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  registerEnabledSources();
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  registerEnabledSources();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    runSync('alarm');
    return;
  }
  if (alarm.name.startsWith(CONTINUE_ALARM_PREFIX)) {
    const providerName = alarm.name.slice(CONTINUE_ALARM_PREFIX.length);
    if (Object.hasOwn(PROVIDERS, providerName)) {
      runSync('continuation', { onlyProvider: providerName }).then((result) => {
        if (result.code === 'already_syncing') scheduleContinuation(providerName);
      });
    }
  }
});

chrome.action.onClicked.addListener(() => runSync('manual'));

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  // Hold the channel only through the bounded local authentication check.
  // Once a run starts, report progress through storage instead of waiting for
  // provider requests that can outlive the channel or this worker.
  const start = (trigger, options = {}) => {
    let responded = false;
    const respond = (result) => {
      if (responded) return;
      responded = true;
      sendResponse(result);
    };
    runSync(trigger, { ...options, onStarted: async () => respond({ ok: true }) })
      .then(respond)
      .catch(() => respond({ ok: false, error: 'AI Memory could not start syncing. Try again.' }));
  };
  if (message?.type === 'syncNow') {
    start('manual');
    return true;
  }
  if (message?.type === 'syncProvider') {
    const providerName = message.provider;
    if (!Object.hasOwn(PROVIDERS, providerName)) {
      sendResponse({ ok: false, error: 'Unknown provider' });
      return false;
    }
    start('manual', { onlyProvider: providerName, full: false });
    return true;
  }
  if (message?.type === 'fullSyncProvider') {
    const providerName = message.provider;
    if (!Object.hasOwn(PROVIDERS, providerName)) {
      sendResponse({ ok: false, error: 'Unknown provider' });
      return false;
    }
    if (activeProviders.has(providerName)) {
      sendResponse({ ok: false, error: 'This provider is already syncing' });
      return false;
    }
    getSettings()
      .then((settings) => {
        if (!settings.providers[providerName]) {
          sendResponse({ ok: false, error: 'Enable this provider before syncing' });
          return;
        }
        if (activeProviders.has(providerName)) {
          sendResponse({ ok: false, error: 'This provider is already syncing' });
          return;
        }
        start('full', {
          onlyProvider: providerName,
          full: true,
        });
      })
      .catch(() =>
        sendResponse({ ok: false, error: 'AI Memory could not start syncing. Try again.' }),
      );
    return true;
  }
  if (message?.type === 'settingsChanged') {
    getSettings()
      .then((settings) => {
        observeSettings(settings);
        return Promise.all([ensureAlarm(), registerEnabledSources(settings)]);
      })
      .then(([, sources]) => {
        sendResponse({ ok: true, sources });
      });
    return true;
  }
  if (message?.type === 'clearActivityLog') {
    chrome.storage.local.set({ activityLog: [] }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === 'verifyPairing') {
    checkConnection(message.candidate).then(sendResponse);
    return true;
  }
  if (message?.type === 'checkApi') {
    checkApi().then(sendResponse);
    return true;
  }
  return false;
});
