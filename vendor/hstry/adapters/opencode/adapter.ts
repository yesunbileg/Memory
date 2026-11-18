/**
 * OpenCode adapter for hstry
 * 
 * Parses OpenCode session history from ~/.local/share/opencode/
 * 
 * Supports three layouts:
 * 
 * SQLITE (v1.1.45+):
 *   opencode.db (SQLite database with session, message, part, project tables)
 * 
 * NEW (v1.1.25+): 
 *   storage/session/<project-id>/<session-id>.json
 *   storage/message/<session-id>/<msg-id>.json
 *   storage/part/<msg-id>/<part-id>.json
 * 
 * OLD (legacy):
 *   project/<project-name>/storage/session/info/<session-id>.json
 *   project/<project-name>/storage/session/message/<session-id>/<msg-id>.json
 *   project/<project-name>/storage/session/part/<session-id>/<msg-id>/<part-id>.json
 */

import { readdir, stat } from 'fs/promises';
import { existsSync } from 'fs';
import { join, basename, dirname } from 'path';
import { homedir } from 'os';
import type { 
  Adapter, 
  AdapterInfo, 
  CanonPart,
  Conversation, 
  Message, 
  ParseOptions,
  ParseStreamResult,
  ToolCall,
} from '../types/index.ts';
import {
  runAdapter,
  readUtf8, checkedDatabase, checkedSource, hidden,
  textPart,
  thinkingPart,
  toolCallPart,
  toolResultPart,
  textOnlyParts,
  isUnderCanonicalRoot,
} from '../types/index.ts';

// AI Memory: Node 24's built-in SQLite, opened read-only; no native addon.
import { DatabaseSync } from 'node:sqlite';
interface SqliteDb {
  exec(sql: string): void;
  prepare(sql: string): SqliteStatement;
  close(): void;
}
interface SqliteStatement {
  iterate(...params: unknown[]): Iterable<unknown>;
  all(...params: unknown[]): unknown[];
  get(...params: unknown[]): unknown;
}


const openDb = (path: string): SqliteDb =>
  new DatabaseSync(checkedDatabase(path), { readOnly: true }) as unknown as SqliteDb;


// OpenCode storage structures
interface SessionInfo {
  id: string;
  version?: string;
  slug?: string;
  title?: string;
  parentID?: string;
  directory?: string;
  projectID?: string;
  time: {
    created: number;
    updated: number;
  };
  summary?: {
    additions?: number;
    deletions?: number;
    files?: number;
  };
}

interface MessageInfo {
  visibility?: string; synthetic?: boolean; ignored?: boolean;
  id: string;
  sessionID: string;
  role: string;
  time: {
    created: number;
    completed?: number;
    updated?: number;
  };
  parentID?: string;
  modelID?: string;
  providerID?: string;
  agent?: string;
  summary?: {
    title?: string;
  };
  tokens?: {
    input?: number;
    output?: number;
    reasoning?: number;
  };
  cost?: number;
}

interface PartInfo {
  id: string;
  messageID: string;
  sessionID: string;
  type: string;
  text?: string;
  updatedAt?: number;
  time?: { updated?: number };
  visibility?: 'ui' | 'hidden';
  synthetic?: boolean;
  ignored?: boolean;
  tool?: string;
  state?: {
    status?: string;
    input?: unknown;
    output?: string;
    error?: string;
    title?: string;
  };
  /** patch parts: edited file paths */
  files?: string[];
  /** patch parts: snapshot hash */
  hash?: string;
}

const DEFAULT_OPENCODE_PATH = join(homedir(), '.local/share/opencode');

/** A lightweight reference to a session, used for streaming enumeration. */
interface SessionRef {
  /** Path to the session JSON file. */
  path: string;
  /** 'new' or 'old' layout. */
  layout: 'new' | 'old';
  /** For old layout: the project name directory. */
  projectName?: string;
}

/**
 * Enumerate all session file references without loading their contents.
 * Deduplicates sessions that appear in both layouts (prefers new layout).
 */
async function enumerateSessionRefs(basePath: string): Promise<SessionRef[]> {
  const refs = new Map<string, SessionRef>();
  for (const layout of ['new', 'old'] as const) {
    const root = join(basePath, layout === 'new' ? 'storage/session' : 'project');
    for (const project of await optionalEntries(root)) {
      if (!project.isDirectory()) continue;
      const dir = layout === 'new' ? join(root, project.name) : join(root, project.name, 'storage/session/info');
      for (const entry of await optionalEntries(dir)) {
        if (!entry.name.startsWith('ses_') || !entry.name.endsWith('.json') || refs.has(entry.name)) continue;
        const path = join(dir, entry.name);
        checkedSource(path);
        if (entry.isFile()) refs.set(entry.name, { path, layout, projectName: project.name });
      }
    }
  }
  return [...refs.values()];
}

async function optionalEntries(dir: string) {
  try {
    return (await readdir(checkedSource(dir), { withFileTypes: true })).sort((a,b) => a.name.localeCompare(b.name));
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return [];
    throw err;
  }
}

