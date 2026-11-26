const fs = require("node:fs");
const readline = require("node:readline");
const vm = require("node:vm");

const [playwright, brave, storage] = process.argv.slice(2);

async function run() {
  let cookies;
  try {
    cookies = JSON.parse(fs.readFileSync(storage, "utf8")).cookies;
    if (!Array.isArray(cookies) || !cookies.length) throw Error();
    // The saved cookie export can label non-Secure cookies SameSite=None.
    // Chromium rejects that combination. Lax retains same-site editor access.
    for (const cookie of cookies) {
      if (cookie.secure === false && cookie.sameSite === "None") cookie.sameSite = "Lax";
    }
  } catch {
    process.stdout.write("auth_required\n");
    return;
  }

  const { chromium } = require(playwright);
  const browser = await chromium.launch({ executablePath: brave, headless: true });
  const input = readline.createInterface({ input: process.stdin, terminal: false });
  try {
    const context = await browser.newContext({ storageState: { cookies, origins: [] } });
    const state = Object.create(null);
    process.stdout.write("ready\n");
    for await (const path of input) {
      let output;
      try {
        const log = (line) => {
          if (typeof line === "string" && line.startsWith("AI_MEMORY_DOCS_RESULT=")) output = line;
        };
        const code = fs.readFileSync(path, "utf8");
        await new vm.Script("(async () => {\n" + code + "\n})()").runInNewContext({
          context,
          state,
          console: { log },
          Buffer,
          URL,
          URLSearchParams,
          require,
        });
      } catch {
        output = undefined;
      }
      process.stdout.write((output ?? "AI_MEMORY_DOCS_RESULT={}") + "\n");
    }
  } finally {
    input.close();
    await browser.close();
  }
}

run().catch(() => {
  process.exitCode = 1;
});
