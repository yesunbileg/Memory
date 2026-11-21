// Poll selected AI conversations and save them to the local AI Memory receiver.

import { scopeFor, nonempty } from './popup-scope.js';
import { pageSelection, forgetPageTab } from './page-selection.js';
import { NotLoggedInError, RateLimitedError } from './lib/common.js';
import { PROVIDERS } from './providers/index.js';
import { normalizeSettings, updateLocalSettings } from './ui.js';
import { checkConnection, discoverConnection, unpaired } from './worker-auth.js';
import { requestGroups } from './transport.js';
import { syncTabScopes } from './providers/tab-scopes.js';
import { sameCaptureTypes } from './providers/selection.js';

const ALARM_NAME = 'hstry-sync';
const CONTINUE_ALARM_PREFIX = 'hstry-sync-continue-';
const QUEUED_SLICE_DELAY_MS = 400;
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
let stopEpoch = 0;
let stopTask;
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

async function assertCurrentCapture(epoch, provider, listKey) {
  const latest = await getSettings();
  observeSettings(latest);
  if (epoch !== captureEpoch || JSON.stringify(latest.scopes[provider]?.scope) !== listKey)
    throw localError('selection_changed');
}

async function getSettings() {
  const [{ settings }, { chatgptSyncList }, { providerSyncScopes }] = await Promise.all([
    chrome.storage.local.get('settings'),
    chrome.storage.local.get('chatgptSyncList'),
    chrome.storage.local.get('providerSyncScopes'),
  ]);
  // Keep raw pairing fields: an array or other malformed value must never
  // become a valid host name through display-oriented string normalization.
  return {
    ...normalizeSettings(settings),
    scopes: Object.fromEntries(
      Object.keys(PROVIDERS).map((p) => [p, scopeFor(p, providerSyncScopes, chatgptSyncList)]),
    ),
    nativeHost: settings?.nativeHost,
    archiveId: settings?.archiveId,
  };
}

async function getStatus() {
  const { status } = await chrome.storage.local.get('status');
  return status ?? {};
}

async function setStatus(status) {
  await chrome.storage.local.set({ status });
}

function updateStatus(change) {
  statusWriteQueue = statusWriteQueue
    .catch(() => {})
    .then(async () => {
      const status = await getStatus();
      if (change(status) !== false) await setStatus(status);
    });
  return statusWriteQueue;
}

function setStatusEntry(name, entry, owner) {
  return updateStatus((status) => {
    status[name] = owner?.signal.aborted && entry.running ? stoppingEntry(entry) : entry;
  });
}

function stoppingEntry(entry) {
  return {
    ...entry,
    stopRequested: true,
    progress: { ...entry.progress, phase: 'stopping' },
  };
}

function withoutQueue(state) {
  if (!state) return state;
  const { pending, discovery, ...saved } = state;
  if (saved.accountRun) {
    saved.accountRun = {
      entries: saved.accountRun.entries.map(([id, account]) => [
        id,
        { state: withoutQueue(account.state) },
      ]),
    };
  }
  return saved;
}

function markStopped(entry) {
  entry.running = false;
  entry.stopRequested = false;
  entry.continuationPending = false;
  entry.nextRetryMs = null;
  entry.lastError = LOCAL_ERRORS.sync_stopped;
  entry.lastErrorCode = 'sync_stopped';
  entry.progress = { ...entry.progress, phase: 'cancelled' };
  entry.state = withoutQueue(entry.state);
}

async function stopSync() {
  stopEpoch++;
  for (const owner of activeProviders.values()) owner.abort(localError('sync_stopped'));
  await startupReady;
  const stopped = [];
  await updateStatus((status) => {
    for (const [name, entry] of Object.entries(status)) {
      if (!Object.hasOwn(PROVIDERS, name)) continue;
      if (!entry.running && !entry.continuationPending && !entry.nextRetryMs) continue;
      if (entry.running && activeProviders.has(name)) status[name] = stoppingEntry(entry);
      else markStopped(entry);
      stopped.push(name);
    }
    return stopped.length > 0;
  });
  await Promise.all(stopped.map((name) => chrome.alarms.clear(CONTINUE_ALARM_PREFIX + name)));
  return { ok: true };
}