const adapter: Adapter = {
  info(): AdapterInfo {
    return {
      name: 'opencode',
      displayName: 'OpenCode',
      version: '3.0.0',
      defaultPaths: [DEFAULT_OPENCODE_PATH],
    };
  },

  async detect(path: string): Promise<number | null> {
    // trx-gzfh defense in depth: opencode owns ~/.local/share/opencode.
    if (!isUnderCanonicalRoot(path, DEFAULT_OPENCODE_PATH)) {
      return null;
    }
    try {
      if (existsSync(join(checkedSource(path), 'opencode.db'))) return 0.95;
      return (await enumerateSessionRefs(path)).length > 0 ? 0.95 : null;
    } catch {
      // Detection is advisory. Parsing reports actual source errors.
      return null;
    }
  },

  async parse(path: string, opts?: ParseOptions): Promise<Conversation[]> {
    if (basename(path) === 'opencode.db' && (await stat(checkedSource(path))).isFile())
      return parseSqliteLayout(dirname(path), opts);
    if (basename(path).startsWith('ses_') && path.endsWith('.json')) {
      const directory = dirname(path);
      const sessionDir = dirname(directory);
      const storageDir = dirname(sessionDir);
      const projectDir = dirname(storageDir);
      const projectsDir = dirname(projectDir);
      let basePath: string;
      let ref: SessionRef;
      if (basename(directory) === 'info' && basename(sessionDir) === 'session' &&
          basename(storageDir) === 'storage' && basename(projectsDir) === 'project') {
        basePath = dirname(projectsDir);
        ref = { path, layout: 'old', projectName: basename(projectDir) };
      } else if (basename(sessionDir) === 'session' && basename(storageDir) === 'storage') {
        basePath = projectDir;
        ref = { path, layout: 'new' };
      } else {
        throw new Error('Unsupported OpenCode session JSON path');
      }
      const conversation = await parseSingleSession(basePath, ref, opts);
      return conversation ? [conversation] : [];
    }
    checkedSource(path);
    if (existsSync(join(path, 'opencode.db'))) return parseSqliteLayout(path, opts);
    const conversations: Conversation[] = [];
    for (const ref of await enumerateSessionRefs(path)) {
      const conv = await parseSingleSession(path, ref, opts);
      if (conv) conversations.push(conv);
    }
    return conversations.sort((a, b) => b.createdAt - a.createdAt || (a.externalId ?? '').localeCompare(b.externalId ?? ''));
  },

  supportsIncremental: true,

  async parseSince(path: string, since: number): Promise<Conversation[]> {
    return this.parse(path, { since });
  },

  async parseStream(path: string, opts?: ParseOptions): Promise<ParseStreamResult> {
    // Do not walk every JSON session first just to distinguish it from SQLite.
    if (existsSync(join(path, 'opencode.db'))) {
      return parseSqliteStream(path, opts);
    }

    // JSON layouts: enumerate session refs and batch through them
    const batchSize = opts?.batchSize ?? 5;
    const cursor = opts?.cursor as { index: number; total: number } | undefined;

    // Always re-enumerate refs (lightweight - just readdir, no file reads).
    // The cursor only carries the index to keep it small.
    const refs = await enumerateSessionRefs(path);
    const startIndex = cursor?.index ?? 0;

    const conversations: Conversation[] = [];
    const endIndex = Math.min(startIndex + batchSize, refs.length);

    for (let i = startIndex; i < endIndex; i++) {
      const ref = refs[i];
      const conv = await parseSingleSession(path, ref, opts);
      if (conv) {
        conversations.push(conv);
      }
    }

    const done = endIndex >= refs.length;

    return {
      conversations,
      cursor: done ? undefined : { index: endIndex, total: refs.length },
      done,
    };
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

    if (opts.format !== 'opencode') {
      throw new Error(`Unsupported export format: ${opts.format}`);
    }

    const files = buildOpenCodeFiles(conversations);
    return {
      format: 'opencode',
      files,
      mimeType: 'application/json',
      metadata: {
        root: 'project/',
      },
    };
  },
};

/**
 * Parse a single session from either layout, given a SessionRef.
 * Used by parseStream to process one session at a time.
 */

// ============================================================================
// SQLite layout (v1.1.45+)
// ============================================================================

/** SQLite row types matching the opencode schema */
interface SqliteSessionRow {
  id: string;
  project_id: string;
  parent_id: string | null;
  slug: string;
  directory: string;
  title: string;
  version: string;
  summary_additions: number | null;
  summary_deletions: number | null;
  summary_files: number | null;
  time_created: number;
  time_updated: number;
}

interface SqliteMessageRow {
  id: string;
  session_id: string;
  time_created: number;
  time_updated: number;
  data: string; // JSON blob
}

interface SqlitePartRow {
  id: string;
  message_id: string;
  session_id: string;
  time_created: number;
  time_updated: number;
  data: string; // JSON blob
}

interface SqliteProjectRow {
  id: string;
  worktree: string;
}

/** OpenCode 2.x `session_v2` row. */
interface SqliteV2SessionRow {
  id: string;
  project_id: string;
  parent_id: string | null;
  slug: string | null;
  directory: string | null;
  title: string | null;
  version: string;
  summary_additions: number | null;
  summary_deletions: number | null;
  summary_files: number | null;
  cost: number;
  tokens_input: number;
  tokens_output: number;
  tokens_reasoning: number;
  model: string | null;
  agent: string | null;
  time_created: number;
  time_updated: number;
}

/** OpenCode 2.x `session_message` row; `data` is a JSON SessionMessage blob. */
interface SqliteV2MessageRow {
  id: string;
  session_id: string;
  type: string;
  seq: number;
  time_created: number;
  time_updated: number;
  data: string;
}

function tableExists(db: SqliteDb, name: string): boolean {
  const row = db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?").get(name);
  return Boolean(row);
}

/**
 * Parse all conversations from the SQLite layout.
 */
