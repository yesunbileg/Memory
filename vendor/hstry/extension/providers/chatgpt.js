// ChatGPT provider: syncs conversations (personal + Teams workspaces) via the
// chatgpt.com backend API, authenticated with the browser session cookies.

import {
  NotLoggedInError,
  fetchJson,
  shortId,
  sleep,
  textPart,
  thinkingPart,
  toMs,
} from '../lib/common.js';

import { normalizeCaptureTypes, selectConversation, createSelectionSummary, recordSelection } from './selection.js';

const MAX_DISCOVERY_PAGES = 200;
const BASE = 'https://chatgpt.com';
const PAGE_SIZE = 50;
// Refetch a little history on every run so near-simultaneous edits are not
// missed between polls.
const OVERLAP_MS = 5 * 60 * 1000;
// Pace detail requests to stay under ChatGPT's rate limit. Backoff in
// fetchJson handles bursts, but pacing avoids tripping 429 in the first place.
const THROTTLE_MS = 400;

async function getAccessToken() {
  const data = await fetchJson(`${BASE}/api/auth/session`);
  if (!data?.accessToken) throw new NotLoggedInError('chatgpt.com');
  return data.accessToken;
}

function authHeaders(token, accountId) {
  const headers = { Authorization: `Bearer ${token}` };
  if (accountId) headers['chatgpt-account-id'] = accountId;
  return headers;
}

/** Enumerate accounts (personal + Teams workspaces). Falls back to the
 * default account when the accounts endpoint is unavailable. */
async function listAccounts(token) {
  try {
    const data = await fetchJson(`${BASE}/backend-api/accounts/check/v4-2023-04-27`, {
      headers: authHeaders(token),
    });
    const accounts = [];
    for (const entry of Object.values(data?.accounts ?? {})) {
      const account = entry?.account ?? entry;
      const id = account?.account_id ?? account?.id;
      if (!id || account?.is_deactivated) continue;
      if (accounts.some(a => a.id === id)) continue;
      accounts.push({
        id,
        name: account?.organization_name || account?.name || account?.plan_type || 'personal',
        isWorkspace: (account?.structure ?? account?.plan_type) === 'workspace',
      });
    }
    if (accounts.length > 0) return accounts;
  } catch {
    // Endpoint shape changed or unavailable: sync the default account only.
  }
  return [{ id: null, name: 'default', isWorkspace: false }];
}

async function* listUpdatedConversations(token, accountId, sinceMs) {
  const seen = new Set();
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const offset = page * PAGE_SIZE;
    const data = await fetchJson(
      `${BASE}/backend-api/conversations?offset=${offset}&limit=${PAGE_SIZE}&order=updated`,
      { headers: authHeaders(token, accountId) }
    );
    if (!Array.isArray(data?.items)) throw new Error('ChatGPT conversation list shape is unavailable');
    const items = data.items;
    if (items.length && items.every(item => seen.has(item?.id))) throw new Error('Chatgpt conversation pagination did not advance');
    for (const item of items) seen.add(item?.id);
    for (const item of items) {
      const updatedMs = toMs(item.update_time);
      if (sinceMs && updatedMs && updatedMs <= sinceMs) return;
      yield item;
    }
    if (items.length < PAGE_SIZE || (Number.isFinite(data.total) && offset + PAGE_SIZE >= data.total)) return;
  }
  throw new Error('ChatGPT conversation pagination reached the safety limit');
}

/** Verified community-client endpoint: see donor-audit.md, no endpoint probing. */
async function listProjectGizmos(token, accountId) {
  const projects = [];
  const cursors = new Set();
  let cursor = null;
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const url = new URL(`${BASE}/backend-api/gizmos/snorlax/sidebar`);
    url.searchParams.set('conversations_per_gizmo', '0');
    if (cursor !== null) url.searchParams.set('cursor', String(cursor));
    const data = await fetchJson(url.href, { headers: authHeaders(token, accountId) });
    if (!Array.isArray(data?.items)) throw new Error('ChatGPT project sidebar shape is unavailable');
    for (const entry of data.items) {
      const project = entry?.gizmo?.gizmo;
      if (typeof project?.id !== 'string' || !project.id) throw new Error('ChatGPT project identifier is unavailable');
      projects.push(project);
    }
    const next = data.cursor ?? null;
    if (next === null) return projects;
    if (!['string', 'number'].includes(typeof next) || cursors.has(String(next))) throw new Error('ChatGPT project sidebar cursor is invalid');
    cursors.add(String(next));
    cursor = next;
  }
  throw new Error('ChatGPT project sidebar pagination reached the safety limit');
}

