// Gemini web sync through the authenticated batchexecute RPCs used by the web UI.

import { NotLoggedInError, textPart, toMs } from '../lib/common.js';
import { fetchInTab } from '../lib/tab-fetch.js';

import {
  normalizeCaptureTypes,
  eligibleCapture,
  selectConversation,
  createSelectionSummary,
  recordSelection,
} from './selection.js';

const BASE = 'https://gemini.google.com';
const OVERLAP_MS = 5 * 60 * 1000;
const PAGE_SIZE = 20;
const SYNC_CHUNK_SIZE = 10;
const MAX_PAGES = 200;
const REQUEST_TIMEOUT_MS = 20_000;

function extractSession(html) {
  const read = (key) => html.match(new RegExp(`"${key}":"((?:\\\\.|[^"\\\\])*)"`))?.[1];
  const decode = (value) => (value ? JSON.parse(`"${value}"`) : '');
  // Read an account identity only from Google's own bootstrap assignment, never
  // from a chat title, arbitrary inline JSON, another account menu, or a URL slot.
  const bootstrap = html.match(
    /<script\b[^>]*>\s*(?:window\.)?WIZ_global_data\s*=\s*([\s\S]*?)\s*;?\s*<\/script>/i,
  );
  let gaia = null;
  if (bootstrap) {
    try {
      const data = JSON.parse(bootstrap[1]);
      if (
        typeof data.S06Grb === 'string' &&
        /^\d{21}$/.test(data.S06Grb) &&
        data.W3Yyqf === data.S06Grb &&
        typeof data.oPEP7c === 'string' &&
        /^[^\s@]+@[^\s@]+$/.test(data.oPEP7c)
      )
        gaia = data.S06Grb;
    } catch {
      /* Unsupported bootstrap: require continuity of the active session instead. */
    }
  }
  return {
    gaia,
    at: decode(read('SNlM0e')),
    sid: decode(read('FdrFJe')),
    bl: decode(read('cfb2h')),
  };
}

function parseRpcResponse(text, rpcId) {
  for (const line of text.split('\n')) {
    if (!line.startsWith('[[')) continue;
    try {
      const outer = JSON.parse(line);
      const entry = outer?.[0];
      if (entry?.[0] === 'wrb.fr' && entry?.[1] === rpcId && typeof entry?.[2] === 'string') {
        return JSON.parse(entry[2]);
      }
    } catch {
      // Streaming responses contain size lines and unrelated chunks.
    }
  }
  throw new Error(`Gemini returned an unreadable ${rpcId} response`);
}

async function providerFetch(context, url, init) {
  return context?.tabId != null ? fetchInTab(context, String(url), init) : fetch(url, init);
}

function requestTimeout(signal) {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

async function getSession(context, signal) {
  const tab = context?.tabId != null ? context : null;
  const slot = context?.slot == null ? null : String(context.slot);
  // An originating tab keeps both the boot page and every RPC inside the same
  // signed-in container. Legacy unit callers without a tab keep using /app and
  // verify the returned signed-in route instead.
  const bootPath = tab && slot !== null ? `/u/${slot}/app` : '/app';
  const res = await providerFetch(context, `${BASE}${bootPath}`, {
    credentials: 'include',
    cache: 'no-store',
    signal: requestTimeout(signal),
  });
  if (res.status === 401 || res.status === 403 || res.url.includes('accounts.google.com')) {
    throw new NotLoggedInError('gemini.google.com');
  }
  if (!res.ok) throw new Error(`GET ${BASE}${bootPath} -> ${res.status}`);
  const session = extractSession(await res.text());
  if (!session.at) throw new NotLoggedInError('gemini.google.com');
  const userIndex = res.url.match(/\/u\/(\d+)\//)?.[1] ?? null;
  // A foreground /u/N tab must not grant access to the default account's rows.
  // The slot is routing evidence, never the durable Google account identity.
  if (slot !== null && slot !== userIndex)
    throw Object.assign(new Error('The active Gemini account does not match this tab.'), {
      code: 'principal_changed',
    });
  return { ...session, userIndex };
}

async function sessionFingerprint(session) {
  // These existing RPC credentials identify continuity of this active session,
  // not a durable Google principal. Rotation intentionally forces a full replay.
  // The account slot only disambiguates routing and is never sufficient alone.
  if (!session.at || !session.sid) return null;
  const bytes = new TextEncoder().encode(
    JSON.stringify(['gemini-session-v1', session.at, session.sid, session.userIndex]),
  );
  return hashHex(bytes);
}

async function hashHex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function batchExecute(session, rpcId, arg, context, signal) {
  const reqId = Math.floor(Math.random() * 900000) + 100000;
  const accountPath = session.userIndex === null ? '' : `/u/${session.userIndex}`;
  const url = new URL(`${BASE}${accountPath}/_/BardChatUi/data/batchexecute`);
  url.searchParams.set('rpcids', rpcId);
  url.searchParams.set('source-path', '/app');
  url.searchParams.set('_reqid', String(reqId));
  url.searchParams.set('rt', 'c');
  if (session.bl) url.searchParams.set('bl', session.bl);
  if (session.sid) url.searchParams.set('f.sid', session.sid);

  const body = new URLSearchParams();
  body.set('f.req', JSON.stringify([[[rpcId, JSON.stringify(arg), null, 'generic']]]));
  body.set('at', session.at);
  const res = await providerFetch(context, url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
      'X-Same-Domain': '1',
    },
    body,
    signal: requestTimeout(signal),
  });
  if (res.status === 401 || res.status === 403) throw new NotLoggedInError('gemini.google.com');
  if (!res.ok) throw new Error(`Gemini ${rpcId} -> ${res.status}`);
  return parseRpcResponse(await res.text(), rpcId);
}

