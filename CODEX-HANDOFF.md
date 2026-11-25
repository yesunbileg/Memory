# Codex continuation brief — Memory / Google Docs incremental sync

**Огноо:** 2026-11-25  
**Repository:** `/mnt/work/Documents/Memory`  
**Git branch:** `main` (шалгах үед `origin/main`-аас 1 commit ahead)  
**Зориулалт:** шинэ 5-hour window нээгдсэн Codex энэ файлыг уншаад яг эндээс үргэлжлүүл.

## 1. Хэрэглэгчийн үндсэн зорилго

Memory төсөл Obsidian/AI chat archive-ийн зөвшөөрөгдсөн агуулгыг NotebookLM-д хадгалдаг. Одоогийн Markdown upload нь том segment бүхэлдээ өөрчлөгдөх бүрд дахин upload хийж, интернет муу үед гацаж, дата их хэрэглэдэг.

Хүссэн үр дүн нь **Git шиг зөвхөн өөрчлөгдсөн текстийг дамжуулах**: нэмсэн, зассан, устгасан хэсгийг л sync хийх. Том архивын хэвээр үлдсэн текстийг request бүрд дахин явуулахгүй байх. Network тасарсан/response алдагдсан үед давхар бичилт үүсгэхгүй, зөрчилтэй өөрчлөлтийг дарж бичихгүй, нууц мэдээлэл masking болон consent дүрмийг алдагдуулахгүй. Шинэ source бүрэн бэлэн болсны дараа хуучин source-ийг цэвэрлэнэ.

Хэрэглэгч Google Docs-ийг Google Sheets-ээс сонгож, `continue` гэж зөвшөөрсөн. Сонголтын гол үндэслэл: урт Memory segment-ийг Docs-д цөөн хэсэгт багтаах; Docs editor-ийн save замаар text edit хийх. Хэрэглэгч дахин сонголт/баталгаажуулалт асуух шаардлагагүй — энэ нь шийдэгдсэн.

## 2. Одоогийн шийдлийн загвар

- Зөвшөөрөгдсөн, mask хийсэн segment snapshot-ийг Google Docs баримтад хадгална. Нэг баримтыг 1,000,000 UTF-16 нэгжид хуваана.
- Анхны migration нь **бүтэн snapshot** бичнэ. Дараагийн sync-д line-level diff гаргаж зөвхөн нэмэх/устгах/солих командыг явуулна.
- `DocumentJournal` нь шинэ transaction-ийг network call-оос өмнө SQLite checkpoint-д бичнэ. ACK алдагдвал давтан insert хийхээс өмнө remote document fingerprint-ийг шалган сэргээнэ.
- Google Docs-ийн browser session, document revision, save request ID-г нэг Playwriter session дотор хадгалж дараалсан sync-д дахин ашиглана.
- Docs баримтыг NotebookLM-д Drive source болгон холбоно. Бүх хэсэг index/ready болсон үед л segment publication mapping-ийг атомикаар шинэчилж, дараа нь өмнөх source-уудыг цэвэрлэх ёстой.
- Одоогийн зам нь **Google Docs-ийн public REST API биш**. Нэвтэрсэн Brave-ийн Docs editor-ийн undocumented/private save endpoint-ийг Playwriter-аар ашиглаж байгаа. Google дотоод протокол өөрчилбөл эвдэрч болно. Үүнийг хэрэглэгчид official API гэж тайлбарлаж болохгүй.

## 3. Codex-ийн өмнөх thread-ээс батлагдсан түүхэн туршилтууд

Эдгээр нь Codex-ийн тухайн thread-д хадгалагдсан үр дүн; энэ үргэлжлэл дээр дахин live хэмжээгүй:

- 1,000 үгтэй Google Doc-д анх 8,858 байт request явсан.
- 10 үг нэмэхэд 663 байт, нэг үг солиход 584 байт гэж нэг live туршилтад бүртгэсэн.
- Өөр нэг дараагийн туршилтад үг солих + тусдаа хэсэг устгах нийлээд 354 байтын save request болсон.
- Туршилтын archive copy дээр 9 segment-ийг 18 Google Docs хэсэг болгон шилжүүлж, дараагийн өөрчлөлтгүй sync 0 байт байсан гэж thread тэмдэглэсэн.
- Энэ continuation-оос өмнөх Codex туршилтын дараа 1,000 үгтэй doc-д 10 үгийн append 282 байтын POST body гэж тэмдэглэгдсэн.

