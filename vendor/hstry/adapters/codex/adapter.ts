/**
 * Codex CLI adapter for hstry
 *
 * Parses Codex rollout JSONL files under ~/.codex/sessions
 */

import { readdir, lstat, stat } from 'fs/promises';
import { existsSync } from 'fs';
import { basename, join } from 'path';
import { zstdDecompressSync } from 'zlib';
import { homedir } from 'os';

// AI Memory: Node 24's built-in SQLite, opened read-only; no native addon.
import { DatabaseSync } from 'node:sqlite';
interface SqliteDb {
  prepare(sql: string): SqliteStatement;
  close(): void;
}
interface SqliteStatement {
  get(...params: unknown[]): unknown;
}


const openDb = (path: string): SqliteDb =>
  new DatabaseSync(checkedDatabase(path), { readOnly: true }) as unknown as SqliteDb;

import type {
  Adapter,
  AdapterInfo,
  Conversation,
  Message,
  ParseOptions,
  ToolCall,
} from '../types/index.ts';
import {
  runAdapter,
  checkedSource, checkedDatabase, readSource, hidden,
  readUtf8,
  textOnlyParts,
  thinkingPart,
  toolCallPart,
  toolResultPart,
  isUnderAnyCanonicalRoot,
} from '../types/index.ts';
import { formatFrumTitle } from '../types/first-message.ts';

// Codex honors CODEX_HOME; fall back to ~/.codex like the CLI itself.
const DEFAULT_CODEX_HOME = process.env.CODEX_HOME || join(homedir(), '.codex');
const DEFAULT_PATHS = [
  join(DEFAULT_CODEX_HOME, 'sessions'),
  join(DEFAULT_CODEX_HOME, 'archived_sessions'),
];

interface CodexEvent {
  timestamp?: string;
  type?: string;
  payload?: unknown;
}

interface CodexSessionMeta {
  id?: string;
  forked_from_id?: string | null;
  timestamp?: string;
  cwd?: string;
  originator?: string;
  cli_version?: string;
  source?: string | { subagent?: unknown };
  model_provider?: string | null;
}

interface CodexSessionMetaLine {
  meta?: CodexSessionMeta;
  git?: {
    commit_hash?: string;
    branch?: string;
    repository_url?: string;
  };
}

interface CodexTurnContext {
  model?: string;
  approval_policy?: string;
  sandbox_policy?: { mode?: string } | null;
  cwd?: string;
}

interface ThreadRow {
  id: string;
  title: string;
  model: string | null;
  archived: number;
}

interface CodexResponseItem {
  type?: string;
  id?: string;
  /** response_item.agent_message: inter-agent message metadata. */
  author?: string;
  recipient?: string;
  role?: string;
  content?: Array<{ type?: string; text?: string; image_url?: string; visibility?: string; synthetic?: boolean; ignored?: boolean }>;
  name?: string;
  arguments?: string;
  call_id?: string;
  output?: unknown;
  status?: string;
  action?: unknown;
  input?: string;
  /** reasoning: provider summary of the model's thinking. */
  summary?: Array<{ type?: string; text?: string; visibility?: string; synthetic?: boolean; ignored?: boolean }>;
  /** event_msg.agent_message: assistant text not always mirrored as a response_item. */
  message?: string;
  channel?: string;
  visibility?: 'ui' | 'hidden';
  synthetic?: boolean; ignored?: boolean;
}