async function captureBinding(session, preferAccount = true) {
  if (preferAccount && session.gaia) {
    const hash = await hashHex(new TextEncoder().encode('gemini-gaia-v1:' + session.gaia));
    return { accountId: 'gemini-account-' + hash, fingerprint: 'account:' + hash };
  }
  const fingerprint = await sessionFingerprint(session);
  return { accountId: fingerprint && 'gemini-session-' + fingerprint, fingerprint };
}

export async function geminiPrincipalIds(context, { signal } = {}) {
  const { accountId: id } = await captureBinding(await getSession(context, signal));
  return id ? [id] : [];
}

// Only the list RPC is allowed here. Older responses expose a cursor; newer
// ones may cap a plain [limit] window without returning any continuation.
async function catalogSummaries(session, context, guard = async () => {}, signal, selected) {
  const previous = selected?.previous;
  const rows = new Map((previous?.summaries ?? []).map((row) => [row.id, row])),
    seen = new Set(previous?.seen);
  const limits = [200, 1000, 5000];
  let stage = previous?.stage ?? 0,
    cursor = previous?.cursor ?? null;
  const start = previous?.pages ?? 0;
  for (let page = start; page < 25; page++) {
    await guard();
    if (selected && page - start === 4) {
      await selected.save({
        summaries: [...rows.values()],
        seen: [...seen],
        pages: page,
        stage,
        cursor,
      });
      return { queued: true };
    }
    try {
      const limit = limits[stage];
      const response = await batchExecute(
        session,
        'MaZiqc',
        cursor === null ? [limit] : [limit, cursor],
        context,
        signal,
      );
      if (!Array.isArray(response) || !Array.isArray(response[2])) break;
      for (const item of response[2]) {
        if (!Array.isArray(item) || typeof item[0] !== 'string') throw Error();
        const id = item[0].replace(/^c_/, '');
        if (!/^[a-zA-Z0-9_-]{1,256}$/.test(id)) throw Error();
        rows.set(
          id,
          selected && !selected.wanted.has(id)
            ? { id }
            : {
                id,
                rawId: item[0],
                title:
                  typeof item[1] === 'string' && item[1]
                    ? item[1].slice(0, 500)
                    : 'Untitled conversation',
                updatedAt: toMs(item[5]?.[0]),
              },
        );
        if (rows.size > 5000) throw Error();
      }
      const next = response[1] ?? null;
      if (next !== null) {
        const key = JSON.stringify(next);
        if (seen.has(key) || !response[2].length || rows.size >= 5000) break;
        seen.add(key);
        cursor = next;
      } else if (cursor === null && response[2].length >= limit) {
        if (stage + 1 === limits.length) break;
        stage++;
      } else {
        return { summaries: [...rows.values()], incomplete: false };
      }
    } catch {
      signal?.throwIfAborted();
      break;
    }
  }
  return { summaries: [...rows.values()].slice(0, 5000), incomplete: true };
}

