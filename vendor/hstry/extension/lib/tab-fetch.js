// Ask the provider document to use its own browser cookie container.
export async function fetchInTab(context, url, init = {}) {
  const target = new URL(url);
  let reply;
  try {
    reply = await chrome.tabs.sendMessage(
      context.tabId,
      {
        type: 'FETCH_PROVIDER_TAB',
        url: target.href,
        method: init.method ?? 'GET',
        body: init.body?.toString(),
      },
      { frameId: 0, ...(context.documentId ? { documentId: context.documentId } : {}) },
    );
  } catch {
    // The tab can close or navigate between account verification and a read.
  }
  if (
    typeof reply?.body !== 'string' ||
    !Number.isInteger(reply.status) ||
    reply.status < 200 ||
    reply.status > 599 ||
    typeof reply.url !== 'string' ||
    !reply.url.startsWith(target.origin + '/')
  )
    throw Object.assign(new Error('The provider tab is unavailable.'), {
      code: 'provider_tab_unavailable',
    });
  const response = new Response([204, 205, 304].includes(reply.status) ? null : reply.body, {
    status: reply.status,
    headers: reply.retryAfter ? { 'Retry-After': reply.retryAfter } : {},
  });
  Object.defineProperty(response, 'url', { value: reply.url });
  return response;
}
