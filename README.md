# Viet Dub — prototype 0.6.0

Chrome Extension và server Python cục bộ để dịch phụ đề YouTube / Udemy sang tiếng Việt, sau đó đọc lồng tiếng theo cuốn chiếu. Bản này hỗ trợ chọn provider dịch trong popup: Gemini, OpenAI GPT, Anthropic Claude hoặc Ollama Cloud. TTS vẫn dùng Edge TTS. Ngoài ra có thể **tóm tắt nội dung bài học** và lưu ra **Google Docs** hoặc **Notion**.

## Chạy server trên Windows 10/11

Cần Python 3.11 trở lên (python.org, tick **Add python.exe to PATH**) và Google Chrome/Edge.

1. Giải nén dự án, bấm đúp `install.bat` (tạo `server\.venv` và cài thư viện; chỉ cần làm một lần).
2. Bấm đúp `start-server.bat`. Giữ cửa sổ này mở khi dùng; đóng cửa sổ là dừng server. Kiểm tra `http://127.0.0.1:8765/health`.
3. API key được lưu trong **Windows Credential Manager** (tài khoản Windows của bạn), không nằm trong file. Xem/xóa tại *Control Panel → Credential Manager → Windows Credentials*, mục `viet-dub-api`.
4. Cài extension như phần dưới (`chrome://extensions` hoặc `edge://extensions` → Developer mode → Load unpacked → thư mục `extension`).

Cách chạy thủ công bằng PowerShell:

```powershell
cd server
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8765
```

Nếu Windows Defender Firewall hỏi quyền mạng, server chỉ bind `127.0.0.1` nên có thể bấm Hủy. Nếu cổng 8765 đã bị chiếm, thông báo lỗi sẽ hiện trong cửa sổ server.

## Chạy server trên Linux

Cần Python 3.11 trở lên, Chrome/Chromium và một kho bí mật Linux đang hoạt động (Secret Service như GNOME Keyring hoặc KWallet). Khởi chạy server từ phiên desktop người dùng để Python truy cập kho bí mật; server sẽ từ chối lưu khóa nếu chỉ tìm thấy backend lưu dạng thường hoặc không an toàn.

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8765
```

Server bind vào `127.0.0.1`; không đổi sang `0.0.0.0`. Cache và metadata provider/model nằm trong `server/data/vietdub.sqlite3`. API key không nằm trong SQLite.

## Nhập API key từ popup extension

1. Tại `chrome://extensions`, bật **Developer mode**, bấm **Load unpacked** và chọn thư mục `extension`.
2. Bấm biểu tượng Viet Dub trên thanh extension. Chọn provider, kiểm tra hoặc sửa `Model ID`, dán API key vào ô mật khẩu.
3. Bấm **Kiểm tra API** để gửi một câu mẫu và xác nhận provider/model/key hoạt động. Nút này gọi API thật nên có thể dùng quota/chi phí. Kiểm tra thành công rồi bấm **Lưu cấu hình**; key sẽ được xóa khỏi ô nhập sau khi lưu.
4. Nút **Kiểm tra** ở trên cùng chỉ kiểm tra server và tải metadata cấu hình đã lưu khi bạn chủ động bấm. Riêng trạng thái Google/Notion được đọc lại khi mở popup, nhưng chỉ sau khi server trả lời `/health`.
5. Mở video YouTube có phụ đề, bật CC, bấm **Tải script**, rồi chọn **Phát & lồng tiếng**. Popup giữ nguyên nút bật/tắt lồng tiếng hiện có.

Tên provider/model lưu trong Chrome để điền lại giao diện; API key không được ghi vào `chrome.storage`, mã nguồn, SQLite hay log. Server lưu khóa trong kho bí mật của hệ điều hành. Extension gửi key tới server cục bộ qua loopback trong thao tác **Kiểm tra API** hoặc **Lưu cấu hình**; server mới gọi dịch vụ AI.

Nút **Xóa cấu hình & khóa** xóa cấu hình đang chọn và dọn các API key đã lưu cho cả bốn provider khỏi keyring.

Nếu keyring báo chưa sẵn sàng, hãy cài/bật và mở khóa Secret Service/GNOME Keyring hoặc KWallet, sau đó khởi chạy lại server từ terminal trong phiên desktop. Viet Dub không tự chuyển sang file văn bản nếu keyring không khả dụng. Trên máy Linux không có desktop/keyring, provider API key chưa được hỗ trợ an toàn trong bản này.

## Provider và model mặc định

| Provider | Model mặc định gợi ý | API key/giao thức |
|---|---|---|
| Google Gemini | `gemini-3.8-flash` | API key Google AI Studio; Gemini Interactions API |
| OpenAI GPT | `gpt-6-luna` | OpenAI API key; Responses API |
| Anthropic Claude | `claude-haiku-5-5` | Claude Console API key; Messages API |
| Ollama Cloud | `gpt-oss:120b` | Ollama Cloud API key |