const adapter: Adapter = {
  info(): AdapterInfo {
    return {
      name: 'codex',
      displayName: 'Codex CLI',
      version: '1.1.0',
      defaultPaths: DEFAULT_PATHS,
    };
  },

  async detect(path: string): Promise<number | null> {
    // trx-gzfh defense in depth: codex only owns ~/.codex/sessions and
    // ~/.codex/archived_sessions.
    if (!isUnderAnyCanonicalRoot(path, DEFAULT_PATHS)) {
      return null;
    }
    const files = await findRolloutFiles(path, { shallowOnly: true });
    return files.length > 0 ? 0.9 : null;
  },

  async parse(path: string, opts?: ParseOptions): Promise<Conversation[]> {
    const files = await findRolloutFiles(path, { shallowOnly: false });
    if (files.length === 0) return [];

    const threadIndex = loadThreadIndex(path, opts?.skipThreadIndex);
    const conversations: Conversation[] = [];

    try {
      for (const filePath of files) {
        const conv = await parseRolloutFile(filePath, threadIndex, opts);
        if (conv) conversations.push(conv);
        if (opts?.limit && conversations.length >= opts.limit) break;
      }
    } finally { threadIndex.close(); }

    conversations.sort((a, b) => b.createdAt - a.createdAt);
    return conversations;
  },

  async export(conversations, opts) {
    if (opts.format === 'markdown') {
      return {
        format: 'markdown',
        content: conversationsToMarkdown(conversations),
        mimeType: 'text/markdown',
      };
    }

    if (opts.format === 'json') {
      return {
        format: 'json',
        content: JSON.stringify(conversations, null, opts.pretty ? 2 : 0),
        mimeType: 'application/json',
      };
    }

    if (opts.format !== 'codex') {
      throw new Error(`Unsupported export format: ${opts.format}`);
    }

    const files = conversations.map(conv => {
      const jsonl = buildCodexRollout(conv);
      const name = `${conv.externalId ?? 'session'}-rollout.jsonl`;
      return { path: name, content: jsonl };
    });

    return {
      format: 'codex',
      files,
      mimeType: 'application/jsonl',
    };
  },
};

/**
 * Load thread metadata (titles, model, archived flag) from Codex's
 * newest state_<N>.sqlite index. The desktop app and newer CLIs maintain it in
 * CODEX_HOME next to the sessions directory; rollout JSONL files stay
 * authoritative for message content.
 */
function loadThreadIndex(searchPath: string, skip?: boolean) {
  let db: SqliteDb | null = null;
  let lookup: SqliteStatement | undefined;
  try {
    const home = skip ? null : resolveCodexHome(searchPath);
    const path = home && latestStateDb(home);
    if (path) {
      db = openDb(path);
      lookup = db.prepare('SELECT id,title,model FROM threads WHERE id=?');
    }
  } catch { /* Missing, unsafe, locked, or older optional index. */ }
  return {
    get(id: string): ThreadRow | undefined {
      try { return lookup?.get(id) as ThreadRow | undefined; }
      catch { return undefined; }
    },
    close() { db?.close(); },
  };
}

/** Codex bumps state_<N>.sqlite on schema changes; use the newest present. */
function latestStateDb(home: string): string | null {
  for (let generation = 128; generation >= 1; generation--) {
    const path = join(home, `state_${generation}.sqlite`);
    if (existsSync(path)) return path;
  }
  return null;
}

function resolveCodexHome(searchPath: string): string | null {
  const segments = searchPath.split(/[\\/]/);
  for (let i = segments.length - 1; i >= 0; i--) {
    if (segments[i] === 'sessions' || segments[i] === 'archived_sessions') {
      const home = segments.slice(0, i).join('/');
      if (home) return home;
    }
  }
  // An explicitly selected fixture/export must never inspect an unrelated home.
  return /\.jsonl(?:\.zst)?$/.test(searchPath) ? null : searchPath;
}

