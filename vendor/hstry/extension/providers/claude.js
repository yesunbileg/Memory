import { listPopupCatalog, syncPopupScope, nativeId, unavailable } from './popup-catalog.js';
import { drainSelected, preparePrincipalRun } from './queue.js';
// Claude provider: syncs conversations from claude.ai via its web API,
// authenticated with the browser session cookies (no bearer token needed).

import { fetchJson, shortId, textPart, thinkingPart, toMs } from '../lib/common.js';

import {
  normalizeCaptureTypes,
  selectConversation,
  createSelectionSummary,
  recordSelection,
} from './selection.js';

const MAX_DISCOVERY_PAGES = 200;
const BASE = 'https://claude.ai';
const PAGE_SIZE = 50;
const OVERLAP_MS = 5 * 60 * 1000;

async function listOrganizations() {
  const data = await fetchJson(`${BASE}/api/organizations`);
  const orgs = Array.isArray(data) ? data : (data?.organizations ?? data?.data);
  if (!Array.isArray(orgs)) {
    throw new Error('Claude returned an unreadable organizations response');
  }
  const available = orgs
    .map((org) => ({ id: org?.uuid ?? org?.id, name: org?.name ?? 'organization' }))
    .filter((org) => org.id);
  if (
    available.length !== orgs.length ||
    available.some((org) => typeof org.id !== 'string') ||
    available.length === 0
  ) {
    throw new Error('Claude returned no available organizations');
  }
  return available;
}

async function* listUpdatedConversations(orgId, sinceMs, readPage, v2 = false) {
  const seen = new Set();
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const offset = page * PAGE_SIZE;
    const data = await readPage(
      `${BASE}/api/organizations/${encodeURIComponent(orgId)}/${v2 ? 'chat_conversations_v2' : 'chat_conversations'}?limit=${PAGE_SIZE}&offset=${offset}&consistency=eventual`,
    );
    const items = Array.isArray(data)
      ? data
      : (data?.data ?? data?.items ?? data?.chat_conversations);
    if (!Array.isArray(items)) throw new Error('Claude conversation list shape is unavailable');
    if (items.length && items.every((item) => seen.has(item?.uuid)))
      throw new Error('Claude conversation pagination did not advance');
    for (const item of items) seen.add(item?.uuid);
    for (const item of items) {
      const updatedMs = toMs(item.updated_at);
      if (sinceMs && updatedMs && updatedMs <= sinceMs) return;
      yield item;
    }
    if (
      items.length < PAGE_SIZE &&
      !Array.isArray(data) &&
      (data.has_more === true ||
        data.next_cursor != null ||
        data.next_offset != null ||
        (Number.isFinite(data.total) && offset + items.length < data.total))
    )
      throw new Error('Incomplete pagination');
    if (items.length < PAGE_SIZE) return;
  }
  throw new Error('Claude conversation pagination reached the safety limit');
}

/** Known Claude project collection endpoints; evidence is in donor-audit.md. */
async function listProjects(orgId, readPage) {
  const data = await readPage(`${BASE}/api/organizations/${encodeURIComponent(orgId)}/projects`);
  // The verified project catalog response is one complete array, not a guessed
  // cursor API. A changed/paginated envelope must be reported as unavailable.
  if (!Array.isArray(data)) throw new Error('Claude project catalog shape is unavailable');
  for (const project of data) {
    if (typeof project?.uuid !== 'string' || !project.uuid)
      throw new Error('Claude project identifier is unavailable');
  }
  return data;
}

async function* listProjectConversations(orgId, projectId, readPage) {
  const seen = new Set();
  for (let page = 0; page < MAX_DISCOVERY_PAGES; page++) {
    const offset = page * PAGE_SIZE;
    const url = new URL(
      `${BASE}/api/organizations/${encodeURIComponent(orgId)}/projects/${encodeURIComponent(projectId)}/conversations_v2`,
    );
    url.searchParams.set('limit', String(PAGE_SIZE));
    url.searchParams.set('offset', String(offset));
    const data = await readPage(url.href);
    const items = Array.isArray(data) ? data : data?.data;
    if (!Array.isArray(items)) throw new Error('Claude project conversation shape is unavailable');
    if (items.length && items.every((item) => seen.has(item?.uuid)))
      throw new Error('Claude project pagination did not advance');
    for (const item of items) seen.add(item?.uuid);
    for (const item of items) yield item;
    if (
      items.length < PAGE_SIZE &&
      !Array.isArray(data) &&
      (data.has_more === true ||
        data.next_cursor != null ||
        data.next_offset != null ||
        (Number.isFinite(data.total) && offset + items.length < data.total))
    )
      throw new Error('Incomplete pagination');
    if (items.length < PAGE_SIZE) return;
  }
  throw new Error('Claude project conversation pagination reached the safety limit');
}

