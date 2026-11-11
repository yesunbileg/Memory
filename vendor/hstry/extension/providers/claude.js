// Claude provider: syncs conversations from claude.ai via its web API,
// authenticated with the browser session cookies (no bearer token needed).

import {
  fetchJson,
  shortId,
  sleep,
  textPart,
  thinkingPart,
  toMs,
} from '../lib/common.js';

import { normalizeCaptureTypes, selectConversation, createSelectionSummary, recordSelection } from './selection.js';

const MAX_DISCOVERY_PAGES = 200;
const BASE = 'https://claude.ai';
const PAGE_SIZE = 50;
const OVERLAP_MS = 5 * 60 * 1000;
const THROTTLE_MS = 400;

async function listOrganizations() {
  const data = await fetchJson(`${BASE}/api/organizations`);
  const orgs = Array.isArray(data) ? data : (data?.organizations ?? data?.data);
  if (!Array.isArray(orgs)) {
    throw new Error('Claude returned an unreadable organizations response');
  }
  const available = orgs
    .map(org => ({ id: org?.uuid ?? org?.id, name: org?.name ?? 'organization' }))
    .filter(org => org.id);
  if (available.length === 0) {
    throw new Error('Claude returned no available organizations');
  }
  return available;
}

async function* listUpdatedConversations(orgId, sinceMs) {
  const seen = new Set();
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const offset = page * PAGE_SIZE;
    const data = await fetchJson(
      `${BASE}/api/organizations/${orgId}/chat_conversations?limit=${PAGE_SIZE}&offset=${offset}&consistency=eventual`
    );
    const items = Array.isArray(data) ? data : data?.data;
    if (!Array.isArray(items)) throw new Error('Claude conversation list shape is unavailable');
    if (items.length && items.every(item => seen.has(item?.uuid))) throw new Error('Claude conversation pagination did not advance');
    for (const item of items) seen.add(item?.uuid);
    for (const item of items) {
      const updatedMs = toMs(item.updated_at);
      if (sinceMs && updatedMs && updatedMs <= sinceMs) return;
      yield item;
    }
    if (items.length < PAGE_SIZE) return;
  }
  throw new Error('Claude conversation pagination reached the safety limit');
}

/** Known Claude project collection endpoints; evidence is in donor-audit.md. */
async function listProjects(orgId) {
  const data = await fetchJson(`${BASE}/api/organizations/${encodeURIComponent(orgId)}/projects`);
  // The verified project catalog response is one complete array, not a guessed
  // cursor API. A changed/paginated envelope must be reported as unavailable.
  if (!Array.isArray(data)) throw new Error('Claude project catalog shape is unavailable');
  for (const project of data) {
    if (typeof project?.uuid !== 'string' || !project.uuid) throw new Error('Claude project identifier is unavailable');
  }
  return data;
}

async function* listProjectConversations(orgId, projectId) {
  const seen = new Set();
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const offset = page * PAGE_SIZE;
    const url = new URL(`${BASE}/api/organizations/${encodeURIComponent(orgId)}/projects/${encodeURIComponent(projectId)}/conversations_v2`);
    url.searchParams.set('limit', String(PAGE_SIZE));
    url.searchParams.set('offset', String(offset));
    const data = await fetchJson(url.href);
    const items = Array.isArray(data) ? data : data?.data;
    if (!Array.isArray(items)) throw new Error('Claude project conversation shape is unavailable');
    if (items.length && items.every(item => seen.has(item?.uuid))) throw new Error('Claude project pagination did not advance');
    for (const item of items) seen.add(item?.uuid);
    for (const item of items) yield item;
    if (items.length < PAGE_SIZE) return;
  }
  throw new Error('Claude project conversation pagination reached the safety limit');
}

function extractMessage(msg, index) {
  const sender = msg?.sender;
  const role = sender === 'human' ? 'user' : sender === 'assistant' ? 'assistant' : null;
  if (!role || msg?.visibility === 'hidden' || msg?.is_hidden) return null;
  const parts = [];
  const texts = [];
  const blocks = Array.isArray(msg.content) ? msg.content : [];
  for (const block of blocks) {
    if (!block?.type || block.visibility === 'hidden') continue;
    if (block.type === 'text' && typeof block.text === 'string' && block.text) {
      texts.push(block.text);
      parts.push(textPart(block.text));
    } else if (block.type === 'thinking' && block.visibility === 'ui' && typeof block.summary === 'string' && block.summary) {
      parts.push({ ...thinkingPart(block.summary), visibility: 'ui' });
    }
  }
  let content = texts.join('');
  if (blocks.length === 0 && typeof msg.text === 'string') {
    content = msg.text;
    if (content) parts.push(textPart(content));
  }
  if (!content && parts.length === 0) return null;
  return {
    role, content, createdAt: toMs(msg.created_at), model: msg.model ?? null, parts,
    metadata: { id: msg.uuid ?? msg.id ?? `record:${index + 1}` },
  };
}

