# Локалд шинэчлэх, extension холбох, тест хийх

Командуудыг repository-ийн хавтсанд ажиллуулна. Доорх жишээ `data/conversations.sqlite` архивыг ашиглана. Өөр архивтай бол бүх `ai-memory` командын өмнө `--db /бүтэн/зам/archive.sqlite` нэмнэ; extension, worker, MCP гурав ижил архив ашиглах ёстой.

## 1. Кодоо шинэчлэх

```bash
cd /mnt/work/Documents/AI-CONVARSATION
git switch main
git pull --ff-only
uv sync --frozen --extra login
```

Хавтас чинь өөр бол эхний мөрийн замыг солино. `login` extra нь одоо ашиглаж буй browser-ийн нэвтрэлтийг унших SDK нэмэлт; Chromium татахгүй. Өмнөх архив, notebook болон auth файлаа үргэлжлүүлэн ашиглана.

Google ашиглахгүй үндсэн шалгалт:

```bash
.venv/bin/ai-memory demo --output data/local-demo
.venv/bin/python scripts/mcp_smoke.py --db data/local-demo/conversations.sqlite
```

Энэ нь тусдаа хиймэл архивт Codex/OpenCode-ийн 2 яриа, 5 мессеж импортлож, MCP-ийн хоёр tool-ийг шалгана. Дахин ажиллуулахад мессеж давхардахгүй. Google retrieval шалгасан гэсэн үг биш.

## 2. Extension суулгах эсвэл шинэчлэх

1. Brave дээр `brave://extensions/` нээнэ (`chrome://extensions/` мөн болно).
2. **Developer mode** асаана.
3. **Load unpacked** → repository доторх `vendor/hstry/extension` хавтсыг сонгоно.
4. Өмнө суулгасан бол **Reload** дарна. Settings дээр **0.5.2** харагдана.

ZIP хэрэгтэй бол:

```bash
.venv/bin/python scripts/package_extension.py
```

`dist/ai-memory-extension-0.5.2.zip` үүснэ. Задалсан, `manifest.json` нь шууд дотроо байгаа хавтсыг **Load unpacked**-д сонгоно. Багцад icon, UI, provider код болон license орно; token, connection/auth файл, тест болон хувийн архив орохгүй.

## 3. Компьютертэй холбох

```bash
.venv/bin/ai-memory connect
```

Terminal-ийг нээлттэй байлгана. Extension → Settings → **Choose connection file** → terminal дээр гарсан `extension-connection.json` файлыг сонгоно. Default архивт энэ нь `data/extension-connection.json` байна. **Connected to your computer** гэж гарахад token болон архивын холболт батлагдсан гэсэн үг.

ChatGPT эсвэл Claude-оо асааж, дараах checklist-ийг сонгоно:

- Projects & folders — асаалттай.
- Pinned & favorites — асаалттай.
- GPTs & agents — хүссэн үед асаалттай.
- **Ordinary Recents — унтраалттай.**

Project/pinned яриа Recents жагсаалтад давхар харагдсан ч өөрийн category-аар орно. **Capture selected chats** дарна. Activity-д үр дүн, алгассан чат болон алдааны мэдээлэл гарна.

Gemini project/pinned/agent төрлөө найдвартай мэдээлдэггүй тул одоогийн энэ сонголтоор Gemini чат хадгалагдахгүйг Settings болон Popup урьдчилан тайлбарлана. Checklist-ийг app өөрөө өргөжүүлэхгүй. Codex нь тусдаа local collector; browser-ийн checkbox нь локал Codex session-ийг шүүхгүй.

## 4. Projects/Pinned орж, энгийн Recents орохгүйг бодитоор шалгах

ChatGPT-д дараах нэртэй **шинэ** дөрвөн чат үүсгэнэ:

| Нэр | Байрлал | Хүлээгдэх үр дүн |
|---|---|---|
| `AI-MEMORY-CHECK-PROJECT` | Project дотор | Орно |
| `AI-MEMORY-CHECK-PINNED` | Pin хийсэн | Орно |
| `AI-MEMORY-CHECK-AGENT` | Custom GPT/agent-ийн яриа | Орно |
| `AI-MEMORY-CHECK-RECENT` | Зөвхөн энгийн Recents | Орохгүй |

Дээрх гурван төрөл асаалттай, Ordinary Recents унтраалттай үед capture хийнэ. Эхний удаа **Rescan selected chats** мөн ашиглаж болно. Default архивыг өөрчлөхгүй уншиж шалгах команд:

```bash
.venv/bin/python - <<'PY'
import sqlite3
prefix = "AI-MEMORY-CHECK-"
with sqlite3.connect("file:data/conversations.sqlite?mode=ro", uri=True) as db:
    rows = db.execute(
        "SELECT id,title FROM conversations WHERE platform=? AND title LIKE ?",
        ("chatgpt", prefix + "%"),
    ).fetchall()
for conversation_id, title in rows:
    print(conversation_id, title)
titles = {title for _, title in rows}
assert {prefix + kind for kind in ("PROJECT", "PINNED", "AGENT")} <= titles
assert prefix + "RECENT" not in titles
print("PASS: selected chats saved; ordinary Recents excluded")
PY
```

Өөр DB ашиглаж байгаа бол snippet доторх `file:data/conversations.sqlite` замыг мөн солино. API category metadata өөрчлөгдсөн эсвэл дутуу бол Activity-ийн тайлбарыг шалгана. Өмнө архивласан чатыг checklist өөрчилснөөр устгахгүй тул шинэ нэртэй жишээ ашиглана.

