# Continuity Ledger

## Goal
Viet Dub 0.6.0 — extension lồng tiếng Việt cho YouTube **và** Udemy, kèm tóm tắt nội dung
bài học xuất ra **Google Docs** hoặc **Notion**.

## Constraints & Assumptions
- Git đã cấu hình; branch `main`; remote `git@github.com:An2412/trans-vietsub.git`.
- User identity: `phamngocan2412` <an.207ct68609@gmail.com> · GitHub `An2412`.
- Server chỉ bind `127.0.0.1:8765`; API key / OAuth token / Notion token **chỉ** nằm trong
  kho bí mật hệ điều hành (keyring). Không ghi vào Chrome storage, SQLite, mã nguồn hay log.
- Provider AI cho phần tóm tắt phải là provider có API key (Gemini / OpenAI / Claude / Ollama).
  Hai provider miễn phí (MyMemory, Google web) chỉ dịch, không sinh văn bản.

## Key Decisions
- Viết OAuth2 Google + Notion API bằng `urllib` thuần ⇒ **không** cần `google-auth`,
  `google-auth-oauthlib`, `google-api-python-client` (đã bỏ khỏi `requirements.txt`).
- Notion dùng **Internal Integration** (token `secret_...`) + parent page ID, mỗi video
  tạo một **trang con** trong trang cha.
- Google Docs dùng OAuth client loại **Web application** với redirect
  `http://127.0.0.1:8765/google-auth/callback`; client secret chỉ nằm phía server
  (keyring hoặc `server/oauth_client.json` đã gitignore), không bao giờ gửi xuống browser.
- Client ID nhúng sẵn trong `server/google_auth.py` (`_BUILTIN_CLIENT_ID`) vì đó là
  định danh công khai; `_BUILTIN_CLIENT_SECRET` **phải để trống** trong repo.
- Tóm tắt bài học bị **gate**: chỉ mở khi `connected` (Google) hoặc Notion `configured`;
  UI khoá ở popup + widget, server trả 403 ở `/summarize`. Phần dịch/lồng tiếng không bị gate.
- Widget dùng `position:absolute` khi có player container (YouTube, Udemy) và
  `position:fixed` khi không tìm thấy container.
- Content script gọi server qua `background.js` (service worker) vì `local_only_guard`
  chặn request có `Origin: https://<site>`; fetch của content script dùng origin của trang.
- `install.bat` fallback sang `python` nếu không có launcher `py`.

## Progress State

### Done (đã xong, đã test)
- **Server**
  - `google_auth.py`: OAuth2 (auth URL, CSRF state, exchange code, refresh token),
    lưu keyring; Docs helpers: tạo doc, tìm/tạo folder "Viet Dub Notes", append,
    markdown → `batchUpdate` (heading 1–3, bullet, numbered, bold/italic inline).
  - `notion_export.py` (mới): token + parent page ID trong keyring, `test_connection`,
    `create_child_page`, `append_to_page`, markdown → Notion blocks (chia 100 block/request,
    rich_text chia 1900 ký tự, giữ bold/italic/code, giữ bullet/numbered).
  - `summarizer.py`: prompt 5 mục; **chia đoạn** transcript dài (6000 ký tự/đoạn,
    tối đa 12 phần) rồi gộp — không còn cắt cụt ở 8000 ký tự.
  - `translator.py`: `FallbackTranslator.generate_text()` (retry + cooldown) và
    `GenerationNotSupported` để trả 400 thay vì 502 khi chưa có provider AI.
  - `api_providers.py`: `generate_text()` cho Gemini / OpenAI / Anthropic.
  - `local_cache.py`: bảng `summaries` + `get_summary` / `set_summary`.
  - `main.py`: endpoints `/google-auth/{client-config,status,url,callback,disconnect}`,
    `/notion/{config,status,test,disconnect}`, `/summarize` (export_to = none|gdocs|notion,
    target_id để append). Cache tóm tắt theo **tiêu đề + hash nội dung**.
    `/google-auth/status` trả thêm `client_configured` để popup biết đã sẵn sàng
    cho nút "Đăng nhập Google" hay chưa.
  - `/summarize` trả lỗi rõ ràng: 403 khi chưa có nơi lưu (Google chưa đăng nhập **và**
    Notion chưa cấu hình), 400 khi chưa có provider AI / export_to sai,
    401 khi đã có nơi lưu nhưng đích cụ thể chưa sẵn sàng.
