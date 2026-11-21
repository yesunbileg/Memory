import assert from 'node:assert/strict';
import { eligibleCapture } from '../providers/selection.js';
import {
  captureOutcome,
  captureProgress,
  describeEffectivePlan,
  normalizeSettings,
  providerState,
  statusForPairing,
  formatSyncBreakdown,
} from '../ui.js';

const organized = { projects: true, pinned: true, agents: true, recents: false };
const agentsOnly = { projects: false, pinned: false, agents: true, recents: false };
for (const [provider, eligible] of [
  ['chatgpt', true],
  ['claude', false],
  ['gemini', false],
]) {
  assert.equal(eligibleCapture(provider, agentsOnly), eligible);
  assert.equal(
    describeEffectivePlan({ [provider]: true }, agentsOnly).readyCount,
    Number(eligible),
  );
}
assert.equal(eligibleCapture('gemini', organized), false);
const legacy = normalizeSettings(
  { providers: { claude: true }, captureTypes: agentsOnly, intervalMinutes: 37 },
  {},
);
assert.equal(legacy.intervalMinutes, 37);
assert.equal(legacy.providers.claude, true);
assert.deepEqual(legacy.captureTypes, agentsOnly);
assert.equal(legacy.autoPaused, false);
const run = (props = {}) => ({
  lastRunMs: 20,
  progress: { phase: 'complete', created: 2, updated: 1, unchanged: 4 },
  selection: { skipped: 1 },
  ...props,
});
assert.match(
  captureOutcome({ claude: run() }, { claude: true }),
  /2 new.*1 updated.*4 unchanged.*1 skipped/,
);
assert.match(
  captureOutcome(
    { claude: run({ lastErrorCode: 'not_logged_in' }), chatgpt: run() },
    { claude: true, chatgpt: true },
  ),
  /Partial.*Claude.*sign in/i,
);
assert.match(
  captureOutcome(
    { claude: run({ lastErrorCode: 'not_logged_in', progress: {} }) },
    { claude: true },
  ),
  /Claude.*sign in/i,
);
assert.match(
  captureOutcome(
    { gemini: run({ progress: { phase: 'complete', created: 0, updated: 0, unchanged: 0 } }) },
    { gemini: true },
  ),
  /No matching chats/,
);
assert.match(captureOutcome({ claude: run({ running: true }) }, { claude: true }), /Capturing/);
assert.equal(captureOutcome({ claude: run() }, { claude: true }, 21), '');
const progress = {
  chatgpt: run({
    running: true,
    progress: { phase: 'importing', detected: 200, processed: 11, accepted: 10, accountsTotal: 1 },
    selection: { selected: 71 },
  }),
};
assert.deepEqual(captureProgress(progress, { chatgpt: true }, 20), {
  value: 11,
  max: 71,
  text: 'Capturing selected chats… 11/71 checked · 10 saved',
});
assert.equal(captureProgress(progress, { chatgpt: true }, 21), null);
assert.equal(
  providerState(
    run({ capturePolicy: { agents: true, pinned: true, projects: true, recents: false } }),
    true,
    { apiConnected: true, captureTypes: organized },
  ).label,
  'Up to date',
  'Brave storage can reorder capture policy keys without changing the policy',
);

const archive = 'a'.repeat(32);
const otherArchive = 'b'.repeat(32);
const scoped = (id, site, name) => ({
  ...run(),
  captureScope: JSON.stringify([id, site, 'synthetic-profile', name, organized]),
});
const mixed = {
  chatgpt: scoped(archive, 'chatgpt', 'work'),
  claude: scoped(otherArchive, 'claude', 'work'),
  gemini: scoped(archive, 'gemini', 'home'),
};
assert.deepEqual(Object.keys(statusForPairing(mixed, { archiveId: archive, account: 'work' })), [
  'chatgpt',
]);
assert.deepEqual(
  statusForPairing(
    { claude: run(), gemini: { ...run(), captureScope: 'broken' } },
    { archiveId: archive, account: 'work' },
  ),
  {},
);
assert.deepEqual(statusForPairing(mixed, { archiveId: '', account: 'work' }), {});
assert.equal(
  formatSyncBreakdown({ running: true, progress: { detected: 4, processed: 0 } }).percent,
  0,
);

console.log('PASS cross-surface capture eligibility, migration, scoped outcomes and progress');