async function parseRolloutFile(
  filePath: string,
  threadIndex: ReturnType<typeof loadThreadIndex>,
  opts?: ParseOptions
): Promise<Conversation | null> {
  // Newer Codex builds may compress rollouts as rollout-*.jsonl.zst.
  const raw = filePath.endsWith('.zst')
    ? new TextDecoder('utf-8', { fatal: true }).decode(zstdDecompressSync(await readSource(filePath)))
    : await readUtf8(filePath);
  const lines = raw.split(/\r?\n/);
  if (lines.length === 0) return null;

  let sessionMeta: CodexSessionMeta | undefined;
  let gitInfo: CodexSessionMetaLine['git'] | undefined;
  let model: string | undefined;
  const pendingToolCalls = new Map<string, ToolCall>();
  const messages: Message[] = [];

  let firstUserMessage: string | undefined;
  let lastTimestamp: number | undefined;

  for (const [recordIndex, line] of lines.entries()) {
    if (!line.trim()) continue;
    let event: CodexEvent | null = null;
    try {
      event = JSON.parse(line) as CodexEvent;
    } catch {
      throw new Error(`Invalid Codex JSONL record ${recordIndex + 1}`);
    }

    if (!event?.type) continue;

    const parsedTimestamp = event.timestamp ? Date.parse(event.timestamp) : NaN;
    const timestampMs = Number.isFinite(parsedTimestamp) ? parsedTimestamp : undefined;
    if (timestampMs !== undefined && (lastTimestamp === undefined || timestampMs > lastTimestamp)) {
      lastTimestamp = timestampMs;
    }

    switch (event.type) {
      case 'session_meta': {
        const payload = event.payload as CodexSessionMetaLine | CodexSessionMeta | undefined;
        if (!payload) break;
        if ('meta' in payload && payload.meta) {
          sessionMeta = payload.meta;
          gitInfo = payload.git;
        } else {
          sessionMeta = payload as CodexSessionMeta;
        }
        break;
      }
      case 'turn_context': {
        const payload = event.payload as CodexTurnContext | undefined;
        if (payload?.model) {
          model = payload.model;
        }
        break;
      }
      case 'response_item': {
        const payload = event.payload as CodexResponseItem | undefined;
        if (!payload?.type || hidden(payload)) break;

        if (payload.type === 'reasoning') {
          // Reasoning is private unless the source explicitly marks a UI summary.
          if (payload.visibility !== 'ui') break;
          const summary = (payload.summary ?? [])
            .filter(item => item?.type === 'summary_text' && !hidden(item) && typeof item.text === 'string')
            .map(item => item.text!).join('');
          if (!summary) break;
          messages.push({
            role: 'assistant', content: summary,
            parts: [{ ...thinkingPart(summary), visibility: 'ui' }],
            createdAt: timestampMs, model,
            metadata: { id: payload.id, record: recordIndex + 1, kind: 'thinking_summary', visibility: 'ui' },
          });
          break;
        }

        // Inter-agent messages are private routing traffic, not the user's transcript.
        if (payload.type === 'agent_message') break;

        if (payload.type === 'message') {
          if (!['user', 'assistant'].includes(payload.role ?? '')) break;
          if (payload.channel && !['final', 'commentary'].includes(payload.channel)) break;
          if (payload.recipient && payload.recipient !== 'all') break;
          const content = extractContent(payload.content);
          if (!content || (payload.role === 'user' && isCodexBootstrap(content))) break;

          if (!firstUserMessage && payload.role === 'user') {
            firstUserMessage = content;
          }

          messages.push({
            role: mapRole(payload.role ?? 'assistant'),
            content,
            parts: textOnlyParts(content),
            createdAt: timestampMs,
            model,
            metadata: { source: 'response_item.message', id: payload.id, record: recordIndex + 1, channel: payload.channel, visibility: payload.visibility, kind: payload.channel === 'commentary' ? 'progress' : 'text' },
          });
          break;
        }

        // Compaction text is a model-facing synthetic summary.
        if (payload.type === 'compacted') break;

        if (payload.type === 'function_call' || payload.type === 'custom_tool_call') {
          const callId = payload.call_id;
          if (callId && payload.name) {
            const parsedInput = safeParseJson(payload.arguments ?? payload.input);
            pendingToolCalls.set(callId, {
              toolName: payload.name,
              input: parsedInput,
              status: 'pending',
            });
            // Emit the tool_call as an assistant message part
            messages.push({
              role: 'assistant',
              content: '',
              parts: [toolCallPart(callId, payload.name, parsedInput)],
              createdAt: timestampMs,
              model,
            });
          }
          break;
        }

        if (payload.type === 'function_call_output' || payload.type === 'custom_tool_call_output') {
          const callId = payload.call_id;
          if (!callId) break;

          const toolCall = pendingToolCalls.get(callId) ?? {
            toolName: payload.type,
          };
          toolCall.output = stringifyOutput(payload.output);
          toolCall.status = 'success';
          toolCall.durationMs = undefined;

          const resultOutput = toolCall.output ?? '';
          messages.push({
            role: 'tool',
            content: resultOutput,
            parts: [toolResultPart(callId, resultOutput, { name: toolCall.toolName })],
            createdAt: timestampMs,
            toolCalls: [toolCall],
          });

          pendingToolCalls.delete(callId);
          break;
        }

        if (payload.type === 'local_shell_call' || payload.type === 'web_search_call') {
          const toolName = payload.type === 'local_shell_call' ? 'shell' : 'web_search';
          const shellCallId = `${toolName}-${timestampMs}`;
          const toolCall: ToolCall = {
            toolName,
            input: payload.action,
            status: payload.status as ToolCall['status'],
          };
          const shellContent = stringifyOutput(payload.action);
          messages.push({
            role: 'tool',
            content: shellContent,
            parts: [
              toolCallPart(shellCallId, toolName, payload.action),
              toolResultPart(shellCallId, shellContent, { name: toolName }),
            ],
            createdAt: timestampMs,
            toolCalls: [toolCall],
          });
          break;
        }

        break;
      }
      case 'event_msg': {
        // Mirrors are reconciled one-to-one after all response items are known.
        const payload = event.payload as CodexResponseItem | undefined;
        if (!payload || hidden(payload) || !['agent_message', 'user_message'].includes(payload.type ?? '')) break;
        if (payload.channel && !['final', 'commentary'].includes(payload.channel)) break;
        const content = payload.message;
        if (typeof content !== 'string' || !content) break;
        const role = payload.type === 'user_message' ? 'user' : 'assistant';
        if (role === 'user' && isCodexBootstrap(content)) break;
        if (!firstUserMessage && role === 'user') firstUserMessage = content;
        messages.push({
          role, content, parts: textOnlyParts(content), createdAt: timestampMs, model,
          metadata: { source: 'event_msg.' + payload.type, id: payload.id, record: recordIndex + 1, visibility: payload.visibility, kind: payload.channel === 'commentary' ? 'progress' : 'text' },
        });
        break;
      }
      default:
        break;
    }
  }

  if (messages.length === 0) return null;
  if (sessionMeta?.source && typeof sessionMeta.source === 'object' && 'subagent' in sessionMeta.source) return null;

  // Match each mirror once. A global text Set would drop legitimate repeated replies.
  const paired = new Set<Message>();
  const responses = messages.filter(m => m.metadata?.source === 'response_item.message');
  for (const mirror of messages.filter(m => String(m.metadata?.source ?? '').startsWith('event_msg.'))) {
    const candidates = responses.filter(m => !paired.has(m) && m.role === mirror.role && m.content === mirror.content &&
      (m.createdAt === undefined || mirror.createdAt === undefined || Math.abs(m.createdAt - mirror.createdAt) <= 1000));
    candidates.sort((a, b) => Math.abs(Number(a.metadata?.record) - Number(mirror.metadata?.record)) - Math.abs(Number(b.metadata?.record) - Number(mirror.metadata?.record)));
    const response = candidates[0];
    if (!response) continue;
    paired.add(response);
    paired.add(mirror);
    response.metadata = { ...response.metadata, mirror, identityRecord: response.metadata?.record, record: Math.min(Number(response.metadata?.record), Number(mirror.metadata?.record)) };
  }
  const reconciled = messages.filter(m => !(paired.has(m) && String(m.metadata?.source ?? '').startsWith('event_msg.')));
  // Source record order wins over missing/out-of-order wall clock timestamps.
  reconciled.sort((a, b) => Number(a.metadata?.record ?? 0) - Number(b.metadata?.record ?? 0));
  const metaTimestamp = sessionMeta?.timestamp ? Date.parse(sessionMeta.timestamp) : NaN;
  const createdAt = Number.isFinite(metaTimestamp) ? metaTimestamp : undefined;

  // Check both created and updated time so modified sessions are re-imported
  if (opts?.since) {
    const lastModified = lastTimestamp ?? createdAt;
    if (createdAt !== undefined && lastModified !== undefined && createdAt < opts.since && lastModified < opts.since) {
      return null;
    }
  }

  const thread = sessionMeta?.id ? threadIndex.get(sessionMeta.id) : undefined;
  // Workspace and date are separate fields; the title is what was asked.
  const derivedTitle = firstUserMessage ? formatFrumTitle(firstUserMessage) : undefined;
  const title = thread?.title || derivedTitle;

  return {
    externalId: sessionMeta?.id ?? basename(filePath).replace(/\.zst$/, ''),
    title: title || undefined,
    createdAt,
    updatedAt: lastTimestamp,
    model: thread?.model ?? model ?? undefined,
    workspace: sessionMeta?.cwd,
    messages: reconciled,
    metadata: {
      file: filePath,
      ...(thread?.archived ? { archived: true } : {}),
      forkedFromId: sessionMeta?.forked_from_id ?? undefined,
      originator: sessionMeta?.originator,
      cliVersion: sessionMeta?.cli_version,
      source: sessionMeta?.source,
      modelProvider: sessionMeta?.model_provider,
      git: gitInfo,
    },
  };
}

