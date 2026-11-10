# AI Memory System — Эцсийн архитектур ба хөгжүүлэлтийн төлөвлөгөө

Хоёр төлөвлөгөөний санааг нэгтгэж, баталгаажуулсан бүх шийдвэрийг тусгасан бүрэн хувилбар.

## 1. Системийн зорилго

Codex, OpenCode, Claude Code, ChatGPT болон бусад AI агентын яриаг автоматаар цуглуулж, NotebookLM + MCP ашиглан төвлөрсөн санах ойн систем бүтээнэ.

Үндсэн шаардлага:

- Бүрэн үнэгүй үйлчилгээ ашиглана.
- Төхөөрөмжийн нөөцийн хэрэглээ бага байна.
- NotebookLM нь цорын ганц AI Memory Engine байна.
- Бүх яриаг эх текстээр нь 1:1 хадгална.
- Local SQLite нь ярианы архив, системийн төлөвийг хадгална.
- Бүх source Markdown `.md` форматтай байна.
- NotebookLM-д Claude-ийн Segment архитектурыг ашиглана.
- Агентууд MCP-ээр дамжуулан шаардлагатай санах ойгоо авна.
- Шаардлагагүй код, feature, dependency нэмэхгүй.
- Өмнө нь шийдэгдсэн асуудлыг шинээр бүтээхгүй. Бэлэн repository-г судалж, хэрэгтэй хэсгийг ашиглана.

## 2. Баталгаажсан шийдвэрүүд

| Хэсэг                  | Шийдвэр                           |
| ---------------------- | --------------------------------- |
| AI Memory              | NotebookLM                        |
| Notebook               | Нэг үндсэн notebook               |
| Local archive          | SQLite                            |
| Source format          | Markdown + YAML                   |
| Source бүтэц           | Segment                           |
| Conversation collector | hstry                             |
| NotebookLM integration | notebooklm-py                     |
| Agent integration      | MCP                               |
| Web collector          | Browser extension                 |
| Main / Medium Memory   | Байхгүй                           |
| INDEX source           | Байхгүй                           |
| Local Smart Memory     | Байхгүй                           |
| Local LLM / Vector DB  | Байхгүй                           |
| GUI Dashboard          | Одоохондоо байхгүй                |
| Model metadata         | YAML дахь `models` жагсаалт       |
| Хураангуйлах           | Бүх Segment дүүрэх хүртэл хийхгүй |

## 3. Системийн бүтэн архитектур


Систем хоёр үндсэн урсгалтай.

WRITE — яриа хадгалах:

AI яриа → hstry → Filter → SQLite → MD Segment → NotebookLM.

READ — санах ой сэргээх:

AI Agent → MCP → NotebookLM → Эх сурвалжтай хариулт → AI Agent.

Агент өөрөө яриаг архивлах, source үүсгэх, notebook сонгох шаардлагагүй. Эдгээрийг систем автоматаар хариуцна.

## 4. Donor repository

Шинээр бүх системийг зохиохгүй. Өмнөх судалгааны хоёр үндсэн төслийг ашиглана.

