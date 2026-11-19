// Only the extension's top-frame content script can request this broker. The website UI
// gets back state for its own visible rows, not the catalog, titles, accounts or messages.
import './page-picker-routes.js';
import { readPageScope, setPageItem, validOwner } from './popup-scope.js';

const LIMIT = 10000,
  TTL = 15 * 60 * 1000;
const uuid = /^(?:[a-f\d]{32}|[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12})$/i;
const native = /^[a-z\d_-]{1,256}$/i;
const tokenPattern = /^[a-f\d-]{36}$/i;
const fail = (code) => {
  throw Object.assign(new Error(code), { code });
};
const storageError = () => fail('storage_unavailable');
const own = (v, k) => Object.hasOwn(v, k);
const only = (o, fields) =>
  o &&
  typeof o === 'object' &&
  !Array.isArray(o) &&
  Object.keys(o).every((k) => fields.includes(k));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const proofKey = (id) => 'pagePicker_' + id;
const queues = new Map();

function normalizedId(provider, kind, id) {
  if (typeof id !== 'string') return null;
  if (provider === 'gemini') return kind === 'conversation' && native.test(id) ? id : null;
  let raw = id;
  if (kind === 'project' && provider === 'chatgpt') {
    if (!id.toLowerCase().startsWith('g-p-')) return null;
    raw = id.slice(4);
  }
  return uuid.test(raw) ? raw.toLowerCase().replaceAll('-', '') : null;
}
function key(provider, kind, id) {
  if (kind !== 'project' && kind !== 'conversation') return null;
  const value = normalizedId(provider, kind, id);
  return value && (kind === 'project' ? 'p:' : 'c:') + value;
}

function pageSender(sender) {
  if (
    typeof chrome.runtime.id !== 'string' ||
    sender?.id !== chrome.runtime.id ||
    !Number.isInteger(sender.tab?.id) ||
    sender.tab.id < 0 ||
    sender.frameId !== 0 ||
    typeof sender.documentId !== 'string' ||
    !sender.documentId ||
    sender.documentId.length > 128 ||
    (sender.documentLifecycle && sender.documentLifecycle !== 'active')
  )
    fail('untrusted_sender');
  const context = globalThis.aiMemoryPageRoutes.pageContext(sender.url);
  if (!context || (sender.origin !== undefined && sender.origin !== new URL(sender.url).origin))
    fail('untrusted_sender');
  if (sender.tab.url !== undefined) {
    const tab = globalThis.aiMemoryPageRoutes.pageContext(sender.tab.url);
    if (!tab || tab.provider !== context.provider || tab.slot !== context.slot)
      fail('untrusted_sender');
  }
  return { ...context, documentId: sender.documentId, tabId: sender.tab.id };
}

function validate(message, provider) {
  const read = message.type === 'READ_PAGE_SELECTION',
    set = message.type === 'SET_PAGE_SELECTION';
  const fields = read
    ? ['type', 'items', 'token']
    : set
      ? ['type', 'token', 'kind', 'id', 'checked']
      : ['type', 'token'];
  if (
    !only(message, fields) ||
    ((!read || own(message, 'token')) &&
      (typeof message.token !== 'string' || !tokenPattern.test(message.token)))
  )
    fail('selection_invalid');
  if (read) {
    if (
      !Array.isArray(message.items) ||
      message.items.length > 500 ||
      !message.items.every((i) => only(i, ['kind', 'id']) && key(provider, i.kind, i.id))
    )
      fail('selection_invalid');
  } else if (
    set &&
    (!key(provider, message.kind, message.id) || typeof message.checked !== 'boolean')
  )
    fail('selection_invalid');
}

async function makeProof(ctx, adapter) {
  const raw = await adapter.listCatalog(ctx);
  if (!Array.isArray(raw?.entries) || !raw.entries.length || raw.entries.length > 128)
    fail('catalog_unavailable');
  let remaining = LIMIT,
    incomplete = raw.incomplete === true;
  const entries = [];
  for (const e of raw.entries) {
    if (
      !validOwner(e?.principalId) ||
      entries.some((x) => x.owner === e.principalId) ||
      !Array.isArray(e.projects) ||
      !Array.isArray(e.conversations)
    )
      fail('catalog_unavailable');
    const projects = [],
      conversations = [];
    for (const project of e.projects) {
      if (!remaining) {
        incomplete = true;
        break;
      }
      remaining--;
      if (key(ctx.provider, 'project', project?.id)) projects.push(project.id);
    }
    const known = new Set(projects);
    for (const chat of e.conversations) {
      if (!remaining) {
        incomplete = true;
        break;
      }
      remaining--;
      if (
        key(ctx.provider, 'conversation', chat?.id) &&
        (chat.projectId == null || known.has(chat.projectId))
      ) {
        conversations.push(chat.projectId == null ? [chat.id] : [chat.id, chat.projectId]);
      }
    }
    entries.push({ owner: e.principalId, projects, conversations });
  }
  // An incomplete organization inventory cannot disambiguate identical row IDs between organizations.
  if (incomplete && entries.length > 1) fail('ambiguous_selection');
  return {
    token: crypto.randomUUID(),
    documentId: ctx.documentId,
    provider: ctx.provider,
    slot: ctx.slot,
    expires: Date.now() + TTL,
    entries,
    incomplete,
    owner: null,
    shown: [],
  };
}

async function oldProof(ctx, token) {
  const k = proofKey(ctx.tabId);
  const proof = (await chrome.storage.session.get(k).catch(storageError))[k];
  if (
    !proof ||
    proof.token !== token ||
    proof.documentId !== ctx.documentId ||
    proof.provider !== ctx.provider ||
    proof.slot !== ctx.slot ||
    proof.expires <= Date.now()
  )
    fail('catalog_expired');
  return proof;
}