Tên model là giá trị gợi ý và có thể đổi theo quyền truy cập hoặc việc nhà cung cấp cập nhật model. Nếu kiểm tra báo `model_not_found`, xem lại model ID và quyền truy cập. ChatGPT Plus, Claude Pro hay Gemini Advanced không đồng nghĩa với API key hoặc quota API; hãy dùng key tạo trong trang quản lý API của provider. Mỗi provider có cách tính phí, quota và quyền model riêng.

Chỉ provider bạn đã chọn được gọi; Viet Dub không tự rơi về dịch vụ miễn phí khi API trả lỗi. Nếu chưa cấu hình key, server dùng MyMemory/Google miễn phí như trước và có thể gặp 429. Để đổi provider, chọn provider khác, nhập key/model tương ứng rồi lưu; đổi provider mà giữ nguyên key cũ thường dẫn tới lỗi xác thực.

## Dùng trên Udemy (và các web video khác)

Extension đã khai báo sẵn `udemy.com` trong `content_scripts` và `host_permissions`.

1. Mở bài học trên Udemy, bật **CC/Subtitles** trong player và đợi phụ đề tiếng Anh hiện lên.
2. Bấm **Tải script** trong widget Viet Dub. Extension đọc `textTracks` của thẻ `<video>`; nếu trình duyệt chưa nạp cue thì extension ép track về chế độ `hidden` rồi thử lại, sau đó mới tải file VTT qua service worker.
3. Bấm **Phát & lồng tiếng** để nghe bản dịch lồng theo video.
4. Bấm **📋 Tóm tắt bài học** để tạo ghi chú (xem phần kế tiếp).

Khi bạn chuyển sang bài khác trên Udemy (SPA đổi URL), extension tự phát hiện và reset trạng thái để bạn tải script lại.

Với website khác không có trong `matches`, hãy thêm domain vào `content_scripts` và `host_permissions` trong `extension/manifest.json` rồi bấm **Reload** tại `chrome://extensions`.

## Tóm tắt bài học: Google Docs hoặc Notion

### Cách dùng

Tính năng tóm tắt chỉ mở sau khi có **một nơi lưu**: đăng nhập Google hoặc cấu hình
Notion. Khi chưa có, popup hiện ghi chú 🔒 và nút Tóm tắt trong widget bị khoá; server
cũng trả `403` nếu gọi `/summarize` trực tiếp, nên không thể dùng vòng qua giao diện.

1. Trong popup chọn **Nơi lưu bản tóm tắt**: `Google Docs`, `Notion (trang con)` hoặc `Chỉ hiện trong widget`.
2. Bấm **Tải script** rồi **📋 Tóm tắt bài học** trên widget của video. Extension sẽ dịch phần phụ đề chưa có bản Tiếng Việt, nhờ AI rút gọn theo 5 mục (Mục tiêu / Nội dung chính / Khái niệm quan trọng / Kỹ năng–Công cụ / Tóm tắt một câu), rồi lưu ra đích đã chọn.
3. Bản tóm tắt luôn hiện trong widget (mục *Xem bản tóm tắt*) kèm link mở tài liệu.
4. Tùy chọn **Ghi tiếp vào tài liệu đã tạo**: bật thì các video sau nối tiếp vào cùng một tài liệu; tắt thì mỗi video tạo một tài liệu riêng.

Bản tóm tắt được cache trong SQLite local theo cặp *tiêu đề + nội dung phụ đề*, nên bấm lại trên cùng video không tốn quota.

### Google Docs — đăng nhập bằng Google

Muốn ghi tóm tắt vào Google Docs, Viet Dub phải được Google cấp quyền cho tài khoản
của bạn. Việc đó diễn ra bằng chính trang xác thực của Google:

1. Mở popup Viet Dub → mục **Google Docs** → bấm **Tiếp tục với Google**.
2. Trình duyệt mở trang đăng nhập Google. Chọn tài khoản và bấm **Tiếp tục / Cho phép**
   cho các quyền *xem và tạo tài liệu* và *tạo tệp trong Drive*.
3. Google chuyển hướng về `http://127.0.0.1:8765/google-auth/callback`; trang báo
   **“Đã kết nối Google”**. Đóng tab là xong.
4. Quay lại popup: trạng thái hiện **“Đã đăng nhập: `<email của bạn>`”**. Popup tự cập
   nhật trong lúc bạn duyệt quyền, không cần bấm lại.

Sau khi kết nối, bản tóm tắt được tạo trong thư mục Drive tên **Viet Dub Notes**
(mỗi video một tài liệu, hoặc nối tiếp vào cùng một tài liệu nếu bật *Ghi tiếp*).