- **Extension**
  - `background.js`: thêm `summarize`, `google_auth_url`, `google_auth_status`,
    `notion_status`, `fetch_text` (tải VTT cross-origin cho Udemy).
  - `content.js`: `detectPlatform`, `findPlayerContainer`, `getVideo` đa nền tảng,
    loader Udemy (ép `textTracks` mode `hidden`, fallback tải VTT qua background),
    `videoTitle`, nút **📋 Tóm tắt bài học**, khung xem tóm tắt + link tài liệu,
    tự dịch phần phụ đề còn thiếu trước khi tóm tắt, theo dõi SPA navigation.
  - `popup.html` / `popup.js`: khối đăng nhập Google đặt **đầu popup** (nút trắng
    “Tiếp tục với Google” có logo G, ẩn khi đã đăng nhập, thay bằng email + Ngắt kết nối);
    OAuth Client ID/Secret chỉ còn trong `<details>` “Dùng OAuth client khác (nâng cao)”;
    phần **Tóm tắt bài học** (đích xuất + Notion) bị gate, hiện ghi chú 🔒 và lối thoát
    “dùng Notion thay thế”; khối Notion nằm trong vùng được mở khoá.
  - `content.js`: nút Tóm tắt đổi thành “🔒 Tóm tắt: đăng nhập Google” và bị disable khi
    chưa có nơi lưu; kiểm tra lại qua `google_auth_status` / `notion_status` mỗi lần tải script.
  - `manifest.json`: 0.6.0, `udemy.com`, host_permissions `*.udemy.com`.
- **Tests**
  - `server/test_export.py`: markdown → Notion blocks / Docs requests, tóm tắt ngắn & dài.
  - `server/test_summarize_api.py`: 7 test cho `/summarize` (none/gdocs/notion/append/lỗi/cache).
  - `tests/check-wiring.js`: kiểm tra tĩnh id widget, `ui.*`, message type, manifest, popup id.
  - `tests/jsdom/smoke.js`: mount widget trên YouTube/Udemy/trang không video + chạy trọn
    luồng Tóm tắt (tự dịch → gửi message → hiện kết quả + link).
- **Đã cấu hình sẵn trên máy này**: OAuth Client ID/Secret của user đã lưu vào
  Windows Credential Manager (`viet-dub-google`). Auth URL sinh đúng.

### Verified
- `python -m py_compile` toàn bộ server OK.
- `unittest discover` : 31 test, chỉ 1 lỗi **có sẵn từ trước** ở
  `test_provider_settings.test_secret_is_not_written_to_sqlite_and_is_not_returned`
  (Windows khoá file sqlite khi xoá temp dir — không liên quan thay đổi này).
- Server chạy thật: `/health`, `/google-auth/*`, `/notion/*`, `/summarize` trả đúng mã lỗi.
- `node --check` cho `content.js`, `background.js`, `popup.js` OK.

### Next (việc user cần tự làm)
1. **Google Cloud Console**: bật `Google Docs API` + `Google Drive API`, và thêm
   Authorized redirect URI `http://127.0.0.1:8765/google-auth/callback` cho OAuth client
   (loại **Web application** — server giữ client secret nên là confidential client).
   Đã kiểm tra thật: Google chấp nhận cặp client_id + redirect_uri này.
2. **Google Auth Platform → Test users**: thêm email của user, nếu không sẽ bị
   `access_denied` ở chế độ Testing.
3. Mở popup → **Tiếp tục với Google** → chọn tài khoản → duyệt quyền Docs/Drive
   (popup tự cập nhật trạng thái, không cần bấm lại).
4. Tạo Notion integration + invite vào trang cha, dán token + page ID vào popup (nếu dùng Notion).
5. Reload extension tại `chrome://extensions` để nhận code mới.
6. Test thật trên một video YouTube và một bài Udemy có phụ đề tiếng Anh.
7. Commit & push.

### Phát hành cho người khác (đã ghi trong README)
- Mỗi người dùng đăng nhập bằng Google account của chính họ.
- Phải thêm email họ vào Test users; tối đa 100 tài khoản ở chế độ Testing.
- Redirect URI là loopback cố định nên giống nhau mọi máy — không cần đăng ký lại.
- Muốn mở cho công chúng: phải publish *In production* + xác minh sensitive scope
  (3–5 ngày, cần trang chủ + privacy policy + video demo).
- `client_secret` giờ là tuỳ chọn → hỗ trợ client Desktop app (ship sẵn Client ID).
