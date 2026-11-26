import { listPopupCatalog, syncPopupScope, unavailable } from './popup-catalog.js';
import { readSyncList, hasItems, sessionPrincipal } from '../chatgpt-sync-list.js';
import { drainSelected, preparePrincipalRun } from './queue.js';
// ChatGPT provider: syncs conversations (personal + Teams workspaces) via the
// chatgpt.com backend API, authenticated with the browser session cookies.

import {
  NotLoggedInError,
  fetchJson,
  shortId,
  textPart,
  thinkingPart,
  toMs,
  withAbort,
} from '../lib/common.js';

import {
  normalizeCaptureTypes,
  selectConversation,
  createSelectionSummary,
  recordSelection,
} from './selection.js';

const MAX_DISCOVERY_PAGES = 200;
const BASE = 'https://chatgpt.com';
const PAGE_SIZE = 50;
// Refetch a little history on every run so near-simultaneous edits are not
// missed between polls.
const OVERLAP_MS = 5 * 60 * 1000;
// Pace detail requests to stay under ChatGPT's rate limit. Backoff in
// fetchJson handles bursts, but pacing avoids tripping 429 in the first place.

export async function getChatGPTSession(context, signal) {
  const data = await (context
    ? withAbort(
        () =>
          chrome.tabs.sendMessage(
            context.tabId,
            { type: 'GET_CHATGPT_TAB_SESSION' },
            { frameId: 0, ...(context.documentId ? { documentId: context.documentId } : {}) },
          ),
        signal,
      ).catch(() => {
        signal?.throwIfAborted();
        throw Object.assign(new Error('The ChatGPT tab is unavailable.'), {
          code: 'provider_tab_unavailable',
        });
      })
    : fetchJson(`${BASE}/api/auth/session`, { cache: 'no-store', signal }));
  if (!data?.accessToken) throw new NotLoggedInError('chatgpt.com');
  return data;
}

function authHeaders(token, accountId) {
  const headers = { Authorization: `Bearer ${token}` };
  if (accountId) headers['chatgpt-account-id'] = accountId;
  return headers;
}

/** Enumerate accounts (personal + Teams workspaces). Falls back to the
 * default account when the accounts endpoint is unavailable. */
async function listAccounts(token, options) {
  try {
    const data = await fetchJson(`${BASE}/backend-api/accounts/check/v4-2023-04-27`, {
      headers: authHeaders(token),
      ...options,
    });
    const accounts = [];
    let incomplete = false;
    for (const entry of Object.values(data?.accounts ?? {})) {
      const account = entry?.account ?? entry;
      const id = account?.account_id ?? account?.id;
      if (account?.is_deactivated) continue;
      if (typeof id !== 'string' || !id) {
        incomplete = true;
        continue;
      }
      if (accounts.some((a) => a.id === id)) continue;
      accounts.push({
        id,
        name: account?.organization_name || account?.name || account?.plan_type || 'personal',
        isWorkspace: (account?.structure ?? account?.plan_type) === 'workspace',
      });
    }
    if (accounts.length > 0) return { accounts, incomplete };
  } catch {
    options?.signal?.throwIfAborted();
    // Endpoint shape changed or unavailable: sync the default account only.
  }
  return { accounts: [{ id: null, name: 'default', isWorkspace: false }], incomplete: true };
}

async function* listUpdatedConversations(token, accountId, sinceMs, readPage) {
  const seen = new Set();
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const offset = page * PAGE_SIZE;
    const data = await readPage(
      `${BASE}/backend-api/conversations?offset=${offset}&limit=${PAGE_SIZE}&order=updated`,
      { headers: authHeaders(token, accountId) },
    );
    if (!Array.isArray(data?.items))
      throw new Error('ChatGPT conversation list shape is unavailable');
    const items = data.items;
    if (items.length && items.every((item) => seen.has(item?.id)))
      throw new Error('Chatgpt conversation pagination did not advance');
    for (const item of items) seen.add(item?.id);
    for (const item of items) {
      const updatedMs = toMs(item.update_time);
      if (sinceMs && updatedMs && updatedMs <= sinceMs) return;
      yield item;
    }
    if (
      items.length < PAGE_SIZE &&
      Number.isFinite(data.total) &&
      offset + items.length < data.total
    )
      throw new Error('ChatGPT conversation page is incomplete');
    if (
      items.length < PAGE_SIZE ||
      (Number.isFinite(data.total) && offset + PAGE_SIZE >= data.total)
    )
      return;
  }
  throw new Error('ChatGPT conversation pagination reached the safety limit');
}