async function parseSqliteLayout(basePath: string, opts?: ParseOptions): Promise<Conversation[]> {
  return withSnapshot(basePath, db => {
    const conversations: Conversation[] = [];
    let cursor: unknown;
    do {
      const page = sqlitePage(db, { ...opts, batchSize: 256, cursor });
      conversations.push(...page.conversations);
      if (page.done) break;
      cursor = page.cursor;
    } while (true);
    return conversations;
  });
}

function withSnapshot<T>(basePath: string, read: (db: SqliteDb) => T): T {
  const db = openDb(join(basePath, 'opencode.db'));
  try {
    db.exec('BEGIN');
    return read(db);
  } finally { db.close(); } // close releases the read-only transaction
}

async function parseSqliteStream(basePath: string, opts?: ParseOptions): Promise<ParseStreamResult> {
  return withSnapshot(basePath, db => sqlitePage(db, opts));
}

function sqlitePage(db: SqliteDb, opts?: ParseOptions): ParseStreamResult {
    const batchSize = Math.max(1, Math.min(opts?.batchSize ?? 25, 500));
    const offset = (opts?.cursor as {offset: number} | undefined)?.offset ?? 0;
    // OpenCode 2.x replaced the v1 session/message/part tables with session_v2
    // + session_message (typed, event-sourced rows). Prefer the new schema when
    // present; a migrated database may still carry the v1 tables.
    if (tableExists(db, 'session_v2') && tableExists(db, 'session_message')) {
      return parseSqliteV2Stream(db, opts);
    }

    // Build session query with optional time filter
    let sessionQuery = `
      SELECT s.id, s.project_id, s.parent_id, s.slug, s.directory, s.title,
             s.version, s.summary_additions, s.summary_deletions, s.summary_files,
             s.time_created, s.time_updated
      FROM session s
    `;
    const params: unknown[] = [];

    if (opts?.since) {
      sessionQuery += ` WHERE s.time_created >= ? OR s.time_updated >= ?`;
      params.push(opts.since, opts.since);
    }

    sessionQuery += ` ORDER BY s.time_created DESC, s.id LIMIT ? OFFSET ?`;
    params.push(batchSize + 1, offset); // fetch one extra to check if done

    const sessionRows = db.prepare(sessionQuery).all(...params) as SqliteSessionRow[];

    const hasMore = sessionRows.length > batchSize;
    const rows = hasMore ? sessionRows.slice(0, batchSize) : sessionRows;

    if (rows.length === 0) {
      return { conversations: [], done: true };
    }

    // Build a project worktree lookup
    const projectIds = [...new Set(rows.map(r => r.project_id))];
    const projectMap = new Map<string, string>();
    if (projectIds.length > 0) {
      const placeholders = projectIds.map(() => '?').join(',');
      const projects = db.prepare(
        `SELECT id, worktree FROM project WHERE id IN (${placeholders})`
      ).all(...projectIds) as SqliteProjectRow[];
      for (const p of projects) {
        projectMap.set(p.id, p.worktree);
      }
    }

    // Load messages and parts for all sessions in this batch
    const sessionIds = rows.map(r => r.id);
    const placeholders = sessionIds.map(() => '?').join(',');

    const messageRows = db.prepare(
      `SELECT id, session_id, time_created, time_updated, data
       FROM message WHERE session_id IN (${placeholders})
       ORDER BY session_id, time_created, id`
    ).iterate(...sessionIds) as Iterable<SqliteMessageRow>;

    // Group messages by session
    const messagesBySession = new Map<string, SqliteMessageRow[]>();
    for (const row of messageRows) {
      let list = messagesBySession.get(row.session_id);
      if (!list) {
        list = [];
        messagesBySession.set(row.session_id, list);
      }
      list.push(row);
    }

    // Load parts for all messages
    const messageIds = Array.from(messagesBySession.values()).flatMap(rows => rows.map(r => r.id));
    const partsByMessage = new Map<string, SqlitePartRow[]>();
    if (messageIds.length > 0) {
      // Process in chunks to avoid SQLite variable limit
      const chunkSize = 500;
      for (let i = 0; i < messageIds.length; i += chunkSize) {
        const chunk = messageIds.slice(i, i + chunkSize);
        const ph = chunk.map(() => '?').join(',');
        const partRows = db.prepare(
          `SELECT id, message_id, session_id, time_created, time_updated, data
           FROM part WHERE message_id IN (${ph})
           ORDER BY message_id, id`
        ).iterate(...chunk) as Iterable<SqlitePartRow>;

        for (const row of partRows) {
          let list = partsByMessage.get(row.message_id);
          if (!list) {
            list = [];
            partsByMessage.set(row.message_id, list);
          }
          list.push(row);
        }
      }
    }

    // Build conversations
    const conversations: Conversation[] = [];
    for (const session of rows) {
      const conv = buildSqliteConversation(
        session,
        messagesBySession.get(session.id) ?? [],
        partsByMessage,
        projectMap.get(session.project_id),
        opts,
      );
      if (conv) conversations.push(conv);
    }

    const done = !hasMore;

    return {
      conversations,
      cursor: done ? undefined : { offset: offset + batchSize },
      done,
    };
}

/**
 * Build a Conversation from SQLite rows.
 */