export async function listGeminiCatalog(context) {
  const session = await getSession(context);
  const { accountId: id } = await captureBinding(session);
  if (!id) return { entries: [], incomplete: true };
  const { summaries, incomplete } = await catalogSummaries(session, context);
  // A sign-in change while the picker was loading must not label the rows with
  // a stale account. Save independently rechecks the same principal.
  if (!(await geminiPrincipalIds(context)).includes(id)) return { entries: [], incomplete: true };
  return {
    entries: [
      {
        principalId: id,
        title: id.startsWith('gemini-account-')
          ? 'Current Gemini account'
          : 'Current Gemini session',
        projects: [],
        conversations: summaries.map(({ id, title }) => ({ id, title })),
      },
    ],
    incomplete,
  };
}

async function listChats(session, sinceMs, previous, guard, save, context, signal) {
  const chats = new Map((previous?.summaries ?? []).map((c) => [c.rawId, c]));
  const cursors = new Set(previous?.cursors ?? []);
  let cursor = previous?.cursor ?? null;
  let budget = 4;
  try {
    for (let page = 0; page < MAX_PAGES; page++) {
      if (!budget--) return { queued: true };
      await guard();
      const arg = cursor === null ? [PAGE_SIZE] : [PAGE_SIZE, cursor];
      const data = await batchExecute(session, 'MaZiqc', arg, context, signal);
      if (!Array.isArray(data) || !Array.isArray(data[2]))
        throw new Error('Gemini conversation list shape is unavailable');
      const nextCursor = data[1] ?? null;
      const items = data[2];
      let reachedOld = false;
      for (const item of items) {
        if (!Array.isArray(item) || typeof item[0] !== 'string' || !item[0])
          throw new Error('Gemini conversation identifier is unavailable');
        const rawId = item[0];
        const updatedAt = toMs(item?.[5]?.[0]);
        if (sinceMs && updatedAt && updatedAt <= sinceMs) {
          reachedOld = true;
          continue;
        }
        chats.set(rawId, {
          rawId,
          id: rawId.replace(/^c_/, ''),
          title: String(item?.[1] ?? 'Untitled conversation'),
          updatedAt,
        });
      }
      if (reachedOld || nextCursor === null)
        return { summaries: [...chats.values()], incomplete: false };
      const key = JSON.stringify(nextCursor);
      if (cursors.has(key)) throw new Error('Gemini conversation pagination did not advance');
      cursors.add(key);
      cursor = nextCursor;
      if (cursors.size >= MAX_PAGES) break;
      try {
        await save({ summaries: [...chats.values()], cursor, cursors: [...cursors] });
      } catch (error) {
        throw Object.assign(new Error('Discovery state not saved'), {
          code: error?.code ?? 'storage_unavailable',
        });
      }
    }
  } catch (error) {
    if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable') throw error;
    // Keep verified earlier pages, but never represent an unknown response as empty success.
    return { summaries: [...chats.values()], incomplete: true };
  }
  return { summaries: [...chats.values()], incomplete: true };
}

function nativeTurnId(turn) {
  if (typeof turn?.[0] === 'string' && turn[0]) return turn[0];
  if (typeof turn?.[0]?.[0] === 'string' && turn[0][0]) return turn[0][0];
  return null;
}

function assistantText(turn) {
  const direct = turn?.[3]?.[0]?.[0]?.[1]?.[0];
  if (typeof direct === 'string') return direct;
  return '';
}