/** Verified community-client endpoint: see donor-audit.md, no endpoint probing. */
async function listProjectGizmos(token, accountId, readPage) {
  const projects = [];
  const cursors = new Set();
  let cursor = null;
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const url = new URL(`${BASE}/backend-api/gizmos/snorlax/sidebar`);
    url.searchParams.set('conversations_per_gizmo', '0');
    if (cursor !== null) url.searchParams.set('cursor', String(cursor));
    const data = await readPage(url.href, { headers: authHeaders(token, accountId) });
    if (!Array.isArray(data?.items))
      throw new Error('ChatGPT project sidebar shape is unavailable');
    for (const entry of data.items) {
      const project = entry?.gizmo?.gizmo;
      if (typeof project?.id !== 'string' || !project.id)
        throw new Error('ChatGPT project identifier is unavailable');
      projects.push({
        ...project,
        ...(unavailable(entry) || unavailable(entry?.gizmo) ? { is_hidden: true } : {}),
        display: project.display ?? entry.gizmo?.display ?? entry.display,
        title: project.title ?? entry.title ?? entry.name,
      });
    }
    const next = data.cursor ?? null;
    if (next === null && (data.has_more === true || data.next_cursor != null))
      throw new Error('Incomplete pagination');
    if (next === null) return projects;
    if (!['string', 'number'].includes(typeof next) || cursors.has(String(next)))
      throw new Error('ChatGPT project sidebar cursor is invalid');
    cursors.add(String(next));
    cursor = next;
  }
  throw new Error('ChatGPT project sidebar pagination reached the safety limit');
}

async function* listProjectConversations(token, accountId, projectId, readPage) {
  const cursors = new Set();
  let cursor = 0;
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const url = new URL(
      `${BASE}/backend-api/gizmos/${encodeURIComponent(projectId)}/conversations`,
    );
    url.searchParams.set('limit', String(PAGE_SIZE));
    url.searchParams.set('cursor', String(cursor));
    const data = await readPage(url.href, { headers: authHeaders(token, accountId) });
    if (!Array.isArray(data?.items))
      throw new Error('ChatGPT project conversation shape is unavailable');
    for (const item of data.items) yield item;
    const next = data.cursor ?? null;
    if (next === null && (data.has_more === true || data.next_cursor != null))
      throw new Error('Incomplete pagination');
    if (next === null) return;
    if (!['string', 'number'].includes(typeof next) || cursors.has(String(next)))
      throw new Error('ChatGPT project conversation cursor is invalid');
    cursors.add(String(next));
    cursor = next;
  }
  throw new Error('ChatGPT project conversation pagination reached the safety limit');
}

/** Linearize the active branch of the mapping tree (current_node -> root).
 * If ChatGPT returns a broken parent chain, preserve all message-bearing nodes
 * in deterministic chronological order rather than silently losing the prefix. */