**Чухал:** дээрх тоонууд нь Docs editor-ийн тодорхой POST request body-ийн хэмжилт болохоос бүх интернетийн нийт дата биш. Browser/page ачаалалт, auth, response, NotebookLM refresh/indexing, fingerprint export зэрэг бусад traffic ороогүй. Том document дахь жижиг edit request нь document-ийн нийт уртаас үл хамааран ойролцоо хэмжээтэй байж болох зарчим нь туршигдсан, гэхдээ бүх network traffic-ийн баталгаа биш.

## 4. Энэ continuation дээр хийсэн өөрчлөлтүүд

### Шинэ source файлууд

- `src/ai_memory/web_text_delta.py` — UTF-16 offset ашигласан, мөрийн түвшний Myers diff; insert/delete/replace edit-үүдийг буурах offset дарааллаар гаргана. `_MAX_DIFF_WORK = 1_000_000`, edit distance дээд тал нь 256; төсөв хэтэрвэл нэг яг таг trimmed edit fallback.
- `src/ai_memory/web_docs_protocol.py` — Docs editor-ийн multi-edit command encoding болон server revision ACK баталгаажуулалт.
- `src/ai_memory/web_docs_journal.py` — баримтыг 1,000,000 UTF-16 нэгжээр хуваах, SQLite pending transaction journal, fingerprint-д суурилсан lost-ACK recovery.
- `src/ai_memory/web_docs_transport.py` — Playwriter CLI-аар сонгосон Brave session-тай холбогдох, driver script-ийг private temp файлд ажиллуулах, алдааг safe code болгон буцаах.
- `src/ai_memory/web_docs_driver.js` — Google Docs editor-ийн session bootstrap/revision-г авах, fingerprint export, private save endpoint руу delta POST хийх, session дотор document page/cache хадгалах.
- `src/ai_memory/web_docs_publication.py` — Docs үүсгэх, NotebookLM-д Drive source болгон холбох, бүх хэсгийн indexing/readiness-ийг баталгаажуулах, status гаргах, legacy source солих publication logic.
- `tests/test_web_docs_sync.py` — diff, protocol, journal, publication, retry, multi-part болон sync integration-ийн synthetic regression tests.

### Хуучин файлуудад хийсэн integration

- `src/ai_memory/sync.py` — Docs publisher-ийг existing snapshot/queue pipeline-д холбосон; sync үр дүнд `docs_web_write_payload_bytes` metric буцаана.
- `src/ai_memory/cli.py` — `docs-web-enable [--session ID]` командыг нэмсэн.
- `src/ai_memory/notebook.py` — Google Docs Drive source нэмэх, refresh/index readiness шалгах adapter.
- `src/ai_memory/publication.py`, `src/ai_memory/store.py` — Docs source mapping, ownership/receipt болон retrieval-д зөвшөөрөгдөх эх сурвалжийн дүрмийг нэгтгэсэн.
- `src/ai_memory/segments.py`, `mem_backend.py`, `pyproject.toml`, `src/ai_memory/diagnostics.py` — одоогийн Codex-ийн integration/CLI/state changes.
- `tests/test_sync.py`, `tests/test_mem_backend.py`, `tests/test_memory_audit_e2e.py` — integration regression.
- `README.md`, `docs/local-testing.mn.md`, `docs/google-docs-incremental-sync.mn.md` — сонголт, setup, limitation, хэмжилтийн тайлбар.

### Энэ continuation дээр тусгайлан нэмсэн сайжруулалт

