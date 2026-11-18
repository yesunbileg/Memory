// Private connection files are read locally, never uploaded or echoed in errors.
export function isArchiveHost(archiveId, nativeHost) {
  return (
    typeof archiveId === 'string' &&
    /^[a-f0-9]{32}$/.test(archiveId) &&
    nativeHost === 'org.ai_memory.archive_' + archiveId
  );
}

export function parseConnectionFile(text) {
  const invalid = () => new Error('Choose a valid AI Memory connection file from your computer.');
  if (typeof text !== 'string' || text.length > 16_384) throw invalid();
  let file;
  try {
    file = JSON.parse(text);
  } catch {
    throw invalid();
  }
  if (file?.schema === 1 && file?.application === 'ai-memory' && file?.host === '127.0.0.1')
    throw new Error(
      'Legacy connection file. Run ai-memory connect --extension-id with your extension ID and choose the new connection file.',
    );
  if (
    !file ||
    file.schema !== 2 ||
    file.application !== 'ai-memory' ||
    file.transport !== 'native' ||
    !isArchiveHost(file.archive_instance_id, file.native_host) ||
    Object.keys(file).length !== 5
  )
    throw invalid();
  return { nativeHost: file.native_host, archiveId: file.archive_instance_id };
}

export function connectionPresentation(result) {
  if (result?.ok === true)
    return {
      tone: 'success',
      title: 'Connected to your computer',
      label: 'Connected',
      detail: 'Your connection is verified. Chats are saved to your local archive.',
    };
  const messages = {
    missing_token: [
      'Let’s connect your computer',
      'Not connected',
      'Start the local app, then choose its connection file below.',
    ],
    legacy_pairing: [
      'Upgrade your connection',
      'Reconnect',
      'Run ai-memory connect --extension-id with this extension ID and select the new connection file.',
    ],
    archive_changed: [
      'Your archive changed',
      'Reconnect',
      'Verify the intended archive, then select its connection file again.',
    ],
    unauthorized: [
      'Your connection needs an update',
      'Check connection',
      'Register the native host for this extension ID, then choose its connection file.',
    ],
    offline: [
      'Your computer is not connected',
      'Offline',
      'Register the native host for this browser and extension ID, then try again.',
    ],
    incompatible_server: [
      'Update your local app',
      'Update needed',
      'Update AI Memory, restart it, and choose the new connection file.',
    ],
  };
  const [title, label, detail] = messages[result?.code] ?? messages.offline;
  return { tone: result?.code === 'missing_token' ? 'idle' : 'error', title, label, detail };
}