function linearize(detail) {
  const mapping = detail?.mapping ?? {};
  const chain = [];
  const visited = new Set();
  let nodeId = detail?.current_node;
  let incomplete = !nodeId;

  while (nodeId) {
    if (!mapping[nodeId] || visited.has(nodeId)) {
      incomplete = true;
      break;
    }
    visited.add(nodeId);
    chain.push(mapping[nodeId]);
    nodeId = mapping[nodeId].parent;
  }

  if (!incomplete) {
    chain.reverse();
    return chain;
  }

  const compareEntries = (left, right) =>
    left.createdAt - right.createdAt ||
    left.id.localeCompare(right.id) ||
    left.key.localeCompare(right.key);
  const seenIds = new Set();
  const entries = Object.entries(mapping)
    .map(([key, node]) => ({
      key,
      node,
      id: String(node?.id ?? key),
      createdAt: toMs(node?.message?.create_time ?? node?.create_time) ?? 0,
    }))
    .sort(compareEntries)
    .filter((entry) => {
      if (seenIds.has(entry.id)) return false;
      seenIds.add(entry.id);
      return true;
    });

  // A topological sort keeps parents before children when ChatGPT assigns the
  // same timestamp to several nodes. The chronological/id ordering of the
  // ready queue makes the result deterministic across repeated syncs.
  const byId = new Map(entries.map((entry) => [entry.id, entry]));
  const children = new Map();
  const indegree = new Map(entries.map((entry) => [entry.id, 0]));
  for (const entry of entries) {
    const parentKey = entry.node?.parent;
    const parentId = parentKey ? String(mapping[parentKey]?.id ?? parentKey) : null;
    if (!parentId || parentId === entry.id || !byId.has(parentId)) continue;
    indegree.set(entry.id, 1);
    const siblings = children.get(parentId) ?? [];
    siblings.push(entry.id);
    children.set(parentId, siblings);
  }

  const ready = entries.filter((entry) => indegree.get(entry.id) === 0).sort(compareEntries);
  const ordered = [];
  while (ready.length > 0) {
    const entry = ready.shift();
    ordered.push(entry);
    for (const childId of children.get(entry.id) ?? []) {
      indegree.set(childId, indegree.get(childId) - 1);
      if (indegree.get(childId) === 0) {
        ready.push(byId.get(childId));
        ready.sort(compareEntries);
      }
    }
  }

  if (ordered.length < entries.length) {
    const orderedIds = new Set(ordered.map((entry) => entry.id));
    ordered.push(...entries.filter((entry) => !orderedIds.has(entry.id)).sort(compareEntries));
  }

  return ordered.filter((entry) => entry.node?.message).map((entry) => entry.node);
}

// Only documented visible text fields are imported. Arbitrary metadata may
// contain private prompts, tool arguments, or internal reasoning.
function extractMessage(node, index) {
  const msg = node?.message;
  if (!msg) return null;
  const role = msg.author?.role;
  if (role !== 'user' && role !== 'assistant') return null;
  if (msg.metadata?.is_visually_hidden_from_conversation || msg.metadata?.visibility === 'hidden')
    return null;
  const channel = msg.channel ?? msg.metadata?.channel;
  if (channel && !['final', 'commentary'].includes(channel)) return null;
  if (msg.recipient && msg.recipient !== 'all') return null;
  const parts = [];
  const texts = [];
  const content = msg.content;
  if (
    !content ||
    typeof content !== 'object' ||
    Array.isArray(content) ||
    typeof content.content_type !== 'string'
  )
    throw new Error('ChatGPT message content shape is unavailable');
  const addText = (text) => {
    if (typeof text !== 'string' || text.length === 0) return;
    texts.push(text);
    parts.push(textPart(text));
  };
  if (content.content_type === 'text' || content.content_type === 'multimodal_text') {
    if (!Array.isArray(content.parts))
      throw new Error('ChatGPT message parts shape is unavailable');
    for (const part of content.parts) {
      if (typeof part === 'string') addText(part);
      else if (part?.content_type === 'text' && typeof part.text === 'string') addText(part.text);
      else if (
        !['image_asset_pointer', 'audio_asset_pointer', 'video_container_asset_pointer'].includes(
          part?.content_type,
        )
      )
        throw new Error('ChatGPT text part is unavailable');
    }
  } else if (content.content_type === 'code' && typeof content.text === 'string') {
    addText(content.text);
  } else if (content.content_type === 'thoughts' && content.visibility === 'ui') {
    for (const thought of content.thoughts ?? []) {
      const text = thought?.summary;
      if (typeof text === 'string' && text) parts.push({ ...thinkingPart(text), visibility: 'ui' });
    }
  } else if (
    ![
      'thoughts',
      'reasoning_recap',
      'tether_quote',
      'tether_browsing_display',
      'execution_output',
      'tool_result',
    ].includes(content.content_type)
  ) {
    throw new Error('ChatGPT visible content format is unavailable');
  }
  const contentStr = texts.join('');
  if (!contentStr && parts.length === 0) return null;
  return {
    role,
    content: contentStr,
    createdAt: toMs(msg.create_time),
    model: msg.metadata?.model_slug ?? null,
    parts,
    metadata: {
      id: msg.id ?? node.id ?? `record:${index + 1}`,
      ...(channel === 'commentary' ? { kind: 'progress' } : {}),
    },
  };
}

