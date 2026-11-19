// The persistent permission list contains identities and IDs only. A catalog
// grants no permission; its short-lived proof contains no titles or content.
import { readSyncList } from './chatgpt-sync-list.js';
const MAX_IDS = 10000,
  MAX_OWNERS = 128,
  MAX_CATALOG = MAX_IDS,
  TTL_MS = 15 * 60 * 1000;
const keys = ['projectIds', 'conversationIds'];
const own = (obj, k) => Object.hasOwn(obj, k);
const record = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const validOwner = (v) =>
  typeof v === 'string' && v.trim() !== '' && v.length <= 600 && !/[\x00-\x1f\x7f]/.test(v);
const validId = (v) => typeof v === 'string' && /^[A-Za-z0-9_-]{1,256}$/.test(v);
const fail = (code) => {
  throw Object.assign(new Error(code), { code });
};
const empty = () => ({ entries: [] });
const ordered = (ids) => [...new Set(ids)].sort();

function normalize(raw, stored = false) {
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
        (k) => !['principalId', ...keys, ...(stored ? ['legacyConversationIds'] : [])].includes(k),
      )
    )
      fail('selection_invalid');
    const next = { principalId: e.principalId };
    for (const k of keys) {
      if (!Array.isArray(e[k]) || (count += e[k].length) > MAX_IDS || !e[k].every(validId))
        fail('selection_invalid');
      next[k] = ordered(e[k]);
    }
    if (stored && own(e, 'legacyConversationIds')) {
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
      return { scope: normalize(rawScopes[provider], true) };
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
export const nonempty = (scope) =>
  scope?.entries.some((e) => e.projectIds.length || e.conversationIds.length) === true;

function visible(scope, owners) {
  return {
    entries: scope.entries
      .filter((e) => owners.has(e.principalId))
      .map(({ principalId, projectIds, conversationIds }) => ({
        principalId,
        projectIds,
        conversationIds,
      })),
  };
}
function title(v, fallback) {
  return typeof v === 'string' && v.trim() ? v.trim().slice(0, 320) : fallback;
}
function catalog(raw) {
  if (!record(raw) || !Array.isArray(raw.entries)) fail('catalog_unavailable');
  const entries = [],
    owners = new Set();
  let incomplete = raw.incomplete === true || raw.entries.length > MAX_OWNERS;
  let budget = MAX_CATALOG;
  for (const e of raw.entries.slice(0, MAX_OWNERS)) {
    if (
      !validOwner(e?.principalId) ||
      owners.has(e.principalId) ||
      !Array.isArray(e.projects) ||
      !Array.isArray(e.conversations)
    )
      fail('catalog_unavailable');
    owners.add(e.principalId);
    const next = { principalId: e.principalId, title: title(e.title, 'Current account') };
    for (const key of ['projects', 'conversations']) {
      const seen = new Set();
      next[key] = [];
      if (e[key].length > MAX_CATALOG) incomplete = true;
      for (const row of e[key].slice(0, MAX_CATALOG)) {
        if (!budget) {
          incomplete = true;
          break;
        }
        if (!validId(row?.id)) {
          incomplete = true;
          continue;
        }
        if (seen.has(row.id)) continue;
        seen.add(row.id);
        const item = {
          id: row.id,
          title: title(row.title, key === 'projects' ? 'Untitled project' : 'Untitled chat'),
        };
        if (key === 'conversations' && validId(row.projectId)) item.projectId = row.projectId;
        next[key].push(item);
        budget--;
      }
    }
    entries.push(next);
  }
  return { entries, incomplete };
}
async function currentSettings(provider) {
  const [{ providerSyncScopes }, { chatgptSyncList }] = await Promise.all([
    chrome.storage.local.get('providerSyncScopes'),
    chrome.storage.local.get('chatgptSyncList'),
  ]);
  return { providerSyncScopes, ...scopeFor(provider, providerSyncScopes, chatgptSyncList) };
}
const memoryProof = new Map(),
  sequence = new Map();
const proofKey = (p) => 'popupCatalog_' + p;
async function readProof(p, token) {
  const key = proofKey(p);
  const proof = chrome.storage.session
    ? (await chrome.storage.session.get(key))[key]
    : memoryProof.get(p);
  if (!proof || proof.token !== token || typeof token !== 'string' || proof.expires < Date.now())
    fail('catalog_expired');
  return proof;
}

export async function listPopupCatalog(provider, adapter) {
  const generation = (sequence.get(provider) ?? 0) + 1;
  sequence.set(provider, generation);
  const { entries, incomplete } = catalog(await adapter.listCatalog());
  const { scope } = await currentSettings(provider);
  if (sequence.get(provider) !== generation) fail('catalog_expired');
  const token = crypto.randomUUID();
  const proof = {
    token,
    expires: Date.now() + TTL_MS,
    entries: entries.map((e) => ({
      principalId: e.principalId,
      projectIds: e.projects.map((i) => i.id),
      conversationIds: e.conversations.map((i) => i.id),
    })),
  };
  if (chrome.storage.session) await chrome.storage.session.set({ [proofKey(provider)]: proof });
  else memoryProof.set(provider, proof);
  return {
    ok: true,
    catalogToken: token,
    entries,
    incomplete,
    selectedScope: visible(scope, new Set(entries.map((e) => e.principalId))),
  };
}

// One critical section for all providers: local storage has no read-modify-write transaction.
let writes = Promise.resolve();
export function savePopupScope(provider, adapter, message, remove) {
  const work = writes
    .catch(() => {})
    .then(async () => {
      const requested = normalize(message.selection).entries;
      const proof = await readProof(provider, message.catalogToken);
      const allowed = new Map(proof.entries.map((e) => [e.principalId, e]));
      if (requested.some((e) => !allowed.has(e.principalId))) fail('selection_invalid');
      const identities = await adapter.principalIds();
      if (
        !Array.isArray(identities) ||
        !identities.every(validOwner) ||
        proof.entries.some((e) => !identities.includes(e.principalId))
      )
        fail('principal_changed');
      const { scope, providerSyncScopes, legacy } = await currentSettings(provider);
      const changes = new Map(requested.map((e) => [e.principalId, e]));
      const merged = new Map(scope.entries.map((e) => [e.principalId, e]));
      for (const [principalId, shown] of allowed) {
        const old = merged.get(principalId) ?? { principalId, projectIds: [], conversationIds: [] };
        const input = changes.get(principalId) ?? { projectIds: [], conversationIds: [] };
        const next = { principalId };
        for (const key of keys) {
          const permitted = new Set(remove ? old[key] : shown[key]);
          if (input[key].some((id) => !permitted.has(id))) fail('selection_invalid');
          const delta = new Set(input[key]);
          next[key] = remove
            ? old[key].filter((id) => !delta.has(id))
            : ordered([...old[key], ...delta]);
        }
        if (old.legacyConversationIds) {
          const touched = new Set(input.conversationIds);
          const remaining = old.legacyConversationIds.filter((id) => !touched.has(id));
          if (remaining.length) next.legacyConversationIds = remaining;
        }
        if (next.projectIds.length || next.conversationIds.length) merged.set(principalId, next);
        else merged.delete(principalId);
      }
      const saved = normalize({ entries: [...merged.values()] }, true);
      await readProof(provider, message.catalogToken);
      if (providerSyncScopes !== undefined && !record(providerSyncScopes))
        fail('selection_invalid');
      try {
        await chrome.storage.local.set({
          providerSyncScopes: { ...providerSyncScopes, [provider]: saved },
          // All legacy principals were copied, including unseen IDs with their original restrictions.
          // Retire the older copy in the same write: a removed ID must not reappear on fallback.
          ...(legacy ? { chatgptSyncList: { version: 1, entries: [] } } : {}),
        });
      } catch {
        fail('storage_unavailable');
      }
      return { ok: true, selectedScope: visible(saved, new Set(allowed.keys())) };
    });
  writes = work;
  return work;
}
