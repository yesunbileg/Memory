// hstry sync service worker: polls AI chat platforms on an alarm and pushes
// new/updated conversations to a local hstry-api instance (POST /ingest).

import { NotLoggedInError, RateLimitedError } from './lib/common.js';
import { DEFAULT_PROVIDER_SETTINGS, PROVIDERS } from './providers/index.js';
import { DEFAULT_CAPTURE_TYPES, normalizeCaptureTypes } from './providers/selection.js';
import { normalizeSettings } from './ui.js';
import { checkConnection } from './worker-auth.js';

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
const activeProviders = new Set();
let statusWriteQueue = Promise.resolve();

async function getSettings() {
  const { settings } = await chrome.storage.local.get('settings');
  const normalized = normalizeSettings({
    ...DEFAULT_SETTINGS,
    ...settings,
    providers: { ...DEFAULT_SETTINGS.providers, ...settings?.providers },
  }, DEFAULT_SETTINGS);
  return { ...normalized, captureTypes: normalizeCaptureTypes(settings?.captureTypes) };
}

async function getStatus() {
  const { status } = await chrome.storage.local.get('status');
  return status ?? {};
}

async function setStatus(status) {
  await chrome.storage.local.set({ status });
}

function setStatusEntry(name, entry) {
  statusWriteQueue = statusWriteQueue.then(async () => {
    const status = await getStatus();
    status[name] = entry;
    await setStatus(status);
  });
  return statusWriteQueue;
}

async function clearInterruptedRuns() {
  const status = await getStatus();
  let changed = false;
  for (const entry of Object.values(status)) {
    if (!entry?.running) continue;
    entry.running = false;
    entry.lastError = 'Previous sync was interrupted. Run it again.';
    entry.progress = { ...entry.progress, phase: 'failed' };
    changed = true;
  }
  if (changed) await setStatus(status);
}

const startupReady = clearInterruptedRuns();