async function readChat(session, summary, guard, context, signal) {
  const turns = [];
  const turnIds = new Set();
  const pages = new Set();
  const cursors = new Set();
  let cursor = null;
  let incomplete = true;
  try {
    for (let page = 0; page < MAX_PAGES; page++) {
      await guard();
      const data = await batchExecute(
        session,
        'hNvQHb',
        [summary.rawId, 20, cursor, 1, [0], [4], null, 1],
        context,
        signal,
      );
      if (!Array.isArray(data) || data.length < 2 || !Array.isArray(data[0]))
        throw new Error('Gemini conversation detail shape is unavailable');
      const pageTurns = data[0];
      const pageKey = JSON.stringify(pageTurns);
      if (pageTurns.length && pages.has(pageKey))
        throw new Error('Gemini turn pagination did not advance');
      pages.add(pageKey);
      for (const turn of pageTurns) {
        const hasUser = typeof turn?.[2]?.[0]?.[0] === 'string';
        const hasAssistant = typeof turn?.[3]?.[0]?.[0]?.[1]?.[0] === 'string';
        if (
          !Array.isArray(turn) ||
          (!hasUser && !hasAssistant) ||
          (turn[2] != null && !hasUser) ||
          (turn[3] != null && !hasAssistant)
        )
          throw new Error('Gemini turn text shape is unavailable');
        const id = nativeTurnId(turn);
        if (id !== null && turnIds.has(id)) continue;
        if (id !== null) turnIds.add(id);
        turns.push(turn);
      }
      const nextCursor = data[1] ?? null;
      if (nextCursor === null) {
        incomplete = false;
        break;
      }
      const key = JSON.stringify(nextCursor);
      if (cursors.has(key)) throw new Error('Gemini turn pagination did not advance');
      cursors.add(key);
      cursor = nextCursor;
    }
  } catch (error) {
    signal?.throwIfAborted();
    if (error?.code === 'selection_changed' || error?.code === 'storage_unavailable') throw error;
    // The old completion watermark keeps unavailable history eligible.
  }
  // Gemini supplies newest-first pages without reliable per-turn timestamps.
  // Archiving a partial newest page would permanently place older unseen turns
  // after it on retry. Only complete details can establish archive order safely.
  if (incomplete) return { conversation: null, incomplete: true };
  turns.reverse();

  const messages = [];
  for (const [index, turn] of turns.entries()) {
    const turnId = nativeTurnId(turn) ?? `record:${index + 1}`;
    const user = turn?.[2]?.[0]?.[0];
    const assistant = assistantText(turn);
    if (typeof user === 'string' && user.length) {
      messages.push({
        role: 'user',
        content: user,
        createdAt: null,
        model: null,
        parts: [textPart(user)],
        metadata: { id: `${turnId}:user` },
      });
    }
    if (assistant.length) {
      messages.push({
        role: 'assistant',
        content: assistant,
        createdAt: null,
        model: null,
        parts: [textPart(assistant)],
        metadata: { id: `${turnId}:assistant` },
      });
    }
  }
  if (messages.length === 0) return { conversation: null, incomplete };
  return {
    incomplete,
    conversation: {
      externalId: summary.id,
      title: summary.title,
      createdAt: null,
      updatedAt: summary.updatedAt,
      model: null,
      provider: 'google',
      messages,
      metadata: { url: `${BASE}/app/${summary.id}` },
    },
  };
}

