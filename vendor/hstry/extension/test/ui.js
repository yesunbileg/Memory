import { DEFAULT_PROVIDER_SETTINGS, friendlyError, formatTime, normalizeSettings, progressDetail, providerState } from '../ui.js';

let failures = 0;
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`);
  if (!ok) { console.log(`  got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`); failures++; }
}

const now = Date.parse('2026-07-10T12:00:00Z');
check('new providers are opt-in', DEFAULT_PROVIDER_SETTINGS, {
  chatgpt: false,
  claude: false,
  gemini: false,
});
check('never synced time', formatTime(null, now), 'Never');
check('relative sync time', formatTime(now - 17 * 60_000, now), '17m ago');
check('fresh provider is informative', providerState(undefined, true, { apiConnected: true }), { tone: 'idle', label: 'Ready to sync', detail: 'Selected chat types will appear here.' });
check('disabled provider is paused', providerState(undefined, false).label, 'Off');
check('offline error is actionable', friendlyError('cannot reach hstry-api at x'), 'Start AI Memory on this computer, then try again.');
check('login error is actionable', friendlyError('claude.ai: not logged in'), 'Open this provider and sign in, then sync again.');
check('recovered API makes stale offline failure retryable', providerState({
  lastRunMs: now - 27 * 60_000,
  lastError: 'cannot reach hstry-api at http://127.0.0.1:41770/ingest',
}, true, { apiConnected: true }), {
  tone: 'idle',
  label: 'Ready to retry',
  detail: 'The API is connected now. Run sync again.',
});
check('running provider exposes live phase', providerState({
  running: true,
  progress: { phase: 'importing', detected: 12, processed: 7, created: 3, updated: 2 },
}, true), {
  tone: 'syncing',
  label: 'Syncing',
  detail: '12 detected · 7 processed · 3 new · 2 updated',
});
check('queued provider explains continuation', providerState({
  continuationPending: true,
  progress: { phase: 'queued', detected: 219, processed: 10 },
}, true), {
  tone: 'syncing',
  label: 'Continuing',
  detail: '219 detected · 10 processed · next batch queued',
});
check('progress detail omits empty counters', progressDetail({ detected: 4, processed: 1 }), '4 detected · 1 processed');
check('visible settings normalize before sync', normalizeSettings({
  port: '3434',
  token: ' hello ',
  intervalMinutes: '15',
  providers: { chatgpt: true, claude: true },
}, { port: 41770, intervalMinutes: 15 }), {
  port: 3434,
  token: 'hello',
  account: 'browser-local',
  intervalMinutes: 15,
  providers: { chatgpt: true, claude: true, gemini: false },
  captureTypes: { projects: true, pinned: true, agents: true, recents: false },
});
check('invalid port cannot escape form constraints', normalizeSettings({
  port: '70000',
  token: '',
  intervalMinutes: '0',
  providers: {},
}, { port: 41770, intervalMinutes: 15 }), {
  port: 41770,
  token: '',
  account: 'browser-local',
  intervalMinutes: 15,
  providers: { chatgpt: false, claude: false, gemini: false },
  captureTypes: { projects: true, pinned: true, agents: true, recents: false },
});

check('no connection never claims a provider is ready', providerState(undefined, true).label, 'Waiting for connection');
check('unavailable category data is visible', providerState({ lastRunMs: now, selectionWarning: 'Unknown category; skipped.' }, true, { apiConnected: true }).tone, 'warning');
check('raw provider diagnostics never shown', friendlyError('internal bearer very-private-value'), 'This provider could not finish. Open the site, check your sign-in, and try again.');
check('changed checklist does not reuse an old completed status', providerState({
  lastRunMs: now, capturePolicy: { projects: true, pinned: true, agents: true, recents: true },
  progress: { phase: 'complete', created: 1, updated: 0, detected: 1 },
}, true, { apiConnected: true, captureTypes: { projects: true, pinned: true, agents: true, recents: false } }).label, 'Selection updated');
check('legacy capture-all status needs a selective run', providerState({lastRunMs:now,progress:{phase:'complete'}}, true,
  {apiConnected:true,captureTypes:{projects:true,pinned:true,agents:true,recents:false}}).label, 'Selection updated');

process.exit(failures ? 1 : 0);
