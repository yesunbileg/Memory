# AI Memory extension холбох

Эхлээд төслийн хавтсаа нээгээд шинэ хувилбарыг авна:

```bash
git pull --ff-only
uv sync --frozen
```

Хуучин `serve` ажиллаж байгаа terminal дээр Ctrl+C дарж зогсооно. Дараа нь:

```bash
.venv/bin/ai-memory connect
```

Команд token-ийг үүсгэж эсвэл байгааг ашиглан, private connection файл бэлдээд локал серверийг асаана. Terminal-ийг нээлттэй үлдээнэ. Тусдаа SQLite архив сонгосон бол connection файл хаана үүссэнийг terminal-ийн замаас харна. Өөр архивтай сервер ажиллаж байвал чимээгүйгээр түүнтэй холбохгүй, алдаа мэдээлнэ.

1. Brave дээр `brave://extensions`, Chrome дээр `chrome://extensions` нээнэ.
2. Анх суулгах бол Developer mode → Load unpacked → төслийн `vendor/hstry/extension` хавтсыг сонгоно. Өмнө суулгасан бол Reload дарна.
3. AI Memory → Settings → Choose connection file дээр дарна.
4. Төслийн `data/extension-connection.json` файлыг эсвэл terminal-д хэвлэсэн connection файлыг сонгоно.
5. Token-ийн бодит шалгалт амжилттай бол Connected гэж гарна.
6. Хүссэн website/provider-оо асаана. Browser дээр тухайн сайтад нэвтэрсэн байна.
7. Checklist-ээс ярианы төрлөө сонгоод Capture selected chats дарна.

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
- Offline: `connect` terminal ажиллаж байгааг шалгана.
- Check connection: token зөрсөн байж болно; зөв файлаа дахин сонгоно.
- Update needed: хуучин серверээ зогсоож шинэ кодоор `connect` ажиллуулна.
- Some chats skipped: дутуу таталт эсвэл website-ийн category мэдээлэл дутуу. Activity-ийн тайлбарыг уншаад нэвтрэлтээ шалган, дахин оролдоно.
- Selection updated: checklist шинэчлэгдсэн; өмнөх амжилтын төлөв шинэ сонголтын баталгаа биш.

Gemini-ийн project/pinned төрлийг одоогоор найдвартай ялгах боломжгүй тул сонголт дутуу гэсэн анхааруулгатай алгасана. ChatGPT/Claude-ийн pin болон agent мэдээлэл API-д байгаа үед л ашиглагдана. Website-ийн private API өөрчлөгдөж болно. Бодит нэвтэрсэн account дээр бүрэн шалгасан гэсэн баталгаа байхгүй.

Энэ холболт яриаг локал SQLite-д хадгална. NotebookLM рүү дамжуулахад тусдаа approve, Google browser нэвтрэлт болон sync шаардлагатай.

NotebookLM-ийн нэвтрэлтийг одоо ашиглаж буй Brave-аасаа `notebooklm login --browser-cookies brave` командаар авна (`chrome`, `firefox` мөн дэмжинэ). Chromium тусад нь суулгах шаардлагагүй. Нэг удаа `notebooklm-py[cookies]` нэмэлтийг суулгасан байх ёстой; [дэлгэрэнгүй заавар](../README.md#connect-notebooklm). Worker-тэй ижил auth хавтас/файл ашиглана.
