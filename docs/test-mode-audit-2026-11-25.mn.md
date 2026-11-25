# AI Memory — Test Mode аудит, Codex continuation (2026-11-25)

**Шийдвэр: PARTIAL / production rollout-д бүрэн баталгаажаагүй.** Баталсан төлөвлөгөөний үндсэн SQLite → Docs → NotebookLM live trace өмнөх Codex дээр PASS болсон. Шинэ кодын offline regression, package болон TUI smoke PASS. Үлдсэн live нөхцөлүүдийг PASS гэж тооцоогүй.

## Нотолгоо ба туршилтын матриц

| Шалгалт | Үр дүн | Нотолгоо / хязгаар |
|---|---|---|
| Үндсэн архивын хамгаалалт | PASS | Өмнөх baseline-тай read-only харьцуулалт: бүх талбар яг таарсан; 10 active segment, 19 Docs parts, 19 eligible sources, 0 pending, auto off. |
| Python lint, format, JS syntax, git diff check | PASS | Ruff, 83 файлын format, node syntax, diff check; Prettier мөн PASS. |
| Python regression suite | PASS | 140 + 141 + 284 = 565 passed. |
| Extension regression suite | PASS | 156 passed. |
| Wheel / installed MCP | PASS | Тусдаа fresh venv, driver багцад байна; stdio memory_ask ба memory_status, invalid question rejected. |
| Packaged extension, provider selection | PASS (fixture, өмнөх Codex) | ChatGPT/Claude/Gemini clean Brave fixture; 7 native archive records, unchanged detail reads 0, one edit reads 1, cancellation works. |
| ChatGPT/Claude/Gemini хоёр аккаунтын бодит capture | BLOCKED | Ямар test profiles нэвтэрсэн нь баталгаажаагүй; fixture-г live гэж тооцохгүй. |
| Codex/OpenCode hooks, consent, privacy, auto/lock | PASS (offline) | Python regression; Codex-ийн live trace нь synthetic rollout ашигласан. Production service-д fault injection хийгээгүй. |
| SQLite → Docs → NotebookLM → Memory Ask | PASS (өмнөх Codex live) | data/test-mode-20261125/evidence.json; origin/SQLite SHA256 таарсан; reopened Docs SHA256 таарсан, шинэ өгүүлбэр 1 удаа, cited message exact, source/Drive ID ба revision тогтвортой. |
| Incremental save / no change | PASS (өмнөх Codex live) | Seed 1248 B; append 888 B, 1 save; unchanged 0 B, stable IDs. Энэ нь Docs save body, нийт network bytes биш. |
| Same 10-word append small vs near 1M UTF-16 | PASS (synthetic), BLOCKED (live pair) | 2 KiB synthetic form cap, unchanged marker байхгүй, UTF-16 surrogate boundary аюулгүй. Бодит хоёр Docs-ийн live benchmark хийгдээгүй. |
| Lost save ACK, definitely unsent, title, create/link ACK, multipart cleanup | PASS (synthetic), BLOCKED (live) | tests/test_web_docs_sync.py. Тусгаарласан browser дээр бодит fault injection хийгдээгүй. |
| Wrong Drive/source identity | PASS (synthetic) | Шинээр нэмсэн хоёр regression; explicit mismatch → missing hold, retrieval scope-оос хасагдана. SDK Drive ID огт өгөхгүй бол өмнөх бүртгэлд тулгуурлана. |
| TUI 140×42 / 54×24 | PASS (simulated remote) | scripts/audit_tui.py; actual Textual navigation Tab/Enter/Esc, Docs connect → flow → sync → ask, indexing phase ба 888 B. Live auth/UI fault test биш. |
| Backup/restore | PASS (өмнөх Codex) | SQLite quick_check ok; 2 messages, 0 pending, hash таарсан. |
| Network total | BLOCKED / incomplete | NotebookLM streamed response bytes, failed browser requests, actual wire/TLS traffic бүрэн хэмжигдээгүй. |

## Энэ continuation-д хийсэн засвар

- `tests/test_runtime_diagnostics.py`, `tests/test_sync.py`: restrictive shell umask 0077 нөхцөлд public fixture 0700 болж 2 тест унасныг дахин гаргасан. 0022-т PASS. Зөвхөн fixture-г 0755 болгож тогтворжуулсан; production file permission өөрчлөөгүй.
- `src/ai_memory/web_docs_publication.py`, `src/ai_memory/publication.py`: бүртгэлтэй source ID өөр Drive document ID-г заасан explicit inventory mismatch-ийг баталгаатай гэж үзэхгүй. `remote_missing` hold; тухайн segment retrieval scope-оос хасагдана. Дахин бичих/устгахгүй. Хоёр regression test PASS.
- `tests/test_web_docs_sync.py`: near-limit ба 1000-үгийн ижил 10-үг append-ийн synthetic form cap, unchanged text exclusion, surrogate-boundary regression PASS.
- `scripts/audit_tui.py`: бодит Textual TUI-г synthetic DB ба simulated remote-оор ажиллуулах давтагдах smoke script.

## Дахин ажиллуулах

```sh
cd /mnt/work/Documents/Memory
.venv/bin/python scripts/check.py
npm run format:check
node --check --input-type=module < src/ai_memory/web_docs_driver.js
git diff --check
uv run --offline --script scripts/audit_tui.py --db data/test-mode-20261125/audit.sqlite
```

Tool 60s hard timeout-той тул Python suite-г 3 бүлэгт тусад нь бүрэн ажиллуулсан. Өмнөх Codex full runner PASS гэж session log-д тэмдэглэсэн; энэ continuation full runner-г нэг process-оор ажиллуулаагүй. Fresh installed wheel smoke evidence: /tmp/ai-memory-takeover.J4l7Pz/mcp-install.json. Өмнөх package/extension fixture evidence: /tmp/ai-memory-audit-eqGfSM0J/.

## Үлдсэн ажлын дараалал

1. Тусгаарласан Brave session-д title POST abort, save POST abort-before/response-lost-after, NotebookLM link response lost live fault injection; journal, remote text hash, Drive/source ID, duplicate count, retry result бүртгэх. Шинэ test Docs/notebook л ашиглана. ACK uncertain бөгөөд өмнөх session устсан үед blind retry хийхгүй.
2. Тусдаа хоёр test document дээр 1000 үг ба хязгаарт ойр хэмжээг seed хийж ижил 10 үг append; real save body <= 2048 B; no-change 0 saves. Failed requests, page load, NotebookLM streamed data-г тусад нь хэмжих.
3. Test-only account profiles баталгаажсаны дараа 3 provider тус бүрд хоёр account capture, project/selected scope, revoked chat, edit, Claude detail bytes. Clean profile fixture нь live биш.
4. Browser session lost, auth revoked, editor conflict, quota/timeout, multipart shrink ба TUI-гүй worker-г тусгаарласан live орчинд шалгах. Drive-д үлдсэн хуучин part document-ийн lifecycle/consent removal-ыг тусад нь тодорхойлох; user files автоматаар устгахгүй.
5. Эцсийн PASS/FAIL/BLOCKED report ба baseline-ийг дахин тулгах. Google Docs private editor route албан ёсны REST API биш бөгөөд өөрчлөгдөх эрсдэлтэй. Production sync/migration, commit, push хийгээгүй.

