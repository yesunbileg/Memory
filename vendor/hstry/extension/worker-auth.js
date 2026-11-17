// Only an authenticated identity check establishes an AI Memory connection.
export async function checkConnection(
  settings,
  { fetchImpl = globalThis.fetch, requireArchive = false } = {},
) {
  const port = Number(settings?.port);
  const validPort = Number.isInteger(port) && port >= 1 && port <= 65535;
  const url = `http://127.0.0.1:${validPort ? port : 41770}/auth/check`;
  const token = typeof settings?.token === 'string' ? settings.token.trim() : '';
  if (!token || token.length > 512 || /[^\x21-\x7e]/.test(token)) {
    return {
      ok: false,
      url,
      code: 'missing_token',
      error: 'Connect AI Memory using your private connection file first.',
    };
  }
  if (!validPort) {
    return {
      ok: false,
      url,
      code: 'incompatible_server',
      error: 'Choose a valid AI Memory connection file.',
    };
  }
  let response;
  try {
    response = await fetchImpl(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: '{}',
      redirect: 'error',
      credentials: 'omit',
      cache: 'no-store',
      signal: AbortSignal.timeout(4000),
    });
  } catch {
    return {
      ok: false,
      url,
      code: 'offline',
      error: 'AI Memory is offline. Run ai-memory connect and keep its terminal open.',
    };
  }
  if (response.status === 401 || response.status === 403) {
    return {
      ok: false,
      url,
      code: 'unauthorized',
      error: 'This connection file does not match the running AI Memory receiver.',
    };
  }
  let data;
  try {
    data = response.ok ? await response.json() : null;
  } catch {
    data = null;
  }
  if (data?.status !== 'ok' || data?.service !== 'ai-memory' || data?.schema !== 1) {
    return {
      ok: false,
      url,
      code: 'incompatible_server',
      error: 'This port is not a compatible AI Memory receiver. Run ai-memory connect.',
    };
  }
  // Legacy probes remain recognizable, but capture requires a logical archive identity.
  const archiveId =
    typeof data.archive_instance_id === 'string' && data.archive_instance_id
      ? data.archive_instance_id
      : null;
  if (requireArchive && !archiveId) {
    return {
      ok: false,
      url,
      code: 'incompatible_server',
      error:
        'This receiver cannot identify its archive. Restart AI Memory with the current version before capturing.',
    };
  }
  return { ok: true, url, archiveId };
}