1. **Line ending canonicalization:** `docs_text()` CRLF, CR, LF-ийг LF болгон normalize хийнэ. `DocumentJournal.sync()`-ийн яг эхэнд normalize хийж checkpoint/hash/diff-ийг remote Docs export-той ижил хэлбэрт оруулсан. Энэ нь sync бүрд худал conflict илрүүлэхээс хамгаална.
2. **Diff performance:** анхны `SequenceMatcher` implementation давтагдсан мөртэй synthetic 32,000-line жишээнд ~9.6 секунд зарцуулсан. Үүнийг Myers-ийн bounded edit-distance алгоритмаар сольсон.
3. **Authentication preflight:** Playwriter `ping` нь `docs.google.com/document/u/0/` руу нэвтэрч шалгаад sign-in redirect илэрвэл `docs_web_auth_required` буцаана. `docs-web-enable`-ийн өмнөх ping энэ кодыг илрүүлж болохоор transport/diagnostics-ийг шинэчилсэн. Live session дээр одоогоор энэ branch-ийг баталгаажуулаагүй.
4. **Metric naming:** `docs_web_request_bytes` нэрийг хэт өргөн тул `docs_web_write_payload_bytes` болгосон. Энэ нь зөвхөн Google Docs save POST form body-ийн байт; бүх сүлжээний дата биш.

## 5. Энэ continuation дээр хийсэн бодит verification

**Python:** `pytest --collect-only` 554 тест / 35 test file илрүүлсэн. Бүгдийг гурван багц болгон ажиллуулсан: 140 passed, 141 passed, 273 passed. **Нийт 554/554 Python тест passed.**

**Нэмэлт checks:**

- `.venv/bin/ruff check src tests scripts config plugins mem.py mem_backend.py mem_flow.py` — PASS.
- Ижил бүх path дээр `ruff format --check` — PASS.
- `node --check --input-type=module < src/ai_memory/web_docs_driver.js` — PASS.
- `git diff --check` — PASS.
- `node --test vendor/hstry/extension/test/*.js` — **156/156 PASS**.
- Myers diff-ийн UTF-16 editor command round-trip: Unicode/emoji, combining mark, line-ending зэрэгтэй **2,000 randomized case PASS**.
- Performance: 40,001 мөртэй давтагдсан document-д нэг жижиг өөрчлөлт ~0.018 sec, 1 patch, зөвхөн 14 UTF-16 unit-ийн өөрчлөлтийн агуулга. 10,001 unique line дээр хоёр алслагдсан edit-ийг 2 patch болгосон. Өндөр edit-distance fallback-ийн synthetic кейсүүд <1ms орчим байсан.

`scripts/check.py`-г нэг дор ажиллуулахад tool-ийн 60 секундын timeout хүрч, pytest ~39%-д байхад SIGTERM болсон. Үүнийг бүрэн script PASS гэж тэмдэглэж болохгүй. Харин Python lint/format, 554 Python test, 156 Node test хэсгүүдийг тус тусад нь бүрэн ажиллуулж PASS болгосон.

**Энэ continuation-д Google/NotebookLM рүү production archive sync хийгээгүй.** `docs-web-enable`, `ai-memory sync`, archive migration ажиллуулаагүй. Одоогийн Google Docs code-ийн шинэчлэлүүд synthetic tests-ээр шалгагдсан болохоос энэ continuation дээр live Google account-аар дахин шалгагдаагүй. Codex-ийн өмнөх thread-д тусдаа test Docs дээр live туршилтууд бүртгэгдсэн.

## 6. Одоогийн Git төлөв ба хамгаалах зүйл

Сүүлийн шалгалтаар branch `main` нь `origin/main`-аас 1 commit ahead. 14 tracked file modified байсан бөгөөд 7 шинэ source/test file, 1 шинэ docs file untracked байсан; энэ handoff нэмэгдээд untracked болно. Өөрчлөлтүүд commit/stage хийгдээгүй.

Шинэ ажлын эхэнд `git status`-ийг дахин шалга. **`git reset`, `git checkout`, `git clean`, blanket formatter/build cleanup бүү ажиллуул.** Хэрэглэгчийн/Codex-ийн uncommitted өөрчлөлтийг устгаж болохгүй. Commit/push хийхээс өмнө diff-ийг бүрэн хянаж хэрэглэгчийн зорилго, repo policy-тай тааруул.

## 7. Дараагийн ажлын дараалал — Codex үргэлжлүүлэх

