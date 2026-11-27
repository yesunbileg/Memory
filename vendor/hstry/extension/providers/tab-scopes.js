import { hasScopeItems } from '../popup-scope.js';
import { createSelectionSummary } from './selection.js';

function summarize(records, phase) {
  const selection = createSelectionSummary();
  const progress = {
    phase,
    detected: 0,
    processed: 0,
    alreadyCurrent: 0,
    accountsTotal: records.size,
    accountsChecked: 0,
    accountsUnavailable: 0,
    tabBridgeFailures: 0,
  };
  for (const entry of records.values()) {
    const p = entry.progress ?? {};
    progress.detected += p.detected ?? 0;
    progress.processed += p.processed ?? 0;
    progress.alreadyCurrent += p.alreadyCurrent ?? 0;
    progress.accountsChecked += Number(entry.done === true && !entry.unavailable);
    progress.accountsUnavailable += Number(entry.unavailable === true);
    for (const key of ['selected', 'classified', 'skipped', 'unknown'])
      selection[key] += p.selection?.[key] ?? 0;
    for (const key of Object.keys(selection.byType))
      selection.byType[key] += p.selection?.byType?.[key] ?? 0;
  }
  return { ...progress, selection };
}

export async function syncTabScopes(provider, contexts, options) {
  const { selectedScope, state, continuing, guard, report, checkpoint, signal } = options;
  const selected = selectedScope.entries.filter(hasScopeItems);
  const previous = new Map(state.accountRun?.entries ?? []);
  const records = new Map(
    selected.map(({ principalId }) => {
      const old = previous.get(principalId);
      return [
        principalId,
        continuing && old
          ? old
          : { state: old?.state ?? (selected.length === 1 && !state.accountRun ? state : {}) },
      ];
    }),
  );
  let next = continuing ? (state.accountRun?.next ?? 0) : 0;
  const remaining = () => [...records.values()].some((entry) => !entry.done);
  const snapshot = () => ({
    accountRun: { entries: [...records], next },
    ...(remaining() ? { pending: true } : {}),
  });
  const update = async (phase) => report(summarize(records, phase));
  await update('discovering');

  // A tab or slot is routing information. Only a fresh provider principal can
  // authorize a saved selection, including after moving it to another tab.
  // The regular session remains usable without an open tab. A closed
  // container has no usable tab and cannot impersonate that regular session.
  const candidates = contexts.length ? contexts : [undefined];
  const routes = new Map();
  const verified = await Promise.allSettled(
    candidates.map((ctx) => provider.principalIds(ctx, { signal })),
  );
  verified.forEach((result, index) => {
    if (result.status === 'fulfilled' && Array.isArray(result.value)) {
      for (const principalId of result.value)
        if (!routes.has(principalId)) routes.set(principalId, candidates[index]);
    }
  });
  const tabBridgeFailures = verified.filter(
    (result) => result.status === 'rejected' && result.reason?.code === 'provider_tab_unavailable',
  ).length;
  await guard();
  for (const [id, account] of records)
    if (!account.done && !routes.has(id)) account.done = account.unavailable = true;

  let conversations = 0;
  for (let visit = 0; visit < selected.length; visit++) {
    const entry = selected[next % selected.length];
    next = (next + 1) % selected.length;
    const account = records.get(entry.principalId);
    if (account.done) continue;
    const tabContext = routes.get(entry.principalId);
    try {
      const result = await provider.sync({
        ...options,
        state: account.state,
        selectedScope: { entries: [entry] },
        tabContext,
        async report(progress) {
          account.progress = { ...account.progress, ...progress };
          await update(progress.phase ?? account.progress.phase);
        },
        async checkpoint(nextState) {
          account.state = nextState;
          await checkpoint(snapshot());
        },
      });
      account.state = result.state;
      account.done = !result.hasMore;
      account.warning = Boolean(result.selectionWarning);
      account.warningDetail = result.selectionWarning || null;
      conversations = result.conversations ?? 0;
      break;
    } catch (error) {
      if (
        !['provider_tab_unavailable', 'principal_changed'].includes(error?.code) &&
        error?.name !== 'NotLoggedInError'
      )
        throw error;
      account.done = account.unavailable = true;
    }
  }

  const hasMore = remaining();
  const final = summarize(records, hasMore ? 'queued' : 'complete');
  final.tabBridgeFailures = tabBridgeFailures;
  const unavailable = final.accountsUnavailable;
  const warnings = [];
  if (unavailable)
    warnings.push(
      `Open a signed-in tab for ${unavailable} account${unavailable === 1 ? '' : 's'}.`,
    );
  if (unavailable && tabBridgeFailures)
    warnings.push('Some provider tabs could not be connected. Reload the affected tabs if needed.');
  const details = [
    ...new Set(
      [...records.values()]
        .filter((e) => e.warning)
        .map((e) => e.warningDetail || 'Some selected chats could not be verified.'),
    ),
  ];
  warnings.push(...details);
  const selectionWarning = warnings.join(' ') || null;
  await guard();
  if (!hasMore && selectionWarning) final.phase = 'attention';
  await report({ ...final, selectionWarning });
  return {
    state: snapshot(),
    conversations,
    hasMore,
    selection: final.selection,
    selectionWarning,
  };
}