async function* listProjectConversations(token, accountId, projectId) {
  const cursors = new Set();
  let cursor = 0;
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const url = new URL(`${BASE}/backend-api/gizmos/${encodeURIComponent(projectId)}/conversations`);
    url.searchParams.set('limit', String(PAGE_SIZE));
    url.searchParams.set('cursor', String(cursor));
    const data = await fetchJson(url.href, { headers: authHeaders(token, accountId) });
    if (!Array.isArray(data?.items)) throw new Error('ChatGPT project conversation shape is unavailable');
    for (const item of data.items) yield item;
    const next = data.cursor ?? null;
    if (next === null) return;
    if (!['string', 'number'].includes(typeof next) || cursors.has(String(next))) throw new Error('ChatGPT project conversation cursor is invalid');
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
    .filter(entry => {
      if (seenIds.has(entry.id)) return false;
      seenIds.add(entry.id);
      return true;
    });

  // A topological sort keeps parents before children when ChatGPT assigns the
  // same timestamp to several nodes. The chronological/id ordering of the
  // ready queue makes the result deterministic across repeated syncs.
  const byId = new Map(entries.map(entry => [entry.id, entry]));
  const children = new Map();
  const indegree = new Map(entries.map(entry => [entry.id, 0]));
  for (const entry of entries) {
    const parentKey = entry.node?.parent;
    const parentId = parentKey ? String(mapping[parentKey]?.id ?? parentKey) : null;
    if (!parentId || parentId === entry.id || !byId.has(parentId)) continue;
    indegree.set(entry.id, 1);
    const siblings = children.get(parentId) ?? [];
    siblings.push(entry.id);
    children.set(parentId, siblings);
  }

  const ready = entries.filter(entry => indegree.get(entry.id) === 0).sort(compareEntries);
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
    const orderedIds = new Set(ordered.map(entry => entry.id));
    ordered.push(...entries.filter(entry => !orderedIds.has(entry.id)).sort(compareEntries));
  }

  return ordered.filter(entry => entry.node?.message).map(entry => entry.node);
}

// Only documented visible text fields are imported. Arbitrary metadata may
// contain private prompts, tool arguments, or internal reasoning.
function extractMessage(node, index) {
  const msg = node?.message;
  if (!msg) return null;
  const role = msg.author?.role;
  if (role !== 'user' && role !== 'assistant') return null;
  if (msg.metadata?.is_visually_hidden_from_conversation || msg.metadata?.visibility === 'hidden') return null;
  const channel = msg.channel ?? msg.metadata?.channel;
  if (channel && !['final', 'commentary'].includes(channel)) return null;
  if (msg.recipient && msg.recipient !== 'all') return null;
  const parts = [];
  const texts = [];
  const content = msg.content ?? {};
  const addText = text => {
    if (typeof text !== 'string' || text.length === 0) return;
    texts.push(text);
    parts.push(textPart(text));
  };
  if (content.content_type === 'text' || content.content_type === 'multimodal_text') {
    for (const part of content.parts ?? []) {
      if (typeof part === 'string') addText(part);
      else if (part?.content_type === 'text' && typeof part.text === 'string') addText(part.text);
    }
  } else if (content.content_type === 'code' && typeof content.text === 'string') {
    addText(content.text);
  } else if (content.content_type === 'thoughts' && content.visibility === 'ui') {
    for (const thought of content.thoughts ?? []) {
      const text = thought?.summary;
      if (typeof text === 'string' && text) parts.push({ ...thinkingPart(text), visibility: 'ui' });
    }
  }
  const contentStr = texts.join('');
  if (!contentStr && parts.length === 0) return null;
  return {
    role, content: contentStr, createdAt: toMs(msg.create_time),
    model: msg.metadata?.model_slug ?? null, parts,
    metadata: { id: msg.id ?? node.id ?? `record:${index + 1}`, ...(channel === 'commentary' ? { kind: 'progress' } : {}) },
  };
}

function toParsedConversation(detail, conversationId, accountId) {
  const messages = linearize(detail)
    .map(extractMessage)
    .filter(Boolean);
  if (messages.length === 0) return null;

  const model = [...messages].reverse().find(m => m.model)?.model ?? null;

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
    },
  };
}