async function verify(ctx, adapter, proof) {
  const ids = await adapter.principalIds(ctx);
  if (
    !Array.isArray(ids) ||
    !ids.every(validOwner) ||
    !same(proof.entries.map((e) => e.owner).sort(), [...ids].sort())
  )
    fail('principal_changed');
  if (proof.expires <= Date.now()) fail('catalog_expired');
}

// Match only the IDs the content script actually saw. Multiple candidate owners, multiple native
// spellings or conflicting membership for the same visible link cannot authorize a write.
function match(ctx, proof, requested) {
  const wants = new Set(requested.map((i) => key(ctx.provider, i.kind, i.id)));
  const found = new Map();
  const inspect = (owner, kind, id, projectId) => {
    const k = key(ctx.provider, kind, id);
    if (!wants.has(k)) return;
    const row = { owner, kind, id, projectId };
    if (found.has(k) && !same(found.get(k), row)) fail('ambiguous_selection');
    found.set(k, row);
  };
  for (const { owner, projects, conversations } of proof.entries) {
    for (const id of projects) inspect(owner, 'project', id);
    for (const [id, pid] of conversations) inspect(owner, 'conversation', id, pid);
  }
  const owners = new Set([...found.values()].map((r) => r.owner));
  if (proof.owner) owners.add(proof.owner);
  if (owners.size > 1) fail('ambiguous_selection');
  return found;
}

async function handle(message, ctx, adapter) {
  const fresh = message.type === 'READ_PAGE_SELECTION' && !own(message, 'token');
  const k = proofKey(ctx.tabId);
  // Reopening mode revokes the previous grant even if the new discovery fails.
  if (fresh) await chrome.storage.session.remove(k).catch(storageError);
  const proof = fresh ? await makeProof(ctx, adapter) : await oldProof(ctx, message.token);
  if (message.type === 'CLOSE_PAGE_SELECTION') {
    await chrome.storage.session.remove(k).catch(storageError);
    return { ok: true };
  }
  const recheck = async () => {
    try {
      await verify(ctx, adapter, proof);
    } catch (e) {
      if (e?.code === 'principal_changed')
        await chrome.storage.session.remove(k).catch(storageError);
      throw e;
    }
  };
  if (message.type === 'SET_PAGE_SELECTION') {
    const { kind, id, checked } = message,
      itemKey = key(ctx.provider, kind, id);
    if (!proof.shown.includes(itemKey)) fail('selection_invalid');
    const row = match(ctx, proof, [{ kind, id }]).get(itemKey);
    if (!row || proof.owner !== row.owner) fail('selection_invalid');
    await setPageItem(
      ctx.provider,
      row.owner,
      kind,
      row.id,
      checked,
      (a, b) => key(ctx.provider, kind, a) === key(ctx.provider, kind, b),
      recheck,
    );
    return { ok: true, token: proof.token, kind, id, checked };
  }
  let rows;
  try {
    rows = match(ctx, proof, message.items);
  } catch (error) {
    // A mixed/new organization invalidates the earlier view, even for a previously shown row.
    if (error?.code === 'ambiguous_selection')
      await chrome.storage.session.remove(k).catch(storageError);
    throw error;
  }
  const shown = new Set(proof.shown);
  const added = [...rows.keys()].filter((id) => !shown.has(id));
  if (added.length) {
    proof.owner = rows.values().next().value.owner;
    proof.shown.push(...added);
  }
  await recheck();
  const scope = await readPageScope(ctx.provider);
  if (fresh || added.length) await chrome.storage.session.set({ [k]: proof }).catch(storageError);
  const saved = scope.entries.find((e) => e.principalId === proof.owner);
  const projects = new Set(saved?.projectIds.map((id) => key(ctx.provider, 'project', id)));
  const conversations = new Set(
    saved?.conversationIds.map((id) => key(ctx.provider, 'conversation', id)),
  );
  const items = [];
  for (const { kind, id } of message.items) {
    const itemKey = key(ctx.provider, kind, id),
      row = rows.get(itemKey);
    if (!row) continue;
    const checked = (kind === 'project' ? projects : conversations).has(itemKey);
    items.push({
      kind,
      id,
      checked,
      ...(row.projectId ? { projectId: row.projectId } : {}),
      inherited: !!row.projectId && projects.has(key(ctx.provider, 'project', row.projectId)),
    });
  }
  return { ok: true, token: proof.token, items, incomplete: proof.incomplete };
}

export async function pageSelection(message, sender, providers) {
  const ctx = pageSender(sender);
  validate(message, ctx.provider);
  // A close/reopen must not be overtaken by a stale in-flight catalog or checkbox write from the same tab.
  const work = (queues.get(ctx.tabId) ?? Promise.resolve())
    .catch(() => {})
    .then(() => handle(message, ctx, providers[ctx.provider]));
  queues.set(ctx.tabId, work);
  try {
    return await work;
  } finally {
    if (queues.get(ctx.tabId) === work) queues.delete(ctx.tabId);
  }
}

export function forgetPageTab(tabId) {
  const work = (queues.get(tabId) ?? Promise.resolve())
    .catch(() => {})
    .then(() => chrome.storage.session.remove(proofKey(tabId)));
  queues.set(tabId, work);
  work
    .catch(() => {})
    .finally(() => {
      if (queues.get(tabId) === work) queues.delete(tabId);
    });
}