// Only these explicit fields may enter the persistent activity log.
function recordActivity(name, entry, outcome) {
  const p = entry.progress;
  return appendActivityLog({
    id: `${name}-${entry.lastRunMs}`,
    timestampMs: Date.now(),
    provider: name,
    trigger: entry.trigger,
    outcome,
    errorCode: entry.lastErrorCode,
    durationMs: entry.durationMs,
    counts: {
      detected: p.detected ?? 0,
      processed: p.processed ?? 0,
      created: p.created ?? 0,
      updated: p.updated ?? 0,
      unchanged: p.unchanged ?? 0,
      skipped: entry.selection?.skipped ?? 0,
      unknown: entry.selection?.unknown ?? 0,
    },
  });
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
    if (entry.stopRequested) {
      markStopped(entry);
      changed = true;
      continue;
    }
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
        created += data.created;
        updated += data.updated;
        unchanged += data.unchanged;
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

// This describes local packet sources; it does not register anything remotely.
function configuredSources(settings) {
  return Object.fromEntries(
    Object.entries(PROVIDERS)
      .filter(([id]) => settings.providers[id])
      .map(([id, provider]) => [id, { source: provider.sourceId, adapter: provider.adapter }]),
  );
}

async function checkApi() {
  const settings = await getSettings();
  if (!unpaired(settings)) return checkConnection(settings);
  const found = await discoverConnection(chrome.runtime.id);
  if (!found.ok) return found.code === 'missing_token' ? checkConnection(settings) : found;
  const saved = await updateLocalSettings((current) =>
    unpaired(current)
      ? {
          ...current,
          archiveId: found.archiveId,
          nativeHost: 'org.ai_memory.archive_' + found.archiveId,
          token: '',
        }
      : null,
  );
  return saved ? found : checkConnection(await getSettings());
}

const LOCAL_ERRORS = {
  storage_unavailable: 'Browser storage could not save capture progress. Free space and retry.',
  missing_token: 'Set up the local connection in AI Memory Settings first.',
  unauthorized: 'The local AI Memory host does not allow this extension.',
  offline: 'The local AI Memory host is offline. Check the connection in Settings.',
  archive_changed: 'The receiving archive changed during capture. Capture again to replay safely.',
  archive_unavailable: 'AI Memory could not save this batch. Check its terminal and retry.',
  body_too_large:
    'A conversation item is too large for the local receiver. Split the source item before retrying.',
  selection_changed: 'Your selection changed. Capture again to use the updated settings.',
  sync_stopped: 'Sync stopped.',
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
      !nonempty(settings.scopes[name]?.scope) ||
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

async function providerTabs(selected) {
  const tabs =
    (await chrome.tabs?.query?.({ url: selected.map(([, p]) => `https://${p.site}/*`) })) ?? [];
  const contexts = new Map(selected.map(([name]) => [name, []]));
  for (const tab of tabs.sort((a, b) => Number(b.active) - Number(a.active))) {
    if (!Number.isInteger(tab.id) || tab.id < 0 || tab.discarded) continue;
    const ctx = globalThis.aiMemoryPageRoutes.pageContext(tab.url);
    if (ctx && contexts.has(ctx.provider))
      contexts.get(ctx.provider).push({ ...ctx, tabId: tab.id });
  }
  return contexts;
}

async function runSync(
  trigger,
  { onlyProvider = null, full = false, onStarted = async () => {}, expected = null } = {},
) {
  const startStopEpoch = stopEpoch;
  await startupReady;
  await stopTask?.catch(() => {});
  const settings = await getSettings();
  const epoch = observeSettings(settings);
  if (
    !(onlyProvider ? [onlyProvider] : Object.keys(PROVIDERS)).some(
      (name) => settings.providers[name] && nonempty(settings.scopes[name]?.scope),
    )
  ) {
    return {
      ok: false,
      code: 'no_saved_selection',
      error: 'Enable a provider and check chats or projects on its website in list mode first.',
    };
  }
  const status = await getStatus();
  if (startStopEpoch !== stopEpoch) return { ok: false, code: 'sync_stopped' };
  if (
    expected &&
    (epoch !== expected.epoch ||
      status[onlyProvider]?.lastRunMs !== expected.lastRunMs ||
      !status[onlyProvider]?.continuationPending ||
      JSON.stringify(settings.scopes[onlyProvider]?.scope) !== expected.listKey)
  )
    return { ok: false, code: 'stale_alarm' };
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
      nonempty(settings.scopes[name]?.scope) &&
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
  const owner = new AbortController();
  const { signal } = owner;
  for (const [name] of selected) activeProviders.set(name, owner);
  const release = (name) => {
    if (activeProviders.get(name) === owner) activeProviders.delete(name);
  };
  try {
    const tabs = await providerTabs(selected);
    await chrome.action.setBadgeBackgroundColor({ color: '#007aff' });
    await chrome.action.setBadgeText({ text: '…' });

    // Verify local credentials before any provider makes remote account or chat reads.
    const connection = await checkConnection(settings);
    if (signal.aborted) {
      await chrome.action.setBadgeText({ text: activeProviders.size > selected.length ? '…' : '' });
      return { ok: false, code: 'sync_stopped' };
    }
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
        const saved = settings.scopes[name];
        const listKey = JSON.stringify(saved.scope);
        const guard = async () => {
          signal.throwIfAborted();
          await assertCurrentCapture(epoch, name, listKey);
          signal.throwIfAborted();
        };
        // A changed list invalidates both discovery pages and queued detail IDs.
        // Do not duplicate the saved IDs into the public status summary.
        const listHash = Array.from(
          new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(listKey))),
          (b) => b.toString(16).padStart(2, '0'),
        ).join('');
        const captureScope = connection.archiveId
          ? JSON.stringify([
              connection.archiveId,
              name,
              profile,
              settings.account,
              { savedList: listHash },
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
        const policyChanged = !sameCaptureTypes(status[name]?.capturePolicy, settings.captureTypes);
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
          stopRequested: false,
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
        await setStatusEntry(name, entry, owner);
        if (!startedReported) {
          startedReported = true;
          await onStarted();
        }
        const report = async (progress) => {
          entry.progress = { ...entry.progress, ...progress };
          if (Object.hasOwn(progress, 'selection')) entry.selection = progress.selection;
          if (Object.hasOwn(progress, 'selectionWarning'))
            entry.selectionWarning = progress.selectionWarning;
          await setStatusEntry(name, entry, owner);
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
          guard,
          connection.archiveId,
        );
        try {
          await guard();
          const options = {
            state: entry.state ?? {},
            captureTypes: { ...settings.captureTypes },
            selectedScope: saved.scope,
            push,
            report,
            guard,
            signal,
            checkpoint: async (state) => {
              await guard();
              entry.state = state;
              entry.continuationPending = Boolean(state?.pending || state?.discovery);
              await setStatusEntry(name, entry, owner);
            },
            // Provider diagnostics can contain account or conversation text.
            log: () => {},
          };
          const contexts = tabs.get(name);
          const result =
            contexts.length || saved.scope.entries.length > 1 || options.state.accountRun
              ? await syncTabScopes(provider, contexts, { ...options, continuing })
              : await provider.sync(options);
          await guard();
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
          await recordActivity(
            name,
            entry,
            result.hasMore ? 'queued' : entry.selectionWarning ? 'warning' : 'success',
          );
        } catch (err) {
          if (signal.aborted) {
            markStopped(entry);
            entry.durationMs = Math.max(0, Date.now() - startedAt);
            await recordActivity(name, entry, 'cancelled');
            return;
          }
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
          await recordActivity(
            name,
            entry,
            failure.code === 'selection_changed' ? 'cancelled' : 'error',
          );
        } finally {
          try {
            if (signal.aborted && (entry.continuationPending || entry.nextRetryMs))
              markStopped(entry);
            await setStatusEntry(name, entry, owner);
            if (!signal.aborted && (entry.nextRetryMs || entry.continuationPending))
              await scheduleContinuation(name, entry.nextRetryMs ?? 0);
            if (!signal.aborted && entry.continuationPending && !entry.nextRetryMs) {
              // The alarm still recovers the checkpoint if this worker is suspended.
              setTimeout(() => {
                runSync('continuation', {
                  onlyProvider: name,
                  expected: { epoch, listKey, lastRunMs: entry.lastRunMs },
                }).catch(() => {});
              }, QUEUED_SLICE_DELAY_MS);
            }
          } finally {
            release(name);
          }
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

chrome.runtime.onInstalled.addListener(() => ensureAlarm());
chrome.runtime.onStartup.addListener(() => ensureAlarm());

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

function popupSender(sender) {
  return (
    typeof chrome.runtime.id === 'string' &&
    sender?.id === chrome.runtime.id &&
    !sender.tab &&
    sender.url?.split(/[?#]/, 1)[0] === 'chrome-extension://' + chrome.runtime.id + '/popup.html' &&
    (sender.frameId === undefined || sender.frameId === 0)
  );
}

// Page content scripts use only the broker; provider settings, pairing, and other accounts stay worker/popup-only.
chrome.storage.local.setAccessLevel?.({ accessLevel: 'TRUSTED_CONTEXTS' }).catch(() => {});
chrome.tabs?.onRemoved?.addListener(forgetPageTab);

const pageErrors = {
  untrusted_sender: 'Open a supported chat page to use list mode.',
  selection_invalid: 'This row cannot be changed. Refresh list mode and try again.',
  catalog_expired: 'List mode expired. Refresh the checkboxes and try again.',
  principal_changed:
    'The active account or workspace changed. Refresh list mode in the intended account.',
  ambiguous_selection: 'These rows cannot be assigned to one verified account or workspace.',
  catalog_unavailable:
    'The provider could not verify these rows. Check your sign-in and try again.',
  not_logged_in: 'Sign in to this provider to use list mode.',
  rate_limited: 'The provider is busy. Wait a few minutes, then try again.',
  storage_unavailable: 'The selection could not be saved locally. Check browser storage and retry.',
};
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (
    ['READ_PAGE_SELECTION', 'SET_PAGE_SELECTION', 'CLOSE_PAGE_SELECTION'].includes(message?.type)
  ) {
    pageSelection(message, sender, PROVIDERS).then(sendResponse, (err) => {
      const code = Object.hasOwn(pageErrors, err?.code)
        ? err.code
        : err instanceof NotLoggedInError
          ? 'not_logged_in'
          : err instanceof RateLimitedError
            ? 'rate_limited'
            : 'catalog_unavailable';
      sendResponse({ ok: false, code, error: pageErrors[code] });
    });
    return true;
  }
  if (!popupSender(sender)) {
    sendResponse({
      ok: false,
      code: 'untrusted_sender',
      error: 'Open the extension popup to perform this action.',
    });
    return false;
  }
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
  if (message?.type === 'stopSync') {
    stopTask = stopSync();
    stopTask.then(sendResponse, () =>
      sendResponse({ ok: false, error: 'Could not stop the sync. Try again.' }),
    );
    return true;
  }
  if (message?.type === 'syncProvider') {
    const providerName = message.provider;
    if (typeof providerName !== 'string' || !Object.hasOwn(PROVIDERS, providerName)) {
      sendResponse({ ok: false, error: 'Unknown provider' });
      return false;
    }
    start('manual', { onlyProvider: providerName, full: false });
    return true;
  }
  if (message?.type === 'fullSyncProvider') {
    const providerName = message.provider;
    if (typeof providerName !== 'string' || !Object.hasOwn(PROVIDERS, providerName)) {
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
    getSettings().then(async (settings) => {
      observeSettings(settings);
      await ensureAlarm();
      sendResponse({ ok: true, sources: configuredSources(settings) });
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
    checkApi().then(sendResponse, () => sendResponse({ ok: false, code: 'storage_unavailable' }));
    return true;
  }
  return false;
});
