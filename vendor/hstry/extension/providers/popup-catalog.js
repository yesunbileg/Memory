// Popup discovery is metadata-only. Durable capture state includes IDs and acknowledged update times.
import { fetchJson, NotLoggedInError } from '../lib/common.js';
import { createSelectionSummary, classifyConversation } from './selection.js';
import { discoveryPages, drainSelected, summaryItem } from './queue.js';
import { hasScopeItems } from '../popup-scope.js';

const cap = 10000;
// hstry rechecks the last five minutes so a concurrent edit can settle before we skip it.
const OVERLAP_MS = 5 * 60 * 1000;
export const nativeId = (id) => typeof id === 'string' && /^[a-zA-Z0-9_-]{1,256}$/.test(id);
const label = (...values) =>
  values
    .find((v) => typeof v === 'string' && v.trim())
    ?.trim()
    .slice(0, 512);
const invalid = () =>
  new Error('Provider catalog is unavailable. Check the signed-in account and retry.');
const changed = () =>
  Object.assign(new Error('The signed-in account changed.'), { code: 'principal_changed' });

export function unavailable(item) {
  return [item, item?.metadata].some(
    (s) =>
      s &&
      ([
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
        'is_forbidden',
        'forbidden',
        'is_inaccessible',
        'is_deactivated',
      ].some((k) => s[k] != null && s[k] !== false) ||
        ['is_visible', 'is_visible_in_sidebar', 'can_read', 'can_access'].some(
          (k) => s[k] === false,
        ) ||
        ['archived_at', 'deleted_at', 'trashed_at'].some((k) => !!s[k]) ||
        [
          'locked',
          'forbidden',
          'archived',
          'deleted',
          'trashed',
          'hidden',
          'inaccessible',
        ].includes(s.status)),
  );
}

export function memberships(item) {
  return [
    ...new Set(
      [item, item?.metadata]
        .filter(Boolean)
        .flatMap((s) => [
          s.project_id,
          s.project_uuid,
          s.folder_id,
          s.folder_uuid,
          s.project?.id,
          s.project?.uuid,
          s.folder?.id,
          s.folder?.uuid,
          typeof s.gizmo_id === 'string' && s.gizmo_id.startsWith('g-p-') ? s.gizmo_id : null,
        ])
        .filter((x) => x != null),
    ),
  ];
}

const identity = (ps) => JSON.stringify(ps.map((p) => p.principalId).sort());

// Catalog eligibility needs the existence of a label, not its private text.
// Keep the same allow/deny rules when replaying an already verified page.
function captureSummary(item) {
  const result = summaryItem(item, false, false);
  if (!result) return result;
  for (const field of ['id', 'conversation_id'])
    if (item[field] != null) result[field] = nativeId(item[field]) ? item[field] : false;
  for (const field of ['title', 'name', 'short_name'])
    if (label(item[field])) result[field] = 'Listed item';
  if (label(item.display?.name, item.display?.title)) result.display = { name: 'Listed item' };
  if (unavailable(item)) result.is_hidden = true;
  for (const data of [item, item.metadata]) {
    if (!data) continue;
    const target = data === item ? result : (result.metadata = summaryItem(data, false, false));
    if (!target) continue;
    for (const field of ['project_id', 'project_uuid', 'folder_id', 'folder_uuid'])
      if (data[field] != null && !nativeId(data[field])) target[field] = false;
    for (const field of ['project', 'folder'])
      for (const key of ['id', 'uuid'])
        if (data[field]?.[key] != null && !nativeId(data[field][key]))
          (target[field] ??= {})[key] = false;
  }
  if (item.gizmo?.gizmo) {
    result.gizmo = {
      ...(unavailable(item.gizmo) && { is_hidden: true }),
      ...(label(item.gizmo.display?.name, item.gizmo.display?.title) && {
        display: { name: 'Listed item' },
      }),
      ...(label(item.gizmo.title) && { title: 'Listed item' }),
      gizmo: captureSummary(item.gizmo.gizmo),
    };
  }
  return result;
}

function capturePage(page) {
  if (Array.isArray(page)) return page.map(captureSummary);
  if (!page || typeof page !== 'object') return null;
  const result = {};
  for (const field of ['items', 'data', 'chat_conversations'])
    if (Object.hasOwn(page, field))
      result[field] = Array.isArray(page[field]) ? page[field].map(captureSummary) : null;
  for (const field of ['cursor', 'total', 'has_more', 'next_cursor', 'next_offset'])
    if (Object.hasOwn(page, field)) {
      const value = page[field];
      result[field] =
        value === null || ['number', 'boolean', 'string'].includes(typeof value) ? value : false;
    }
  return result;
}