function makePush(settings, onResult = async () => {}) {
  const url = `http://127.0.0.1:${settings.port}/ingest`;
  return async function push(sourceId, adapter, conversations) {
    if (!settings.token) throw localError('missing_token');
    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${settings.token}`,
        },
        redirect: 'error',
        credentials: 'omit',
        body: JSON.stringify({ source: sourceId, adapter, account: settings.account, conversations }),
      });
    } catch {
      throw localError('offline');
    }
    if (res.status === 401) {
      throw localError('unauthorized');
    }
    if (!res.ok) {
      throw localError('archive_unavailable');
    }
    const data = await res.json();
    await onResult({
      accepted: data?.conversations ?? conversations.length,
      created: data?.created ?? 0,
      updated: data?.updated ?? 0,
    });
    return data?.conversations ?? conversations.length;
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
  return checkConnection(await getSettings());
}

const LOCAL_ERRORS = {
  missing_token: 'Connect AI Memory using your private connection file first.',
  unauthorized: 'This connection file does not match the running AI Memory receiver.',
  offline: 'AI Memory is offline. Run ai-memory connect and keep its terminal open.',
  archive_unavailable: 'AI Memory could not save this batch. Check its terminal and retry.',
};

function localError(code) {
  return Object.assign(new Error(LOCAL_ERRORS[code]), { code });
}

function safeSyncFailure(error) {
  if (Object.hasOwn(LOCAL_ERRORS, error?.code)) return { code: error.code, error: LOCAL_ERRORS[error.code] };
  if (error instanceof NotLoggedInError) return { code: 'not_logged_in', error: 'Open this provider and sign in, then sync again.' };
  if (error instanceof RateLimitedError) return { code: 'rate_limited', error: 'This provider is busy. Wait a few minutes and try again.' };
  return { code: 'provider_error', error: 'This provider could not finish syncing. Try again in a moment.' };
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
}

async function scheduleContinuation(providerName) {
  await chrome.alarms.create(`${CONTINUE_ALARM_PREFIX}${providerName}`, {
    delayInMinutes: 0.1,
  });
}

async function runSync(
  trigger,
  { onlyProvider = null, full = false, onStarted = async () => {} } = {}
) {
  await startupReady;
  const settings = await getSettings();
  const status = await getStatus();
  const eligible = Object.entries(PROVIDERS).filter(
    ([name]) =>
      settings.providers[name] &&
      (!onlyProvider || name === onlyProvider)
  );
  const selected = eligible.filter(([name]) => !activeProviders.has(name));
  if (selected.length === 0) return {
    ok: false,
    code: eligible.length ? 'already_syncing' : 'no_enabled_providers',
    error: eligible.length ? 'This provider is already syncing.' : 'Choose a provider in settings before syncing.',
  };
  for (const [name] of selected) activeProviders.add(name);
  await chrome.action.setBadgeText({ text: '…' });

  // Verify local credentials before any provider makes remote account or chat reads.
  const connection = await checkConnection(settings);
  if (!connection.ok) {
    for (const [name] of selected) {
      await setStatusEntry(name, {
        ...(status[name] ?? {}), lastRunMs: Date.now(), trigger, running: false,
        continuationPending: false, lastError: connection.error, lastErrorCode: connection.code,
        progress: { phase: 'failed', detected: 0, processed: 0, accepted: 0, created: 0, updated: 0 },
      });
      activeProviders.delete(name);
    }
    await chrome.action.setBadgeText({ text: '!' });
    await chrome.action.setBadgeBackgroundColor({ color: '#cc3333' });
    return connection;
  }

  let anyError = false;
  let startedReported = false;
  await Promise.all(
    selected.map(async ([name, provider]) => {
      const policyChanged = !status[name]?.capturePolicy ||
        JSON.stringify(status[name].capturePolicy) !== JSON.stringify(settings.captureTypes);
      const entry = {
        ...(status[name] ?? {}),
        ...((full && name === onlyProvider) || policyChanged ? { state: {} } : {}),
        ...(policyChanged ? { lastSuccessMs: null, lastCount: null, selection: null, selectionWarning: null } : {}),
        capturePolicy: { ...settings.captureTypes },
        continuationPending: false,
        lastRunMs: Date.now(),
        trigger,
        running: true,
        progress: {
          phase: 'discovering',
          detected: 0,
          processed: 0,
          accepted: 0,
          created: 0,
          updated: 0,
        },
      };
      await setStatusEntry(name, entry);
      if (!startedReported) {
        startedReported = true;
        await onStarted();
      }
      const report = async progress => {
        entry.progress = { ...entry.progress, ...progress };
        if (Object.hasOwn(progress, 'selection')) entry.selection = progress.selection;
        if (Object.hasOwn(progress, 'selectionWarning')) entry.selectionWarning = progress.selectionWarning;
        await setStatusEntry(name, entry);
      };
      const push = makePush(settings, async result => {
        await report({
          accepted: entry.progress.accepted + result.accepted,
          created: entry.progress.created + result.created,
          updated: entry.progress.updated + result.updated,
        });
      });
      const register = makeRegister(settings);
      try {
        const result = await provider.sync({
          state: entry.state ?? {},
          captureTypes: { ...settings.captureTypes },
          push,
          register,
          report,
          // Provider diagnostics can contain account or conversation text.
          log: () => {},
        });
        entry.state = result.state;
        entry.selection = Object.hasOwn(result, 'selection') ? result.selection : entry.progress.selection ?? null;
        entry.selectionWarning = Object.hasOwn(result, 'selectionWarning') ? result.selectionWarning : entry.progress.selectionWarning ?? null;
        if (!entry.selectionWarning) entry.lastSuccessMs = Date.now();
        else anyError = true;
        entry.lastCount = result.conversations;
        entry.lastError = null;
        entry.lastErrorCode = null;
        entry.running = false;
        entry.continuationPending = Boolean(result.hasMore);
        entry.progress = {
          ...entry.progress,
          phase: result.hasMore ? 'queued' : entry.selectionWarning ? 'attention' : 'complete',
          selection: entry.selection,
          selectionWarning: entry.selectionWarning,
        };
        if (result.hasMore) await scheduleContinuation(name);
      } catch (err) {
        anyError = true;
        const failure = safeSyncFailure(err);
        entry.lastError = failure.error;
        entry.lastErrorCode = failure.code;
        entry.running = false;
        entry.progress = { ...entry.progress, phase: 'failed' };
      } finally {
        activeProviders.delete(name);
        await setStatusEntry(name, entry);
      }
    })
  );

  await chrome.action.setBadgeText({ text: anyError ? '!' : activeProviders.size > 0 ? '…' : '' });
  if (anyError) {
    await chrome.action.setBadgeBackgroundColor({ color: '#cc3333' });
  }
  return { ok: true };
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  registerEnabledSources();
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  registerEnabledSources();
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM_NAME) {
    runSync('alarm');
    return;
  }
  if (alarm.name.startsWith(CONTINUE_ALARM_PREFIX)) {
    const providerName = alarm.name.slice(CONTINUE_ALARM_PREFIX.length);
    if (Object.hasOwn(PROVIDERS, providerName)) {
      runSync('continuation', { onlyProvider: providerName }).then(result => {
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
    const respond = result => {
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
      .then(settings => {
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
      .catch(() => sendResponse({ ok: false, error: 'AI Memory could not start syncing. Try again.' }));
    return true;
  }
  if (message?.type === 'settingsChanged') {
    Promise.all([ensureAlarm(), registerEnabledSources()]).then(([, sources]) => {
      sendResponse({ ok: true, sources });
    });
    return true;
  }
  if (message?.type === 'checkApi') {
    checkApi().then(sendResponse);
    return true;
  }
  return false;
});
