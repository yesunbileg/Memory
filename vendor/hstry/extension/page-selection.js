// Only the extension's top-frame content script can request this broker. The website UI
// gets back state for its own visible rows, not the catalog, titles, accounts or messages.
import './page-picker-routes.js';
import {
  readPageScope,
  restoreBulkPageItems,
  setBulkPageItems,
  setPageItem,
  validOwner,
} from './popup-scope.js';

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
const bulkKey = (id) => 'popupBulk_' + id;
const undoKey = (id) => 'popupUndo_' + id;
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
  if (kind === 'recents') return provider === 'chatgpt' && id === 'recents' ? 'recents' : null;
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

async function makeProof(ctx, adapter, titles = false) {
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
        conversations.push(
          ctx.provider === 'chatgpt' && chat.isRecent === true && chat.projectId == null
            ? [chat.id, null, true]
            : chat.projectId == null
              ? [chat.id]
              : [chat.id, chat.projectId],
        );
      }
    }
    const title = titles && typeof e.title === 'string' ? e.title.trim().slice(0, 120) : '';
    entries.push({ owner: e.principalId, ...(title ? { title } : {}), projects, conversations });
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
  const inspect = (owner, kind, id, projectId, isRecent) => {
    const k = key(ctx.provider, kind, id);
    if (!wants.has(k)) return;
    const row = { owner, kind, id, projectId, isRecent };
    if (found.has(k) && !same(found.get(k), row)) fail('ambiguous_selection');
    found.set(k, row);
  };
  for (const { owner, projects, conversations } of proof.entries) {
    if (ctx.provider === 'chatgpt' && proof.entries.length === 1)
      inspect(owner, 'recents', 'recents');
    for (const id of projects) inspect(owner, 'project', id);
    for (const [id, pid, isRecent] of conversations)
      inspect(owner, 'conversation', id, pid, isRecent);
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
    const checked =
      kind === 'recents'
        ? saved?.includeRecents === true
        : (kind === 'project' ? projects : conversations).has(itemKey);
    items.push({
      kind,
      id,
      checked,
      ...(row.projectId ? { projectId: row.projectId } : {}),
      inherited:
        (!!row.projectId && projects.has(key(ctx.provider, 'project', row.projectId))) ||
        (row.isRecent === true && saved?.includeRecents === true),
    });
  }
  return { ok: true, token: proof.token, items, incomplete: proof.incomplete };
}

async function inTabOrder(tabId, task) {
  const work = (queues.get(tabId) ?? Promise.resolve()).catch(() => {}).then(task);
  queues.set(tabId, work);
  try {
    return await work;
  } finally {
    if (queues.get(tabId) === work) queues.delete(tabId);
  }
}

export async function pageSelection(message, sender, providers) {
  const ctx = pageSender(sender);
  validate(message, ctx.provider);
  // A close/reopen must not be overtaken by a stale in-flight catalog or checkbox write from the same tab.
  return inTabOrder(ctx.tabId, () => handle(message, ctx, providers[ctx.provider]));
}

async function activeBulkTab(tabId, provider) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const route = globalThis.aiMemoryPageRoutes.pageContext(tab?.url);
  if (!tab?.active || tab.discarded || route?.provider !== provider) fail('selection_invalid');
  return { ...route, tabId, url: tab.url };
}

export async function bulkPageSelection(message, providers) {
  const { tabId, provider, action, token, index, url } = message;
  if (
    !only(message, ['type', 'tabId', 'provider', 'action', 'token', 'index', 'url']) ||
    !Number.isInteger(tabId) ||
    tabId < 0 ||
    typeof url !== 'string' ||
    !own(providers, provider) ||
    !['select', 'clear', 'undo'].includes(action) ||
    (token !== undefined && (typeof token !== 'string' || !tokenPattern.test(token))) ||
    (action === 'undo'
      ? token === undefined || index !== undefined
      : token === undefined
        ? index !== undefined
        : !Number.isInteger(index))
  )
    fail('selection_invalid');
  return inTabOrder(tabId, () => changeBulkItems(message, providers[provider]));
}