function toParsedConversation(detail, conversationId, accountId, capturePrincipal) {
  const mapping = detail?.mapping;
  if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping))
    throw new Error('ChatGPT conversation detail shape is unavailable');
  for (const node of Object.values(mapping)) {
    if (
      !node ||
      typeof node !== 'object' ||
      Array.isArray(node) ||
      !Object.hasOwn(node, 'message') ||
      (node.message !== null &&
        (!node.message ||
          typeof node.message !== 'object' ||
          Array.isArray(node.message) ||
          typeof node.message.author?.role !== 'string'))
    )
      throw new Error('ChatGPT conversation node shape is unavailable');
  }
  const messages = linearize(detail).map(extractMessage).filter(Boolean);
  if (messages.length === 0) return null;

  const model = [...messages].reverse().find((m) => m.model)?.model ?? null;

  return {
    externalId: conversationId,
    title: detail?.title ?? null,
    createdAt: toMs(detail?.create_time),
    updatedAt: toMs(detail?.update_time),
    model,
    provider: 'openai',
    messages,
    metadata: {
      url: `${BASE}/c/${conversationId}`,
      ...(accountId ? { accountId } : {}),
      ...(capturePrincipal && { capturePrincipal }),
    },
  };
}

// Metadata that explicitly marks an item as private or unavailable overrides its sidebar link.
function excluded(item) {
  return [item, item?.metadata].some(
    (value) =>
      value &&
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
      ].some((key) => value[key] === true) ||
        value.is_visible === false ||
        value.is_visible_in_sidebar === false),
  );
}

function explicitProjects(item) {
  return [item, item?.metadata]
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
    .filter((id) => typeof id === 'string' && id);
}

const tabRequestOptions = { credentials: 'omit', redirect: 'error' };
const readFor = (p, read) => (url, init) => read(url, { ...init, ...p.requestOptions });

async function popupPrincipals(context, signal) {
  const session = await getChatGPTSession(context, signal);
  const { userId, accountId } = sessionPrincipal(session);
  const requestOptions = { ...(context ? tabRequestOptions : {}), signal };
  const discovery = await listAccounts(session.accessToken, requestOptions);
  const account = discovery.accounts.find((a) => a.id === accountId);
  if (!account) throw new Error('Current ChatGPT account is not verified.');
  return [
    {
      principalId: JSON.stringify([userId, accountId]),
      capturePrincipal: [userId, accountId],
      id: accountId,
      title: account.name,
      token: session.accessToken,
      requestOptions,
      sourceId: account.isWorkspace ? `chatgpt-web-${shortId(accountId)}` : 'chatgpt-web',
    },
  ];
}

const popup = (context, signal) => ({
  name: 'ChatGPT',
  recents: true,
  field: 'accounts',
  adapter: 'chatgpt-web',
  principals: () => popupPrincipals(context, signal),
  owns: async (p) => {
    const { userId, accountId } = sessionPrincipal(await getChatGPTSession(context, signal));
    return JSON.stringify([userId, accountId]) === p.principalId;
  },
  id: (item) =>
    item?.id != null && item?.conversation_id != null && item.id !== item.conversation_id
      ? null
      : (item?.id ?? item?.conversation_id),
  updatedAt: (item) => toMs(item.update_time),
  projects: (p, read) => listProjectGizmos(p.token, p.id, readFor(p, read)),
  chats: (p, read) => listUpdatedConversations(p.token, p.id, null, readFor(p, read)),
  members: (p, id, read) => listProjectConversations(p.token, p.id, id, readFor(p, read)),
  detail: (p, id) =>
    fetchJson(`${BASE}/backend-api/conversation/${encodeURIComponent(id)}`, {
      headers: authHeaders(p.token, p.id),
      ...p.requestOptions,
    }),
  parse: (detail, id, p) => toParsedConversation(detail, id, p.id, p.capturePrincipal),
});
export const listChatGPTCatalog = (context) => listPopupCatalog(popup(context));
export const chatGPTPrincipalIds = async (context, { signal } = {}) =>
  (await popupPrincipals(context, signal)).map((p) => p.principalId);

