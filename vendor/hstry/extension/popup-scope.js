// Existing profiles store the same verified principal and native ID lists across versions.
import { readSyncList } from './chatgpt-sync-list.js';
const MAX_IDS = 10000,
  MAX_OWNERS = 128;
const keys = ['projectIds', 'conversationIds'];
const own = (obj, k) => Object.hasOwn(obj, k);
const record = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
export const validOwner = (v) =>
  typeof v === 'string' && v.trim() !== '' && v.length <= 600 && !/[\x00-\x1f\x7f]/.test(v);
const validId = (v) => typeof v === 'string' && /^[A-Za-z0-9_-]{1,256}$/.test(v);
const fail = (code) => {
  throw Object.assign(new Error(code), { code });
};
const empty = () => ({ entries: [] });
const ordered = (ids) => [...new Set(ids)].sort();
export const hasScopeItems = (entry) =>
  entry.includeRecents === true || entry.projectIds.length > 0 || entry.conversationIds.length > 0;

function normalize(raw, provider) {
  if (!record(raw) || !Array.isArray(raw.entries) || raw.entries.length > MAX_OWNERS)
    fail('selection_invalid');
  let count = 0;
  const owners = new Set();
  const entries = raw.entries.map((e) => {
    if (!record(e) || !validOwner(e.principalId) || owners.has(e.principalId))
      fail('selection_invalid');
    owners.add(e.principalId);
    if (
      Object.keys(e).some(
        (k) =>
          !['principalId', ...keys, 'legacyConversationIds'].includes(k) &&
          !(provider === 'chatgpt' && k === 'includeRecents' && typeof e[k] === 'boolean'),
      )
    )
      fail('selection_invalid');
    const next = { principalId: e.principalId };
    if (e.includeRecents === true) next.includeRecents = true;
    for (const k of keys) {
      if (!Array.isArray(e[k]) || (count += e[k].length) > MAX_IDS || !e[k].every(validId))
        fail('selection_invalid');
      next[k] = ordered(e[k]);
    }
    if (own(e, 'legacyConversationIds')) {
      const ids = new Set(next.conversationIds);
      if (
        !Array.isArray(e.legacyConversationIds) ||
        e.legacyConversationIds.length > MAX_IDS ||
        !e.legacyConversationIds.every((id) => ids.has(id))
      )
        fail('selection_invalid');
      if (e.legacyConversationIds.length)
        next.legacyConversationIds = ordered(e.legacyConversationIds);
    }
    return next;
  });
  entries.sort((a, b) =>
    a.principalId < b.principalId ? -1 : a.principalId > b.principalId ? 1 : 0,
  );
  return { entries };
}

export function scopeFor(provider, rawScopes, legacy) {
  if (rawScopes !== undefined && !record(rawScopes)) return { scope: empty() };
  if (rawScopes && own(rawScopes, provider)) {
    try {
      return { scope: normalize(rawScopes[provider], provider) };
    } catch {
      return { scope: empty() };
    }
  }
  if (provider === 'chatgpt' && legacy !== undefined) {
    const list = readSyncList(legacy);
    return {
      legacy: list,
      scope: {
        entries: list.entries.map((e) => ({
          principalId: JSON.stringify([e.userId, e.accountId]),
          projectIds: e.projectIds,
          conversationIds: e.conversationIds,
          ...(e.conversationIds.length ? { legacyConversationIds: e.conversationIds } : {}),
        })),
      },
    };
  }
  return { scope: empty() };
}
export const nonempty = (scope) => scope?.entries.some(hasScopeItems) === true;

async function currentSettings(provider) {
  const [{ providerSyncScopes }, { chatgptSyncList }] = await Promise.all([
    chrome.storage.local.get('providerSyncScopes'),
    chrome.storage.local.get('chatgptSyncList'),
  ]).catch(() => fail('storage_unavailable'));
  // Refuse to replace an unreadable saved list with a partially reconstructed one.
  if (providerSyncScopes !== undefined && !record(providerSyncScopes)) fail('selection_invalid');
  if (providerSyncScopes && own(providerSyncScopes, provider))
    return { providerSyncScopes, scope: normalize(providerSyncScopes[provider], provider) };
  return { providerSyncScopes, ...scopeFor(provider, providerSyncScopes, chatgptSyncList) };
}

export const readPageScope = async (provider) => (await currentSettings(provider)).scope;

// Local storage has no read-modify-write transaction. Serialize these short ID edits across all tabs/providers.
let writes = Promise.resolve();
function writePageScope(provider, principalId, edit, verify) {
  const work = writes
    .catch(() => {})
    .then(async () => {
      const { scope, providerSyncScopes, legacy } = await currentSettings(provider);
      const entries = new Map(scope.entries.map((e) => [e.principalId, e]));
      const old = entries.get(principalId) ?? { principalId, projectIds: [], conversationIds: [] };
      const next = edit(old);
      if (next && hasScopeItems(next)) entries.set(principalId, next);
      else entries.delete(principalId);
      const saved = normalize({ entries: [...entries.values()] }, provider);
      await verify();
      if (legacy || JSON.stringify(saved) !== JSON.stringify(scope)) {
        try {
          await chrome.storage.local.set({
            providerSyncScopes: { ...providerSyncScopes, [provider]: saved },
            // All v1 owners and their untouched restrictions were copied. A removed ID cannot reappear on fallback.
            ...(legacy ? { chatgptSyncList: { version: 1, entries: [] } } : {}),
          });
        } catch {
          fail('storage_unavailable');
        }
      }
      return hasScopeItems(old) ? old : null;
    });
  writes = work;
  return work;
}

export function setPageItem(provider, principalId, kind, id, checked, sameId, verify) {
  return writePageScope(
    provider,
    principalId,
    (old) => {
      const next = { ...old };
      if (kind === 'recents') {
        if (provider !== 'chatgpt' || id !== 'recents') fail('selection_invalid');
        if (checked) next.includeRecents = true;
        else delete next.includeRecents;
      } else {
        const key = kind === 'project' ? 'projectIds' : 'conversationIds';
        next[key] = old[key].filter((v) => !sameId(v, id));
        if (checked) next[key].push(id);
      }
      if (kind === 'conversation' && old.legacyConversationIds) {
        const untouched = old.legacyConversationIds.filter((v) => !sameId(v, id));
        if (untouched.length) next.legacyConversationIds = untouched;
        else delete next.legacyConversationIds;
      }
      return next;
    },
    verify,
  );
}

export function setBulkPageItems(provider, principalId, items, canonical, verify) {
  return writePageScope(
    provider,
    principalId,
    (old) => {
      if (items === null) return null;
      const selected = new Set(items.map(canonical));
      const keep = (ids) => ids.filter((id) => !selected.has(canonical(id)));
      const next = { ...old, conversationIds: [...keep(old.conversationIds), ...items] };
      if (next.legacyConversationIds) next.legacyConversationIds = keep(next.legacyConversationIds);
      return next;
    },
    verify,
  );
}

export function restoreBulkPageItems(provider, previous, verify) {
  return writePageScope(
    provider,
    previous.principalId,
    (current) => {
      if (hasScopeItems(current)) fail('selection_changed');
      return previous;
    },
    verify,
  );
}