## 5. NotebookLM-д өөрийн Brave-аар нэвтрэх

Brave-ийн ашиглах profile дээрээ NotebookLM-д нэвтэрсэн байна. Дараа нь:

```bash
.venv/bin/notebooklm login --browser-cookies brave
.venv/bin/ai-memory check
```

Chrome/Firefox бол `brave`-ийг `chrome`/`firefox` гэж солино. Profile сонгох бол `--browser-cookies 'brave::Profile 1'`; олон Google account-тай бол `--account YOUR_EMAIL` нэмнэ. Desktop keyring түгжээтэй байвал эхлээд өөрийн desktop session-д unlock хийнэ.

Өмнө `NOTEBOOKLM_HOME` эсвэл тусгай storage файл тохируулсан бол тэрийгээ хэрэглэнэ. Тусгай файлтай login жишээ: `notebooklm login --browser-cookies brave --storage /private/path/storage_state.json`. MCP/worker мөн ижил auth location ашиглана. Дахин нэвтрэхдээ шинэ notebook үүсгэх шаардлагагүй. Анхны notebook тохиргоо бол [README](../README.md#connect-notebooklm)-ийн `init --notebook-id` алхмыг ашиглана.

Хиймэл өгөгдөлтэй тусдаа Google notebook дээр upload/query шалгах:

```bash
.venv/bin/python scripts/live_demo.py --output data/live-local-demo
```

`live_notebooklm_test: executed_review_answer` гарвал хүсэлт гүйцэтгэсэн гэсэн үг. Хариултын SQLite/NotebookLM шийдвэрүүд source-той таарч байгааг шалгана. Free-form хариулт доторх conversation ID-г дангаар нь баталгаатай гэж үзэхгүй; structured source/segment reference болон эх архивтай тулгана.

Өөрийн жинхэнэ ярианаас Google руу дамжуулахыг зөвшөөрсөн ID-уудыг л `ai-memory approve ID1 ID2` командад өгч, дараа нь `ai-memory sync` ажиллуулна. Web capture нь Google upload зөвшөөрлийг автоматаар өгөхгүй.

## 6. Codex/OpenCode болон background worker

```bash
.venv/bin/ai-memory collect codex ~/.codex/sessions
.venv/bin/ai-memory collect opencode ~/.local/share/opencode
.venv/bin/ai-memory status
```

`~/.codex/archived_sessions` байгаа бол түүнийг мөн тусад нь `collect codex` командад өгнө. `collect` нь эхлээд локал архивт хадгална. Эвдэрсэн Codex/OpenCode JSON session-ийг бусдаас тусгаарлаж, дараагийн scan-д дахин оролдоно.

```bash
.venv/bin/ai-memory watch --codex-path ~/.codex/sessions --opencode-path ~/.local/share/opencode
```

Terminal-ийг нээлттэй байлгавал 30 секунд тутам scan хийж, зөвшөөрсөн өгөгдлийг default 12 цагийн давтамжтай sync хийнэ. Хүлээгдэж буй retry нь queue дахь хугацаагаа дагана. Google login/quota/import алдааг terminal дээр нууц утга гаргалгүй мэдээлнэ.

Terminal-гүй автоматаар асаахыг хүсвэл `config/ai-memory.service.example` болон `config/ai-memory-receiver.service.example` доторх executable, repository, **ижил archive**-ын абсолют замуудыг өөрийн замаар солино. Custom auth directory хэрэглэдэг бол service-д мөн ижил `NOTEBOOKLM_HOME` өгнө. Хоёр файлыг `~/.config/systemd/user/` руу `.service` нэрээр байрлуулаад foreground receiver/worker-ээ Ctrl+C-ээр зогсоосны дараа:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ai-memory.service ai-memory-receiver.service
journalctl --user -u ai-memory.service -u ai-memory-receiver.service -f
```

Эдгээр нь таны локал Linux-д суулгах жишээ; cloud agent таны user service-ийг автоматаар асаахгүй.

## 7. MCP болон хөгжүүлэлтийн тест

Байгаа MCP тохиргоондоо [Codex](../config/codex.toml.example) эсвэл [OpenCode](../config/opencode.json.example) жишээний зөвхөн memory server entry-г нэгтгэнэ. Executable/DB замыг солиод MCP холболтоо restart хийнэ. Agent-д `memory_status` болон өмнөх ярианы талаар `memory_ask` дуудаж шалгуулна. [Agent ашиглах заавар](agent-instructions.md)-ыг нэмнэ.

```bash
.venv/bin/pytest -q
for test_file in vendor/hstry/extension/test/*.js; do node "$test_file"; done
.venv/bin/ruff check src tests scripts
```

Optional browser UI шалгалт нь Playwright library болон таны суулгасан Brave/Chrome-ийг ашиглана; Google login хийхгүй, хувийн profile нээхгүй:

```bash
uv run --frozen --extra login --with playwright python scripts/extension_ui_check.py
```

Browser автоматаар олдохгүй бол `--browser-executable /бүтэн/зам/browser` нэмнэ. Зургууд `data/ui-check/`-д гарна. Энэ harness тэнцэх нь бодит account-ын private API болон installed-extension шалгалтыг орлохгүй.

Одоогоор Claude Code/Gemini CLI local adapter байхгүй. Gemini organized category, бодит Google quota, урт хугацааны login, Монгол retrieval чанарыг бүрэн батлагдсан гэж тэмдэглэхгүй. Гол V1 урсгалын regression болон суулгах алхмуудыг [validation](validation.md)-д бүртгэнэ.