export async function syncGemini({
  state = {},
  selectedScope,
  captureTypes,
  tabContext,
  push,
  register = async () => {},
  log = () => {},
  report = async () => {},
  guard = async () => {},
  signal,
  checkpoint = async () => {},
}) {
  const policy = normalizeCaptureTypes(captureTypes);
  const selection = createSelectionSummary();
  const explicit = selectedScope !== undefined;
  const entries = Array.isArray(selectedScope?.entries) ? selectedScope.entries : [];
  if (
    explicit
      ? !entries.some((e) => Array.isArray(e?.conversationIds) && e.conversationIds.length)
      : !Object.values(policy).some(Boolean)
  ) {
    await report({
      phase: 'complete',
      detected: 0,
      processed: 0,
      selection,
      selectionWarning: null,
    });
    return { state, conversations: 0, selection, selectionWarning: null, hasMore: false };
  }
  let selectionWarning =
    !explicit && (policy.projects || policy.pinned || policy.agents)
      ? 'Gemini category metadata is unavailable in the verified private RPC schema; organized chats cannot be identified. Ordinary Recents must be explicitly enabled to capture listed chats.'
      : null;
  if (!explicit && !eligibleCapture('gemini', policy)) {
    await report({ phase: 'complete', processed: 0, detected: 0, selection, selectionWarning });
    return { state, conversations: 0, selection, selectionWarning, hasMore: false };
  }
  await report({ phase: 'discovering', selection, selectionWarning });
  const session = await getSession(tabContext, signal);
  const { fingerprint, accountId: boundId } = await captureBinding(session, explicit);
  const accountId = boundId || 'gemini-session-' + crypto.randomUUID();
  const wanted = explicit
    ? new Set(
        entries
          .filter((e) => fingerprint && e?.principalId === accountId)
          .flatMap((e) =>
            Array.isArray(e.conversationIds)
              ? e.conversationIds.filter((id) => typeof id === 'string')
              : [],
          ),
      )
    : null;
  if (explicit && !wanted.size) {
    const selectionWarning =
      'The saved list belongs to another or expired Gemini session. Refresh and review your choices.';
    await report({ phase: 'attention', processed: 0, detected: 0, selection, selectionWarning });
    return { state, conversations: 0, hasMore: false, selection, selectionWarning };
  }
  const scopeKey = explicit ? JSON.stringify([...wanted].sort()) : undefined;
  const scopeState = explicit ? { selectedIdsKey: scopeKey } : {};
  const matchingSession =
    fingerprint !== null &&
    state?.sessionFingerprint === fingerprint &&
    state.selectedIdsKey === scopeKey;
  const verifySession = async () => {
    await guard();
    if (explicit && !(await geminiPrincipalIds(tabContext, { signal })).includes(accountId))
      throw Object.assign(new Error('The Gemini session changed. Refresh your saved list.'), {
        code: tabContext ? 'principal_changed' : 'selection_changed',
      });
  };
  await register('gemini-web', 'gemini');
  const lastSyncMs = matchingSession ? (state?.lastSyncMs ?? null) : null;
  const since = lastSyncMs ? lastSyncMs - OVERLAP_MS : null;
  const pending = matchingSession ? state?.pending : null;
  if (!fingerprint)
    selectionWarning = [
      selectionWarning,
      'Gemini session identity is unavailable; previous checkpoints cannot be verified and history is replayed without saving a new checkpoint.',
    ]
      .filter(Boolean)
      .join(' ');
  const previousDiscovery = matchingSession ? state.discovery : null;
  const runStartedMs = pending?.runStartedMs ?? previousDiscovery?.runStartedMs ?? Date.now();
  let discoveryState = { lastSyncMs, sessionFingerprint: fingerprint, ...scopeState };
  const resuming = Array.isArray(pending?.summaries);
  const discovery = explicit
    ? await catalogSummaries(session, tabContext, guard, signal, {
        wanted,
        previous: previousDiscovery,
        save: async (progress) => {
          await verifySession();
          discoveryState = {
            ...discoveryState,
            ...(pending && { pending }),
            discovery: { ...progress, runStartedMs },
          };
          await guard();
          await checkpoint(discoveryState);
        },
      })
    : resuming
      ? { summaries: pending.summaries, incomplete: Boolean(pending.discoveryIncomplete) }
      : await listChats(
          session,
          since,
          previousDiscovery,
          guard,
          async (progress) => {
            if (!fingerprint) return;
            discoveryState = {
              lastSyncMs,
              sessionFingerprint: fingerprint,
              discovery: { ...progress, runStartedMs },
            };
            await guard();
            await checkpoint(discoveryState);
          },
          tabContext,
          signal,
        );
  if (discovery.queued) {
    if (!fingerprint)
      selectionWarning =
        'Session identity is unavailable. Gemini discovery is incomplete and cannot safely resume.';
    await report({ phase: fingerprint ? 'queued' : 'attention', selection, selectionWarning });
    return {
      state: discoveryState,
      conversations: 0,
      hasMore: Boolean(fingerprint),
      selection,
      selectionWarning,
    };
  }
  const startIndex =
    resuming && Number.isInteger(pending.nextIndex)
      ? Math.max(0, Math.min(pending.nextIndex, pending.summaries.length))
      : 0;
  let summaries = discovery.summaries;
  let retrySummaries = [...(pending?.retrySummaries ?? [])];
  let missing = false;
  if (explicit) {
    const listed = new Map(summaries.map((s) => [s.id, s]));
    if (resuming) {
      // A saved ID is not proof that its transcript is still available. Recheck the
      // unprocessed and retry queues against one fresh listing, in original queue order.
      const recheck = (queued) =>
        queued.flatMap(({ id }) => {
          if (!wanted.has(id)) return [];
          if (listed.has(id)) return [listed.get(id)];
          missing = true;
          return [];
        });
      summaries = [
        ...pending.summaries.slice(0, startIndex),
        ...recheck(pending.summaries.slice(startIndex)),
      ];
      retrySummaries = recheck(retrySummaries);
      discovery.incomplete ||= Boolean(pending.discoveryIncomplete);
    } else {
      missing = [...wanted].some((id) => !listed.has(id));
      summaries = summaries.filter(
        (s) => wanted.has(s.id) && (!since || !s.updatedAt || s.updatedAt > since),
      );
    }
  }
  if (missing)
    selectionWarning =
      'Some saved Gemini chats are unavailable. Refresh the list to review your choices.';
  if (discovery.incomplete)
    selectionWarning = [
      selectionWarning,
      'Gemini conversation discovery is incomplete; retry to capture unavailable history.',
    ]
      .filter(Boolean)
      .join(' ');
  // The tuple schema has no verified project, pin or Gem membership fields.
  // Do not interpret opaque booleans/indices or a suggestive title as consent.
  for (const _summary of summaries)
    recordSelection(
      selection,
      explicit
        ? { selected: true, categories: [], unknown: false }
        : selectConversation(null, policy, { recents: true }),
    );
  const endIndex = Math.min(startIndex + SYNC_CHUNK_SIZE, summaries.length);
  await report({
    phase: 'importing',
    detected: summaries.length,
    processed: startIndex,
    selection,
    selectionWarning,
  });
  let total = 0;
  let failures = discovery.incomplete || missing ? 1 : 0;
  let processed = startIndex;
  const batch = [];
  for (const summary of summaries.slice(startIndex, endIndex)) {
    await guard();
    try {
      const detail = await readChat(
        session,
        summary,
        explicit ? verifySession : guard,
        tabContext,
        signal,
      );
      if (detail.incomplete) {
        failures++;
        retrySummaries.push(summary);
      }
      if (detail.conversation) {
        detail.conversation.metadata.accountId = accountId;
        batch.push(detail.conversation);
      }
    } catch (error) {
      signal?.throwIfAborted();
      if (
        [
          'selection_changed',
          'storage_unavailable',
          'principal_changed',
          'provider_tab_unavailable',
        ].includes(error?.code)
      )
        throw error;
      failures++;
      retrySummaries.push(summary);
      log(`gemini: skipping conversation ${summary.id}: ${error.message}`);
    }
    processed++;
    await report({ processed });
  }
  if (explicit) await verifySession();
  if (batch.length) {
    const accepted = await push('gemini-web', 'gemini', batch);
    total += accepted;
    if (accepted !== batch.length)
      return {
        state,
        conversations: total,
        hasMore: false,
        selection,
        selectionWarning: 'Local archive receipt incomplete. Retry capture.',
      };
  }
  const remaining = endIndex < summaries.length;
  const hasMore = remaining && fingerprint !== null;
  const hadFailures = Boolean(pending?.hadFailures) || failures > 0 || (remaining && !fingerprint);
  if (remaining && !fingerprint)
    selectionWarning +=
      ' Capture is incomplete and automatic continuation is paused until session identity is available.';
  if (hadFailures)
    selectionWarning = [
      selectionWarning,
      'Some selected Gemini conversations could not be read. Capture is incomplete; retry after checking your sign-in.',
    ]
      .filter(Boolean)
      .join(' ');
  await guard();
  const finalState = hasMore
    ? {
        lastSyncMs,
        sessionFingerprint: fingerprint,
        ...scopeState,
        pending: {
          runStartedMs,
          summaries,
          nextIndex: endIndex,
          retrySummaries,
          hadFailures,
          discoveryIncomplete: discovery.incomplete,
        },
      }
    : {
        lastSyncMs: hadFailures || !fingerprint ? lastSyncMs : runStartedMs,
        sessionFingerprint: fingerprint,
        ...scopeState,
      };
  if (!hasMore && fingerprint && retrySummaries.length && !discovery.incomplete)
    finalState.pending = {
      runStartedMs,
      summaries: retrySummaries,
      nextIndex: 0,
      hadFailures: false,
      discoveryIncomplete: false,
    };
  await checkpoint(finalState);
  await report({ phase: hasMore ? 'queued' : 'complete', processed, selection, selectionWarning });
  return {
    state: finalState,
    conversations: total,
    hasMore,
    selection,
    selectionWarning,
  };
}
