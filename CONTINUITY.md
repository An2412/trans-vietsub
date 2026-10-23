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
- **Google Docs xuất theo mô hình “một sổ tay, mỗi video một thẻ”** (yêu cầu của user):
  doc chủ “Viet Dub — Tóm tắt bài học” trong folder “Viet Dub Notes”; video đầu chiếm
  thẻ gốc (đổi tên bằng `updateDocumentTabProperties`), video mới tạo thẻ bằng
  `addDocumentTab {tabProperties: {title}}`, tóm tắt lại cùng video nối tiếp vào thẻ
  đó bằng `insertText` + `endOfSegmentLocation {tabId}`; mọi location/range khi ghi
  thẻ đều mang `tabId` (Range có tabId — đã đối chiếu tài liệu Docs API 10/2026).
  ID sổ tay nằm ở keyring `viet-dub-google/gdocs_master`; nếu doc bị xoá → tìm lại
  theo tên qua Drive rồi tự tạo mới. target_id cũ trong chrome.storage bị server
  bỏ qua cho nhánh gdocs (chỉ còn ý nghĩa với Notion).
- Popup: dòng trạng thái Google không lặp lại email (email chỉ ở dòng tài khoản kèm
  nút “Ngắt kết nối”).
- **Chính sách phát lồng tiếng mới (yêu cầu của user, 10/2026): video không bao giờ
  tự dừng vì lồng tiếng.** Bỏ `pauseForWait`/`pauseForError` (từng dừng video khi
  audio chưa kịp hoặc lỗi); thay bằng: câu lỗi → state `skipfail` và video chạy tiếp
  (`Thử lại lỗi` retry được), audio vượt qua điểm câu sau → cắt nốt câu trước
  (`cutCurrentAudio`) và đọc ngay câu sau, audio vào giữa câu (tua/reload) → nhảy
  audio tới đúng vị trí bằng `midCueResumeFraction` thay vì đọc lại từ đầu.
  Dò phụ đề YouTube phải bật CC thật nudge; nếu CC vốn tắt thì `restoreCcButton`
  tắt lại sau khi lấy xong script (phụ đề không “tự mở”).
- **Nút lưu tóm tắt là thủ công 100%** (yêu cầu của user): nhãn widget đổi thành
  “💾 Lưu tóm tắt để học”; không hề có auto-save (chỉ bấm mới tóm tắt/lưu).
  buildCues tách câu ở khoảng lặng ≥ 0.45s (trước 0.65s, min độ dài 14) để khoảng
  nghỉ giữa câu lồng tiếng bám nhịp giọng đọc gốc.
- Edge TTS rớt thoáng NoAudioReceived (tái hiện trên máy: cùng câu/giọng lần 1-2
  FAIL lần 3 OK): `synth()` tự retry theo plan HoaiMy → HoaiMy (0.7s) → fallback
  NamMinh (1.4s); stream 0 byte cũng tính là fail (`test_tts_retry.py`, 4 case).- Content script gọi server qua `background.js` (service worker) vì `local_only_guard`
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
- **Lỗi `CredWrite` 1783 đã được chẩn đoán bằng dữ liệu thật**: ghi thử blob tăng dần
  qua keyring trên chính máy user — ≤1024 byte OK, ~1,5–2 KB bị chặn đúng lỗi
  `(1783, 'CredWrite', 'The stub received bad data')` mà user gặp. Nguyên nhân:
  `save_tokens()` cũ lưu **toàn bộ** phản hồi token của Google, trong đó `id_token`
  là JWT ~1200 ký tự (không bao giờ được đọc lại). Đã sửa: chỉ lưu `access_token`,
  `refresh_token`, `expires_in`, `email`, `saved_at`; từ chối ghi sẽ bắt lỗi và ném
  thông báo tiếng Việt rõ ràng. Kiểm chứng end-to-end: blob đầy đủ 1653 byte tái
  hiện lỗi 1783, bản rút gọn ghi OK, vòng lặp đọc-ghi đúng, xoá sạch sau đó.
  Đây cũng là nguyên nhân vì sao trước đó thử lưu token rác "at" không thấy để lại
  dấu vết đọc được.
- `google_auth.py:get_valid_access_token()` giờ giữ lại `email` khi refresh
  (Google không trả email trong refresh response — trước kia email biến mất
  sau lần refresh đầu).

### Verified
- `python -m py_compile` toàn bộ server OK.
- `unittest discover` : 32 test, chỉ 1 lỗi **có sẵn từ trước** ở
  `test_provider_settings.test_secret_is_not_written_to_sqlite_and_is_not_returned`
  (Windows khoá file sqlite khi xoá temp dir — không liên quan thay đổi này).
- Server chạy thật: `/health`, `/version`, `/google-auth/*`, `/notion/*`, `/summarize` trả đúng mã lỗi.
- Lưu token thật bị rút gọn qua keyring: ghi/read/xoá OK trên máy user (xem Progress State).
- Shell của agent từng mất toàn bộ stdout (mọi lệnh đều "(no output)"); xử lý bằng
  cách redirect output vào file rồi đọc lại bằng tool read. Nếu tái diễn: dùng
  `cmd /c "... > check.txt 2>&1"` rồi đọc file đó.
- `node --check` cho `content.js`, `background.js`, `popup.js` OK.
- `tests/check-wiring.js` + `tests/jsdom/smoke.js` pass toàn bộ.

### Next (việc user cần tự làm)
1. **Google Auth Platform → Khách gia (Audience) → Người dùng thử**: thêm
   `an.207ct68609@gmail.com` — đã xong bước này (lỗi `403 access_denied` trước đó
   chính là vì thiếu test user; đã khắc phục).
2. Reload extension tại `chrome://extensions` (↻) — popup có thêm bảng tra lỗi
   đăng nhập và nút **Kiểm tra** giờ hiển thị "bản mới / bản cũ" của server.
3. Mở popup → **Tiếp tục với Google** → chọn tài khoản → duyệt quyền (sẽ thấy
   cảnh báo "chưa xác minh" → **Nâng cao → Đến tran-sub (không an toàn)** — bình
   thường ở chế độ Testing). Trang callback giờ hiển thị "✔ Đã kết nối Google".
   Token được lưu rút gọn nên không còn lỗi CredWrite 1783.
4. Nếu vẫn lỗi: tra theo bảng lỗi đăng nhập Google trong README, hoặc gửi URL
   trang lỗi (không chứa secret).
5. Tạo Notion integration + invite vào trang cha, dán token + page ID vào popup (nếu dùng Notion).
6. Lưu API key AI (Gemini/OpenAI/Claude) trong popup — cần cho `/summarize`
   (hiện đang trả 400 vì chưa có provider AI).
7. Test thật trên một video YouTube và một bài Udemy có phụ đề tiếng Anh.
8. Commit & push.

### Phát hành cho người khác (đã ghi trong README)
- Mỗi người dùng đăng nhập bằng Google account của chính họ.
- Phải thêm email họ vào Test users; tối đa 100 tài khoản ở chế độ Testing.
- Redirect URI là loopback cố định nên giống nhau mọi máy — không cần đăng ký lại.
- Muốn mở cho công chúng: phải publish *In production* + xác minh sensitive scope
  (3–5 ngày, cần trang chủ + privacy policy + video demo).
- `client_secret` giờ là tuỳ chọn → hỗ trợ client Desktop app (ship sẵn Client ID).