function isCodexBootstrap(content: string): boolean {
  // Only an entire, unambiguous harness envelope is reserved. A heading,
  // fragment, quoted wrapper or text following its closing tag remains visible.
  const text = content.trim();
  const agents = /^# AGENTS\.md[^\r\n]*\r?\n\s*(<INSTRUCTIONS>[\s\S]*)$/.exec(text);
  const body = agents ? agents[1] : text;
  for (const tag of ['INSTRUCTIONS', 'environment_context', 'permissions instructions',
      'available_deferred_tools', 'available_skills', 'SYSTEM_PROMPT']) {
    if (tag === 'INSTRUCTIONS' && !agents) continue;
    const start = '<' + tag + '>', end = '</' + tag + '>';
    if (body.startsWith(start) && body.endsWith(end)) {
      const inside = body.slice(start.length, -end.length);
      return !inside.includes(start) && !inside.includes(end);
    }
  }
  return false;
}

function extractContent(
  content?: Array<{ type?: string; text?: string; image_url?: string; visibility?: string; synthetic?: boolean; ignored?: boolean }>
): string {
  if (!content) return '';
  const parts: string[] = [];
  for (const item of content) {
    if (!item || hidden(item)) continue;
    if (item.type === 'input_text' || item.type === 'output_text' || item.type === 'text') {
      if (typeof item.text === 'string') parts.push(item.text);
    }
  }
  return parts.join('');
}

