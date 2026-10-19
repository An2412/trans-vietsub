# Viet Dub — prototype 0.5.5

Chrome Extension và server Python cục bộ để dịch phụ đề YouTube sang tiếng Việt, sau đó đọc lồng tiếng theo cuốn chiếu. Bản này hỗ trợ chọn provider dịch trong popup: Gemini, OpenAI GPT, Anthropic Claude hoặc Ollama Cloud. TTS vẫn dùng Edge TTS như trước.

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
4. Nút **Kiểm tra** ở trên cùng chỉ kiểm tra server và tải metadata cấu hình đã lưu khi bạn chủ động bấm; mở popup không tự kết nối.
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

Widget chỉ hiển thị tốc độ YouTube hiện tại; chỉnh tốc độ trực tiếp trong trình phát. Kéo thanh tiêu đề để di chuyển widget, bấm **−** để thu nhỏ thành icon **VD**; kéo icon rồi thả chỉ di chuyển icon. Khi YouTube phát quảng cáo, Viet Dub tạm ngưng tạo/phát giọng và tiếp tục sau quảng cáo.

## Tài liệu API chính thức

- [Gemini API keys và bảo mật](https://ai.google.dev/gemini-api/docs/api-key), [Gemini Interactions API](https://ai.google.dev/gemini-api/docs/interactions)
- [OpenAI API overview](https://developers.openai.com/api/reference/overview/), [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Anthropic API overview](https://platform.claude.com/docs/en/api/overview), [Messages API](https://platform.claude.com/docs/en/api/messages/create)
- [Python keyring](https://keyring.readthedocs.io/en/latest/)