function buildSqliteConversation(
  session: SqliteSessionRow,
  messageRows: SqliteMessageRow[],
  partsByMessage: Map<string, SqlitePartRow[]>,
  worktree: string | undefined,
  opts?: ParseOptions,
): Conversation | null {
  const messages: Message[] = [];

  for (const msgRow of messageRows) {
    const msgData = JSON.parse(msgRow.data);
    const role = msgData.role;
    if (msgData.sessionID !== undefined && msgData.sessionID !== session.id) throw new Error('Invalid message parent');
    if (msgData.id !== undefined && msgData.id !== msgRow.id) throw new Error('Invalid message identity');
    if (typeof role !== 'string' || hidden(msgData) || !['user','assistant'].includes(mapRole(role))) continue;

    // Reconstruct parts from the part table
    const partRows = partsByMessage.get(msgRow.id) ?? [];
    const parts: PartInfo[] = [];

    for (const partRow of partRows) {
      const partData = JSON.parse(partRow.data);
      if (partRow.session_id !== session.id || (partData.sessionID !== undefined && partData.sessionID !== session.id)
          || (partData.messageID !== undefined && partData.messageID !== msgRow.id)
          || (partData.id !== undefined && partData.id !== partRow.id)) throw new Error('Invalid part parent');
      // The data blob stores type/text/tool/state etc. directly
      parts.push({
        id: partRow.id,
        messageID: msgRow.id,
        sessionID: msgRow.session_id,
        type: partData.type,
        text: partData.text,
        updatedAt: latestUpdate(partData.time?.updated, partRow.time_updated),
        visibility: partData.visibility,
        synthetic: partData.synthetic,
        ignored: partData.ignored,
        tool: partData.tool,
        state: partData.state,
        files: Array.isArray(partData.files) ? partData.files : undefined,
        hash: typeof partData.hash === 'string' ? partData.hash : undefined,
      });
    }

    const projection = projectMessageParts(parts, opts?.includeTools !== false);

    // Extract token info from assistant message data
    const tokens = (msgData.tokens?.input || 0) + (msgData.tokens?.output || 0) + (msgData.tokens?.reasoning || 0);

    messages.push({
      role: mapRole(role),
      ...projection,
      createdAt: msgData.time?.created ?? msgRow.time_created,
      updatedAt: latestUpdate(msgData.time?.updated, msgRow.time_updated),
      model: msgData.modelID || undefined,
      tokens: tokens > 0 ? tokens : undefined,
      costUsd: msgData.cost || undefined,
      metadata: {
        id: msgRow.id,
        parentId: msgData.parentID,
        provider: msgData.providerID,
        agent: msgData.agent,
      },
    });
  }

  return buildConversation(
    {
      id: session.id,
      version: session.version,
      slug: session.slug,
      title: session.title,
      parentID: session.parent_id ?? undefined,
      directory: session.directory,
      projectID: session.project_id,
      time: {
        created: session.time_created,
        updated: session.time_updated,
      },
      summary: (session.summary_additions !== null || session.summary_deletions !== null) ? {
        additions: session.summary_additions ?? undefined,
        deletions: session.summary_deletions ?? undefined,
        files: session.summary_files ?? undefined,
      } : undefined,
    },
    messages,
    session.directory || worktree,
    session.project_id,
  );
}

// ============================================================================
// SQLite layout, OpenCode 2.x (session_v2 + session_message)
// ============================================================================

/**
 * Parse the OpenCode 2.x schema, where each session_message row is one typed
 * record (`user`, `assistant`, `synthetic`, `system`, `skill`, `shell`,
 * `compaction`, `idle`, `agent-switched`, `model-switched`, `location-switched`).
 */
function parseSqliteV2Stream(db: SqliteDb, opts?: ParseOptions): ParseStreamResult {
  const batchSize = Math.max(1, Math.min(opts?.batchSize ?? 25, 500));
  const cursor = opts?.cursor as { offset: number } | undefined;
  const offset = cursor?.offset ?? 0;

  let sessionQuery = `
    SELECT s.id, s.project_id, s.parent_id, s.slug, s.directory, s.title,
           s.version, s.summary_additions, s.summary_deletions, s.summary_files,
           s.cost, s.tokens_input, s.tokens_output, s.tokens_reasoning,
           s.model, s.agent, s.time_created, s.time_updated
    FROM session_v2 s
  `;
  const params: unknown[] = [];
  if (opts?.since) {
    sessionQuery += ` WHERE s.time_created >= ? OR s.time_updated >= ?`;
    params.push(opts.since, opts.since);
  }
  sessionQuery += ` ORDER BY s.time_created DESC, s.id LIMIT ? OFFSET ?`;
  params.push(batchSize + 1, offset);

  const sessionRows = db.prepare(sessionQuery).all(...params) as SqliteV2SessionRow[];
  const hasMore = sessionRows.length > batchSize;
  const rows = hasMore ? sessionRows.slice(0, batchSize) : sessionRows;
  if (rows.length === 0) return { conversations: [], done: true };

  const projectIds = [...new Set(rows.map(r => r.project_id).filter(Boolean))];
  const projectMap = new Map<string, string>();
  if (projectIds.length > 0) {
    const placeholders = projectIds.map(() => '?').join(',');
    const projects = db.prepare(`SELECT id, worktree FROM project WHERE id IN (${placeholders})`).all(...projectIds) as SqliteProjectRow[];
    for (const p of projects) projectMap.set(p.id, p.worktree);
  }

  const sessionIds = rows.map(r => r.id);
  const placeholders = sessionIds.map(() => '?').join(',');
  const messageRows = db.prepare(
    `SELECT id, session_id, type, seq, time_created, time_updated, data
     FROM session_message WHERE session_id IN (${placeholders})
     ORDER BY session_id, seq`
  ).iterate(...sessionIds) as Iterable<SqliteV2MessageRow>;

  const messagesBySession = new Map<string, SqliteV2MessageRow[]>();
  for (const row of messageRows) {
    const list = messagesBySession.get(row.session_id);
    if (list) list.push(row);
    else messagesBySession.set(row.session_id, [row]);
  }

  const conversations: Conversation[] = [];
  for (const session of rows) {
    const conv = buildSqliteV2Conversation(session, messagesBySession.get(session.id) ?? [], projectMap.get(session.project_id), opts);
    if (conv) conversations.push(conv);
  }

  return { conversations, cursor: hasMore ? { offset: offset + batchSize } : undefined, done: !hasMore };
}

