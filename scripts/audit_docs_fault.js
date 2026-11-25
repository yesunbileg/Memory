// Test-only routing; scopes every fault to an explicitly created synthetic document.
const id = faultInput.id;
if (!/^[\w-]{20,150}$/.test(id)) throw Error("invalid test document");
const faults = (state.memoryDocsFaults ??= {});
const key = id;
if (faultInput.action === "arm") {
  if (faults[key]) throw Error("existing fault must be cleared");
  const endpoint = faultInput.mode === "title" ? "chrome" : "save";
  const match = url => url.origin === "https://docs.google.com"
    && (faultInput.mode === "title" ? url.pathname.startsWith("/document/d/" + id + "/") : url.pathname === "/document/d/" + id + "/" + endpoint);
  const record = {mode: faultInput.mode, matches: 0, serverResponses: [], posts: {}, match};
  record.handler = async route => {
    const request = route.request();
    if (request.method() !== "POST") return route.continue();
    if (record.mode === "title") {
      const title = faultInput.title;
      const forms = [title, encodeURIComponent(title), title.replaceAll(" ", "+")];
      const path = new URL(request.url()).pathname.split("/").at(-1);
      const titleMatched = forms.some(value => request.postData()?.includes(value) || request.url().includes(value));
      record.posts[path] ??= {count: 0, titleMatched: 0};
      record.posts[path].count++;
      if (titleMatched) record.posts[path].titleMatched++;
      if (!titleMatched && path !== "chrome") return route.continue();
    } else if (record.matches) return route.continue();
    record.matches++;
    if (record.mode === "after") {
      try {
        const response = await route.fetch({timeout: 18000, maxRetries: 0});
        record.serverResponses.push(response.status());
      } catch {
        record.serverResponses.push(null);
      }
    }
    await route.abort("failed");
  };
  faults[key] = record;
  await context.route(match, record.handler);
  console.log("FAULT_RESULT=" + JSON.stringify({armed: true}));
} else if (faultInput.action === "clear") {
  const record = faults[key];
  if (!record) throw Error("no fault");
  await context.unroute(record.match, record.handler);
  delete faults[key];
  console.log("FAULT_RESULT=" + JSON.stringify({mode: record.mode,
    matches: record.matches, serverResponses: record.serverResponses, posts: record.posts}));
} else {
  throw Error("unknown fault action");
}
