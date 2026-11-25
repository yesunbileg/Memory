// Diagnostic for the isolated title-recovery document only.
const doc = titleInput.id, expected = titleInput.title;
if (!/^[\w-]{20,150}$/.test(doc)) throw Error("invalid test document");
const origin = "https://docs.google.com";
const address = origin + "/document/d/" + doc + "/edit";
let stage = "open", page, reread;
const evidence = {posts: {}, aborted: 0};
const matchTitle = actual => actual === expected || actual.startsWith(expected + " - ");
try {
  page = await context.newPage();
  await page.goto(address, {waitUntil:"domcontentloaded", timeout:22000});
  const name = page.locator(".docs-title-input");
  stage = "editor_ready";
  await name.waitFor({state:"visible",timeout:18000});
  evidence.initial = await page.title();
  const routeMatch = url => url.origin === origin && url.pathname.startsWith("/document/d/" + doc + "/");
  await page.route(routeMatch, async route => {
    const request = route.request();
    if (request.method() !== "POST") return route.continue();
    const end = new URL(request.url()).pathname.split("/").at(-1);
    evidence.posts[end] = (evidence.posts[end] ?? 0) + 1;
    const forms = [expected, encodeURIComponent(expected), expected.replaceAll(" ", "+")];
    const isTitle = end === "chrome" || forms.some(form => request.postData()?.includes(form) || request.url().includes(form));
    if (!isTitle) return route.continue();
    evidence.aborted++;
    return route.abort("failed");
  });
  stage = "rename";
  await name.click({timeout:6000});
  await name.fill(expected, {timeout:5000});
  await name.press("Enter",{timeout:5000});
  stage = "local_title";
  await page.waitForTimeout(3500);
  evidence.localTitleMatches = matchTitle(await page.title());
  evidence.localInputMatches = await name.inputValue() === expected;
  evidence.savedBadge = await page.getByText("Saved to Drive",{exact:false}).count() > 0;
  await page.close();
  stage = "remote_title";
  reread = await context.newPage();
  await reread.goto(address+"?memory_title_probe="+Date.now(),{waitUntil:"domcontentloaded",timeout:18000});
  await reread.locator("title").waitFor({state:"attached",timeout:6000});
  evidence.remoteTitleMatches = matchTitle(await reread.title());
  evidence.remoteStillInitial = await reread.title() === evidence.initial;
  stage = "complete";
} catch (error) {
  evidence.error = String(error?.name ?? "Error");
} finally {
  if (page && !page.isClosed()) await page.close();
  if (reread && !reread.isClosed()) await reread.close();
}
delete evidence.initial;
console.log("TITLE_PROBE="+JSON.stringify({...evidence,stage}));
