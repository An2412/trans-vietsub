"""Tạo bản preview tĩnh của popup để XEM GIAO DIỆN mà không cần cài extension.

CẢNH BÁO: file sinh ra KHÔNG kết nối server thật — mọi phản hồi đều là dữ liệu
giả định nghĩa trong MOCK bên dưới. Nó chỉ dùng để xem bố cục/ màu sắc.
Popup thật là popup của extension (bấm icon Viet Dub trên thanh công cụ Chrome).

Chạy: python tools/make_popup_preview.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
OUT = ROOT / "tools" / "popup-preview.html"

html = (EXT / "popup.html").read_text(encoding="utf-8")
js = (EXT / "popup.js").read_text(encoding="utf-8")

# Dữ liệu giả — cố ý để trống/đánh dấu rõ để không ai nhầm là cấu hình thật.
STUB = r"""
<script>
// ================= BẢN XEM TRƯỚC =================
// Toàn bộ dữ liệu dưới đây là GIẢ. Không có request nào tới server thật.
// ==================================================
const _store = {
  enabled: true,
  provider: "gemini",
  model: "gemini-3.8-flash",
  exportTarget: "gdocs",
  appendExisting: true,
};
window.chrome = {
  storage: {
    local: {
      get: (keys) => Promise.resolve(
        Object.fromEntries([].concat(keys).filter((k) => k in _store).map((k) => [k, _store[k]]))
      ),
      set: (obj) => { Object.assign(_store, obj); return Promise.resolve(); },
      remove: (keys) => { for (const k of [].concat(keys)) delete _store[k]; return Promise.resolve(); },
    },
    onChanged: { addListener() {} },
  },
  tabs: {
    create: () => Promise.resolve({}),
    // Bấm "Tiếp tục với Google" trong bản xem trước chỉ hiện cảnh báo.
    query: () => Promise.resolve([]),
  },
};

const MOCK = {
  "/health": { status: "ok" },
  "/provider-config": {
    provider: "gemini", model: "gemini-3.8-flash", has_api_key: false,
    secure_store_ready: true, store_message: "",
  },
  "/google-auth/status": {
    connected: false,
    email: "",
    client_configured: true,
    last_error: "",
  },
  "/notion/status": {
    configured: false, has_token: false, has_parent_page: false, parent_page_id: "",
  },
};

window.fetch = async (url, options = {}) => {
  const path = String(url).replace("http://127.0.0.1:8765", "");
  // Chặn mọi thao tác ghi: bản xem trước không được giả vờ "đã lưu".
  if (options && options.method && options.method !== "GET") {
    return {
      ok: false,
      status: 400,
      json: async () => ({ detail: "Đây là bản xem trước giao diện, không lưu được gì." }),
      headers: { get: () => "0" },
    };
  }
  const body = MOCK[path];
  return {
    ok: Boolean(body),
    status: body ? 200 : 404,
    json: async () => body || { detail: "not found" },
    headers: { get: () => "0" },
  };
};
</script>
"""

BANNER = """
<div style="position:sticky;top:0;z-index:99;margin:-14px -14px 12px;padding:7px 10px;
            background:#b3261e;color:#fff;font:600 11px/1.45 system-ui,sans-serif;text-align:center;">
  BẢN XEM TRƯỚC GIAO DIỆN — dữ liệu giả, KHÔNG kết nối server thật.<br>
  <span style="font-weight:400;opacity:.9">
    Popup thật: bấm icon Viet Dub trên thanh công cụ Chrome.
  </span>
</div>
"""

html = html.replace(
    '<script src="popup.js"></script>',
    STUB + "<script>\n" + js + "\n</script>",
)
html = html.replace("<body>", "<body>" + BANNER, 1)
# Mở rộng khung nhìn cho dễ xem trong Review pane.
html = html.replace(
    "body { width: 340px; max-height: 600px; overflow-y: auto;",
    "body { width: 340px; max-height: none; overflow: visible; margin: 0 auto;",
)

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(html, encoding="utf-8")
print(f"Wrote {OUT}")
