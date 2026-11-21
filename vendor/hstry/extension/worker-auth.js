import { isArchiveHost } from './connection-file.js';

// Native Messaging launches only the OS-registered host with a matching allowed extension origin.
// A loopback HTTP response (even with a matching archive ID) is never sufficient.
const errors = {
  missing_token: 'Set up the local connection in AI Memory Settings first.',
  legacy_pairing:
    'Update the local connection using ai-memory connect --extension-id, then reopen AI Memory.',
  unauthorized: 'The native host does not allow this extension.',
  offline: 'The native host is unavailable. Register it for this browser and extension ID.',
  archive_changed: 'The registered archive has changed. Reconnect the intended archive.',
  incompatible_server: 'Update the local AI Memory installation and check the connection.',
};
const sendMessage = (host, message) => chrome.runtime.sendNativeMessage(host, message);
const failed = (code) => ({ ok: false, code, error: errors[code] });
const unset = (value) => value == null || value === '';

export function unpaired(settings) {
  return unset(settings?.nativeHost) && unset(settings?.archiveId);
}

export function validPair(settings) {
  return isArchiveHost(settings?.archiveId, settings?.nativeHost);
}

export async function checkConnection(settings, { sendNative = sendMessage } = {}) {
  if (unpaired(settings)) return failed(settings?.token ? 'legacy_pairing' : 'missing_token');
  if (!validPair(settings)) return failed('incompatible_server');
  try {
    const reply = await sendNative(settings.nativeHost, {
      op: 'check',
      archive_instance_id: settings.archiveId,
    });
    if (reply?.error && Object.hasOwn(errors, reply.error)) return failed(reply.error);
    if (reply?.status !== 'ok' || reply.service !== 'ai-memory' || reply.schema !== 2)
      return failed('incompatible_server');
    if (reply.archive_instance_id !== settings.archiveId) return failed('archive_changed');
    return { ok: true, archiveId: settings.archiveId };
  } catch {
    return failed('offline');
  }
}

export async function discoverConnection(extensionId) {
  if (typeof extensionId !== 'string' || !/^[a-p]{32}$/.test(extensionId))
    return failed('missing_token');
  let reply;
  try {
    reply = await sendMessage('org.ai_memory.connect_' + extensionId, { op: 'discover' });
  } catch {
    return failed('missing_token');
  }
  if (Object.hasOwn(errors, reply?.error)) return failed(reply.error);
  const archiveId = reply?.archive_instance_id;
  if (
    reply?.status !== 'ok' ||
    reply.service !== 'ai-memory' ||
    reply.schema !== 2 ||
    typeof archiveId !== 'string' ||
    !/^[a-f0-9]{32}$/.test(archiveId)
  )
    return failed('incompatible_server');
  return checkConnection({ archiveId, nativeHost: 'org.ai_memory.archive_' + archiveId });
}
