// Native Messaging launches only the OS-registered host with a matching allowed extension origin.
// A loopback HTTP response (even with a matching archive ID) is never sufficient.
const errors = {
  missing_token: 'Choose a native connection file from AI Memory first.',
  legacy_pairing:
    'Upgrade your connection using ai-memory connect --extension-id and choose its new connection file.',
  unauthorized: 'The native host does not allow this extension.',
  offline: 'The native host is unavailable. Register it for this browser and extension ID.',
  archive_changed: 'The registered archive has changed. Reconnect the intended archive.',
  incompatible_server: 'Choose a valid native AI Memory connection file.',
};

export function validPair(settings) {
  return (
    typeof settings?.archiveId === 'string' &&
    /^[a-f0-9]{32}$/.test(settings.archiveId) &&
    settings.nativeHost === 'org.ai_memory.archive_' + settings.archiveId
  );
}

export async function checkConnection(
  settings,
  { sendNative = (host, message) => chrome.runtime.sendNativeMessage(host, message) } = {},
) {
  const failed = (code) => ({ ok: false, code, error: errors[code] });
  if (!settings?.nativeHost && !settings?.archiveId)
    return failed(settings?.token ? 'legacy_pairing' : 'missing_token');
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
