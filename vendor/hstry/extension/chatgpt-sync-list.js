// Local, ID-only permission list. Absence is the legacy category policy.
// A present but malformed (or intentionally emptied) list must never broaden it.
const uuid = '(?:[a-f\\d]{32}|[a-f\\d]{8}-(?:[a-f\\d]{4}-){3}[a-f\\d]{12})';
const chat = new RegExp('^' + uuid + '$', 'i');
const project = new RegExp('^g-p-' + uuid + '$', 'i');
const identity = (value) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value);
const cap = 10000;
export const emptyList = () => ({ version: 1, entries: [] });

export function sessionPrincipal(session) {
  const userId = session?.user?.id;
  const accountId = session?.account?.id ?? session?.account?.account_id;
  if (!identity(userId) || !identity(accountId) || !session?.accessToken)
    throw new Error('Open ChatGPT, sign in, and reload the active workspace before adding items.');
  return { userId, accountId };
}

export function itemIds(value) {
  const clean = (values, pattern) => {
    if (
      !Array.isArray(values) ||
      values.length > cap ||
      !values.every((id) => typeof id === 'string' && pattern.test(id))
    )
      throw new Error('Invalid ChatGPT selection. Reload ChatGPT and select the rows again.');
    return [...new Set(values.map((value) => value.toLowerCase()))].sort();
  };
  return {
    projectIds: clean(value?.projectIds, project),
    conversationIds: clean(value?.conversationIds, chat),
  };
}

function validList(value) {
  if (value?.version !== 1 || !Array.isArray(value.entries) || value.entries.length > cap)
    throw new Error('Invalid saved sync list.');
  const owners = new Set();
  let count = 0;
  return {
    version: 1,
    entries: value.entries.map((entry) => {
      if (!identity(entry?.userId) || !identity(entry.accountId))
        throw new Error('Invalid saved principal.');
      const owner = JSON.stringify([entry.userId, entry.accountId]);
      if (owners.has(owner)) throw new Error('Duplicate saved principal.');
      owners.add(owner);
      const ids = itemIds(entry);
      count += ids.projectIds.length + ids.conversationIds.length;
      if (count > cap) throw new Error('Sync list is full.');
      return { userId: entry.userId, accountId: entry.accountId, ...ids };
    }),
  };
}

export function readSyncList(value) {
  if (value === undefined) return undefined;
  try {
    return validList(value);
  } catch {
    return emptyList();
  }
}

export function hasItems(list) {
  return (
    list?.entries.some((entry) => entry.projectIds.length || entry.conversationIds.length) ?? false
  );
}

export function editSyncList(stored, principal, selection, remove = false) {
  const list = stored === undefined ? emptyList() : validList(stored);
  const ids = itemIds(selection);
  if (!ids.projectIds.length && !ids.conversationIds.length)
    throw new Error('Select at least one item.');
  const entry = list.entries.find(
    (e) => e.userId === principal.userId && e.accountId === principal.accountId,
  );
  if (entry || !remove) {
    const next = entry ?? { ...principal, projectIds: [], conversationIds: [] };
    for (const key of ['projectIds', 'conversationIds'])
      next[key] = remove
        ? next[key].filter((id) => !ids[key].includes(id))
        : [...new Set([...next[key], ...ids[key]])].sort();
    if (!entry) list.entries.push(next);
  }
  list.entries = list.entries.filter((e) => e.conversationIds.length || e.projectIds.length);
  return validList(list);
}