function buildSqliteV2Conversation(
  session: SqliteV2SessionRow,
  messageRows: SqliteV2MessageRow[],
  worktree: string | undefined,
  opts?: ParseOptions,
): Conversation | null {
  const includeTools = opts?.includeTools !== false;
  const messages: Message[] = [];
  const modelRef = parseModelRef(session.model);

  for (const row of messageRows) {
    const data: Record<string, any> = JSON.parse(row.data);
    if ((data.sessionID !== undefined && data.sessionID !== session.id) ||
        (data.id !== undefined && data.id !== row.id)) throw new Error('Invalid message parent');
    if (hidden(data)) continue;
    const type = row.type || data.type;
    const createdAt = data.time?.created ?? row.time_created;
    const updatedAt = latestUpdate(data.time?.updated, row.time_updated);

    if (type === 'user') {
      const text = typeof data.text === 'string' ? data.text : '';
      const parts: CanonPart[] = [];
      if (text.length) parts.push(textPart(text));
      for (const file of Array.isArray(data.files) ? data.files : []) {
        const uri = typeof file === 'string' ? file : file?.uri ?? file?.path;
        if (typeof uri === 'string') parts.push({ id: `x-file-${row.id}-${parts.length}`, type: 'file_ref', uri, ...(file?.name ? { label: file.name } : {}) });
      }
      if (!text.length && parts.length === 0) continue;
      messages.push({ role: 'user', content: text, parts: parts.length ? parts : textOnlyParts(text), createdAt, updatedAt, metadata: { id: row.id } });
      continue;
    }

    if (type === 'assistant') {
      const parts: CanonPart[] = [];
      const chunks: string[] = [];
      for (const block of Array.isArray(data.content) ? data.content : []) {
        if (!block || typeof block !== 'object' || block.visibility === 'hidden' || block.synthetic === true || block.ignored === true) continue;
        if (block.type === 'text' && typeof block.text === 'string' && block.text.length) {
          parts.push({ ...textPart(block.text), updatedAt: latestUpdate(block.time?.updated, block.time_updated) });
          chunks.push(block.text);
        } else if (block.type === 'reasoning' && typeof block.text === 'string' && block.text.length) {
          parts.push({ ...thinkingPart(block.text), updatedAt: latestUpdate(block.time?.updated, block.time_updated), ...(block.visibility === 'ui' ? { visibility: 'ui' } : {}) });
          chunks.push(block.text);
        } else if (block.type === 'tool' && includeTools) {
          parts.push(...v2ToolParts(block));
        }
      }
      const content = chunks.join('');
      if (!content && !parts.length) continue;
      const total = v2TokenTotal(data.tokens);
      messages.push({
        role: 'assistant',
        content: content || projectPartsToContent(parts),
        parts: parts.length ? parts : textOnlyParts(content),
        createdAt,
        updatedAt,
        model: data.model?.id ?? modelRef?.id,
        tokens: total || undefined,
        costUsd: typeof data.cost === 'number' ? data.cost : undefined,
        metadata: { id: row.id, agent: data.agent, provider: data.model?.providerID ?? modelRef?.providerID, finish: data.finish },
      });
      continue;
    }

    // A shell record is a tool execution (command + output/exit).
    if (type === 'shell') {
      const callId = String(data.shellID ?? row.id);
      const output = typeof data.output === 'string' ? data.output : '';
      const failed = data.status === 'error' || (typeof data.exit === 'number' && data.exit !== 0);
      messages.push({
        role: 'tool',
        content: output || String(data.command ?? ''),
        parts: [toolCallPart(callId, 'shell', { command: data.command }), toolResultPart(callId, output, { name: 'shell', isError: failed })],
        createdAt,
        metadata: { id: row.id, status: data.status, exit: data.exit },
      });
      continue;
    }

    // synthetic/system/skill carry model-facing text; compaction carries a summary.
    if (type === 'system' || type === 'synthetic' || type === 'skill' || (type === 'compaction' && data.status !== 'running')) {
      const text = String(data.text ?? data.summary ?? data.description ?? '').trim();
      if (!text) continue;
      messages.push({ role: 'system', content: text, parts: textOnlyParts(text), createdAt, metadata: { id: row.id, type } });
      continue;
    }
    // idle / agent-switched / model-switched / location-switched / running
    // compaction are bookkeeping with no conversational content.
  }

  if (messages.length === 0) return null;
  // session_message.seq is authoritative; wall clock timestamps can go backwards.

  const conv: Conversation = {
    externalId: session.id,
    title: session.title || undefined,
    createdAt: session.time_created,
    updatedAt: session.time_updated,
    workspace: session.directory || worktree,
    model: modelRef?.id,
    provider: modelRef?.providerID,
    tokensIn: session.tokens_input || undefined,
    tokensOut: (session.tokens_output || 0) + (session.tokens_reasoning || 0) || undefined,
    costUsd: session.cost || undefined,
    messages,
    metadata: {
      version: session.version,
      slug: session.slug,
      parentId: session.parent_id ?? undefined,
      projectId: session.project_id,
      agent: session.agent ?? undefined,
      schema: 'session_v2',
    },
  };
  if (session.parent_id) {
    conv.parentExternalId = session.parent_id;
    conv.forkType = 'thread';
  }
  return conv;
}

