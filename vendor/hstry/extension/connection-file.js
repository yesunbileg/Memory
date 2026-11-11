// Private connection files are read locally, never uploaded or echoed in errors.
export function parseConnectionFile(text) {
  const invalid = () => new Error('Choose a valid AI Memory connection file from your computer.');
  if (typeof text !== 'string' || text.length > 16_384) throw invalid();
  let file;
  try {
    file = JSON.parse(text);
  } catch {
    throw invalid();
  }
  if (
    !file ||
    Array.isArray(file) ||
    file.schema !== 1 ||
    file.application !== 'ai-memory' ||
    file.host !== '127.0.0.1' ||
    !Number.isInteger(file.port) ||
    file.port < 1 ||
    file.port > 65535 ||
    typeof file.token !== 'string' ||
    file.token.length < 24 ||
    file.token.length > 512 ||
    /[^\x21-\x7e]/.test(file.token)
  )
    throw invalid();
  return { port: file.port, token: file.token };
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
    unauthorized: [
      'Your connection needs an update',
      'Check connection',
      'Choose the latest connection file, or check the token in manual settings.',
    ],
    offline: [
      'Your computer is not connected',
      'Offline',
      'Keep the local app running on this computer, then try again.',
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