function conversationsToMarkdown(conversations: Conversation[]): string {
  const blocks: string[] = [];
  for (const conv of conversations) {
    const title = conv.title ?? 'Conversation';
    blocks.push(`# ${title}`);
    blocks.push('');
    blocks.push(`- Created: ${new Date(conv.createdAt).toISOString()}`);
    if (conv.updatedAt) {
      blocks.push(`- Updated: ${new Date(conv.updatedAt).toISOString()}`);
    }
    if (conv.workspace) {
      blocks.push(`- Workspace: ${conv.workspace}`);
    }
    blocks.push('');

    for (const msg of conv.messages) {
      blocks.push(`## ${msg.role}`);
      if (msg.createdAt) {
        blocks.push(`_at ${new Date(msg.createdAt).toISOString()}_`);
      }
      blocks.push('');
      blocks.push(msg.content || '');
      blocks.push('');
    }
  }
  return blocks.join('\n').trim() + '\n';
}

function buildCodexRollout(conv: Conversation): string {
  const lines: string[] = [];
  const sessionId = conv.externalId ?? randomId('session');
  const timestamp = new Date(conv.createdAt).toISOString();

  lines.push(
    JSON.stringify({
      timestamp,
      type: 'session_meta',
      payload: {
        meta: {
          id: sessionId,
          timestamp,
          cwd: conv.workspace ?? '',
          originator: 'hstry',
          cli_version: '0.0.0',
          source: 'cli',
          model_provider: null,
        },
      },
    })
  );

  for (const msg of conv.messages) {
    const msgTimestamp = new Date(msg.createdAt ?? conv.createdAt).toISOString();
    lines.push(
      JSON.stringify({
        timestamp: msgTimestamp,
        type: 'response_item',
        payload: {
          type: 'message',
          role: msg.role,
          content: [
            {
              type: msg.role === 'user' ? 'input_text' : 'output_text',
              text: msg.content,
            },
          ],
        },
      })
    );
  }

  return lines.join('\n') + '\n';
}