[Is GitHub down? | GitHub outage details](https://images.openai.com/static-rsc-4/hEjBv6NLFi5BCSMZCz6rZIMFrsm3_FwUsnS_pI2QblAm3ssTh34zpyJtzbWtuoDJBwhdYq10KU2iLy3QatHKwVIgA5PAk9P-9TNysS2O75NXKh5MsujRry3R3WTEYYyAMbe24FmmLkm2P1NO_PPG-U9ATm9C0U20eN3aP3by9s4?purpose=inline)

[hstry](https://github.com/byteowlz/hstry?utm_source=chatgpt.com)

Нэгдсэн conversation collector. Local agent adapter, web extension, SQLite, incremental sync зэрэг хэрэгтэй хэсгийг ашиглана.

| Авах                   | Авахгүй                       |
| ---------------------- | ----------------------------- |
| Local agent adapters   | Smart memory                  |
| Browser extension      | mmry integration              |
| Conversation parser    | Semantic memory search        |
| SQLite core            | Embedding                     |
| Incremental collection | Cross-agent resume            |
| Web ingest API         | Илүүдэл TUI болон CLI feature |

[Google Developer Groups GDG on Campus Goethe University - Frankfurt, Germany](https://images.openai.com/static-rsc-4/anPKpzLUjz2c5Z3HwDQvW8OtDC793HEgznFWUOtx6_uSQGvergucKE3n6MMEMNQxW3qtUYJ_3fz-t1WR5moJb_Kmpwwzqr2G7P9fjDg-zajleOSjzCuyXgrraz0ZJxoT9TIUUadH1PdPyeY9zaaWEFJOMSKXYrYz9LthHfd9HXo?purpose=inline)

[notebooklm-py](https://github.com/teng-lin/notebooklm-py?utm_source=chatgpt.com)

NotebookLM-ийн authentication, source удирдлага, upload болон query хийх боломжийг ашиглана.

| Авах                   | Авахгүй          |
| ---------------------- | ---------------- |
| Google authentication  | Audio generation |
| Notebook management    | Video generation |
| MD source upload       | Flashcards       |
| Source status / delete | Quiz             |
| Notebook query         | Slides           |
| Authentication refresh | Deep Research    |

Дүрэм: Donor төслийг сохроор бүхэлд нь хуулбарлахгүй. Хэрэгтэй хэсгийг эх код, dependency, лицензийн аудитаар тодорхойлж ашиглана. Одоо байгаа найдвартай ажиллагааг эвдэх хэмжээнд dependency хасахгүй.

## 5. Conversation Collector

### Дэмжих платформ

| Platform    | Цуглуулах арга    |
| ----------- | ----------------- |
| Codex       | Local adapter     |
| OpenCode    | Local adapter     |
| Claude Code | Local adapter     |
| Gemini CLI  | Local adapter     |
| ChatGPT Web | Browser extension |
| Claude Web  | Browser extension |
| Gemini Web  | Browser extension |

V1-ийн хамгийн түрүүнд баталгаажуулах агентууд нь Codex болон OpenCode байна. Дараа нь бусад collector-ийг холбоно.

### Цуглуулах ажиллагаа

1. Анхны тохиргоонд төхөөрөмж дээр байгаа хуучин яриануудыг импортлоно.
2. Шинэ session болон мессежийг incremental scan эсвэл file watch ашиглан танина.
3. Web яриаг browser extension local ingest API руу дамжуулна.
4. Бүх платформын өгөгдлийг нэгдсэн стандартад оруулна.
5. Зөвхөн шаардлагатай мэдээллийг SQLite-д бичнэ.

Browser нь web яриа цуглуулах үед шаардлагатай. Local collector, NotebookLM sync болон MCP ажиллагаанд байнгын browser ажиллуулахгүй.

## 6. Text-only Filter

Collector-ийн гол үүрэг нь яриаг өөрчлөхгүйгээр хэрэггүй event-үүдийг хасах юм.

| Хадгалах ✅                          | Хадгалахгүй ❌             |
| ----------------------------------- | ------------------------- |
| User текст                          | Tool calls                |
| Assistant текст                     | Tool output               |
| Хэрэглэгчид харагдсан Thinking      | Нууц дотоод reasoning     |
| Хэрэглэгчид харагдсан Progress      | Terminal execution log    |
| AI өөрөө харуулсан Thinking Summary | Raw code diff             |
| Assistant-ийн хариултад бичсэн код  | Raw file content          |
| Platform                            | System prompt             |
| `models`                            | Token usage               |
| Conversation ID                     | Дотоод event              |
| Project                             | Compaction event          |
| Source URL                          | Хавсралтын binary өгөгдөл |
| Огноо, цаг, мессежийн дараалал      | Хэрэггүй UI metadata      |

### Хатуу дүрэм

- Эх текстийг 1:1 хадгална.
- Хураангуйлахгүй.
- Орчуулахгүй.
- Дахин найруулахгүй.
- LLM ашиглан текст боловсруулахгүй.
- Байхгүй мэдээлэл зохиож нэмэхгүй.

Thinking Summary гэдэг нь манай систем үүсгэх хураангуй биш. AI тухайн үед хэрэглэгчид харуулсан текстийг эхээр нь хадгална гэсэн үг.

UI-ийн хэрэггүй тэмдэглэгээг цэвэрлэхдээ хэрэглэгчид харагдсан агуулгыг алдагдуулахгүй.

## 7. Local SQLite

SQLite бол үндсэн эх архив (Source of Truth) юм. AI Memory Engine биш.

```
AI-Memory/
└── data/
    └── conversations.sqlite
```

### Database бүтэц

| Table              | Хадгалах мэдээлэл                                        |
| ------------------ | -------------------------------------------------------- |
| `conversations`    | ID, гарчиг, platform, project, models, огноо, source URL |
| `messages`         | Ярианы ID, role, төрөл, эх текст, timestamp, sequence    |
| `segments`         | Segment ID, хэмжээ, open/sealed төлөв, revision          |
| `segment_messages` | Аль мессеж аль Segment-д багтсан                         |
| `source_map`       | Segment ID ↔ NotebookLM source ID                        |
| `sync_jobs`        | Upload, replace, retry, error төлөв                      |
| `checkpoints`      | Collector-ийн хамгийн сүүлд боловсруулсан байрлал        |
| `settings`         | Notebook ID, account, collector болон sync тохиргоо      |

Энэ нь санал болгож буй логик schema. Donor audit хийсний дараа hstry-ийн одоо байгаа table-уудыг ашиглаж, шаардлагатай хэсгийг л нэмнэ.

SQLite-д хийхгүй зүйл: AI memory search, embedding, vector search, мэдлэг ялгаж авах, автомат хураангуй үүсгэх.

Тусдаа байнгын Markdown conversation архив үүсгэхгүй. Segment MD файлуудыг SQLite-аас шаардлагатай үед автоматаар үүсгэнэ.

## 8. NotebookLM — Segment архитектур

Нэг яриа бүрийг тусдаа source болгохгүй. Олон яриаг хугацааны дарааллаар нэг том MD source-д багцална.

## NotebookLM

Нэг notebook

SEG-001.md

Эхний хугацааны ярианууд

Sealed

SEG-002.md

Дараагийн хугацааны ярианууд

Sealed

SEG-003.md

Одоогийн шинэ ярианууд

Open

Нөөц source байр

Шинэчлэх, нэгтгэхэд ашиглана

### Source байрны хуваарилалт

| Source | Зориулалт          |
| ------ | ------------------ |
| 1–49   | Ярианы Segment-үүд |
| 50     | Нөөц байр          |

Main Memory, Medium Memory, INDEX байхгүй.

### Segment-ийн үндсэн дүрэм

- Зөвхөн хамгийн сүүлийн Segment нээлттэй байна.
- Шинэ мессежүүд нээлттэй Segment-д орно.
- Нээлттэй Segment багтаамждаа хүрвэл битүүмжилж, шинэ Segment эхлүүлнэ.
- Өмнөх Segment-үүд ердийн sync үед өөрчлөгдөхгүй.
- Хуучин яриаг үргэлжлүүлбэл шинэ мессежийг одоогийн нээлттэй Segment-д оруулна.
- Ярианы ID болон мессежийн дугаар хэвээр үлдэнэ.

Ингэснээр төсөл бүрээр notebook үүсгэхгүй, Write Router болон Read Router шаардлагагүй болно.

## 9. Эцсийн Markdown source format

Файлын нэр: `SEG-001.md`

Нэг Segment дотор олон платформын олон яриа байна. Яриа бүр өөрийн metadata, ID, мессежийн дараалалтай.

````

---
schema: 1
segment_id: SEG-001
created_at: "2026-11-10T14:35:00+08:00"
timezone: Asia/Ulaanbaatar
---

# SEG-001

## CX-0412 — Fix OmniMCP bugs

```yaml
conversation_id: abc123
platform: Codex
models:
  - gpt-6
project: OmniMCP
created_at: "2026-11-10T14:35:00+08:00"
last_message_at: "2026-11-10T14:42:00+08:00"
source_url: null
```

### CX-0412 · 0001 · User · 2026-11-10 14:35:00

OmniMCP дээрх дөрвөн алдааг зас.

### CX-0412 · 0002 · Assistant · Progress · 2026-11-10 14:35:05

Одоогийн код болон regression test-үүдийг шалгаж байна.

### CX-0412 · 0003 · Assistant · Thinking Summary · 2026-11-10 14:36:10

Координатын rounding болон focus snapshot дээр
үндсэн асуудал байна.

### CX-0412 · 0004 · Assistant · 2026-11-10 14:42:00

Дөрвөн алдааг заслаа.
Бүх тест амжилттай тэнцсэн.

---

## OC-0021 — Linux configuration

```yaml
conversation_id: def456
platform: OpenCode
models:
  - claude-sonnet
project: Linux System
created_at: "2026-11-10T16:20:00+08:00"
```

### OC-0021 · 0001 · User · 2026-11-10 16:20:00

Hyprland-ийн тохиргоог шалга.

````

Энэ бол зохиомол өгөгдөлтэй форматын жишээ.

Чухал: Мессеж бүрийн гарчигт ярианы ID, дараалал, бүтэн огноо болон role байна. NotebookLM том source-ийн дундаас мэдээлэл сэргээх үед тухайн мессеж аль ярианых болохыг таних боломжтой.

`models` нь жагсаалт байна. Нэг ярианд ашигласан хэд хэдэн model-ийг хадгалах боломжтой. Model-ийн нэр эх сурвалжид байхгүй бол таамаглаж бичихгүй.

## 10. Segment lifecycle

### А. Шинэ яриа нэмэгдэх

```
SQLite
  │
  ├── Conversation A
  ├── Conversation B
  └── Conversation C
          │
          ▼
      SEG-001.md
          │
          ▼
      NotebookLM
```

SQLite-д шинэ мессеж нэмэгдэнэ. Segment Builder шинэ өгөгдлийг авч нээлттэй Segment-ийн шинэ хувилбарыг үүсгэнэ.

### Б. Segment дүүрэх

```
SEG-001 → Sealed
SEG-002 → Open
```

Claude-ийн санал болгосон эхний туршилтын хэмжээ нь 150,000 үг.

Энэ нь эцсийн баталгаатай хэмжээ биш. NotebookLM-ийн upload хурд, retrieval чанар, Монгол текстийн боловсруулалт зэргийг хэмжиж байж тохирох хэмжээг сонгоно.

### В. Хуучин яриа үргэлжлэх

Жишээ нь:

```
SEG-001
  CX-0412 · Message 0001–0050

SEG-006
  CX-0412 · Message 0051–0080
```

Хуучин яриа дахин үргэлжилсэн ч өмнөх 50 мессежийг дахин upload хийхгүй. Зөвхөн шинэ мессежүүдийг одоогийн Segment-д нэмнэ.

### Г. Өмнөх мессеж өөрчлөгдөх

Хэрэглэгч хуучин мессежээ засварласан, AI хариултаа дахин үүсгэсэн бол энгийн append хангалтгүй.

SQLite-д revision-ийг зөв бүртгэж, өөрчлөгдсөн яриаг агуулсан Segment-ийг дахин үүсгэнэ. Энэ нь битүүмжлэгдсэн Segment-д өөрчлөлт оруулах зөвшөөрөгдсөн онцгой нөхцөл юм.

## 11. Sync Engine

Sync хийхдээ AI ашиглахгүй. Script эсвэл хөнгөн worker ажиллуулна.

### Source шинэчлэх дараалал


Эхлээд шинэ source оруулж, амжилттай боловсруулагдсаныг шалгасны дараа хуучин source-ийг устгана.

### Sync-ийн дүрэм

- Мессеж бүрд NotebookLM рүү upload хийхгүй.
- Өөрчлөгдсөн өгөгдлийг багцлан sync хийнэ.
- Эхний туршилтаар session дуусахад эсвэл өдөрт 1–2 удаа sync хийх хувилбарыг ашиглаж болно.
- Өөрчлөлтгүй Segment-ийг дахин upload хийхгүй.
- NotebookLM API-ийн quota болон алдааг хүндэтгэнэ.
- Internet тасарвал ажил SQLite queue-д үлдэнэ.
- Restart хийсэн ч өмнөх sync үргэлжилнэ.

Анхаарах зүйл: `.md` upload, source replacement болон processing status-ийн бодит API ажиллагааг туршилтаар баталгаажуулна. Шууд append эсвэл in-place update боломжтой гэж урьдчилж үзэхгүй.

## 12. Overflow — NotebookLM дүүрэх үед

Өмнөх архитектурын нэг яриа = нэг source загварыг ашиглахгүй.

### Эхний үе

```
SEG-001 · 150k үг
SEG-002 · 150k үг
...
SEG-049 · 150k үг
Нөөц байр · сул
```

### Source байр дүүрэхэд

Хамгийн хуучин, зэрэгцээ Segment-үүдийг багтаамжийн хязгаарт багтаан нэгтгэнэ.

```
ӨМНӨ

SEG-001 · 150k
SEG-002 · 150k
SEG-003 · 150k

ДАРАА

SEG-050 · 450k
```

Энэ жишээнд хоёр source байр чөлөөлөгдөнө.

- Эх текст хураангуйлагдахгүй.
- Ярианы ID өөрчлөгдөхгүй.
- Мессежийн дараалал хадгалагдана.
- SQLite дахь эх архив хэвээр үлдэнэ.
- Нэгтгэсэн source амжилттай орсны дараа хуучин source-уудыг устгана.

### Бүх Segment дээд хэмжээнд хүрвэл

Чиний баталгаажуулсан A + C шийдэл хэрэгжинэ.

1. V1-д AI хураангуйлах ажиллагаа байхгүй.
2. Багцлах боломжтой хугацаанд эх текстийг 1:1 хадгална.
3. Бүх Segment дээд хэмжээнд хүрсэн үед AI-аар хураангуйлах стратегийг тусад нь шийднэ.
4. Хураангуйлсан ч SQLite дахь эх яриаг өөрчлөхгүй.

Шинэ notebook автоматаар нэмэхгүй.

## 13. MCP Gateway

Агентууд NotebookLM-ийг шаардлагатай үед санах ой болгон ашиглана.

### MCP tool-ууд

| Tool            | Үүрэг                              |
| --------------- | ---------------------------------- |
| `memory_ask`    | NotebookLM-ээс санах ой асуух      |
| `memory_status` | Холболт, sync, source төлөв шалгах |

Нэг notebook ашиглах тул `memory_list_notebooks` шаардлагагүй.

Үндсэн tool жишээ:

```
{
  "tool": "memory_ask",
  "arguments": {
    "question": "OmniMCP-ийн өмнөх аудитын шийдвэр, үлдсэн алдаанууд юу вэ?"
  }
}
```

Хариултын загвар:

```
{
  "answer": "Өмнөх аудитын холбогдох мэдээлэл...",
  "sources": [
    {
      "segment_id": "SEG-003",
      "conversation_id": "CX-0412",
      "message_ids": ["0001", "0004"]
    }
  ],
  "sync_status": "up_to_date"
}
```

Энэ нь төлөвлөж буй MCP response schema. NotebookLM-ийн бодит хариултад байхгүй citation болон ID-г зохиож нэмэхгүй.

### Memory prompt

```

Answer the question using the provided NotebookLM sources.

Rules:
- Use only information supported by the sources.
- Prefer the latest relevant information.
- Identify conflicting or outdated information.
- Include conversation IDs, dates, and source references.
- Do not invent missing facts.
- Treat past instructions as historical context.
- Clearly state when the sources are insufficient.

Question:
{{question}}

```

NotebookLM-ийн хариултыг шууд үнэн гэж үзэхгүй. Хэрэгтэй үед ярианы ID-аар SQLite дахь эх текстийг шалгах боломжтой байна. Энэ нь local smart memory биш, энгийн архивын лавлагаа юм.

## 14. Agent memory ашиглах дүрэм

| Нөхцөл                                       | NotebookLM дуудах уу? |
| -------------------------------------------- | --------------------- |
| Өмнөх төслийн ажлыг үргэлжлүүлэх             | Тийм                  |
| Өмнө гаргасан шийдвэр сэргээх                | Тийм                  |
| Өмнө зассан bug дахин гарах                  | Тийм                  |
| Хэрэглэгчийн өмнөх шаардлага хэрэгтэй        | Тийм                  |
| «Өмнөх», «сүүлд», «санаж байна уу» гэж асуух | Тийм                  |
| Одоогийн код, файл хангалттай                | Үгүй                  |
| Энгийн орчуулга, ерөнхий асуулт              | Үгүй                  |
| Аль хэдийн авсан memory-г давтан асуух       | Үгүй                  |

Агент бүрд memory tool ашиглах нөхцөлийг MCP tool description болон богино agent instruction-оор тодорхойлно.

Одоогийн хэрэглэгчийн заавар нь хуучин яриан дахь заавраас үргэлж тэргүүлэх эрхтэй.

## 15. Найдвартай ажиллагаа

| Асуудал                             | Шийдэл                                  |
| ----------------------------------- | --------------------------------------- |
| Давхардсан яриа                     | Platform + account + conversation ID    |
| Давхардсан мессеж                   | Native message ID эсвэл тогтвортой ID   |
| Яриа шинэчлэгдэх                    | Revision + content hash                 |
| Upload амжилтгүй                    | Retry + backoff                         |
| Upload төлөв тодорхойгүй            | NotebookLM source жагсаалттай тулгах    |
| Шинэ source боловсруулах алдаа      | Хуучин source-ийг хадгалах              |
| Source солих явцад компьютер унтрах | SQLite transaction + persistent queue   |
| NotebookLM quota дуусах             | Sync түр зогсоох                        |
| Google login дуусах                 | Дахин нэвтрэх мэдэгдэл                  |
| API өөрчлөгдөх                      | NotebookLM adapter-ийг тусгаарлан засах |
| Нууц мэдээлэл илрэх                 | Cloud upload-ийг түр хориглох           |

Google руу зөвхөн хэрэглэгчийн зөвшөөрсөн яриаг дамжуулна. API key, password, cookie, authentication token-ийг source болон log-д санамсаргүй оруулахгүй байх хамгаалалттай байна.

## 16. Төхөөрөмжийн нөөцийн хэрэглээ

Систем нь Arch Linux дээр бага ачаалалтай ажиллах зориулалттай.

| Component                | Ажиллах хэлбэр                |
| ------------------------ | ----------------------------- |
| Collector                | File watch / incremental scan |
| Database                 | SQLite                        |
| Web collector            | Browser extension             |
| Segment Builder          | Өгөгдөл өөрчлөгдөхөд ажиллана |
| Sync Engine              | Нэг хөнгөн worker             |
| MCP                      | Агент хүсэлт өгөхөд ажиллана  |
| Local LLM                | Байхгүй                       |
| Vector database          | Байхгүй                       |
| Байнгын headless browser | Байхгүй                       |
| Docker / Kubernetes      | Шаардлагагүй                  |
| Dashboard                | Одоохондоо байхгүй            |

Үндсэн төхөөрөмжийн ажилд саад болохгүй. Төхөөрөмжийн ачаалал өндөр үед нэмэлт sync ажиллагааг түр хойшлуулах боломжтой байна.

CPU, RAM болон storage-ийн бодит хэрэглээг хөгжүүлэлтийн дараа benchmark хийж хэмжинэ.

## 17. Repository бүтэц

```
ai-memory/
├── collector/
│   ├── adapters/
│   ├── web-extension/
│   └── ingest/
│
├── core/
│   ├── filter/
│   ├── database/
│   └── segments/
│
├── notebooklm/
│   ├── auth/
│   ├── client/
│   └── sync/
│
├── mcp/
│   └── memory-gateway/
│
├── service/
├── config/
├── tests/
├── data/
│   └── conversations.sqlite
├── README.md
└── LICENSE
```

Энэ бол логик бүтэц. Бодит repo үүсгэхдээ hstry-ийн одоо байгаа зохион байгуулалтыг хүндэтгэж, шаардлагагүй давхар module үүсгэхгүй.

Технологийн чиглэл:

- hstry-ийн одоо байгаа Rust / TypeScript хэсгийг боломжтой хэмжээнд хэвээр ашиглана.
- notebooklm-py-ийн Python integration-ийг дахин бичихгүй.
- Хооронд нь жижиг, тодорхой interface-ээр холбоно.

## 18. Хөгжүүлэлтийн төлөвлөгөө

1. Phase 0 — Donor Audit

   hstry болон notebooklm-py-ийн эх код, dependency, лиценз, одоо байгаа feature-үүдийг шалгана. Аль хэсгийг дахин ашиглах, аль хэсэгт өөрчлөлт хэрэгтэйг тогтооно.
2. Phase 1 — Collector Core

   Codex, OpenCode-ийн яриаг цуглуулах, filter хийх, SQLite-д хадгалах ажиллагааг бүтээнэ. Эх текст, цаг, дарааллыг шалгана.
3. Phase 2 — Web Capture

   hstry browser extension болон local ingest API-г холбоно. ChatGPT, Claude, Gemini-ийн яриаг цуглуулах боломжийг туршина.
4. Phase 3 — MD Segment Builder

   SQLite-аас Markdown + YAML source үүсгэнэ. Conversation ID, models, timestamp, sequence, continuation block-ийн стандартыг баталгаажуулна.
5. Phase 4 — NotebookLM Integration

   Authentication, нэг notebook, MD upload, processing status, source delete болон query ажиллагааг бодитоор шалгана.
6. Phase 5 — Segment Sync

   Open / Sealed lifecycle, source replacement, persistent queue, retry, duplicate prevention ажиллагааг хэрэгжүүлнэ.
7. Phase 6 — Overflow

   Source байр дүүрэхэд Segment нэгтгэх, нөөц байр ашиглах, эх текст алдагдахгүй байх ажиллагааг шалгана.
8. Phase 7 — MCP Integration

   `memory_ask`, `memory_status` tool болон Codex, OpenCode-ийн agent instruction-ийг холбоно.
9. Phase 8 — Testing & Audit

   Бүх урсгалыг end-to-end туршина. Алдаа, CPU/RAM хэрэглээ, dependency, илүүдэл feature, нууцлал болон тогтвортой ажиллагааг шалгана.

## 19. Acceptance Tests

Дараах шалгалтууд амжилттай болсон үед V1 бэлэн гэж үзнэ.

V1 шалгалтын явц

0/14

Codex болон OpenCode-ийн эх яриаг 1:1 хадгалсан

Web яриаг extension-оор SQLite-д дамжуулсан

models, timestamp, conversation ID, sequence зөв хадгалагдсан

Харагдах Thinking / Progress боломжтой хэмжээнд хадгалагдсан

Tool calls, logs, raw diffs архивт ороогүй

Давтан import хийхэд яриа давхардаагүй

MD Segment-ийн шинэчлэлт зөв ажилласан

Хуучин ярианы үргэлжлэл зөв Segment-д орсон

Network failure болон restart үед өгөгдөл алдаагүй

49 source дүүрэхэд нөөц байр ашиглан нэгтгэсэн

NotebookLM-ээс асуухад зөв яриаг ID-тай сэргээсэн

MCP-ээр Codex, OpenCode memory ашигласан

Local LLM, vector DB, илүүдэл smart memory байхгүй

CPU/RAM хэрэглээг бодитоор хэмжсэн

Энэ нь хөгжүүлэлтийн шалгах жагсаалт. Одоогоор эдгээр тестийг ажиллуулаагүй.

## 20. Бодитоор баталгаажуулах үлдсэн зүйлс

| Асуудал                                                         | Төлөв                         |
| --------------------------------------------------------------- | ----------------------------- |
| hstry extension-ийн бүх web platform дээрх найдвартай ажиллагаа | Турших                        |
| Thinking / Progress бүрэн цуглуулах боломж                      | Adapter бүрээр шалгах         |
| NotebookLM MD upload болон source солих ажиллагаа               | Турших                        |
| Segment-ийн хамгийн сайн хэмжээ                                 | 150k, 300k, 450k үгээр турших |
| Монгол хэлний retrieval чанар                                   | Турших                        |
| NotebookLM-ийн үнэгүй хэрэглээний бодит хязгаар                 | Баталгаажуулах                |
| Google authentication удаан хугацаанд ажиллах эсэх              | Турших                        |
| Бүх Segment дээд хэмжээнд хүрэх үеийн хураангуйлах алгоритм     | Дараа шийдэх                  |

Архитектурын хязгаар: notebooklm-py нь албан бус API ашигладаг тул Google-ийн өөрчлөлтөөс хамаарах эрсдэлтэй. Web extension-оор бүх хуучин яриа болон түр харагдаад алга болдог Progress текстийг бүрэн авах боломж бас баталгаажаагүй. Эдгээрийг бодит хэрэгжүүлэлтээр шалгана.

## Эцсийн систем

[Medium](https://images.openai.com/static-rsc-4/1kXCfoqv_nnG8AeqWFtY72rnwYX4riverPS_Q_77Db_iO91BSQOuqLuB7M2A-aJTux3qy4JefDvgQwB0XXwnM3oF8wfDsbcJQ-glNahq7DJNKarTmyRbyd1AY2AXd5U-CVCXKe-y3cmzfSPa4QA7M0pNM2cDh9zGqf0TA1R18PE?purpose=inline)

[medium.com](https://medium.com/flow-specialty/ai-assisted-coding-automating-plan-reviews-with-claude-code-and-codex-for-higher-quality-plans-c7e373a625ca?utm_source=chatgpt.com)

Codex · OpenCode · Claude · ChatGPT

hstry + Text-only Filter

Яриаг автоматаар цуглуулна

Local SQLite

Эх яриа 1:1 · metadata · sync state

Markdown Segment Builder

Олон яриа = нэг MD source

notebooklm-py

Автомат upload · verify · replace

[NotebookLM - AI toolr  tools, reviews, blogs](https://images.openai.com/static-rsc-4/Exi42vueYIoXjU7jVoSu8Zdx8iBDH3thgmx7a9Kq9JLUGvgc7V3r0mYDFiTiybcbMqtu3LoI6pYXAuh_7CFrnd-6w_Xfm4tzQEYz7gvYWfPgL1iazbmf1YwuIqPOXPXwOXBS3hJs7k8625N6x4R1_puEsPHNw_hv8ykFvnUKW30?purpose=inline)

# NotebookLM

Нэг notebook · 49 Segment · 1 нөөц

MCP Gateway

Agent memory хэрэгтэй үед асууна

Гол зарчим: Local SQLite-д эх яриагаа бүрэн хадгална. NotebookLM-д олон яриаг багцалсан Markdown Segment-үүд автоматаар дамжуулна. Агент шаардлагатай үед ганц NotebookLM-ээс санах ойгоо сэргээнэ. Local smart memory, тусдаа Main Memory, Medium Memory, INDEX, олон notebook-ийн routing байхгүй.

Энэ бол нэгтгэсэн V1 архитектур. Одоогоор код бичээгүй, repository өөрчлөөгүй, бодит тест ажиллуулаагүй. SQLite-ийн нарийн schema, Segment-ийн хэмжээ, sync давтамж зэрэг хэрэгжүүлэлтийн параметрийг туршилтаар эцэслэнэ.
