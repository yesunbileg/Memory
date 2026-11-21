// Shared helpers for hstry sync providers.

let partCounter = 0;

export function textPart(text) {
  return { id: `p-${++partCounter}`, type: 'text', text };
}

export function thinkingPart(text) {
  return { id: `p-${++partCounter}`, type: 'thinking', text };
}

export class NotLoggedInError extends Error {
  constructor(service) {
    super(`${service}: not logged in`);
    this.name = 'NotLoggedInError';
  }
}

export async function withAbort(request, signal) {
  if (!signal) return request();
  signal.throwIfAborted();
  let cancel;
  const aborted = new Promise((_, reject) => {
    cancel = () => reject(signal.reason);
    signal.addEventListener('abort', cancel, { once: true });
  });
  try {
    return await Promise.race([aborted, request()]);
  } finally {
    signal.removeEventListener('abort', cancel);
  }
}

export async function sleep(ms, signal) {
  let timer;
  try {
    await withAbort(() => new Promise((resolve) => (timer = setTimeout(resolve, ms))), signal);
  } finally {
    clearTimeout(timer);
  }
}

/** Cap on how long we honor a server-provided Retry-After (ms). */
const MAX_BACKOFF_MS = 60_000;

/**
 * GET/POST JSON with retry-and-backoff on rate limits (429) and transient
 * server errors (502/503). Honors the `Retry-After` header when present,
 * otherwise uses exponential backoff with jitter. 401/403 fail fast as
 * "not logged in" — retrying those is pointless.
 */
export async function fetchJson(
  url,
  init = {},
  { retries = 5, baseDelayMs = 1000, fetcher = fetch } = {},
) {
  const { signal, ...request } = init;
  for (let attempt = 0; ; attempt++) {
    signal?.throwIfAborted();
    const timeout = AbortSignal.timeout(20_000);
    const res = await fetcher(url, {
      credentials: 'include',
      ...request,
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
    signal?.throwIfAborted();

    if (res.status === 401 || res.status === 403) {
      throw new NotLoggedInError(new URL(url).hostname);
    }

    if (res.status === 429 || res.status === 502 || res.status === 503) {
      if (attempt >= retries) {
        throw new RateLimitedError(
          `${init.method ?? 'GET'} ${url} -> ${res.status} (gave up after ${retries} retries)`,
        );
      }
      const retryAfter = Number(res.headers.get('retry-after'));
      const headerMs = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : 0;
      const backoffMs = baseDelayMs * 2 ** attempt + Math.floor(Math.random() * 500);
      await sleep(Math.min(MAX_BACKOFF_MS, Math.max(headerMs, backoffMs)), signal);
      continue;
    }

    if (!res.ok) {
      throw new Error(`${init.method ?? 'GET'} ${url} -> ${res.status}`);
    }
    return res.json();
  }
}

export class RateLimitedError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RateLimitedError';
  }
}

export function toMs(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    // Unix seconds (possibly fractional) vs milliseconds.
    return Math.floor(value < 1e12 ? value * 1000 : value);
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

export function shortId(id) {
  return String(id)
    .replace(/[^a-zA-Z0-9]/g, '')
    .slice(0, 8)
    .toLowerCase();
}