const fetchPage = (spec, signal) => (url, init) =>
  fetchJson(url, { ...init, cache: 'no-store', signal }, { retries: 0, fetcher: spec.fetcher });

// One reader and one total budget per popup action. Do not retry a vanished account.
async function gather(spec, principals, selected, guard, load, pause) {
  const include = (p) =>
    selected === undefined ||
    selected.some((e) => e.principalId === p.principalId && hasScopeItems(e));
  const groups = [];
  let denied = false;
  let pages = 0,
    items = 0,
    incomplete = false;
  const read = async (url, init = {}) => {
    await guard();
    if (++pages > 200) throw invalid();
    return load(url, init);
  };
  const required = (e) =>
    e === pause ||
    ['selection_changed', 'storage_unavailable', 'sync_stopped', 'principal_changed'].includes(
      e?.code,
    );
  for (const p of principals.filter(include)) {
    const blocked = new Set(),
      projects = new Map(),
      convs = new Map(),
      legacyOrdinary = new Set();
    let entryIncomplete = false;
    const count = () => {
      if (++items > cap) throw invalid();
    };
    try {
      const catalog = await spec.projects(p, read);
      for (const project of catalog) {
        count();
        const id = spec.id(project);
        if (!nativeId(id)) throw invalid();
        const title = label(
          project.display?.name,
          project.display?.title,
          project.name,
          project.title,
          project.short_name,
        );
        if (!title || unavailable(project)) {
          blocked.add(id);
          if (!title) entryIncomplete = true;
          continue;
        }
        projects.set(id, { id, title });
      }
      for (const id of blocked) projects.delete(id);
      const skipped = new Set();
      const projectGizmoIds = new Set(projects.keys());
      const scope = selected?.find((e) => e.principalId === p.principalId);
      const direct = scope && new Set(scope.conversationIds);
      const wantedProjects = scope && new Set(scope.projectIds);
      function add(item, where) {
        count();
        const id = spec.id(item);
        if (!nativeId(id)) throw invalid();
        // Bulk Recents must also reconcile project membership. An ordinary-list
        // duplicate can omit its project ID even though its project lists it.
        if (scope && !scope.includeRecents && !direct.has(id) && !wantedProjects.has(where)) return;
        const previous = convs.get(id);
        const parents = new Set([
          ...memberships(item),
          ...(where ? [where] : []),
          ...(previous?.projectId ? [previous.projectId] : []),
        ]);
        if (
          unavailable(item) ||
          parents.size > 1 ||
          [...parents].some((pid) => !projects.has(pid))
        ) {
          skipped.add(id);
          return;
        }
        // A verified project member can be untitled. Its collection ID, not its display
        // name, establishes membership. Ordinary-list rows still need a real title.
        const title = label(item.title, item.name) ?? previous?.title ?? (where && 'Untitled chat');
        if (!title) {
          entryIncomplete = true;
          return;
        }
        const projectId = [...parents][0];
        const recentOnly = classifyConversation(item, {
          projectId,
          projectGizmoIds,
        }).categories.every((k) => k === 'recents');
        if (!recentOnly) legacyOrdinary.delete(id);
        else if (!convs.has(id)) legacyOrdinary.add(id);
        const incomingTime = scope ? spec.updatedAt?.(item) : null;
        const previousTime = previous?.updatedAt;
        const conflict =
          previousTime === false || (previousTime && incomingTime && previousTime !== incomingTime);
        convs.set(id, {
          id,
          title,
          ...(projectId ? { projectId } : {}),
          ...(scope ? { updatedAt: conflict ? false : (incomingTime ?? previousTime) } : {}),
        });
      }
      // Resolve project pages first so a long ordinary list cannot starve them.
      // A failed membership page cannot grant access to that project via Recents.
      let membershipComplete = true;
      const verifiedMember = new Set();
      const chosenProjectsCoverDirect = () =>
        [...direct].every(
          (id) =>
            !skipped.has(id) &&
            verifiedMember.has(id) &&
            wantedProjects.has(convs.get(id)?.projectId),
        );
      // Independent chat selections may require searching the remaining projects. Give whole
      // projects the page budget first, even when the provider orders them last.
      const projectOrder = scope
        ? [
            ...wantedProjects,
            ...(direct.size || scope.includeRecents
              ? [...projects.keys()].filter((id) => !wantedProjects.has(id))
              : []),
          ]
        : projects.keys();
      for (const id of projectOrder) {
        if (
          scope &&
          !scope.includeRecents &&
          !wantedProjects.has(id) &&
          chosenProjectsCoverDirect()
        )
          break;
        if (!projects.has(id)) continue;
        try {
          const seen = [];
          for await (const item of spec.members(p, id, read)) {
            add(item, id);
            seen.push(spec.id(item));
          }
          for (const cid of seen) verifiedMember.add(cid);
        } catch (err) {
          if (required(err)) throw err;
          projects.delete(id);
          membershipComplete = false;
          entryIncomplete = true;
        }
      }
      if (!scope || scope.includeRecents || !chosenProjectsCoverDirect()) {
        try {
          for await (const item of spec.chats(p, read)) add(item);
        } catch (err) {
          if (required(err) || err instanceof NotLoggedInError) throw err;
          entryIncomplete = true;
          // The ordinary list ended early. Its already-read rows still have native
          // IDs; only expose them if the project inventory is complete.
        }
      }
      for (const [id, row] of convs) {
        if (
          skipped.has(id) ||
          (row.projectId && !projects.has(row.projectId)) ||
          (!membershipComplete && !verifiedMember.has(id))
        )
          convs.delete(id);
      }
      groups.push({
        p,
        catalog: {
          principalId: p.principalId,
          title: p.title,
          projects: [...projects.values()],
          conversations: [...convs.values()]
            .map((c) => (spec.recents && legacyOrdinary.has(c.id) ? { ...c, isRecent: true } : c))
            .sort((a, b) => Number(!!a.projectId) - Number(!!b.projectId)),
        },
        legacyOrdinary,
      });
    } catch (error) {
      if (required(error)) throw error;
      // An inaccessible organization cannot supply titles or queue entries. Other
      // organizations remain usable only after fresh principal verification below.
      if (error instanceof NotLoggedInError) denied = true;
      // Incomplete project membership can't authorize ordinary-list duplicates.
      entryIncomplete = true;
    }
    incomplete ||= entryIncomplete;
  }
  if (denied && !groups.length) throw invalid();
  if (groups.length && identity(principals) !== identity(await spec.principals())) throw changed();
  return { groups, incomplete, principals };
}