1. Эхлээд repo instructions, энэ handoff, `git status`, `git diff --stat`, `git diff` болон бүх untracked шинэ файлыг унш. Одоо хийгдсэн ажлыг дахин эхнээс нь бүү бүтээ.
2. Дараахыг нягт review хий: `web_docs_driver.js`, `web_docs_transport.py`, `web_docs_journal.py`, `web_text_delta.py`, `web_docs_protocol.py`, `web_docs_publication.py`, `sync.py`, `publication.py`, `notebook.py`, `store.py`. Ялангуяа editor revision/ACK, lost-ACK recovery, race/concurrent modification, transaction, session cache, stale page/session, privacy/log leakage, retry/backoff.
3. **Part lifecycle:** document-ийн part count багасахад хуучин part source болон Drive Docs document яах, stale parts retrieval-ээс бүрэн хасагдах эсэх, NotebookLM source устах/Google Drive document үлдэх асуудлыг тогтоо. 1 part → 2 parts → 1 part, дахин update, crash recovery тест нэм.
4. **NotebookLM freshness:** `src/ai_memory/notebook.py::refresh_document()` одоогоор `sources.refresh()`, `wait_until_ready()`, `check_freshness()` дууддаг. Drive-linked Docs өөрчлөгдөхөд NotebookLM автоматаар татдаг эсэх, хэдийд/ямар latency-тайг одоогийн official docs болон synthetic test-ээр батал. Давхар refresh request-ийг хасах бол freshness guarantee хадгалагдахыг баталсны дараа.
5. **Official API vs private editor route:** Google Docs-ийн албан ёсны `documents.batchUpdate` API энэ use case-д тохирох эсэх, OAuth setup/user consent-ийн зардал/хязгаарыг дахин судалж болно. Гэхдээ хэрэглэгч Google Docs сонгосон; private web endpoint-ийг official гэж нэрлэхгүй. Хэрэглэгчийн зөвшөөрөлгүйгээр Cloud project, OAuth, paid service шинээр үүсгэх/устгахгүй.
6. Playwriter/Brave auth preflight-ийг тусдаа signed-in/signed-out test account дээр шалга. Хэрэглэгчийн одоо нээлттэй browser session-уудыг reset/delete/хааж болохгүй.
7. Live туршилт шаардлагатай бол тусдаа synthetic archive, Google Doc, боломжтой бол хоосон demo NotebookLM notebook ашигла. Жинхэнэ archive дээр `docs-web-enable` нь revision/state өөрчилж, дараагийн sync бүтэн эхний migration хийх тул backup + explicit approval хэрэгтэй. Бодит archive дээр шууд бүү ажиллуул.
8. `docs_web_write_payload_bytes` болон нийт traffic-ийг тусад нь хэмж. Нийт traffic-д browser document load, bootstrap, fingerprint export, save request/response, NotebookLM add/refresh/poll бүгдийг тооц; request body-ийн байтыг бүх дата мэт тайлбарлахгүй.
9. Бүх өөрчлөлтийн дараа 554 Python + 156 Node suite, Ruff/format, JS syntax, diff checks-ийг дахин ажиллуул. Commit/push зөвхөн хэрэглэгчийн тодорхой шаардлагаар.

## 8. Шинэ Codex-д тавих үргэлжлүүлэх заавар

Энэ нь жижиг `list_files` bug биш. Үндсэн даалгавар бол Memory-ийн NotebookLM publication pipeline-д Google Docs-ийн Git-тэй төстэй incremental text sync нэмэх. Энэ handoff-ийг уншаад бодит diff review, edge-case/recovery validation, live synthetic test хүртэл үргэлжлүүл. Хэрэглэгчээс үндсэн зорилгоо дахин тайлбарлуулах хэрэггүй.

## 9. 2026-11-25 ChatGPT continuation

Read docs/test-mode-audit-2026-11-25.mn.md for the latest PASS/BLOCKED matrix, changes, reproducible TUI smoke and pending live tests. 565 Python + 156 extension tests passed; fresh wheel/MCP smoke passed. Production archive baseline matched. No commit/push.
