// Recover the manifest content script after an extension reload. Never retry
// arbitrary failures or inject into a different provider/account/document.
import '../page-picker-routes.js';
import { withAbort } from './common.js';

const recovering = new Map();
const missingReceiver = (e) =>
  /Receiving end does not exist|Could not establish connection/i.test(e?.message ?? '');

async function recover(context) {
  const { tabId, documentId } = context;
  if (!chrome.scripting?.executeScript || !chrome.tabs?.get) throw Error('Recovery unavailable');
  const routes = globalThis.aiMemoryPageRoutes;
  const current = await chrome.tabs.get(tabId);
  const actual = routes.pageContext(current?.url);
  if (
    !actual ||
    current.discarded ||
    (context.provider && actual.provider !== context.provider) ||
    (Object.hasOwn(context, 'slot') && actual.slot !== context.slot)
  ) {
    throw Error('Provider tab changed');
  }
  await chrome.scripting.executeScript({
    target: { tabId, ...(documentId ? { documentIds: [documentId] } : { frameIds: [0] }) },
    files: ['page-picker-routes.js', 'page-picker.js'],
    world: 'ISOLATED',
  });
  const after = await chrome.tabs.get(tabId);
  if (after?.url !== current.url || after.discarded) throw Error('Provider tab changed');
}

export async function sendProviderMessage(context, message, signal) {
  const { tabId, documentId } = context;
  const send = () =>
    chrome.tabs.sendMessage(tabId, message, {
      frameId: 0,
      ...(documentId ? { documentId } : {}),
    });
  try {
    return await withAbort(send, signal);
  } catch (error) {
    signal?.throwIfAborted();
    if (!missingReceiver(error)) throw error;
    const key = JSON.stringify([tabId, documentId, context.provider, context.slot]);
    let task = recovering.get(key);
    if (!task) {
      task = recover(context).finally(() => recovering.delete(key));
      recovering.set(key, task);
    }
    await withAbort(() => task, signal);
    signal?.throwIfAborted();
    return withAbort(send, signal);
  }
}
