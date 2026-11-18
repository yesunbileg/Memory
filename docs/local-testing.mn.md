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
4. Өмнө суулгасан бол **Reload** дарна. Settings дээр **0.6.1** харагдана.

ZIP хэрэгтэй бол:

```bash
.venv/bin/python scripts/package_extension.py
```

`dist/ai-memory-extension-0.6.1.zip` үүснэ. Задалсан, `manifest.json` нь шууд дотроо байгаа хавтсыг **Load unpacked**-д сонгоно. Багцад icon, UI, provider код болон төслийн ба upstream MIT лиценз орно; token, connection/auth файл, тест болон хувийн архив орохгүй.

## 3. Компьютертэй холбох

```bash
.venv/bin/ai-memory --db data/conversations.sqlite connect --extension-id YOUR_EXTENSION_ID --browser brave
```

Linux дээр ажиллана. `YOUR_EXTENSION_ID`-ийн оронд Extensions хуудаст харуулсан 32 үсэгтэй ID-г оруулна. Хөтөчдөө тааруулж `brave`, `chrome` эсвэл `chromium` сонгоно. Settings → **Choose connection file** дээр командын хэвлэсэн `connection_file` замыг сонго. Хөтөч локал програмыг өөрөө асаана. Terminal-ийг нээлттэй байлгах шаардлагагүй. Хуучин bearer файлтай бол шинэ файл үүсгэж дахин холбоно. [Дэлгэрэнгүй](native-pairing.md).

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

Хиймэл өгөгдлөөр туршихдаа хоосон notebook зориуд үүсгэж ID-г нь ав. Локал гаралтын хавтас мөн хоосон байна. Нэр нь таарсан notebook эсвэл хуучин архив ашиглахгүй.

```bash
.venv/bin/python scripts/live_demo.py --notebook-id YOUR_EMPTY_DEMO_NOTEBOOK_ID --output data/new-live-demo
```

`live_notebooklm_test: executed_review_answer` гарвал хүсэлт гүйцэтгэсэн гэсэн үг. Хариултын SQLite/NotebookLM шийдвэрүүд source-той таарч байгааг шалгана. Free-form хариулт доторх conversation ID-г дангаар нь баталгаатай гэж үзэхгүй; structured source/segment reference болон эх архивтай тулгана.

`pending` хүлээгдэж буй яриаг жагсаана. `preview ID` нь илгээх масктай текстийг локалд харуулна. `approve ID...` нь тухайн ярианы ирээдүйн үргэлжлэлийг мөн зөвшөөрнө. `revoke ID...` цаашдын илгээлтийг зогсооно, Google дээрх хуучин хуулбарыг устгахгүй. [Зөвшөөрөл, нөөцлөлт ба сэргээх](consent-backup.md).

Өөрийн жинхэнэ ярианаас Google руу дамжуулахыг зөвшөөрсөн ID-уудыг л `ai-memory approve ID1 ID2` командад өгч, дараа нь `ai-memory sync` ажиллуулна. Web capture нь Google upload зөвшөөрлийг автоматаар өгөхгүй.

Upload хийх хувилбарт танигдсан API key, password/token, cookie, JWT болон private key-ийн нууц утгыг урттай нь тэнцүү тооны `*` тэмдэгтээр солино. Нэр, хашилт, бусад текст, мөрийн шилжилт хадгалагдана. SQLite болон локал Markdown export эх текстээ хадгална; архив бүрийн `ARCHIVE.sqlite.files/sync/` хавтас дахь cloud snapshot масктай байна. Өөр архивтай хавтас хуваалцахгүй. Таних дүрэмд хамрагдаагүй нууц байж болох тул зөвшөөрөл өгөхөөс өмнө эх яриагаа шалгана.

Google дээр өмнө хадгалсан source алга болбол автоматаар дахин илгээхгүй, retrieval-д ашиглахгүй. Дахин илгээхээр шийдвэл `ai-memory republish SEGMENT_ID` командаар тухайн segment-ийг зааж дараа нь `ai-memory sync` ажиллуулна.

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

Terminal-ийг нээлттэй байлгавал 30 секунд тутам scan хийж, зөвшөөрсөн өгөгдлийг default 12 цагийн давтамжтай sync хийнэ. Удаан remote шалгалт локал scan-ийг зогсоохгүй. `status`-ын `watch` хэсгээс амжилттай уншсан сүүлийн хугацаа, дараагийн scan, remote ажил болон алдааг шалгана. Хүлээгдэж буй retry нь queue дахь хугацаагаа дагана. Google login/quota/import алдааг terminal дээр нууц утга гаргалгүй мэдээлнэ.

Локал цуглуулагчийг арын горимоор асаах бол `config/ai-memory.service.example`-д executable, repository болон архивын абсолют замаа тохируулна. Үүнийг `~/.config/systemd/user/ai-memory.service` болгон хадгалаад ажиллуулна. Browser өргөтгөлийн шинэ холболтод тусдаа HTTP receiver үйлчилгээ хэрэггүй.

```bash
systemctl --user daemon-reload
systemctl --user enable --now ai-memory.service
journalctl --user -u ai-memory.service -f
```

## 7. MCP болон хөгжүүлэлтийн тест

Байгаа MCP тохиргоондоо [Codex](../config/codex.toml.example), [OpenCode 2](../config/opencode-v2.json.example), эсвэл [хуучин OpenCode](../config/opencode.json.example) жишээний зөвхөн memory server entry-г нэгтгэнэ. OpenCode 2 нь `mcp.servers`, хуучин client нь `mcp` map ашиглана. Executable/DB замыг солиод MCP холболтоо restart хийнэ. Agent-д `memory_status` болон өмнөх ярианы талаар `memory_ask` дуудаж шалгуулна. [Agent ашиглах заавар](agent-instructions.md)-ыг нэмнэ.

```bash
.venv/bin/python scripts/check.py
```

Энэ нэг команд Python lint/format, бүх Python тест, extension-ийн бүх Node тестийг ажиллуулна. Код засах үед хэрэгтэй formatter болон project-ийн файлуудын үүргийг [development](development.md)-д тайлбарласан. Prettier нь зөвхөн хөгжүүлэлтийн npm dependency; app ажиллуулахад npm install шаардлагагүй.

Optional browser UI шалгалт нь Playwright library болон таны суулгасан Brave/Chrome-ийг ашиглана; Google login хийхгүй, хувийн profile нээхгүй:

```bash
.venv/bin/python scripts/extension_ui_check.py --browser-executable /usr/bin/chromium
xvfb-run -a .venv/bin/python scripts/extension_toolbar_check.py --browser-executable /usr/bin/chromium
```

Browser автоматаар олдохгүй бол `--browser-executable /бүтэн/зам/browser` нэмнэ. Зургууд `data/ui-check/`-д гарна. Эхний тест хөтчийн API-г дуурайлган хуудсуудыг шалгана. Хоёр дахь нь тусгаарласан profile-д өргөтгөл суулгаж toolbar-ийг шалгана. Аль аль нь бодит account-ын API-г шалгахгүй.

Одоогоор Claude Code/Gemini CLI local adapter байхгүй. Gemini organized category, бодит Google quota, урт хугацааны login, Монгол retrieval чанарыг бүрэн батлагдсан гэж тэмдэглэхгүй. Гол V1 урсгалын regression болон суулгах алхмуудыг [validation](validation.md)-д бүртгэнэ.
