// Run in an authenticated Playwriter Brave session. Keep private save URLs in this session.
const origin = "https://docs.google.com";
const cache = (state.memoryWebDocs ??= {});
const creations = (state.memoryWebDocsCreations ??= Object.create(null));
let saveStarted = false;

async function newDocumentPage() {
  const page = await context.newPage();
  // The opt-in audit observes only pages this driver creates in its own session.
  await state.memoryDocsAudit?.watch(page);
  return page;
}

function documentId(url) {
  const parsed = new URL(url);
  return parsed.origin === origin && parsed.pathname.match(/^\/document\/(?:u\/\d+\/)?d\/([\w-]+)\/edit$/)?.[1];
}

async function closeDocument(id) {
  const item = cache[id];
  delete cache[id];
  if (item && !item.page.isClosed()) await item.page.close();
}

async function loadDocument(id) {
  const page = await newDocumentPage();
  try {
    await page.addInitScript(() => {
      let chunk;
      Object.defineProperty(window, "DOCS_modelChunk", {
        configurable: true,
        get: () => chunk,
        set(value) {
          chunk = value;
          if (Number.isSafeInteger(value?.revision)) {
            (window.__aiMemoryDocsChunks ??= []).push(value);
          }
        },
      });
    });
    const bootstrap = page.waitForRequest(request => {
      const url = new URL(request.url());
      return url.origin === origin && url.pathname === `/document/d/${id}/docos/p/uc`
        && url.searchParams.get("id") === id && url.searchParams.has("sid")
        && url.searchParams.has("token");
    }, {timeout: 24000});
    const [request] = await Promise.all([
      bootstrap, page.goto(`${origin}/document/d/${id}/edit`, {waitUntil: "commit"}),
    ]);
    await page.waitForLoadState("domcontentloaded");
    await page.locator(".docs-title-input").waitFor({timeout: 24000});
    const model = await page.evaluate(async () => {
      const chunks = window.__aiMemoryDocsChunks ?? [];
      const revision = Math.max(...chunks.map(chunk => chunk.revision));
      if (!Number.isSafeInteger(revision)) throw Error("Document model revision unavailable");
      const inserts = new Map();
      function collect(commands) {
        for (const command of commands) {
          if (command?.ty === "mlti") collect(command.mts);
          if (command?.ty === "ds") throw Error("Unexpected deletion in document snapshot");
          if (command?.ty !== "is") continue;
          const previous = inserts.get(command.ibi);
          if (previous !== undefined && previous !== command.s) throw Error("Conflicting snapshot chunks");
          inserts.set(command.ibi, command.s);
        }
      }
      for (const chunk of chunks) {
        if (chunk.revision !== revision) continue;
        for (const part of chunk.parts ?? []) collect(part.commands ?? []);
      }
      let next = 1;
      const text = [];
      for (const [offset, value] of [...inserts].sort((a, b) => a[0] - b[0])) {
        if (offset !== next || typeof value !== "string") throw Error("Incomplete document model");
        text.push(value);
        next += value.length;
      }
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text.join("")));
      return {revision, hash: Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("")};
    });
    const url = new URL(request.url());
    url.pathname = `/document/d/${id}/save`;
    url.searchParams.delete("reqid");
    return {page, url: String(url), revision: model.revision, knownHash: model.hash, requestId: 0};
  } catch (error) {
    if (!page.isClosed()) await page.close();
    throw error;
  }
}

async function openDocument(id) {
  if (cache[id] && !cache[id].page.isClosed()) return cache[id];
  const idle = Object.keys(cache).filter(key => key !== id && !cache[key].pending);
  while (idle.length > 1) await closeDocument(idle.shift());
  return (cache[id] = await loadDocument(id));
}

async function currentModel(id) {
  const item = await loadDocument(id);
  try {
    return {hash: item.knownHash, revision: item.revision};
  } finally {
    await item.page.close();
  }
}

async function createDocument(title) {
  if (creations[title]) return {id: creations[title]};
  const page = await newDocumentPage();
  try {
    await page.goto(`${origin}/document/u/0/create`, {waitUntil: "commit", timeout: 18000});
    await page.waitForURL(url => Boolean(documentId(url)), {waitUntil: "commit", timeout: 22000});
    const id = documentId(page.url());
    if (!id) throw Error("Google Docs did not return a document ID");
    creations[title] = id;
    return {id};
  } finally {
    if (!page.isClosed()) await page.close();
  }
}

async function readDocumentTitle(page, id) {
  const url = new URL(`${origin}/document/d/${id}/edit`);
  // Fetch a new editor document instead of reading the title the previous page set locally.
  url.searchParams.set("memory_title_check", String(Date.now()));
  const response = await page.goto(url.toString(), {waitUntil: "commit", timeout: 14000});
  if (!response?.ok() || documentId(page.url()) !== id) throw Error("Document unavailable");
  await page.locator("title").waitFor({state: "attached", timeout: 6000});
  return page.title();
}

async function confirmDocumentTitle(id, title) {
  const page = await newDocumentPage();
  const matches = actual => {
    const suffix = actual.lastIndexOf(" - ");
    return actual === title || (suffix >= 0 && actual.slice(0, suffix) === title);
  };
  try {
    if (!matches(await readDocumentTitle(page, id))) {
      const name = page.locator(".docs-title-input");
      await name.click({timeout: 8000});
      // The window title and the general save badge can change before the rename commits.
      const titleForms = [title, encodeURIComponent(title), title.replaceAll(" ", "+")];
      const saved = page.waitForResponse(response => {
        const url = new URL(response.url());
        const request = response.request();
        return url.origin === origin && url.pathname === `/document/d/${id}/chrome`
          && request.method() === "POST"
          && titleForms.some(form => request.postData()?.includes(form));
      }, {timeout: 13000}).catch(() => null);
      await name.fill(title, {timeout: 4000});
      await name.press("Enter", {timeout: 3000});
      const response = await saved;
      if (response) await response.finished();
      if (!matches(await readDocumentTitle(page, id))) {
        // After an acknowledged rename, Drive metadata can take a moment to catch up.
        if (!response?.ok() || !matches(await readDocumentTitle(page, id))) {
          return {status: "title_unconfirmed"};
        }
      }
    }
    if (creations[title] === id) delete creations[title];
    return {status: "named"};
  } finally {
    if (!page.isClosed()) await page.close();
  }
}