async function changeBulkItems({ tabId, provider, action, token, index, url }, adapter) {
  const ctx = await activeBulkTab(tabId, provider);
  if (ctx.url !== url) fail('principal_changed');
  const storeKey = bulkKey(tabId);
  const receiptKey = undoKey(tabId);
  const verifyTarget = async (proof) => {
    await verify(ctx, adapter, proof);
    if ((await activeBulkTab(tabId, provider)).url !== ctx.url) fail('principal_changed');
  };
  const refreshSidebar = () =>
    chrome.tabs
      .sendMessage?.(tabId, { type: 'REFRESH_PAGE_SELECTION' }, { frameId: 0 })
      .catch(() => {});
  if (action === 'undo') {
    const receipt = (await chrome.storage.session.get(receiptKey).catch(storageError))[receiptKey];
    if (
      receipt?.token !== token ||
      receipt.provider !== provider ||
      receipt.url !== url ||
      receipt.expires <= Date.now()
    )
      fail('catalog_expired');
    await restoreBulkPageItems(provider, receipt.previous, () => verifyTarget(receipt));
    await chrome.storage.session.remove(receiptKey).catch(() => {});
    refreshSidebar();
    return { ok: true, title: receipt.title };
  }
  let proof, entry;
  if (token !== undefined) {
    proof = (await chrome.storage.session.get(storeKey).catch(storageError))[storeKey];
    if (
      proof?.token !== token ||
      proof.url !== ctx.url ||
      proof.action !== action ||
      proof.expires <= Date.now()
    )
      fail('catalog_expired');
    entry = proof.entries[index];
    if (!entry) fail('selection_invalid');
  } else {
    // Clear for a single verified workspace does not need to enumerate its history.
    const ids = action === 'clear' ? await adapter.principalIds(ctx) : null;
    proof =
      ids?.length === 1 && validOwner(ids[0])
        ? { entries: [{ owner: ids[0], title: '' }], expires: Date.now() + TTL }
        : await makeProof(ctx, adapter, true);
    if (action === 'select' && proof.incomplete) fail('catalog_unavailable');
    if (proof.entries.length > 1) {
      await verifyTarget(proof);
      await chrome.storage.session
        .set({ [storeKey]: { ...proof, url: ctx.url, action } })
        .catch(storageError);
      return {
        ok: true,
        token: proof.token,
        choices: proof.entries.map((row, index) => ({
          index,
          title: row.title || `Workspace ${index + 1}`,
        })),
      };
    }
    entry = proof.entries[0];
  }
  const canonical = (id) => normalizedId(provider, 'conversation', id);
  const items =
    action === 'clear'
      ? null
      : [...new Map(entry.conversations.map(([id]) => [canonical(id), id])).values()];
  const previous = await setBulkPageItems(provider, entry.owner, items, canonical, () =>
    verifyTarget(proof),
  );
  const title = entry.title || '';
  let undoToken;
  // Only the extension worker keeps the previous IDs. The popup gets an opaque receipt
  // for this tab, provider and signed-in workspace.
  if (action === 'clear' && previous) {
    const receipt = {
      token: crypto.randomUUID(),
      provider,
      url,
      title,
      expires: Date.now() + TTL,
      entries: proof.entries.map(({ owner }) => ({ owner })),
      previous,
    };
    try {
      await chrome.storage.session.set({ [receiptKey]: receipt });
      undoToken = receipt.token;
    } catch {
      // The clear already succeeded. Omit Undo if browser session storage failed.
    }
  } else await chrome.storage.session.remove(receiptKey).catch(() => {});
  if (token !== undefined) await chrome.storage.session.remove(storeKey).catch(() => {});
  refreshSidebar();
  return { ok: true, count: items?.length ?? 0, title, ...(undoToken && { undoToken }) };
}

export function forgetPageTab(tabId) {
  const work = (queues.get(tabId) ?? Promise.resolve())
    .catch(() => {})
    .then(() =>
      Promise.all([
        chrome.storage.session.remove(proofKey(tabId)),
        chrome.storage.session.remove(bulkKey(tabId)),
        chrome.storage.session.remove(undoKey(tabId)),
      ]),
    );
  queues.set(tabId, work);
  work
    .catch(() => {})
    .finally(() => {
      if (queues.get(tabId) === work) queues.delete(tabId);
    });
}