function toParsedConversation(detail, orgId) {
  const messages = (detail?.chat_messages ?? []).map(extractMessage).filter(Boolean);
  if (messages.length === 0) return null;

  return {
    externalId: detail.uuid,
    title: detail.name || null,
    createdAt: toMs(detail.created_at),
    updatedAt: toMs(detail.updated_at),
    model: detail.model ?? null,
    provider: 'anthropic',
    messages,
    metadata: {
      url: `${BASE}/chat/${detail.uuid}`,
      orgId,
    },
  };
}

export async function syncClaude({ state = {}, captureTypes, push, register = async () => {}, log = () => {}, report = async () => {} }) {
  const policy = normalizeCaptureTypes(captureTypes);
  const selection = createSelectionSummary();
  if (!Object.values(policy).some(Boolean)) {
    await report({ phase: 'complete', detected: 0, processed: 0, selection, selectionWarning: null });
    return { state, conversations: 0, selection, selectionWarning: null };
  }
  await report({ phase: 'discovering', selection });
  const orgs = await listOrganizations();
  const newState = { ...state, orgs: {} };
  const warnings = new Set();
  const missingTypes = new Set();
  let missingMetadata = 0;
  let total = 0;
  let detected = 0;
  let processed = 0;
  const multiOrg = orgs.length > 1;

  for (const org of orgs) {
    const key = shortId(org.id);
    const sourceId = multiOrg ? `claude-web-${key}` : 'claude-web';
    await register(sourceId, 'claude-web');
    const lastSyncMs = state?.orgs?.[key]?.lastSyncMs ?? null;
    const since = lastSyncMs ? lastSyncMs - OVERLAP_MS : null;
    const runStartedMs = Date.now();
    const candidates = new Map();
    let projectMembershipAvailable = false;
    let failures = 0;
    const add = (item, context = {}) => {
      if (typeof item?.uuid !== 'string' || !item.uuid) throw new Error('Claude conversation identifier is unavailable');
      const previous = candidates.get(item.uuid);
      candidates.set(item.uuid, { item: { ...previous?.item, ...item }, context: { ...previous?.context, ...context } });
    };
    try {
      const selective = policy.projects || policy.pinned || policy.agents;
      for await (const item of listUpdatedConversations(org.id, selective ? null : since)) add(item, { recents: true });
    } catch (error) {
      failures++;
      warnings.add('Claude conversation discovery is incomplete; unavailable entries were not captured.');
      log(`claude: skipping organization ${key} conversation list: ${error.message}`);
    }
    if (policy.projects) {
      try {
        const projects = await listProjects(org.id);
        let complete = true;
        for (const project of projects) {
          try {
            for await (const item of listProjectConversations(org.id, project.uuid)) add(item, { projectId: project.uuid });
          } catch (error) {
            complete = false;
            failures++;
            warnings.add('Claude project conversation discovery is incomplete; unavailable pages were not captured.');
            log(`claude: project conversations unavailable: ${error.message}`);
          }
        }
        projectMembershipAvailable = complete;
      } catch (error) {
        failures++;
        warnings.add('Claude project discovery is unavailable; only explicit verified project metadata can match.');
        log(`claude: project catalog unavailable: ${error.message}`);
      }
    }

    let batch = [];
    let first = true;
    for (const { item, context } of candidates.values()) {
      detected++;
      const decision = selectConversation(item, policy, { recents: false, ...context, projectMembershipAvailable });
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
        const detail = await fetchJson(
          `${BASE}/api/organizations/${encodeURIComponent(org.id)}/chat_conversations/${encodeURIComponent(item.uuid)}?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual`
        );
        const conv = toParsedConversation(detail, org.id);
        if (conv) batch.push(conv);
      } catch (error) {
        failures++;
        warnings.add('Some selected Claude conversations could not be read. Capture is incomplete; retry after checking your sign-in.');
        log(`claude: skipping conversation ${item.uuid}: ${error.message}`);
      }
      processed++;
      await report({ phase: 'importing', detected, processed, selection });
      if (batch.length >= 10) {
        total += await push(sourceId, 'claude-web', batch);
        batch = [];
      }
    }
    if (batch.length) total += await push(sourceId, 'claude-web', batch);
    newState.orgs[key] = { lastSyncMs: failures > 0 ? lastSyncMs : runStartedMs, name: org.name };
  }
  if (missingMetadata) warnings.add(`Claude category metadata unavailable for ${missingMetadata} conversation(s) (${[...missingTypes].join(', ')}); those unverified chats were skipped.`);
  const selectionWarning = [...warnings].join(' ') || null;
  await report({ phase: 'complete', detected, processed, selection, selectionWarning });
  return { state: newState, conversations: total, selection, selectionWarning };
}