Token truy cập và refresh token được lưu trong **kho bí mật của hệ điều hành**
(`viet-dub-google`), không nằm trong Chrome storage, SQLite, mã nguồn hay log.
Token tự được làm mới khi hết hạn; nút **Ngắt kết nối** xoá token.

#### Vì sao có bước OAuth client? (giải thích ngắn)

Các app thương mại (Notion, Manus…) không bắt bạn nhập gì vì chúng chạy trên server
của họ: **một** OAuth client do nhà phát triển tạo, nhúng sẵn ở backend, mọi người dùng
chỉ thấy nút “Tiếp tục với Google”.

Viet Dub chạy server trên chính máy bạn, nên chủ dự án Google Cloud cũng chính là bạn.
Client ID (định danh công khai) đã được **nhúng sẵn trong `server/google_auth.py`**;
client secret nằm trong `server/oauth_client.json` (đã gitignore) hoặc kho bí mật của
hệ điều hành. Bạn **không cần nhập gì trong popup** — chỉ bấm “Tiếp tục với Google”.

Client ID/secret **không bao giờ được gửi xuống trình duyệt**: extension chỉ gọi
`http://127.0.0.1:8765/google-auth/url`, còn server Python mới là bên nói chuyện với
Google. Vì vậy mở F12 → Network cũng không thấy secret.

#### Nếu báo lỗi khi đăng nhập (xử lý một lần)

Lỗi `redirect_uri_mismatch`, `invalid_client` hoặc “chưa cấu hình OAuth client” nghĩa là
server chưa có (hoặc sai) OAuth client. Sửa một lần:

1. Vào [Google Cloud Console](https://console.cloud.google.com) → tạo project → bật **Google Docs API** và **Google Drive API**.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
3. Loại ứng dụng: **Web application** (server local của Viet Dub giữ client secret nên đóng
   vai trò *confidential client* — đây là loại client đúng cho kiểu chạy này; loại
   *Desktop app* cũng dùng được nhưng không cần thiết).
4. Thêm **Authorized redirect URI**: `http://127.0.0.1:8765/google-auth/callback` (bấm **Save**).
5. Đặt Client ID + Secret vào `server/oauth_client.json`:

   ```json
   { "client_id": "....apps.googleusercontent.com", "client_secret": "GOCSPX-..." }
   ```

   File này đã nằm trong `.gitignore` — **không commit**. Cách khác: mở mục nâng cao
   *“Dùng OAuth client khác”* trong popup để lưu vào kho bí mật của hệ điều hành.
6. Khởi động lại server rồi bấm **Tiếp tục với Google**.

> Google có thể hiện cảnh báo *“Google hasn’t verified this app”* vì đây là ứng dụng
> Desktop tự tạo. Bấm **Advanced → Go to <tên app> (unsafe)** để tiếp tục, hoặc thêm
> email của bạn vào mục **Test users** trong OAuth consent screen.

### Notion — tạo integration (làm một lần)

1. Vào [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration** → đặt tên, chọn workspace.
2. Copy **Internal Integration Token** (dạng `secret_...`).
3. Mở trang Notion muốn làm **thư mục cha** → **… → Connections → Invite** → chọn integration vừa tạo (bắt buộc, nếu không integration không đọc được trang).
4. Lấy **Parent page ID**: 32 ký tự hex. Cách nhanh nhất: mở trang đó, copy URL dạng `https://www.notion.so/<workspace>/<page-id>?v=...` rồi bỏ dấu `-` trong `<page-id>`.
5. Trong popup → mục **Notion**: dán token + page ID → **Kiểm tra & lưu**.

Mỗi video tạo **một trang con** trong thư mục cha, có cùng cấu trúc 5 mục với Google Docs. Token và page ID lưu trong kho bí mật (`viet-dub-notion`).

### Chia sẻ cho người khác dùng

Mỗi người dùng đăng nhập bằng **Google account của chính họ** (tài liệu Google Docs
tạo ra nằm trong tài khoản đó). Ngoài ra:

1. **Họ vẫn phải chạy server local** — extension không tự chạy được server Python.
2. **Bạn phải thêm email của họ vào Test users** (Google Cloud Console → Google Auth
   Platform → Test users). Ở chế độ *Testing*, app chỉ cho tối đa **100 tài khoản**
   đăng nhập và mọi người đều thấy màn hình cảnh báo “app chưa được Google xác minh”.
3. **Client ID/Secret**: redirect URI là `http://127.0.0.1:8765/...` nên **giống nhau trên
   mọi máy** — không cần đăng ký lại. Client ID đã nhúng sẵn trong `server/google_auth.py`
   (công khai, luôn hiện trong URL xin quyền nên không giấu được và không cần giấu). Secret:
   - Gọn nhất: **chép `server/oauth_client.json`** sang máy họ (file đã gitignore). Server
     đọc ngay, không phải nhập gì trong popup.
   - Hoặc mỗi người dùng client *Web application* riêng, dán ở mục nâng cao
     *“Dùng OAuth client khác”* trong popup.
   - Hoặc dùng client *Desktop app* (Google coi secret loại này là không cần bảo mật) và
     đặt cả ID lẫn secret trong `server/oauth_client.json` — vẫn không commit.

   > Không đặt secret vào `_BUILTIN_CLIENT_SECRET` trong mã nguồn: hằng số đó nằm trong
   > file được commit, đẩy lên GitHub là lộ (và GitHub/Google có thể tự thu hồi secret).

Nếu muốn **ai cũng dùng được, không giới hạn 100 người**, phải chuyển Publishing status
sang *In production* và đi qua xác minh của Google — vì `documents` và `drive.file` là
**sensitive scope** nên bắt buộc phải review trước khi mở cho người dùng chung. Việc này
mất 3–5 ngày làm việc và cần: trang chủ công khai, chính sách bảo mật, branding, video demo.

> Theo [chính sách của Google](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification),
> nếu app chỉ dùng cho bạn và vài người quen biết thì **không cần xác minh** — chỉ cần
> thêm email vào Test users là chạy.

## Thông báo lỗi trong popup

- **API key bị từ chối (401):** key sai, bị thu hồi hoặc không thuộc provider đã chọn.
- **Quyền/API/billing (403):** kiểm tra API đã bật, tài khoản và quyền model.
- **Không tìm thấy model (404):** kiểm tra lại Model ID hoặc quyền truy cập.
- **Quota/rate limit (429):** chờ quota hồi lại hoặc kiểm tra giới hạn/billing; server cung cấp `Retry-After` nếu có.
- **Mạng/time-out:** kiểm tra Internet, DNS, proxy hoặc firewall.
- **Kho bí mật chưa sẵn sàng:** mở khóa keyring; key sẽ không được lưu vào file để thay thế.

Lỗi API được trình bày theo nhóm. Log backend chỉ ghi provider/loại lỗi/HTTP status hoặc fingerprint câu cần chẩn đoán; không ghi Model ID do người dùng nhập, request header, API key hay toàn bộ body lỗi từ nhà cung cấp. Không gửi API key qua chat, ảnh chụp màn hình hoặc issue công khai.

## Chuyển từ bản cũ

Bản trước có thể đọc `OLLAMA_API_KEY` từ `server/.env`; bản 0.5.4 không còn tự nạp `.env`. Hãy nhập lại key trong popup và bấm **Lưu cấu hình**. Sau khi xác nhận server dùng được Ollama từ kho bí mật, bạn có thể tự xóa `server/.env` cũ nếu trong đó chỉ chứa key này. ZIP source không chứa file `.env` hay khóa của bạn.

## Cache, đồng bộ và vận hành

Server lưu bản dịch/audio trong SQLite local; cache tiếp tục dùng được qua các lần khởi động. Cache dịch được phân vùng theo provider/model để không lấy nhầm kết quả đã tạo bằng cấu hình khác. Xem thống kê cache tại `http://127.0.0.1:8765/cache/stats`. Extension giữ cache audio ở IndexedDB của trình duyệt. Nếu cần đổi model/provider, nên dừng lồng tiếng trước khi lưu cấu hình mới.

Widget chỉ hiển thị tốc độ video hiện tại; chỉnh tốc độ trực tiếp trong trình phát. Kéo thanh tiêu đề để di chuyển widget, bấm **−** để thu nhỏ thành icon **VD**; kéo icon rồi thả chỉ di chuyển icon. Khi YouTube phát quảng cáo, Viet Dub tạm ngưng tạo/phát giọng và tiếp tục sau quảng cáo.

## Tài liệu API chính thức

- [Gemini API keys và bảo mật](https://ai.google.dev/gemini-api/docs/api-key), [Gemini Interactions API](https://ai.google.dev/gemini-api/docs/interactions)
- [OpenAI API overview](https://developers.openai.com/api/reference/overview/), [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Anthropic API overview](https://platform.claude.com/docs/en/api/overview), [Messages API](https://platform.claude.com/docs/en/api/messages/create)
- [Google OAuth 2.0 cho Desktop app](https://developers.google.com/identity/protocols/oauth2/limited), [Google Docs API](https://developers.google.com/workspace/docs/api/reference/rest), [Google Drive API v3](https://developers.google.com/drive/api/reference/rest/v3)
- [Notion API — tạo page](https://developers.notion.com/reference/post-page), [Notion API — append block](https://developers.notion.com/reference/patch-block-children), [Notion integrations](https://www.notion.so/my-integrations)
- [Python keyring](https://keyring.readthedocs.io/en/latest/)
