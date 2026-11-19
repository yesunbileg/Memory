// Popup discovery is metadata-only. The only durable state of an explicit run is IDs.
import { fetchJson, NotLoggedInError } from '../lib/common.js';
import { createSelectionSummary, classifyConversation } from './selection.js';
import { drainSelected } from './queue.js';

const cap = 10000;
export const nativeId = (id) => typeof id === 'string' && /^[a-zA-Z0-9_-]{1,256}$/.test(id);
const label = (...values) =>
  values
    .find((v) => typeof v === 'string' && v.trim())
    ?.trim()
    .slice(0, 512);
const invalid = () =>
  new Error('Provider catalog is unavailable. Check the signed-in account and retry.');

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

// One reader and one total budget per popup action. Do not retry a vanished account.
async function gather(spec, selected, guard) {
  const principals = await spec.principals();
  const include = (p) =>
    selected === undefined ||
    selected.some(
      (e) => e.principalId === p.principalId && (e.projectIds.length || e.conversationIds.length),
    );
  const groups = [];
  let denied = false;
  let pages = 0,
    items = 0,
    incomplete = false;
  const read = async (url, init = {}) => {
    await guard();
    if (++pages > 200) throw invalid();
    return fetchJson(url, { ...init, cache: 'no-store' }, { retries: 0 });
  };
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
        if (scope && !direct.has(id) && !wantedProjects.has(where)) return;
        const title = label(item.title, item.name);
        const parents = new Set([
          ...memberships(item),
          ...(where ? [where] : []),
          ...(convs.get(id)?.projectId ? [convs.get(id).projectId] : []),
        ]);
        if (
          unavailable(item) ||
          parents.size > 1 ||
          [...parents].some((pid) => !projects.has(pid))
        ) {
          skipped.add(id);
          return;
        }
        if (!title && !convs.has(id)) {
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
        convs.set(id, {
          id,
          title: title ?? convs.get(id).title,
          ...(projectId ? { projectId } : {}),
        });
      }
      // Resolve project pages first so a long ordinary list cannot starve them.
      // A failed membership page cannot grant access to that project via Recents.
      let membershipComplete = true;
      const verifiedMember = new Set();
      const required = (e) => ['selection_changed', 'storage_unavailable'].includes(e?.code);
      for (const [id] of projects) {
        if (scope && !direct.size && !wantedProjects.has(id)) continue;
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
      if (!scope || direct.size) {
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
          conversations: [...convs.values()].sort(
            (a, b) => Number(!!a.projectId) - Number(!!b.projectId),
          ),
        },
        legacyOrdinary,
      });
    } catch (error) {
      if (['selection_changed', 'storage_unavailable'].includes(error?.code)) throw error;
      // An inaccessible organization cannot supply titles or queue entries. Other
      // organizations remain usable only after fresh principal verification below.
      if (error instanceof NotLoggedInError) denied = true;
      // Incomplete project membership can't authorize ordinary-list duplicates.
      entryIncomplete = true;
    }
    incomplete ||= entryIncomplete;
  }
  if (denied && !groups.length) throw invalid();
  if (groups.length && identity(principals) !== identity(await spec.principals())) throw invalid();
  return { groups, incomplete, principals };
}

export async function listPopupCatalog(spec) {
  try {
    const { groups, incomplete } = await gather(spec, undefined, async () => {});
    return { entries: groups.map((g) => g.catalog), ...(incomplete ? { incomplete: true } : {}) };
  } catch {
    throw invalid();
  }
}

function validSelection(value) {
  return (
    Array.isArray(value?.entries) &&
    value.entries.length <= 100 &&
    new Set(value.entries.map((e) => e?.principalId)).size === value.entries.length &&
    value.entries.every(
      (e) =>
        e &&
        typeof e.principalId === 'string' &&
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
  { selectedScope, state = {}, push, register, guard, report, checkpoint },
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
  if (!validSelection(selectedScope)) return stop('Invalid saved selection. Reopen the popup.');
  const chosen = selectedScope.entries.filter(
    (e) => e.conversationIds.length || e.projectIds.length,
  );
  if (!chosen.length) return stop(null);
  await report({ phase: 'discovering', selection });
  const { groups, incomplete, principals } = await gather(spec, chosen, guard);
  if (!groups.length) return stop('The selected account or its catalog is unavailable.');
  const signature = JSON.stringify([identity(principals), chosen]);
  const prior = state.pending?.signature === signature ? state.pending : null;
  const now = Date.now(),
    run = {
      signature,
      started: prior?.started ?? now,
      processed: prior?.processed ?? 0,
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
            projects.has(c.projectId),
        )
        .map((c) => [c.id, c]),
    );
    missing ||=
      [...direct].some((id) => !allowed.has(id)) ||
      [...projects].some((id) => !catalog.projects.some((c) => c.id === id));
    current.set(p.principalId, { p, allowed, legacy, projects });
    const prev = prior?.groups?.find((g) => g.principalId === p.principalId);
    const ids = prev
      ? [...new Set([...prev.ids, ...prev.failed])].filter((id) => allowed.has(id))
      : [...allowed.keys()];
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
    report,
    canResume: true,
    beforeRead: async (pid) => {
      await guard();
      if (!(await spec.owns(current.get(pid).p))) throw invalid();
      await guard();
    },
    read: async (pid, id) => {
      const { p, allowed, legacy, projects } = current.get(pid),
        row = allowed.get(id);
      if (!row) return null;
      const detail = await spec.detail(p, id);
      if (spec.id(detail) !== id) throw invalid();
      if (unavailable(detail) || memberships(detail).some((v) => v !== row.projectId)) return null;
      if (
        legacy.has(id) &&
        !projects.has(row.projectId) &&
        classifyConversation(detail).categories.some((k) => k !== 'recents')
      )
        return null;
      return spec.parse(detail, id, p);
    },
    push: async (...args) => {
      await guard();
      if (identity(principals) !== identity(await spec.principals())) throw invalid();
      await guard();
      return push(...args);
    },
  });
}
