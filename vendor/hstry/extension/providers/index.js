import { syncChatGPT } from './chatgpt.js';
import { syncClaude } from './claude.js';
import { syncGemini } from './gemini.js';

// Shared provider seam for the worker and both extension surfaces. A provider
// owns its sync implementation; callers only need this small descriptor.
export const PROVIDERS = {
  chatgpt: {
    name: 'ChatGPT',
    site: 'chatgpt.com',
    description: 'Personal and workspace conversations',
    defaultEnabled: false,
    sourceId: 'chatgpt-web',
    adapter: 'chatgpt-web',
    sync: syncChatGPT,
  },
  claude: {
    name: 'Claude',
    site: 'claude.ai',
    description: 'Available organizations',
    defaultEnabled: false,
    sourceId: 'claude-web',
    adapter: 'claude-web',
    sync: syncClaude,
  },
  gemini: {
    name: 'Gemini',
    site: 'gemini.google.com',
    description: 'Conversations from your Google account',
    defaultEnabled: false,
    sourceId: 'gemini-web',
    adapter: 'gemini',
    sync: syncGemini,
  },
};

export const DEFAULT_PROVIDER_SETTINGS = Object.fromEntries(
  Object.entries(PROVIDERS).map(([id, provider]) => [id, provider.defaultEnabled]),
);
