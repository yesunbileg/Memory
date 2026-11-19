# Local review, consent and recovery / Зөвшөөрөл ба сэргээх

Event hooks, explicit imports and browser capture store originals locally. None sends a transcript to Google. Adding an item to a provider’s popup sync list saves its identifier, not its text, and grants no upload consent. `pending` lists conversations without upload permission. `show ID` displays the exact original locally (it can contain credentials). `preview ID` displays the upload documents that would contain that conversation if approved now. A shared document can include other approved conversations. Preview never changes consent or contacts Google. Treat the output as private.

`approve ID1 ID2 ...` grants permission for the complete conversations **and their future continuations**, atomically. Repeated IDs are harmless. An unknown ID changes nothing. `ai-memory sync` publishes the approved documents. The explicit import shortcut `collect --approve-upload` also grants continuing permission, but does not itself publish anything. New imports without that flag do not grant permission.

Cloud fields are the archive conversation ID, platform, title, models and conversation dates, followed by message sequence, role, visible kind, timestamp and text. Native conversation IDs, account labels, project paths and source URLs stay local as metadata. A user may still have written one of these values in a visible message. Credentials recognized by policy 2 are masked. Neither masking nor preview proves that all secrets have been identified. A residual finding blocks upload and the preview omits that document's body. Each outgoing revision records its exact UTF-8 bytes, SHA-256 and policy. Its per-revision archive receipt does not contain an archive secret. A subsequent import or permission change can change the preview. Local Markdown exports contain the unmasked originals.

`revoke ID ...` stops future publication and excludes affected shared documents from retrieval. It does not call Google, delete the local originals or erase existing remote copies. Other approved conversations can be packaged in fresh documents. To remove an existing copy, review and delete that source separately in the selected NotebookLM notebook. If a sync is active, retry revoke once it completes.

```sh
ai-memory --db data/archive.sqlite pending
ai-memory --db data/archive.sqlite preview CX-REPLACE_WITH_ID
ai-memory --db data/archive.sqlite approve CX-REPLACE_WITH_ID
ai-memory --db data/archive.sqlite backup /private/backups/archive-01.sqlite
ai-memory --db /private/restored/new.sqlite restore /private/backups/archive-01.sqlite
```

Opening a schema-1 archive migrates it transactionally to schema 2. A private `*.pre-v2-*.sqlite` copy of the original database is created beside it before the first change; retain the old external sync directory for rollback with the old application. Old remote mappings remain recorded but do not become disclosure-authorized merely because the archive was upgraded. Legacy unfinished snapshots are imported only when their revision-specific checksum agrees. Portable backup refuses to claim success if required bytes cannot be proven.

Use `backup` (not a live copy of the database or WAL files) for a portable SQLite backup. It includes source text, IDs, consent, revisions, sync mappings, jobs and their pending snapshot bytes. Protect it like the original. Restore requires a fresh destination, recreates required private snapshot files, and never replaces an existing archive. No remote service is used. Browser pairing credentials and Google login files are not part of the backup. Pair the restored archive and check the configured login path before reconnecting. Do not use two restored copies as concurrent publishers to the same notebook.

Default exports are immutable, complete directories below `ARCHIVE.sqlite.files/exports/`; the exact directory is returned in the command result. Explicit export destinations must be new or empty.

## Монгол

`pending` зөвшөөрөл хүлээж буй яриаг жагсаана. `show ID` нууц утга агуулж болох локал эхийг харуулна. `preview ID` зөвшөөрвөл Google-д илгээх маскласан баримтыг **зөвшөөрөл өгөхгүйгээр** харуулна. Нэг баримтад өмнө нь зөвшөөрсөн өөр яриа багтаж болно. `approve` нь сонгосон яриа болон **цаашдын үргэлжлэлд** үйлчилнэ. Нэг ID буруу бол шинээр зөвшөөрөл олгохгүй. Илгээхийн тулд `ai-memory sync` командыг тусад нь ажиллуулна.

`revoke` нь дараагийн илгээлтийг зогсооно. Өмнөх Google хуулбар болон локал эхийг устгахгүй. Google хуулбарыг арилгах бол NotebookLM дээр тухайн эх сурвалжийг тусад нь шалгаж устгана. Синк идэвхтэй үед дууссаны дараа дахин оролдоно.

Архивын 1-р хувилбар 2-р хувилбар руу шилжихийн өмнө SQLite-ийн хуучин хувилбарыг тусад нь нөөцөлнө. Хуучин программаар буцаан ашиглахад өмнөх `sync/` хавтас мөн хэрэгтэй. Зөөврийн нөөц үүсгэхдээ дээрх `backup`, хоосон байршилд сэргээхдээ `restore` командыг ашиглана. Бичвэр, зөвшөөрөл, хүлээгдэж буй ажлын баталгаатай байтууд нөөцөд орно. Google-ийн нэвтрэлт, хөтчийн холболтын нууц орохгүй. Нөөцийг эх архивтай адил хамгаална.
