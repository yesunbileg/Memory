# Локалд шинэчлэх, extension холбох, тест хийх

Командуудыг repository-ийн хавтсанд ажиллуулна. Доорх жишээ `data/conversations.sqlite` архивыг ашиглана. Өөр архивтай бол бүх `ai-memory` командын өмнө `--db /бүтэн/зам/archive.sqlite` нэмнэ; өргөтгөл, Codex/OpenCode-ийн нэмэлт, MCP бүгд ижил архив ашиглах ёстой.

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

1. Chromium хөдөлгүүрийн 125 буюу түүнээс шинэ хувилбартай Brave дээр `brave://extensions/` нээнэ (`chrome://extensions/` мөн болно).
2. **Developer mode** асаана.
3. **Load unpacked** → repository доторх `vendor/hstry/extension` хавтсыг сонгоно.
4. Өмнө суулгасан бол **Reload** дарна. Settings дээрх хувилбар `vendor/hstry/extension/manifest.json` дахь `version`-тай таарах ёстой.

ZIP хэрэгтэй бол:

```bash
.venv/bin/python scripts/package_extension.py
```

Команд үүсгэсэн ZIP-ийн зам, хувилбар, SHA-256-г хэвлэнэ. Задалсан, `manifest.json` нь шууд дотроо байгаа хавтсыг **Load unpacked**-д сонгоно. Багцад icon, UI, provider код болон төслийн ба upstream MIT лиценз орно; token, connection/auth файл, тест болон хувийн архив орохгүй.

## 3. Компьютертэй холбох

```bash
.venv/bin/ai-memory --db data/conversations.sqlite connect --extension-id YOUR_EXTENSION_ID --browser brave
```

Linux дээр ажиллана. `YOUR_EXTENSION_ID`-ийн оронд Extensions хуудаст харуулсан 32 үсэгтэй ID-г оруулна. Хөтөчдөө тааруулж `brave`, `chrome` эсвэл `chromium` сонгоно. Settings → **Choose connection file** дээр командын хэвлэсэн `connection_file` замыг сонго. Хөтөч локал програмыг өөрөө асаана. Terminal-ийг нээлттэй байлгах шаардлагагүй. Хуучин bearer файлтай бол шинэ файл үүсгэж дахин холбоно. [Дэлгэрэнгүй](native-pairing.md).

[Сонголтоо хадгалах заавар](extension-setup.mn.md)-ын дагуу хажуугийн жагсаалтаас төсөл, дан яриагаа чагтална. **Add to sync list** дарж байж сонголт хадгалагдана. Дараа нь Settings дээр ChatGPT-ийг идэвхжүүлнэ. **Capture selected chats** бол тусдаа үйлдэл бөгөөд яриаг зөвхөн локал архивт цуглуулна.

## 4. Сонголт хадгалах, яриа цуглуулахыг тусад нь шалгах

Хувийн яриа ашиглахгүйгээр шалгах бол туршилтын бүртгэлд шинэ төсөл, дотор нь нэг яриа, хоёр бэхлээгүй дан яриа үүсгэнэ. Цэвэр туршилтын архив ашиглаж, хуваарьт цуглуулгыг унтраана. Төслөө болон дан ярианыхаа зөвхөн нэгийг чагтална.

| Шалгах үйлдэл | Хүлээгдэх үр дүн |
| --- | --- |
| Чагтлаад табаа шинэчлэх | Хадгалаагүй сонголт арилна. Архив өөрчлөгдөхгүй. |
| Дахин чагтлаад **Add to sync list** дарах | Төсөл, сонгосон дан яриа Settings-д хадгалагдана. Архивын ярианы тоо өөрчлөгдөхгүй. |
| **Capture selected chats** дарах | Төслийн яриа, сонгосон дан яриа архивт орно. Сонгоогүй дан яриа орохгүй. |
| Хадгалсан сонголтоо бүгдийг хасах | Дараагийн цуглуулга бүх чатыг авдаг болохгүй. Өмнө хадгалсан бичвэр архивт үлдэнэ. |

Архивын өөрчлөлтийг `ai-memory --db PATH status`, хүлээгдэж буй яриаг `ai-memory --db PATH pending` командаар шалгана. Аль ч алхамд NotebookLM-д илгээх шаардлагагүй.

## 5. NotebookLM-д өөрийн Brave-аар нэвтрэх

Brave-ийн ашиглах profile дээрээ NotebookLM-д нэвтэрсэн байна. Дараа нь:

```bash
.venv/bin/notebooklm login --browser-cookies brave
.venv/bin/ai-memory check
```

Chrome/Firefox бол `brave`-ийг `chrome`/`firefox` гэж солино. Profile сонгох бол `--browser-cookies 'brave::Profile 1'`; олон Google account-тай бол `--account YOUR_EMAIL` нэмнэ. Desktop keyring түгжээтэй байвал эхлээд өөрийн desktop session-д unlock хийнэ.