function v2ToolParts(block: Record<string, any>): CanonPart[] {
  const callId = String(block.id ?? block.name ?? 'tool');
  const name = String(block.name ?? 'tool');
  const state = block.state ?? {};
  const parts: CanonPart[] = [toolCallPart(callId, name, state.input)];
  if (state.status === 'completed') {
    parts.push(toolResultPart(callId, v2ContentText(state.content)));
  } else if (state.status === 'error') {
    const detail = v2ContentText(state.content) || v2ErrorText(state.error);
    parts.push(toolResultPart(callId, detail, { isError: true }));
  }
  return parts;
}

function v2ContentText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .map(item => (item?.type === 'text' ? item.text : item?.type === 'file' ? `[file] ${item.uri}` : ''))
    .filter(Boolean)
    .join('\n');
}

function v2ErrorText(error: unknown): string {
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object') {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === 'string') return message;
    try {
      return JSON.stringify(error);
    } catch {
      return 'tool failed';
    }
  }
  return 'tool failed';
}

function v2TokenTotal(tokens: unknown): number {
  if (!tokens || typeof tokens !== 'object') return 0;
  const t = tokens as Record<string, any>;
  return (t.input ?? 0) + (t.output ?? 0) + (t.reasoning ?? 0) + (t.cache?.read ?? 0) + (t.cache?.write ?? 0);
}

function parseModelRef(model: string | null | undefined): { id: string; providerID: string } | undefined {
  if (!model) return undefined;
  try {
    const parsed = JSON.parse(model);
    if (parsed && typeof parsed.id === 'string') return { id: parsed.id, providerID: parsed.providerID ?? '' };
  } catch {
    // Some v2 builds stored a plain model id string.
    return { id: model, providerID: '' };
  }
  return undefined;
}

/** Map opencode tool status to hstry ToolStatus. */
function mapToolStatus(status?: string): 'pending' | 'success' | 'error' | undefined {
  switch (status) {
    case 'completed': return 'success';
    case 'error': return 'error';
    case 'pending':
    case 'running': return 'pending';
    default: return undefined;
  }
}

// ============================================================================
// JSON layout helpers (shared by new/old layouts and parseStream)
// ============================================================================

async function parseSingleSession(
  basePath: string,
  ref: SessionRef,
  opts?: ParseOptions,
): Promise<Conversation | null> {
  const sessionContent = await readUtf8(ref.path);
  const session: SessionInfo = JSON.parse(sessionContent);
  if (session.id !== basename(ref.path, '.json') ||
      (ref.layout === 'new' && session.projectID !== undefined && session.projectID !== basename(dirname(ref.path))))
    throw new Error('Invalid session identity');

  // Apply time filter
  if (opts?.since) {
    const lastModified = session.time.updated ?? session.time.created;
    if (session.time.created < opts.since && lastModified < opts.since) {
      return null;
    }
  }

  let messages: Message[];
  let workspace: string | undefined;

  if (ref.layout === 'new') {
    const storageDir = join(basePath, 'storage');
    const messageDir = join(storageDir, 'message');
    const partDir = join(storageDir, 'part');
    messages = await loadMessages(session.id, messageDir, partDir, opts);
    workspace = session.directory;
  } else {
    const projectPath = join(basePath, 'project', ref.projectName!);
    const sessionMessageDir = join(projectPath, 'storage/session/message');
    const sessionPartDir = join(projectPath, 'storage/session/part');
    messages = await loadMessages(session.id, sessionMessageDir, join(sessionPartDir, session.id), opts);
    workspace = session.directory || projectNameToPath(ref.projectName!);
  }

  return buildConversation(session, messages, workspace, ref.projectName);
}

/** Build a Conversation from a parsed session and its messages. */
function buildConversation(
  session: SessionInfo,
  messages: Message[],
  workspace: string | undefined,
  projectName?: string,
): Conversation {
  const conv: Conversation = {
    externalId: session.id,
    title: session.title || undefined,
    createdAt: session.time.created,
    updatedAt: session.time.updated,
    workspace,
    messages,
    metadata: {
      version: session.version,
      slug: session.slug,
      parentId: session.parentID,
      projectId: session.projectID ?? projectName,
    },
  };

  let tokensIn = 0;
  let tokensOut = 0;
  let cost = 0;
  let model: string | undefined;
  let provider: string | undefined;

  for (const msg of messages) {
    if (msg.tokens) {
      if (msg.role === 'user') tokensIn += msg.tokens;
      else tokensOut += msg.tokens;
    }
    if (msg.costUsd) cost += msg.costUsd;
    if (msg.model && !model) model = msg.model;
    if (!provider && msg.metadata && typeof msg.metadata.provider === 'string') {
      provider = msg.metadata.provider;
    }
  }

  if (tokensIn > 0) conv.tokensIn = tokensIn;
  if (tokensOut > 0) conv.tokensOut = tokensOut;
  if (cost > 0) conv.costUsd = cost;
  if (model) conv.model = model;
  if (provider) conv.provider = provider;

  // Child sessions (subagents/threads) carry a parentID; surface the actual
  // session tree link instead of burying it in metadata only.
  if (session.parentID) {
    conv.parentExternalId = session.parentID;
    conv.forkType = 'thread';
  }

  return conv;
}

