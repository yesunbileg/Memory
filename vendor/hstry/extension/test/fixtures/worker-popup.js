export const runtimeId = 'a'.repeat(32);
export const popup = { id: runtimeId, url: 'chrome-extension://' + runtimeId + '/popup.html' };
export const providerSyncScopes = Object.fromEntries(
  ['chatgpt', 'claude'].map((p) => [
    p,
    { entries: [{ principalId: 'test-owner', projectIds: [], conversationIds: ['selected'] }] },
  ]),
);