Өмнө `NOTEBOOKLM_HOME` эсвэл тусгай storage файл тохируулсан бол тэрийгээ хэрэглэнэ. Тусгай файлтай login жишээ: `notebooklm login --browser-cookies brave --storage /private/path/storage_state.json`. MCP болон sync хийхэд ижил нэвтрэлтийн тохиргоо ашиглана. Дахин нэвтрэхдээ шинэ notebook үүсгэх шаардлагагүй. Анхны notebook тохиргоо бол [README](../README.md#connect-notebooklm)-ийн `init --notebook-id` алхмыг ашиглана.

Хиймэл өгөгдлөөр туршихдаа хоосон notebook зориуд үүсгэж ID-г нь ав. Локал гаралтын хавтас мөн хоосон байна. Нэр нь таарсан notebook эсвэл хуучин архив ашиглахгүй.

```bash
.venv/bin/python scripts/live_demo.py --notebook-id YOUR_EMPTY_DEMO_NOTEBOOK_ID --output data/new-live-demo
```

`live_notebooklm_test: executed_review_answer` гарвал хүсэлт гүйцэтгэсэн гэсэн үг. Хариултын SQLite/NotebookLM шийдвэрүүд source-той таарч байгааг шалгана. Free-form хариулт доторх conversation ID-г дангаар нь баталгаатай гэж үзэхгүй; structured source/segment reference болон эх архивтай тулгана.

`pending` хүлээгдэж буй яриаг жагсаана. `preview ID` нь илгээх масктай текстийг локалд харуулна. `approve ID...` нь тухайн ярианы ирээдүйн үргэлжлэлийг мөн зөвшөөрнө. `revoke ID...` цаашдын илгээлтийг зогсооно, Google дээрх хуучин хуулбарыг устгахгүй. [Зөвшөөрөл, нөөцлөлт ба сэргээх](consent-backup.md).

Өөрийн жинхэнэ ярианаас Google руу дамжуулахыг зөвшөөрсөн ID-уудыг л `ai-memory approve ID1 ID2` командад өгч, дараа нь `ai-memory sync` ажиллуулна. Web capture нь Google upload зөвшөөрлийг автоматаар өгөхгүй.

Upload хийх хувилбарт танигдсан API key, password/token, cookie, JWT болон private key-ийн нууц утгыг урттай нь тэнцүү тооны `*` тэмдэгтээр солино. Нэр, хашилт, бусад текст, мөрийн шилжилт хадгалагдана. SQLite болон локал Markdown export эх текстээ хадгална; архив бүрийн `ARCHIVE.sqlite.files/sync/` хавтас дахь cloud snapshot масктай байна. Өөр архивтай хавтас хуваалцахгүй. Таних дүрэмд хамрагдаагүй нууц байж болох тул зөвшөөрөл өгөхөөс өмнө эх яриагаа шалгана.

Google дээр өмнө хадгалсан source алга болбол автоматаар дахин илгээхгүй, retrieval-д ашиглахгүй. Дахин илгээхээр шийдвэл `ai-memory republish SEGMENT_ID` командаар тухайн segment-ийг зааж дараа нь `ai-memory sync` ажиллуулна.

## 6. Codex ба OpenCode-ийн локал нэмэлт

[Суулгах заавар, командууд](local-hooks.md)-ыг Codex эсвэл OpenCode ажилладаг компьютер дээрээ гүйцэтгэнэ. Codex-ийн **нэг локал нэмэлт** нь яриа хадгалах `Stop` үйлдэл болон memory MCP серверийг хамтад нь суулгана. Codex дотор `/hooks` нээж тухайн үйлдлийг шалгаад зөвшөөрнө. OpenCode-д өөрийн SQLite санг зааж тусдаа нэмэлт суулгана. Энэ нь `session.idle` үйл явдлаар зөвхөн тухайн яриаг уншина.

Туршилтын нэг богино яриаг дуусгаад ижил архив дээр `ai-memory pending` ажиллуулж шалгана. Хуучин яриаг нэг удаа оруулах бол:

```bash
.venv/bin/ai-memory collect codex ~/.codex/sessions
.venv/bin/ai-memory collect opencode ~/.local/share/opencode
.venv/bin/ai-memory status
```

`~/.codex/archived_sessions` дотор хэрэгтэй яриа байвал түүнийг тусад нь `collect codex` командад өгнө. Аль ч локал нэмэлт Google-д илгээх зөвшөөрөл шинээр олгохгүй.

## 7. MCP болон хөгжүүлэлтийн тест

Codex-д нэмэлтийн хамт ирсэн MCP серверийг ашиглана. OpenCode-ийн байгаа тохиргоонд [OpenCode 2](../config/opencode-v2.json.example), эсвэл [хуучин OpenCode](../config/opencode.json.example) жишээний зөвхөн memory server entry-г нэгтгэнэ. OpenCode 2 нь `mcp.servers`, хуучин client нь `mcp` map ашиглана. Executable/DB замыг солиод MCP холболтоо restart хийнэ. Agent-д `memory_status` болон өмнөх ярианы талаар `memory_ask` дуудаж шалгуулна. [Agent ашиглах заавар](agent-instructions.md)-ыг нэмнэ.

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