async function loadMessages(
  sessionId: string,
  messageDir: string,
  partDir: string,
  opts?: ParseOptions
): Promise<Message[]> {
  const sessionMessagePath = join(messageDir, sessionId);
  const messages: Message[] = [];

  const messageFiles = (await optionalEntries(sessionMessagePath)).map(entry => entry.name);

  for (const messageFile of messageFiles) {
    if (!messageFile.startsWith('msg_') || !messageFile.endsWith('.json')) {
      continue;
    }

    const messagePath = join(sessionMessagePath, messageFile);
    const messageContent = await readUtf8(messagePath);
    if (!messageContent.trim()) throw new Error('Empty source message file');
    const msgInfo: MessageInfo = JSON.parse(messageContent);
    if (msgInfo.id !== basename(messageFile, '.json') || msgInfo.sessionID !== sessionId)
      throw new Error('Invalid message parent or identity');
    if (hidden(msgInfo) || !['user','assistant'].includes(mapRole(msgInfo.role))) continue;

    // Load parts - in new layout: part/<msg-id>/<part-id>.json
    const msgPartDir = join(partDir, msgInfo.id);
    const parts = await loadParts(msgPartDir, sessionId, msgInfo.id);

    const projection = projectMessageParts(parts, opts?.includeTools !== false);
    const tokens = (msgInfo.tokens?.input || 0) + (msgInfo.tokens?.output || 0) + (msgInfo.tokens?.reasoning || 0);

    messages.push({
      role: mapRole(msgInfo.role),
      ...projection,
      createdAt: msgInfo.time.created,
      updatedAt: msgInfo.time.updated,
      model: msgInfo.modelID || undefined,
      tokens: tokens > 0 ? tokens : undefined,
      costUsd: msgInfo.cost || undefined,
      metadata: {
        id: msgInfo.id,
        parentId: msgInfo.parentID,
        provider: msgInfo.providerID,
        agent: msgInfo.agent,
        summaryTitle: msgInfo.summary?.title,
      },
    });
  }

  messages.sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
  return messages;
}

/** Read one message's parts from its already-resolved native layout. */
async function loadParts(msgPartDir: string, sessionId: string, messageId: string): Promise<PartInfo[]> {
  const parts: PartInfo[] = [];

  const partFiles = (await optionalEntries(msgPartDir)).map(entry => entry.name);

  for (const partFile of partFiles) {
    if (!partFile.startsWith('prt_') || !partFile.endsWith('.json')) {
      continue;
    }

    const partPath = join(msgPartDir, partFile);
    const partContent = await readUtf8(partPath);
    if (!partContent.trim()) throw new Error('Empty source part file');
    const part: PartInfo = JSON.parse(partContent);
    if (part.id !== basename(partFile, '.json') ||
        (part.messageID !== undefined && part.messageID !== messageId) ||
        (part.sessionID !== undefined && part.sessionID !== sessionId))
      throw new Error('Invalid part parent or identity');
    parts.push(part);
  }

  parts.sort((a, b) => a.id.localeCompare(b.id));
  return parts;
}

/**
 * Convert OpenCode project name to filesystem path
 * e.g., "home-wismut-byteowlz-kittenx" -> "/home/wismut/byteowlz/kittenx"
 */
function projectNameToPath(projectName: string): string {
  if (projectName === 'global') return '~';
  return '/' + projectName.replace(/-/g, '/');
}

/** Preserve reliable native edit times, without inventing an observed-time version. */
function latestUpdate(...values: unknown[]): number | undefined {
  const times = values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
  return times.length ? Math.max(...times) : undefined;
}

/** JSON and v1 SQLite messages share exactly the same ordered part projection. */
function projectMessageParts(parts: PartInfo[], includeTools: boolean) {
  const textParts = parts.filter(p => p.type === 'text' && p.text && !p.synthetic && !p.ignored && p.visibility !== 'hidden');
  const content = textParts.map(p => p.text).join('');

  let toolCalls: ToolCall[] | undefined;
  if (includeTools) {
    const toolParts = parts.filter(p => p.type === 'tool' && p.tool);
    if (toolParts.length > 0) {
      toolCalls = toolParts.map(p => ({
        toolName: p.tool!,
        input: p.state?.input,
        output: p.state?.output ?? p.state?.error,
        status: mapToolStatus(p.state?.status),
      }));
    }
  }

  const canonParts = buildCanonParts(parts, includeTools) ?? textOnlyParts(content);
  return { content: content || projectPartsToContent(canonParts), parts: canonParts, toolCalls };
}

