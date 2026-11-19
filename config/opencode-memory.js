// Install with config/install_hooks.py. Contains no captured text or cloud credentials.
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';

async function capture(sessionID) {
  if (typeof sessionID !== 'string' || !sessionID) return;
  try {
    const base = join(homedir(), '.config');
    if (!isAbsolute(base)) throw Error();
    const settings = JSON.parse(await readFile(join(base, 'ai-memory', 'hooks.json'), 'utf8'));
    if (
      settings.schema !== 'ai-memory-hooks-v1' ||
      ![settings.cli, settings.db, settings.opencodeDb].every(
        (p) => typeof p === 'string' && isAbsolute(p),
      ) ||
      typeof settings.account !== 'string'
    )
      throw Error();
    const args = [
      '--db',
      settings.db,
      'hook',
      'opencode',
      '--source',
      settings.opencodeDb,
      '--account',
      settings.account,
    ];
    await new Promise((resolve, reject) => {
      const child = spawn(settings.cli, args, {
        stdio: ['pipe', 'ignore', 'ignore'],
        timeout: 120000,
      });
      child.once('error', reject);
      child.once('close', (code) => (code === 0 ? resolve() : reject(Error())));
      child.stdin.once('error', reject);
      child.stdin.end(JSON.stringify({ event: 'session.idle', session_id: sessionID }));
    });
  } catch {
    // Do not throw into the host or echo private event/command content.
    console.error(
      'AI Memory could not capture this session. Check local hook settings and archive.',
    );
  }
}

export default {
  id: 'ai-memory',
  // OpenCode 1.18.29 and later accept object entrypoints.
  async server() {
    return {
      event: async ({ event }) => {
        if (event.type === 'session.idle') await capture(event.properties?.sessionID);
      },
    };
  },
  // OpenCode 2.x: native events only, filtered by the host.
  setup(ctx) {
    const controller = new AbortController();
    async function consume() {
      try {
        for await (const event of ctx.event.subscribe({
          types: ['session.idle'],
          signal: controller.signal,
        })) {
          if (event.type === 'session.idle') await capture(event.data?.sessionID);
        }
      } catch {
        if (!controller.signal.aborted)
          console.error('AI Memory event subscription ended. Reload OpenCode to resume capture.');
      }
    }
    void consume();
    return () => controller.abort();
  },
};