export async function syncChatGPT({ state = {}, captureTypes, push, register = async () => {}, log = () => {}, report = async () => {} }) {
  const policy = normalizeCaptureTypes(captureTypes);
  const selection = createSelectionSummary();
  if (!Object.values(policy).some(Boolean)) {
    await report({ phase: 'complete', detected: 0, processed: 0, selection, selectionWarning: null });
    return { state, conversations: 0, selection, selectionWarning: null };
  }
  await report({ phase: 'discovering', selection });
  const token = await getAccessToken();
  const accounts = await listAccounts(token);
  const newState = { ...state, accounts: {} };
  const warnings = new Set();
  const missingTypes = new Set();
  let missingMetadata = 0;
  let total = 0;
  let detected = 0;
  let processed = 0;

  for (const account of accounts) {
    const key = account.id ? shortId(account.id) : 'default';
    const sourceId = account.id && account.isWorkspace ? `chatgpt-web-${key}` : 'chatgpt-web';
    await register(sourceId, 'chatgpt-web');
    const lastSyncMs = state?.accounts?.[key]?.lastSyncMs ?? null;
    const since = lastSyncMs ? lastSyncMs - OVERLAP_MS : null;
    const runStartedMs = Date.now();
    const candidates = new Map();
    const projectGizmoIds = new Set();
    let catalogAvailable = false;
    let projectMembershipAvailable = false;
    let failures = 0;
    const add = (item, context = {}) => {
      if (typeof item?.id !== 'string' || !item.id) throw new Error('ChatGPT conversation identifier is unavailable');
      const previous = candidates.get(item.id);
      candidates.set(item.id, { item: { ...previous?.item, ...item }, context: { ...previous?.context, ...context } });
    };
    try {
      // Organized membership can change without changing message timestamps.
      // Scan list metadata completely in selective mode, before fetching text.
      const selective = policy.projects || policy.pinned || policy.agents;
      for await (const item of listUpdatedConversations(token, account.id, selective ? null : since)) add(item, { recents: true });
    } catch (error) {
      failures++;
      warnings.add('ChatGPT conversation discovery is incomplete; unavailable entries were not captured.');
      log(`chatgpt: conversation discovery unavailable: ${error.message}`);
    }
    if (policy.projects || policy.agents) {
      let projects = [];
      try {
        projects = await listProjectGizmos(token, account.id);
        for (const project of projects) projectGizmoIds.add(project.id);
        catalogAvailable = true;
      } catch (error) {
        failures++;
        warnings.add('ChatGPT project discovery is unavailable; only explicit verified category metadata can match.');
        log(`chatgpt: project sidebar unavailable: ${error.message}`);
      }
      if (policy.projects && catalogAvailable) {
        let complete = true;
        for (const project of projects) {
          try {
            for await (const item of listProjectConversations(token, account.id, project.id)) add(item, { projectId: project.id });
          } catch (error) {
            complete = false;
            failures++;
            warnings.add('ChatGPT project conversation discovery is incomplete; unavailable pages were not captured.');
            log(`chatgpt: project conversations unavailable: ${error.message}`);
          }
        }
        projectMembershipAvailable = complete;
      }
    }

    let batch = [];
    let first = true;
    for (const { item, context } of candidates.values()) {
      detected++;
      const decision = selectConversation(item, policy, {
        recents: false, ...context, projectGizmoIds, projectMembershipAvailable,
        gizmoClassificationAvailable: catalogAvailable || !policy.agents,
      });
      recordSelection(selection, decision);
      if (!policy.recents && !decision.selected && decision.unavailable.some(category => policy[category])) {
        missingMetadata++;
        for (const category of decision.unavailable.filter(category => policy[category])) missingTypes.add(category);
      }
      if (!decision.selected) {
        await report({ phase: 'importing', detected, processed, selection });
        continue;
      }
      if (!first) await sleep(THROTTLE_MS);
      first = false;
      try {
        const detail = await fetchJson(`${BASE}/backend-api/conversation/${encodeURIComponent(item.id)}`, {
          headers: authHeaders(token, account.id),
        });
        const conv = toParsedConversation(detail, item.id, account.id);
        if (conv) batch.push(conv);
      } catch (error) {
        failures++;
        warnings.add('Some selected ChatGPT conversations could not be read. Capture is incomplete; retry after checking your sign-in.');
        log(`chatgpt: skipping conversation ${item.id}: ${error.message}`);
      }
      processed++;
      await report({ phase: 'importing', detected, processed, selection });
      if (batch.length >= 10) {
        total += await push(sourceId, 'chatgpt-web', batch);
        batch = [];
      }
    }
    if (batch.length) total += await push(sourceId, 'chatgpt-web', batch);
    newState.accounts[key] = { lastSyncMs: failures > 0 ? lastSyncMs : runStartedMs, name: account.name };
  }
  if (missingMetadata) warnings.add(`ChatGPT category metadata unavailable for ${missingMetadata} conversation(s) (${[...missingTypes].join(', ')}); those unverified chats were skipped.`);
  const selectionWarning = [...warnings].join(' ') || null;
  await report({ phase: 'complete', detected, processed, selection, selectionWarning });
  return { state: newState, conversations: total, selection, selectionWarning };
}
