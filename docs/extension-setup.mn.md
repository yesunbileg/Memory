# AI Memory extension холбох

Эхлээд төслийн хавтсаа нээгээд шинэ хувилбарыг авна:

```bash
git pull --ff-only
uv sync --frozen
```

1. Linux дээр Brave-ийн `brave://extensions` эсвэл Chrome/Chromium-ийн `chrome://extensions` хуудсыг нээнэ.
2. Developer mode → Load unpacked → `vendor/hstry/extension` хавтсыг сонгоно. Суулгасан бол Reload дарна. Extensions хуудаснаас AI Memory-ийн 32 үсэгтэй ID-г хуулна.
3. Дараах командын `YOUR_EXTENSION_ID`-г хуулсан ID-аар, `brave`-ийг шаардлагатай бол `chrome` эсвэл `chromium`-аар солино.

```bash
.venv/bin/ai-memory --db data/conversations.sqlite connect --extension-id YOUR_EXTENSION_ID --browser brave
```

4. Settings → Choose connection file дээр командын хэвлэсэн `connection_file`-ийг сонгоно. Хөтөч энэ архивт холбогдсон локал програмыг өөрөө ажиллуулна. Terminal ажиллаж үлдэх шаардлагагүй.
5. Хуучин token/bearer файлтай бол шинэ хувилбар руу автоматаар холбохгүй. Дээрх командаар v2 файл үүсгэж дахин сонгоно. Буруу файл өмнөх ажиллаж буй тохиргоог солихгүй.
6. Холболт баталгаажсаны дараа нэвтэрсэн сайт, хадгалах ярианы төрлөө сонгоод Capture selected chats дарна.

Стандарт бус browser user-data хавтас болон платформын хязгаарыг [native холболтын заавар](native-pairing.md)-аас үзнэ.

## Checklist

| Сонголт | Ямар яриа вэ? | Анхны төлөв |
| --- | --- | --- |
| Projects & folders | Project эсвэл folder-т харьяалагдах яриа | Асаалттай |
| Pinned & favorites | Pin/star/favorite болгосон яриа | Асаалттай |
| GPTs & agents | Custom GPT/agent-ийн яриа | Асаалттай |
| Ordinary Recents | Дээрх ангилалд хамаараагүй энгийн яриа | Унтраалттай |

Project/pinned чат Recents жагсаалтад давхар харагдсан ч сонгосон төрөлд багтвал орно. Зөвхөн Ordinary Recents-ийг асаавал унтраасан project/pinned/agent төрлийг хүчээр оруулахгүй. Ярианы төрөл тодорхойгүй бол Recents унтраалттай үед алгасана. Өмнө хадгалсан яриаг устгахгүй. Browser-ийн checklist нь локал Codex/OpenCode collector-ийн сонголтыг өөрчлөхгүй.

Ажиллаж буй capture-ийн сонголтыг өөрчилбөл дараагийн хуучин сонголттой batch-ийг явуулахгүй. Өмнө нь серверт хүлээн авсан яриа архивт үлдэнэ. Шинэ сонголтоо хэрэглэж Capture дахин дарна. Аль ч төрөл сонгоогүй бол эхлүүлэхгүй.

## Төлөв болон хязгаар

- Not connected: connection файлаа сонгоно.
- Offline: сонгосон хөтөч, extension ID, локал програмын бүртгэлийг шалгана.
- Check connection: зөв архив, хөтөч болон өргөтгөлд зориулсан v2 файлыг дахин сонгоно.
- Update needed: код болон өргөтгөлийг шинэчлээд дахин холбоно.
- Some chats skipped: дутуу таталт эсвэл website-ийн category мэдээлэл дутуу. Activity-ийн тайлбарыг уншаад нэвтрэлтээ шалган, дахин оролдоно.
- Selection updated: checklist шинэчлэгдсэн; өмнөх амжилтын төлөв шинэ сонголтын баталгаа биш.

Gemini-ийн project/pinned төрлийг одоогоор найдвартай ялгах боломжгүй тул сонголт дутуу гэсэн анхааруулгатай алгасана. ChatGPT/Claude-ийн pin болон agent мэдээлэл API-д байгаа үед л ашиглагдана. Website-ийн private API өөрчлөгдөж болно. Бодит нэвтэрсэн account дээр бүрэн шалгасан гэсэн баталгаа байхгүй.

Энэ холболт яриаг локал SQLite-д хадгална. NotebookLM рүү дамжуулахад тусдаа approve, Google browser нэвтрэлт болон sync шаардлагатай.

NotebookLM-ийн нэвтрэлтийг одоо ашиглаж буй Brave-аасаа `notebooklm login --browser-cookies brave` командаар авна (`chrome`, `firefox` мөн дэмжинэ). Chromium тусад нь суулгах шаардлагагүй. Нэг удаа `notebooklm-py[cookies]` нэмэлтийг суулгасан байх ёстой; [дэлгэрэнгүй заавар](../README.md#connect-notebooklm). Worker-тэй ижил auth хавтас/файл ашиглана.