export async function listPopupCatalog(spec) {
  try {
    const { groups, incomplete } = await gather(
      spec,
      await spec.principals(),
      undefined,
      async () => {},
      fetchPage(spec),
    );
    return { entries: groups.map((g) => g.catalog), ...(incomplete ? { incomplete: true } : {}) };
  } catch {
    throw invalid();
  }
}

function validSelection(value, supportsRecents) {
  return (
    Array.isArray(value?.entries) &&
    value.entries.length <= 100 &&
    new Set(value.entries.map((e) => e?.principalId)).size === value.entries.length &&
    value.entries.every(
      (e) =>
        e &&
        typeof e.principalId === 'string' &&
        (e.includeRecents === undefined ||
          (supportsRecents && typeof e.includeRecents === 'boolean')) &&
        ['projectIds', 'conversationIds'].every(
          (k) => Array.isArray(e[k]) && e[k].length <= cap && e[k].every(nativeId),
        ) &&
        (e.legacyConversationIds === undefined ||
          (Array.isArray(e.legacyConversationIds) &&
            e.legacyConversationIds.length <= cap &&
            e.legacyConversationIds.every((id) => e.conversationIds.includes(id)))),
    )
  );
}

export async function syncPopupScope(
  spec,
  { selectedScope, state = {}, push, register, guard, report, checkpoint, signal },
) {
  const selection = createSelectionSummary();
  const stop = async (message) => {
    await report({
      phase: message ? 'attention' : 'complete',
      detected: 0,
      processed: 0,
      selection,
      selectionWarning: message,
    });
    return { state: {}, conversations: 0, selection, selectionWarning: message };
  };
  if (!validSelection(selectedScope, spec.recents))
    return stop('Invalid saved selection. Reopen the popup.');
  const chosen = selectedScope.entries.filter(hasScopeItems);
  if (!chosen.length) return stop(null);
  await report({ phase: 'discovering', selection });
  const principals = await spec.principals();
  const signature = JSON.stringify([identity(principals), chosen]);
  const verify = async () => {
    await guard();
    if (identity(principals) !== identity(await spec.principals())) throw changed();
    await guard();
  };
  const load = fetchPage(spec, signal);
  const pager = discoveryPages({
    state,
    signature,
    guard,
    checkpoint,
    report,
    selection,
    verify,
    canResume: true,
    load: async (url, init) => capturePage(await load(url, init)),
  });
  let groups, incomplete;
  try {
    ({ groups, incomplete } = await gather(
      spec,
      principals,
      chosen,
      guard,
      pager.read,
      pager.pause,
    ));
  } catch (error) {
    if (error !== pager.pause) throw error;
    return pager.continuation();
  }
  if (!groups.length) return stop('The selected account or its catalog is unavailable.');
  const prior = state.pending?.signature === signature ? state.pending : null;
  const now = Date.now(),
    run = {
      signature,
      started: prior?.started ?? now,
      processed: prior?.processed ?? 0,
      alreadyCurrent: prior?.alreadyCurrent ?? 0,
      detected: 0,
      selection,
      groups: [],
      warnings: [],
    };
  const current = new Map();
  // Don't retain names from previous sessions in durable queues.
  const next = { [spec.field]: {} };
  let missing = incomplete;
  for (const { p, catalog, legacyOrdinary } of groups) {
    const entry = chosen.find((e) => e.principalId === p.principalId);
    const projects = new Set(entry.projectIds),
      direct = new Set(entry.conversationIds),
      legacy = new Set(entry.legacyConversationIds ?? []);
    const allowed = new Map(
      catalog.conversations
        .filter(
          (c) =>
            (direct.has(c.id) && (!legacy.has(c.id) || legacyOrdinary.has(c.id))) ||
            projects.has(c.projectId) ||
            (entry.includeRecents && c.isRecent),
        )
        .map((c) => [c.id, c]),
    );
    missing ||=
      [...direct].some((id) => !allowed.has(id)) ||
      [...projects].some((id) => !catalog.projects.some((c) => c.id === id));
    const saved = state[spec.field]?.[p.principalId]?.archivedUpdates;
    const archivedUpdates = new Map(
      (Array.isArray(saved) ? saved : []).filter(
        (pair) => Array.isArray(pair) && allowed.has(pair[0]) && Number.isFinite(pair[1]),
      ),
    );
    next[spec.field][p.principalId] = { archivedUpdates: [...archivedUpdates] };
    current.set(p.principalId, { p, allowed, legacy, projects, direct, archivedUpdates });
    const prev = prior?.groups?.find((g) => g.principalId === p.principalId);
    const ids = prev
      ? [...new Set([...prev.ids, ...prev.failed])].filter((id) => allowed.has(id))
      : [...allowed.values()]
          .filter(
            ({ id, updatedAt }) =>
              !(
                Number.isFinite(updatedAt) &&
                updatedAt > 0 &&
                updatedAt <= now - OVERLAP_MS &&
                archivedUpdates.get(id) === updatedAt
              ),
          )
          .map(({ id }) => id);
    if (!prev) run.alreadyCurrent += allowed.size - ids.length;
    await guard();
    await register(p.sourceId, spec.adapter);
    run.groups.push({
      principalId: p.principalId,
      key: p.principalId,
      sourceId: p.sourceId,
      adapter: spec.adapter,
      ids,
      failed: [],
      lastSyncMs: null,
      discoveryFailed: incomplete,
    });
    run.detected += catalog.conversations.length;
    selection.classified += catalog.conversations.length;
    selection.selected += allowed.size;
    selection.skipped += catalog.conversations.length - allowed.size;
    for (const c of catalog.conversations) selection.byType[c.projectId ? 'projects' : 'recents']++;
  }
  if (missing) run.warnings.push('Some selected items are unavailable.');
  return drainSelected({
    run,
    state: next,
    field: spec.field,
    provider: spec.name,
    checkpoint,
    guard,
    signal,
    report,
    canResume: true,
    onAccepted(principalId, batch) {
      const { archivedUpdates, allowed } = current.get(principalId);
      for (const chat of batch) {
        const listed = allowed.get(chat.externalId)?.updatedAt;
        if (
          Number.isFinite(listed) &&
          listed > 0 &&
          Number.isFinite(chat.updatedAt) &&
          chat.updatedAt >= listed
        )
          archivedUpdates.set(chat.externalId, listed);
      }
      next[spec.field][principalId].archivedUpdates = [...archivedUpdates];
    },
    beforeRead: async (pid) => {
      await guard();
      if (!(await spec.owns(current.get(pid).p))) throw changed();
      await guard();
    },
    read: async (pid, id) => {
      const { p, allowed, legacy, projects, direct } = current.get(pid),
        row = allowed.get(id);
      if (!row) return null;
      const detail = await spec.detail(p, id);
      if (spec.id(detail) !== id) throw invalid();
      if (unavailable(detail) || memberships(detail).some((v) => v !== row.projectId)) return null;
      if (
        (legacy.has(id) || (row.isRecent && !direct.has(id))) &&
        !projects.has(row.projectId) &&
        classifyConversation(detail).categories.some((k) => k !== 'recents')
      )
        return null;
      return spec.parse(detail, id, p);
    },
    push: async (...args) => {
      await verify();
      return push(...args);
    },
  });
}