/** Build CanonPart[] from opencode PartInfo[]. */
function buildCanonParts(oparts: PartInfo[], includeTools: boolean): CanonPart[] | undefined {
  const canon: CanonPart[] = [];
  for (const p of oparts) {
    if (p.synthetic || p.ignored || p.visibility === 'hidden') continue;
    if (p.type === 'text' && p.text) {
      canon.push({ ...textPart(p.text), updatedAt: latestUpdate(p.updatedAt, p.time?.updated) });
    } else if (p.type === 'reasoning' && p.text) {
      // Assistant turns are split into per-step rows; early rows often carry
      // only reasoning. Preserve it or the step reads as an empty reply.
      canon.push({ ...thinkingPart(p.text), updatedAt: latestUpdate(p.updatedAt, p.time?.updated), ...(p.visibility === 'ui' ? { visibility: 'ui' } : {}) });
    } else if (p.type === 'tool' && p.tool && includeTools) {
      // Tool parts in opencode represent both the call and result.
      // The state has input, output, status.
      const callId = p.id || p.tool;
      canon.push(toolCallPart(callId, p.tool, p.state?.input));
      if (p.state?.output !== undefined || p.state?.status === 'error') {
        // Errored tool states carry their message in `error`, not `output`.
        canon.push(toolResultPart(callId, p.state?.output ?? p.state?.error, { name: p.tool, isError: p.state?.status === 'error' }));
      }
    } else if (p.type === 'patch') {
      canon.push({ id: `x-patch-${p.id}`, type: 'x-patch', payload: { hash: p.hash, files: p.files } });
    }
    // step-start / step-finish are bookkeeping and carry no content.
  }
  return canon.length > 0 ? canon : undefined;
}

/**
 * Compact textual projection of non-text parts, used as message content when
 * a step row has no text part of its own. Keeps content-only readers
 * (`hstry show`, peek bundles, FTS) useful instead of showing empty replies.
 */
function projectPartsToContent(canon: CanonPart[] | undefined): string {
  if (!canon) return '';
  const lines: string[] = [];
  for (const part of canon) {
    if (part.type === 'thinking' && part.text) {
      lines.push(part.text);
    } else if (part.type === 'tool_call') {
      const input = (part as { input?: unknown }).input;
      let hint = '';
      if (input && typeof input === 'object') {
        for (const key of ['command', 'file_path', 'path', 'filePath', 'pattern', 'query', 'url']) {
          const v = (input as Record<string, unknown>)[key];
          if (typeof v === 'string' && v) { hint = v.slice(0, 200); break; }
        }
      }
      lines.push(hint ? `\u{2699} ${part.name}: ${hint}` : `\u{2699} ${part.name}`);
    } else if (part.type === 'x-patch') {
      const files = ((part as { payload?: { files?: string[] } }).payload?.files ?? [])
        .map(f => f.split('/').pop() ?? f)
        .slice(0, 5);
      if (files.length) lines.push(`\u{270E} ${files.join(', ')}`);
    }
  }
  return lines.join('\n').slice(0, 8000);
}

function mapRole(role: string): Message['role'] {
  switch (role.toLowerCase()) {
    case 'user':
    case 'human':
      return 'user';
    case 'assistant':
    case 'agent':
    case 'ai':
      return 'assistant';
    case 'system':
      return 'system';
    case 'tool':
    case 'function':
      return 'tool';
    default:
      return 'system';
  }
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

function buildOpenCodeFiles(conversations: Conversation[]): { path: string; content: string }[] {
  const files: { path: string; content: string }[] = [];

  for (const conv of conversations) {
    const sessionId = conv.externalId ?? randomId('ses_');
    const projectName = workspaceToProjectName(conv.workspace);
    const basePath = `project/${projectName}/storage/session`;
    const infoDir = `${basePath}/info`;
    const messageDir = `${basePath}/message/${sessionId}`;
    const partDir = `${basePath}/part/${sessionId}`;

    const sessionInfo = {
      id: sessionId,
      version: '1',
      title: conv.title ?? null,
      parentID: null,
      directory: conv.workspace ?? null,
      projectID: projectName,
      time: {
        created: conv.createdAt,
        updated: conv.updatedAt ?? conv.createdAt,
      },
    };
    files.push({
      path: `${infoDir}/${sessionId}.json`,
      content: JSON.stringify(sessionInfo, null, 2),
    });

    conv.messages.forEach((msg, idx) => {
      const msgId = `msg_${idx + 1}`;
      const created = msg.createdAt ?? conv.createdAt;
      const messageInfo = {
        id: msgId,
        sessionID: sessionId,
        role: msg.role,
        time: {
          created,
        },
        modelID: msg.model ?? null,
        providerID: null,
        agent: null,
        tokens: msg.tokens ? { input: msg.tokens, output: 0 } : undefined,
        cost: msg.costUsd ?? undefined,
      };
      files.push({
        path: `${messageDir}/${msgId}.json`,
        content: JSON.stringify(messageInfo, null, 2),
      });

      const partEntries: PartInfo[] = [];
      if (msg.content) {
        partEntries.push({
          id: `prt_${idx + 1}_text`,
          messageID: msgId,
          sessionID: sessionId,
          type: 'text',
          text: msg.content,
        });
      }

      if (msg.toolCalls) {
        msg.toolCalls.forEach((tool, toolIdx) => {
          partEntries.push({
            id: `prt_${idx + 1}_tool_${toolIdx + 1}`,
            messageID: msgId,
            sessionID: sessionId,
            type: 'tool',
            tool: tool.toolName,
            state: {
              status: tool.status,
              input: tool.input,
              output: tool.output,
            },
          });
        });
      }

      partEntries.forEach(part => {
        files.push({
          path: `${partDir}/${msgId}/${part.id}.json`,
          content: JSON.stringify(part, null, 2),
        });
      });
    });
  }

  return files;
}

function workspaceToProjectName(workspace?: string): string {
  if (!workspace) return 'global';
  return workspace.replace(/^\/+/, '').replace(/[\\/]/g, '-');
}

function randomId(prefix: string): string {
  const uuid =
    typeof globalThis.crypto?.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}${uuid.replace(/-/g, '').slice(0, 12)}`;
}

// Run the adapter
runAdapter(adapter);
