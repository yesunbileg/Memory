# Local review, backup and recovery / Локал шалгалт, нөөцлөлт ба сэргээх

Event hooks, explicit imports and browser capture store originals locally. Every archived chat follows the same publication rules. A connected notebook and enabled automatic sync allow captures to wake the publisher. Use `/auto off` to pause automatic publication for the archive and `ai-memory sync` for a manual run. Checking an item on a provider website in List mode adds it to the capture selection.

`show ID` displays the original locally, including any credentials it contains. `preview ID` shows the masked outbound documents that contain the selected chat. A shared document can include multiple chats. Memory computes the preview locally without contacting Google or changing the archive. Treat both outputs as private.

Cloud fields are the archive conversation ID, platform, title, models and conversation dates, followed by message headings and visible text. Native conversation IDs, account labels, project paths and source URLs stay local as metadata. A user may still have written one of these values in a visible message. Policy 3 masks the credentials it recognizes. A residual finding blocks upload and the preview omits that document's body. Each outgoing revision records its exact UTF-8 bytes, SHA-256 and policy. The per-revision receipt is derived from the archive's receipt key. A later import or edit can change the preview. Local Markdown exports contain the unmasked originals.

```sh
ai-memory --db data/archive.sqlite preview CX-REPLACE_WITH_ID
ai-memory --db data/archive.sqlite backup /private/backups/archive-01.sqlite
ai-memory --db /private/restored/new.sqlite restore /private/backups/archive-01.sqlite
```

Opening a schema 1 or 2 archive upgrades it transactionally to schema 3. The upgrade runs locally. All chats in the upgraded archive follow the same publication rules. NotebookLM sources still require verified snapshots and readiness before retrieval. For a schema 1 archive, the migration creates a private `*.pre-v2-*.sqlite` copy beside the original before the first change. Retain the old external sync directory for rollback with the old application. Legacy unfinished snapshots are imported only when their revision-specific checksum agrees. Portable backup verifies all required snapshot bytes before writing the backup.

Use `backup` for a consistent, portable SQLite backup. It includes source text, IDs, revisions, sync mappings, jobs and their pending snapshot bytes. Protect it like the original. `restore` accepts schema 2 and 3 portable backups, requires a fresh destination and recreates the required private snapshot files locally. Browser pairing credentials and Google login files are stored separately. Pair the restored archive and check the configured login path before reconnecting. Use a single publisher per notebook when restoring archives.

Default exports are immutable, complete directories below `ARCHIVE.sqlite.files/exports/`. The command returns the exact directory. Explicit export destinations must be new or empty.

## Монгол

Архивт хадгалсан чат бүр ижил нийтлэх дүрмээр явна. NotebookLM холбогдсон, автомат синк асаалттай үед шинэ өгөгдөл синкийг сэрээнэ. `/auto off` нь тухайн архивын автомат синкийг зогсооно. Гараар илгээхдээ `mem` дотор `/sync` эсвэл терминалаас `ai-memory sync` ажиллуулна. `show ID` локал эхийг, `preview ID` илгээх маскласан баримтыг харуулна. Нэг баримтад хэд хэдэн чат багтаж болно.

SQLite архивын 1 ба 2-р хувилбар локалд 3-р хувилбар руу шилжинэ. 1-р хувилбарыг шилжүүлэхийн өмнө SQLite файлыг тусад нь нөөцөлнө. Хуучин программаар буцаан ашиглахад өмнөх `sync/` хавтас мөн хэрэгтэй. Зөөврийн нөөцийг `backup`-аар үүсгэнэ. 2 ба 3-р хувилбарын нөөцийг шинэ байршилд `restore`-оор сэргээнэ. Бичвэр болон хүлээгдэж буй ажлын баталгаажсан snapshot нөөцөд орно. Google-ийн нэвтрэлт ба хөтчийн холболтын нууцыг тусад нь хадгална. Нөөцийг эх архивтай адил хамгаална.