function extractMessage(msg, index) {
  const sender = msg?.sender;
  const role = sender === 'human' ? 'user' : sender === 'assistant' ? 'assistant' : null;
  if (!role || msg?.visibility === 'hidden' || msg?.is_hidden) return null;
  if (
    (msg.content != null && !Array.isArray(msg.content)) ||
    (msg.content == null && typeof msg.text !== 'string')
  )
    throw new Error('Claude message content shape is unavailable');
  const parts = [];
  const texts = [];
  const blocks = Array.isArray(msg.content) ? msg.content : [];
  for (const block of blocks) {
    if (block?.visibility === 'hidden') continue;
    if (
      !block ||
      typeof block.type !== 'string' ||
      (block.type === 'text' && typeof block.text !== 'string') ||
      ![
        'text',
        'thinking',
        'tool_use',
        'tool_result',
        'redacted_thinking',
        'image',
        'document',
      ].includes(block.type)
    )
      throw new Error('Claude visible content format is unavailable');
    if (block.type === 'text' && typeof block.text === 'string' && block.text) {
      texts.push(block.text);
      parts.push(textPart(block.text));
    } else if (
      block.type === 'thinking' &&
      block.visibility === 'ui' &&
      typeof block.summary === 'string' &&
      block.summary
    ) {
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
    role,
    content,
    createdAt: toMs(msg.created_at),
    model: msg.model ?? null,
    parts,
    metadata: { id: msg.uuid ?? msg.id ?? `record:${index + 1}` },
  };
}

function toParsedConversation(detail, orgId) {
  if (
    typeof detail?.uuid !== 'string' ||
    !detail.uuid ||
    !Array.isArray(detail.chat_messages) ||
    detail.chat_messages.some(
      (message) =>
        !message ||
        typeof message !== 'object' ||
        Array.isArray(message) ||
        typeof message.sender !== 'string',
    )
  )
    throw new Error('Claude conversation detail shape is unavailable');
  const messages = detail.chat_messages.map(extractMessage).filter(Boolean);
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

async function signedInAccount() {
  const account = await fetchJson(`${BASE}/api/account`, { cache: 'no-store' });
  if (!nativeId(account?.uuid) || !Array.isArray(account.memberships))
    throw new Error('Current Claude account is not verified.');
  const allowed = new Set(
    account.memberships
      .filter((m) => !unavailable(m) && !unavailable(m?.organization))
      .map((m) => m?.organization?.uuid)
      .filter(nativeId),
  );
  return { userId: account.uuid, allowed };
}
async function popupPrincipals() {
  const { userId, allowed } = await signedInAccount();
  const orgs = await listOrganizations();
  const result = orgs
    .filter((o) => allowed.has(o.id))
    .map((o) => ({
      id: o.id,
      principalId: JSON.stringify([userId, o.id]),
      title: o.name,
      sourceId: orgs.length > 1 ? `claude-web-${shortId(o.id)}` : 'claude-web',
    }));
  if (!result.length) throw new Error('Current Claude organization is not verified.');
  return result;
}
const popup = {
  name: 'Claude',
  field: 'orgs',
  adapter: 'claude-web',
  principals: popupPrincipals,
  id: (item) => item?.uuid,
  projects: (p, read) => listProjects(p.id, read),
  chats: (p, read) => listUpdatedConversations(p.id, null, read, true),
  members: (p, id, read) => listProjectConversations(p.id, id, read),
  owns: async (p) => {
    const { userId, allowed } = await signedInAccount();
    return JSON.stringify([userId, p.id]) === p.principalId && allowed.has(p.id);
  },
  detail: (p, id) =>
    fetchJson(
      `${BASE}/api/organizations/${encodeURIComponent(p.id)}/chat_conversations/${encodeURIComponent(id)}?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual`,
    ),
  parse: (detail, id, p) => toParsedConversation(detail, p.id),
};
export const listClaudeCatalog = () => listPopupCatalog(popup);
export const claudePrincipalIds = async () => (await popupPrincipals()).map((p) => p.principalId);

export async function syncClaude({
  state = {},
  captureTypes,
  selectedScope,
  push,
  register = async () => {},
  log = () => {},
  checkpoint = async () => {},
  guard = async () => {},
  report = async () => {},
}) {
  if (selectedScope !== undefined)
    return syncPopupScope(popup, {
      selectedScope,
      state,
      push,
      register,
      guard,
      report,
      checkpoint,
    });
  const policy = normalizeCaptureTypes(captureTypes);
  const selection = createSelectionSummary();
  if (!Object.values(policy).some(Boolean)) {
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
  const orgs = await listOrganizations();
  const newState = { ...state, orgs: {} };
  const warnings = new Set();
  const missingTypes = new Set();
  let missingMetadata = 0;
  delete newState.discovery;
  const { pager, canResume, resumed, run } = preparePrincipalRun(orgs, state, {
    guard,
    checkpoint,
    report,
    selection,
  });
  if (!resumed)
    for (const org of orgs) {
      await guard();
      const key = shortId(org.id);
      const sourceId = orgs.length > 1 ? `claude-web-${key}` : 'claude-web';
      await register(sourceId, 'claude-web');
      const previous = state?.orgs?.[key];
      const lastSyncMs = previous?.principalId === org.id ? (previous.lastSyncMs ?? null) : null;
      const since = lastSyncMs ? lastSyncMs - OVERLAP_MS : null;
      const ids = [];
      const candidates = new Map();
      let projectMembershipAvailable = false;
      let failures = 0;
      const add = (item, context = {}) => {
        if (typeof item?.uuid !== 'string' || !item.uuid)
          throw new Error('Claude conversation identifier is unavailable');
        const previous = candidates.get(item.uuid);
        candidates.set(item.uuid, {
          item: { ...previous?.item, ...item },
          context: { ...previous?.context, ...context },
        });
      };
      try {
        const selective = policy.projects || policy.pinned || policy.agents;
        for await (const item of listUpdatedConversations(
          org.id,
          selective ? null : since,
          pager.read,
        ))
          add(item, { recents: true });
      } catch (error) {
        if (error === pager.pause) return pager.continuation();
        if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable')
          throw error;
        failures++;
        warnings.add(
          'Claude conversation discovery is incomplete; unavailable entries were not captured.',
        );
        log(`claude: skipping organization ${key} conversation list: ${error.message}`);
      }
      if (policy.projects) {
        try {
          const projects = await listProjects(org.id, pager.read);
          let complete = true;
          for (const project of projects) {
            try {
              for await (const item of listProjectConversations(org.id, project.uuid, pager.read))
                add(item, { projectId: project.uuid });
            } catch (error) {
              if (error === pager.pause) return pager.continuation();
              if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable')
                throw error;
              complete = false;
              failures++;
              warnings.add(
                'Claude project conversation discovery is incomplete; unavailable pages were not captured.',
              );
              log(`claude: project conversations unavailable: ${error.message}`);
            }
          }
          projectMembershipAvailable = complete;
        } catch (error) {
          if (error === pager.pause) return pager.continuation();
          if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable')
            throw error;
          failures++;
          warnings.add(
            'Claude project discovery is unavailable; only explicit verified project metadata can match.',
          );
          log(`claude: project catalog unavailable: ${error.message}`);
        }
      }

      for (const { item, context } of candidates.values()) {
        run.detected++;
        const decision = selectConversation(item, policy, {
          recents: false,
          ...context,
          projectMembershipAvailable,
        });
        recordSelection(selection, decision);
        if (
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
        ids.push(item.uuid);
      }
      run.groups.push({
        principalId: org.id,
        key,
        name: org.name,
        adapter: 'claude-web',
        sourceId,
        ids,
        failed: [],
        lastSyncMs,
        discoveryFailed: failures > 0,
      });
    }
  if (missingMetadata)
    warnings.add(
      `Claude category metadata unavailable for ${missingMetadata} conversation(s) (${[...missingTypes].join(', ')}); those unverified chats were skipped.`,
    );
  if (!resumed) run.warnings = [...warnings];
  for (const group of run.groups)
    newState.orgs[group.key] =
      state.orgs?.[group.key]?.principalId === group.principalId
        ? state.orgs[group.key]
        : { principalId: group.principalId, lastSyncMs: group.lastSyncMs };
  const read = async (principalId, id) => {
    const detail = await fetchJson(
      `${BASE}/api/organizations/${encodeURIComponent(principalId)}/chat_conversations/${encodeURIComponent(id)}?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual`,
    );
    if (detail?.uuid !== id) throw new Error('Wrong conversation');
    return toParsedConversation(detail, principalId);
  };
  return drainSelected({
    run,
    state: newState,
    field: 'orgs',
    provider: 'Claude',
    read,
    push,
    report,
    checkpoint,
    canResume,
    guard,
  });
}