export async function syncChatGPT({
  state = {},
  captureTypes,
  chatgptSyncList,
  selectedScope,
  chatgptTab,
  tabContext,
  push,
  register = async () => {},
  log = () => {},
  checkpoint = async () => {},
  guard = async () => {},
  signal,
  report = async () => {},
}) {
  if (selectedScope !== undefined)
    return syncPopupScope(popup(tabContext ?? chatgptTab, signal), {
      selectedScope,
      state,
      push,
      register,
      guard,
      signal,
      report,
      checkpoint,
    });
  const policy = normalizeCaptureTypes(captureTypes);
  const selection = createSelectionSummary();
  const saved = readSyncList(chatgptSyncList);
  if (saved ? !hasItems(saved) : !Object.values(policy).some(Boolean)) {
    await report({
      phase: 'complete',
      detected: 0,
      processed: 0,
      selection,
      selectionWarning: null,
    });
    return { state, conversations: 0, selection, selectionWarning: null };
  }
  await report({ phase: 'discovering', selection });
  const session = await getChatGPTSession();
  const token = session.accessToken;
  const principal = saved ? sessionPrincipal(session) : null;
  const explicit = saved?.entries.find(
    (entry) => entry.accountId === principal.accountId && entry.userId === principal.userId,
  );
  const noMatch = async () => {
    const selectionWarning = 'No saved selection is available for the signed-in ChatGPT workspace.';
    await report({ phase: 'attention', detected: 0, processed: 0, selection, selectionWarning });
    return { state: {}, conversations: 0, selection, selectionWarning };
  };
  if (saved && !explicit) return noMatch();
  const discovery = await listAccounts(token);
  const accounts = explicit
    ? discovery.accounts.filter((a) => a.id === explicit.accountId)
    : discovery.accounts;
  if (!accounts.length) return noMatch();
  const directIds = new Set(explicit?.conversationIds);
  const projectIds = new Set(explicit?.projectIds);
  const newState = { ...state, accounts: discovery.incomplete ? { ...state.accounts } : {} };
  const warnings = new Set();
  if (discovery.incomplete)
    warnings.add(
      'ChatGPT account discovery is incomplete; other workspaces may be unavailable. Retry to capture them.',
    );
  const missingTypes = new Set();
  let missingMetadata = 0;
  delete newState.discovery;
  const { pager, canResume, resumed, run } = preparePrincipalRun(accounts, state, {
    guard,
    checkpoint,
    report,
    selection,
    scope: explicit,
  });
  if (!resumed)
    for (const account of accounts) {
      await guard();
      const key = account.id ? shortId(account.id) : 'default';
      const sourceId = account.id && account.isWorkspace ? `chatgpt-web-${key}` : 'chatgpt-web';
      await register(sourceId, 'chatgpt-web');
      const previous = state?.accounts?.[key];
      // Short source labels are not principal identities. Legacy/unknown account
      // state is replayed conservatively rather than borrowed by another account.
      const lastSyncMs =
        account.id && previous?.principalId === account.id ? (previous.lastSyncMs ?? null) : null;
      const since = lastSyncMs ? lastSyncMs - OVERLAP_MS : null;
      const ids = [];
      const candidates = new Map();
      const blocked = new Set();
      const projectGizmoIds = new Set();
      let catalogAvailable = false;
      let projectMembershipAvailable = false;
      let failures = discovery.incomplete ? 1 : 0;
      const add = (item, context = {}) => {
        if (typeof item?.id !== 'string' || !item.id)
          throw new Error('ChatGPT conversation identifier is unavailable');
        if (
          explicit &&
          (excluded(item) || explicitProjects(item).some((id) => !projectIds.has(id)))
        )
          blocked.add(item.id);
        const previous = candidates.get(item.id);
        candidates.set(item.id, {
          item: { ...previous?.item, ...item },
          context: { ...previous?.context, ...context },
        });
      };
      try {
        // Organized membership can change without changing message timestamps.
        // Scan list metadata completely in selective mode, before fetching text.
        const selective = policy.projects || policy.pinned || policy.agents;
        if (!explicit || directIds.size)
          for await (const item of listUpdatedConversations(
            token,
            account.id,
            explicit || selective ? null : since,
            pager.read,
          ))
            if (!explicit || directIds.has(item.id)) add(item, { recents: true });
      } catch (error) {
        if (error === pager.pause) return pager.continuation();
        if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable')
          throw error;
        failures++;
        warnings.add(
          'ChatGPT conversation discovery is incomplete; unavailable entries were not captured.',
        );
        log(`chatgpt: conversation discovery unavailable: ${error.message}`);
      }
      if (explicit ? projectIds.size : policy.projects || policy.agents) {
        let projects = [];
        try {
          projects = await listProjectGizmos(token, account.id, pager.read);
          if (explicit) {
            const unavailable = new Set(projects.filter(excluded).map((project) => project.id));
            projects = projects.filter((project) => !unavailable.has(project.id));
          }
          for (const project of projects) projectGizmoIds.add(project.id);
          catalogAvailable = true;
        } catch (error) {
          if (error === pager.pause) return pager.continuation();
          if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable')
            throw error;
          failures++;
          warnings.add(
            'ChatGPT project discovery is unavailable; only explicit verified category metadata can match.',
          );
          log(`chatgpt: project sidebar unavailable: ${error.message}`);
        }
        if (
          explicit &&
          catalogAvailable &&
          [...projectIds].some((id) => !projectGizmoIds.has(id))
        ) {
          failures++;
          warnings.add('Some saved ChatGPT projects are no longer available in this workspace.');
        }
        if ((explicit || policy.projects) && catalogAvailable) {
          let complete = true;
          for (const project of projects) {
            if (explicit && !projectIds.has(project.id)) continue;
            try {
              for await (const item of listProjectConversations(
                token,
                account.id,
                project.id,
                pager.read,
              ))
                add(item, { projectId: project.id });
            } catch (error) {
              if (error === pager.pause) return pager.continuation();
              if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable')
                throw error;
              complete = false;
              failures++;
              warnings.add(
                'ChatGPT project conversation discovery is incomplete; unavailable pages were not captured.',
              );
              log(`chatgpt: project conversations unavailable: ${error.message}`);
            }
          }
          projectMembershipAvailable = complete;
        }
      }

      for (const { item, context } of candidates.values()) {
        run.detected++;
        const decision = selectConversation(item, policy, {
          recents: false,
          ...context,
          projectGizmoIds,
          projectMembershipAvailable,
          gizmoClassificationAvailable: Boolean(explicit) || catalogAvailable || !policy.agents,
        });
        if (explicit)
          decision.selected =
            !blocked.has(item.id) &&
            (projectIds.has(context.projectId) ||
              (directIds.has(item.id) &&
                decision.categories.length === 1 &&
                decision.categories[0] === 'recents'));
        recordSelection(selection, decision);
        if (
          !explicit &&
          !policy.recents &&
          !decision.selected &&
          decision.unavailable.some((category) => policy[category])
        ) {
          missingMetadata++;
          for (const category of decision.unavailable.filter((category) => policy[category]))
            missingTypes.add(category);
        }
        if (!decision.selected) {
          continue;
        }
        ids.push(item.id);
      }
      run.groups.push({
        principalId: account.id,
        key,
        name: account.name,
        adapter: 'chatgpt-web',
        sourceId,
        ids,
        failed: [],
        lastSyncMs,
        discoveryFailed: failures > 0,
      });
    }
  if (missingMetadata)
    warnings.add(
      `ChatGPT category metadata unavailable for ${missingMetadata} conversation(s) (${[...missingTypes].join(', ')}); those unverified chats were skipped.`,
    );
  if (!resumed) run.warnings = [...warnings];
  for (const group of run.groups)
    newState.accounts[group.key] =
      state.accounts?.[group.key]?.principalId === group.principalId
        ? state.accounts[group.key]
        : { principalId: group.principalId, lastSyncMs: group.lastSyncMs };
  const unverifiedPrincipal = 'unverified-' + crypto.randomUUID();
  const read = async (principalId, id) => {
    const detail = await fetchJson(`${BASE}/backend-api/conversation/${encodeURIComponent(id)}`, {
      headers: authHeaders(token, principalId),
    });
    if (detail?.id != null && detail.id !== id) throw new Error('Wrong conversation');
    if (explicit) {
      const memberships = explicitProjects(detail);
      if (excluded(detail) || memberships.some((pid) => !projectIds.has(pid))) return null;
      // Recents can move after discovery or while a detail queue is paused.
      // An explicit chat ID is not permission to capture a different project.
      if (directIds.has(id)) {
        if (
          !memberships.length &&
          selectConversation(detail, policy).categories.some((k) => k !== 'recents')
        )
          return null;
      }
    }
    return toParsedConversation(detail, id, principalId ?? unverifiedPrincipal);
  };
  return drainSelected({
    run,
    state: newState,
    field: 'accounts',
    provider: 'ChatGPT',
    read,
    push,
    report,
    checkpoint,
    canResume,
    guard,
  });
}
