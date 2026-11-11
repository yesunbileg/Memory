// Gemini web sync through the authenticated batchexecute RPCs used by the web UI.

import { NotLoggedInError, textPart, toMs } from '../lib/common.js';

import { normalizeCaptureTypes, selectConversation, createSelectionSummary, recordSelection } from './selection.js';

const BASE = 'https://gemini.google.com';
const OVERLAP_MS = 5 * 60 * 1000;
const PAGE_SIZE = 20;
const SYNC_CHUNK_SIZE = 10;
const REQUEST_TIMEOUT_MS = 20_000;

function extractSession(html) {
  const read = key => html.match(new RegExp(`"${key}":"((?:\\\\.|[^"\\\\])*)"`))?.[1];
  const decode = value => value ? JSON.parse(`"${value}"`) : '';
  return {
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

async function getSession() {
  const res = await fetch(`${BASE}/app`, {
    credentials: 'include',
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (res.status === 401 || res.status === 403 || res.url.includes('accounts.google.com')) {
    throw new NotLoggedInError('gemini.google.com');
  }
  if (!res.ok) throw new Error(`GET ${BASE}/app -> ${res.status}`);
  const session = extractSession(await res.text());
  if (!session.at) throw new NotLoggedInError('gemini.google.com');
  const userIndex = res.url.match(/\/u\/(\d+)\//)?.[1] ?? null;
  return { ...session, userIndex };
}

async function batchExecute(session, rpcId, arg) {
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
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8', 'X-Same-Domain': '1' },
    body,
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (res.status === 401 || res.status === 403) throw new NotLoggedInError('gemini.google.com');
  if (!res.ok) throw new Error(`Gemini ${rpcId} -> ${res.status}`);
  return parseRpcResponse(await res.text(), rpcId);
}

async function listChats(session, sinceMs) {
  const chats = new Map();
  let cursor = null;
  for (let page = 0; page < 200; page++) {
    const arg = cursor === null ? [PAGE_SIZE] : [PAGE_SIZE, cursor];
    const data = await batchExecute(session, 'MaZiqc', arg);
    const nextCursor = data?.[1] ?? null;
    const items = Array.isArray(data?.[2]) ? data[2] : [];
    let reachedOld = false;
    for (const item of items) {
      const rawId = String(item?.[0] ?? '');
      if (!rawId) continue;
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
    if (reachedOld || !nextCursor || nextCursor === cursor) break;
    cursor = nextCursor;
  }
  return [...chats.values()];
}

function assistantText(turn) {
  const direct = turn?.[3]?.[0]?.[0]?.[1]?.[0];
  if (typeof direct === 'string') return direct;
  return '';
}

async function readChat(session, summary) {
  const turns = [];
  let cursor = null;
  for (let page = 0; page < 200; page++) {
    const data = await batchExecute(session, 'hNvQHb', [summary.rawId, 20, cursor, 1, [0], [4], null, 1]);
    const pageTurns = Array.isArray(data?.[0]) ? data[0] : [];
    turns.push(...pageTurns);
    const nextCursor = data?.[1] ?? null;
    if (!nextCursor || nextCursor === cursor) break;
    cursor = nextCursor;
  }
  turns.reverse();

  const messages = [];
  for (const [index, turn] of turns.entries()) {
    const turnId = typeof turn?.[0] === 'string' ? turn[0] : typeof turn?.[0]?.[0] === 'string' ? turn[0][0] : `record:${index + 1}`;
    const user = turn?.[2]?.[0]?.[0];
    const assistant = assistantText(turn);
    if (typeof user === 'string' && user.length) {
      messages.push({ role: 'user', content: user, createdAt: null, model: null, parts: [textPart(user)], metadata: { id: `${turnId}:user` } });
    }
    if (assistant.length) {
      messages.push({ role: 'assistant', content: assistant, createdAt: null, model: null, parts: [textPart(assistant)], metadata: { id: `${turnId}:assistant` } });
    }
  }
  if (messages.length === 0) return null;
  return {
    externalId: summary.id,
    title: summary.title,
    createdAt: null,
    updatedAt: summary.updatedAt,
    model: null,
    provider: 'google',
    messages,
    metadata: { url: `${BASE}/app/${summary.id}` },
  };
}

export async function syncGemini({ state = {}, captureTypes, push, register = async () => {}, log = () => {}, report = async () => {} }) {
  const policy = normalizeCaptureTypes(captureTypes);
  const selection = createSelectionSummary();
  if (!Object.values(policy).some(Boolean)) {
    await report({ phase: 'complete', detected: 0, processed: 0, selection, selectionWarning: null });
    return { state, conversations: 0, selection, selectionWarning: null, hasMore: false };
  }
  let selectionWarning = policy.projects || policy.pinned || policy.agents
    ? 'Gemini category metadata is unavailable in the verified private RPC schema; organized chats cannot be identified. Ordinary Recents must be explicitly enabled to capture listed chats.'
    : null;
  await report({ phase: 'discovering', selection, selectionWarning });
  const session = await getSession();
  await register('gemini-web', 'gemini');
  const lastSyncMs = state?.lastSyncMs ?? null;
  const since = lastSyncMs ? lastSyncMs - OVERLAP_MS : null;
  const pending = state?.pending;
  const runStartedMs = pending?.runStartedMs ?? Date.now();
  const summaries = Array.isArray(pending?.summaries)
    ? pending.summaries
    : await listChats(session, since);
  // The tuple schema has no verified project, pin or Gem membership fields.
  // Do not interpret opaque booleans/indices or a suggestive title as consent.
  for (const _summary of summaries) recordSelection(selection, selectConversation(null, policy, { recents: true }));
  if (!policy.recents) {
    await report({ phase: 'complete', detected: summaries.length, processed: 0, selection, selectionWarning });
    return { state: { lastSyncMs }, conversations: 0, hasMore: false, selection, selectionWarning };
  }
  const startIndex = Number.isInteger(pending?.nextIndex) ? pending.nextIndex : 0;
  const endIndex = Math.min(startIndex + SYNC_CHUNK_SIZE, summaries.length);
  await report({ phase: 'importing', detected: summaries.length, processed: startIndex, selection, selectionWarning });
  let total = 0;
  let failures = 0;
  let processed = startIndex;
  let batch = [];
  for (const summary of summaries.slice(startIndex, endIndex)) {
    try {
      const conversation = await readChat(session, summary);
      if (conversation) batch.push(conversation);
    } catch (error) {
      failures++;
      log(`gemini: skipping conversation ${summary.id}: ${error.message}`);
    }
    processed++;
    await report({ processed });
    if (batch.length >= 10) {
      total += await push('gemini-web', 'gemini', batch);
      batch = [];
    }
  }
  if (batch.length) total += await push('gemini-web', 'gemini', batch);
  const hasMore = endIndex < summaries.length;
  const hadFailures = Boolean(pending?.hadFailures) || failures > 0;
  if (hadFailures) selectionWarning = [selectionWarning,
    'Some selected Gemini conversations could not be read. Capture is incomplete; retry after checking your sign-in.',
  ].filter(Boolean).join(' ');
  await report({ phase: hasMore ? 'queued' : 'complete', processed, selection, selectionWarning });
  return {
    state: hasMore
      ? {
          lastSyncMs,
          pending: {
            runStartedMs,
            summaries,
            nextIndex: endIndex,
            hadFailures,
          },
        }
      : { lastSyncMs: hadFailures ? lastSyncMs : runStartedMs },
    conversations: total,
    hasMore,
    selection,
    selectionWarning,
  };
}

export const geminiInternals = { extractSession, parseRpcResponse };
