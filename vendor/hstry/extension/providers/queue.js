// Only principal-bound, selected IDs are durable. Session secrets and message text never enter the queue.
import { fetchJson, sleep } from '../lib/common.js';

export async function drainSelected({
  run,
  state,
  field,
  provider,
  read,
  beforeRead,
  push,
  onAccepted,
  report,
  checkpoint,
  canResume,
  guard,
  signal,
}) {
  const warnings = new Set(run.warnings);
  const retryMessage = `Some selected ${provider} conversations could not be read. Capture is incomplete; retry after checking your sign-in.`;
  let budget = 10,
    conversations = 0;
  await report({
    phase: 'importing',
    detected: run.detected,
    processed: run.processed,
    alreadyCurrent: run.alreadyCurrent ?? 0,
    selection: run.selection,
  });
  if (canResume && run.groups.some((g) => g.ids.length)) {
    state.pending = run;
    await guard();
    await checkpoint(structuredClone(state));
  }
  for (const group of run.groups) {
    const ids = group.ids.slice(0, budget);
    const batch = [],
      failed = [];
    let processed = run.processed;
    for (const id of ids) {
      await guard();
      if (budget < 10) await sleep(400, signal);
      // An invalidated scope is not a failed conversation. Leave the durable
      // frontier untouched so it cannot resume on behalf of a different user.
      if (beforeRead) await beforeRead(group.principalId);
      try {
        const item = await read(group.principalId, id);
        if (item) batch.push(item);
      } catch {
        signal?.throwIfAborted();
        failed.push(id);
      }
      budget--;
      await report({ processed: ++processed });
    }
    // Never move an ID past the durable frontier on a partial receiver receipt.
    if (batch.length) {
      const accepted = await push(group.sourceId, group.adapter, batch);
      if (accepted !== batch.length) throw new Error('Incomplete archive receipt');
      onAccepted?.(group.principalId, batch);
      conversations += accepted;
    }
    group.failed.push(...failed);
    group.ids.splice(0, ids.length);
    run.processed += ids.length;
    state[field][group.key] = {
      ...state[field][group.key],
      principalId: group.principalId,
      name: group.name,
      lastSyncMs:
        group.ids.length || group.failed.length || group.discoveryFailed || !group.principalId
          ? group.lastSyncMs
          : run.started,
    };
    if (canResume && ids.length) {
      await guard();
      await checkpoint(structuredClone(state));
    }
    if (!budget) break;
  }
  const more = run.groups.some((g) => g.ids.length);
  const failures = run.groups.some((g) => g.failed.length);
  if (failures) warnings.add(retryMessage);
  if (!canResume && more)
    warnings.add('Account identity is unavailable; remaining history cannot safely resume.');
  const hasMore = canResume && more;
  if (!more && failures)
    for (const g of run.groups) {
      g.ids = g.failed;
      g.failed = [];
    }
  if (canResume && (more || failures)) state.pending = run;
  else delete state.pending;
  await guard();
  await checkpoint(state);
  const selectionWarning = [...warnings].join(' ') || null;
  await report({
    phase: hasMore ? 'queued' : selectionWarning ? 'attention' : 'complete',
    processed: run.processed,
    alreadyCurrent: run.alreadyCurrent ?? 0,
    detected: run.detected,
    selection: run.selection,
    selectionWarning,
  });
  return { state, conversations, hasMore, selection: run.selection, selectionWarning };
}