function randomId(prefix: string): string {
  const uuid =
    typeof globalThis.crypto?.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${uuid.replace(/-/g, '').slice(0, 12)}`;
}

function mapRole(role: string): Message['role'] {
  switch (role.toLowerCase()) {
    case 'user':
    case 'human':
      return 'user';
    case 'assistant':
    case 'ai':
      return 'assistant';
    case 'system':
      return 'system';
    case 'tool':
      return 'tool';
    default:
      return 'assistant';
  }
}

function safeParseJson(value?: string): unknown {
  if (!value) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function stringifyOutput(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

async function findRolloutFiles(
  path: string,
  opts: { shallowOnly: boolean }
): Promise<string[]> {
  const stats = await stat(checkedSource(path));

  if (stats.isFile()) {
    return isRolloutName(path) ? [path] : [];
  }

  const roots: string[] = [];
  if (stats.isDirectory()) {
    roots.push(path);
    const sessionsDir = join(path, 'sessions');
    const archivedDir = join(path, 'archived_sessions');
    if (await existsDir(sessionsDir)) roots.push(sessionsDir);
    if (await existsDir(archivedDir)) roots.push(archivedDir);
  }

  const files: string[] = [];
  for (const root of roots) {
    const found = await walkForJsonl(root, opts.shallowOnly ? 3 : Infinity);
    files.push(...found);
  }

  // A rollout may exist both plain and compressed; the plain file wins so
  // the same session is never imported twice.
  const plain = new Set(files.filter(file => file.endsWith('.jsonl')));
  return [...new Set(files)].filter(file => !file.endsWith('.zst') || !plain.has(file.slice(0, -4)));
}

async function walkForJsonl(root: string, depth: number): Promise<string[]> {
  const results: string[] = [];
  if (depth < 0) return results;

  const entries = await readdir(checkedSource(root), { withFileTypes: true });
  for (const entry of entries) {
    const entryPath = join(root, entry.name);
    if (entry.isFile() && isRolloutName(entry.name)) {
      results.push(entryPath);
    } else if (entry.isDirectory()) {
      results.push(...(await walkForJsonl(entryPath, depth - 1)));
    }
  }
  return results;
}

function isRolloutName(path: string): boolean {
  return path.endsWith('.jsonl') || path.endsWith('.jsonl.zst');
}

async function existsDir(path: string): Promise<boolean> {
  const stats = await lstat(path).catch(() => null);
  return !!stats?.isDirectory();
}

runAdapter(adapter);