async function findDocument(title) {
  if (creations[title]) return {ids: [creations[title]]};
  const page = await newDocumentPage();
  try {
    const url = new URL("https://drive.google.com/drive/u/0/search");
    url.searchParams.set("q", title);
    await page.goto(url.toString(), {waitUntil: "commit"});
    const matches = page.getByText(title, {exact: true});
    await matches.first().waitFor({timeout: 18000});
    const ids = await matches.evaluateAll(elements => Array.from(new Set(elements.map(element =>
      element.closest("[data-id]")?.getAttribute("data-id")
    ).filter(id => typeof id === "string" && /^[\w-]{20,150}$/.test(id)))));
    return {ids};
  } finally {
    if (!page.isClosed()) await page.close();
  }
}

async function send(item, input) {
  if (item.pending && item.pending.transaction !== input.transaction) return {status: "uncertain"};
  // The editor endpoint can transform and commit stale writes before returning
  // an ambiguous acknowledgement. A cached hash is not evidence of the current
  // server revision. Check a newly opened model before sending any packet.
  // This reduces the conflict window; it is not an atomic server-side CAS.
  const actual = await currentModel(input.id);
  if (actual.hash === input.after && (input.recover || item.knownHash !== input.before)) {
    await closeDocument(input.id);
    return {status: "committed", bytes: 0};
  }
  if (actual.hash !== input.before || actual.revision !== item.revision) return {status: "conflict"};
  item.knownHash = actual.hash;
  if (input.recover && !item.pending) return {status: "uncertain"};
  const pending = (item.pending ??= {
    transaction: input.transaction,
    body: new URLSearchParams({
      rev: String(item.revision),
      bundles: JSON.stringify([{
        commands: input.commands,
        sid: new URL(item.url).searchParams.get("sid"),
        reqId: item.requestId,
      }]),
    }).toString(),
  });
  saveStarted = true;
  const ack = await item.page.evaluate(async ({address, body}) => {
    const response = await fetch(address, {
      method: "POST", credentials: "same-origin", signal: AbortSignal.timeout(20000),
      headers: {"content-type": "application/x-www-form-urlencoded;charset=UTF-8"}, body,
    });
    const raw = (await response.text()).replace(/^\)\]\}'\n?/, "");
    if (!response.ok) return {http: response.status};
    const json = JSON.parse(raw);
    const ranges = json.revisionRanges;
    return {http: response.status, metadata: {
      needsTransformOnClient: json.metadata?.needsTransformOnClient,
      serverRevision: json.metadata?.serverRevision,
    }, revisionRanges: Array.isArray(ranges) && ranges.every(range =>
      Array.isArray(range) && range.length === 2 && range.every(Number.isSafeInteger)
    ) ? ranges : null};
  }, {address: item.url, body: pending.body});
  const previous = item.revision;
  const next = previous + 1;
  const range = ack.revisionRanges?.[0];
  if (ack.metadata?.needsTransformOnClient === false && ack.metadata.serverRevision === previous
      && ack.revisionRanges?.length === 1 && range?.[0] === next && range?.[1] === next) {
    item.revision = next;
    item.requestId++;
    item.knownHash = input.after;
    item.pending = null;
  }
  return {status: "submitted", previous, ack, bytes: Buffer.byteLength(pending.body)};
}

async function checkAccess() {
  const page = await newDocumentPage();
  try {
    const response = await page.goto(`${origin}/document/u/0/`, {
      waitUntil: "domcontentloaded", timeout: 20000,
    });
    const current = new URL(page.url());
    if (current.hostname === "accounts.google.com" || /signin|servicelogin/i.test(current.pathname)) {
      return {status: "auth_required"};
    }
    if (current.origin !== origin || response?.status() >= 400) {
      return {status: "unavailable"};
    }
    return {status: "ready"};
  } finally {
    if (!page.isClosed()) await page.close();
  }
}

async function execute() {
  if (input.action === "ping") return checkAccess();
  if (input.action === "create") return createDocument(input.title);
  if (input.action === "find") return findDocument(input.title);
  if (!/^[\w-]{20,150}$/.test(input.id)) throw Error("Invalid document identifier");
  if (input.action === "title") return confirmDocumentTitle(input.id, input.title);
  if (input.action === "close") return closeDocument(input.id);
  if (input.action === "check") return currentModel(input.id);
  const item = await openDocument(input.id);
  if (input.action === "prepare") {
    return {status: item.knownHash === input.known ? "ready" : "conflict"};
  }
  if (input.action === "write") return send(item, input);
  throw Error("Unknown Google Docs action");
}

try {
  const output = await execute();
  console.log("AI_MEMORY_DOCS_RESULT=" + JSON.stringify(output ?? {}));
} catch {
  const status = input.action === "write" ? (saveStarted ? "uncertain" : "not_sent")
    : input.action === "title" ? "title_unconfirmed" : "unavailable";
  console.log("AI_MEMORY_DOCS_RESULT=" + JSON.stringify({status}));
}