// Cache only discovery/selection metadata, not unselected titles, snippets or provider responses.
const fields = new Set([
  'id',
  'uuid',
  'project_id',
  'project_uuid',
  'folder_id',
  'folder_uuid',
  'gizmo_id',
  'gpt_id',
  'agent_id',
  'custom_gpt_id',
  'assistant_id',
  'is_pinned',
  'pinned',
  'is_starred',
  'starred',
  'is_favorite',
  'favorite',
  'is_favorited',
  'pinned_time',
  'in_recents',
  'is_archived',
  'archived',
  'is_locked',
  'locked',
  'is_hidden',
  'hidden',
  'is_deleted',
  'deleted',
  'is_trashed',
  'trashed',
  'is_visible',
  'is_visible_in_sidebar',
  'updated_at',
  'update_time',
]);
const scalar = (v) => v === null || ['boolean', 'number', 'string'].includes(typeof v);
function summaryItem(item, withMetadata = true, withGizmo = true) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
  const result = {};
  for (const [k, v] of Object.entries(item)) {
    if (fields.has(k) && scalar(v)) result[k] = v;
    if (['project', 'folder', 'agent', 'gpt'].includes(k))
      result[k] =
        v && typeof v === 'object'
          ? Object.fromEntries(
              ['id', 'uuid'].filter((x) => typeof v[x] === 'string').map((x) => [x, v[x]]),
            )
          : null;
  }
  if (withGizmo && typeof item?.gizmo?.gizmo?.id === 'string')
    result.gizmo = { gizmo: summaryItem(item.gizmo.gizmo, true, false) };
  if (withMetadata && item.metadata) result.metadata = summaryItem(item.metadata, false, false);
  return result;
}
function projectSummary(data) {
  if (Array.isArray(data)) return data.map((item) => summaryItem(item));
  if (!data || typeof data !== 'object') return null;
  const result = {};
  for (const k of ['data', 'items'])
    if (Object.hasOwn(data, k))
      result[k] = Array.isArray(data[k]) ? data[k].map((item) => summaryItem(item)) : null;
  for (const k of ['cursor', 'total', 'has_more', 'next_cursor', 'next_offset'])
    if (Object.hasOwn(data, k)) result[k] = scalar(data[k]) ? data[k] : false;
  return result;
}

function discoveryPages({ state, signature, canResume, guard, checkpoint, report, selection }) {
  const cache =
    canResume && state.discovery?.signature === signature
      ? structuredClone(state.discovery)
      : { signature, pages: {}, started: Date.now() };
  const storeState = () => ({ ...state, pending: undefined, discovery: cache });
  let count = 0;
  const pause = new Error('Discovery continuation');
  return {
    started: cache.started,
    pause,
    async read(url, init) {
      await guard();
      const key = JSON.stringify([init?.headers?.['chatgpt-account-id'] ?? null, String(url)]);
      if (Object.hasOwn(cache.pages, key)) return cache.pages[key];
      if (count++ >= 4) throw pause;
      const value = projectSummary(await fetchJson(url, init));
      cache.pages[key] = value;
      if (canResume) {
        await guard();
        try {
          await checkpoint(storeState());
        } catch (error) {
          throw Object.assign(new Error('Discovery state not saved'), {
            code: error?.code ?? 'storage_unavailable',
          });
        }
      }
      return value;
    },
    async continuation() {
      const selectionWarning = canResume
        ? null
        : 'Account identity is unavailable. Discovery is incomplete and cannot safely resume.';
      const next = canResume ? storeState() : state;
      await report({ phase: canResume ? 'queued' : 'attention', selection, selectionWarning });
      return { state: next, conversations: 0, hasMore: canResume, selection, selectionWarning };
    },
  };
}

// A principal change invalidates both listing pages and the detail queue.
// Do not resume using only the shortened source labels displayed by the UI.
export function preparePrincipalRun(principals, state, hooks) {
  const ids = principals.map((p) => p.id);
  const signature = JSON.stringify(hooks.scope ? [ids.sort(), hooks.scope] : ids.sort());
  const canResume = ids.every((id) => typeof id === 'string' && id.length);
  const pager = discoveryPages({ state, signature, canResume, ...hooks });
  const prior = canResume && state.pending?.signature === signature ? state.pending : null;
  const run = prior
    ? structuredClone(prior)
    : {
        signature,
        started: pager.started,
        groups: [],
        processed: 0,
        detected: 0,
        selection: hooks.selection,
        warnings: [],
      };
  return { pager, canResume, resumed: Boolean(prior), run };
}
