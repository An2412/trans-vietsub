"""Tạo bản preview tĩnh của popup (mock chrome.* + fetch) để xem giao diện
mà không cần cài extension. Chạy: python tools/make_popup_preview.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
OUT = ROOT / "tools" / "popup-preview.html"

html = (EXT / "popup.html").read_text(encoding="utf-8")
js = (EXT / "popup.js").read_text(encoding="utf-8")

STUB = r"""
<script>
// ---- Mock chrome.* để popup chạy được ngoài extension ----
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
  tabs: { create: () => Promise.resolve({}) },
};

// ---- Mock server ----
const MOCK = {
  "/health": { status: "ok" },
  "/provider-config": {
    provider: "gemini", model: "gemini-3.8-flash", has_api_key: true,
    secure_store_ready: true, store_message: "",
  },
  "/google-auth/status": {
    // Trạng thái thật của máy user lúc này: đã có OAuth client, chưa đăng nhập.
    "connected": false,
    "email": "",
    "client_configured": true,
  },
  "/notion/status": { configured: true, has_token: true, has_parent_page: true, parent_page_id: "1f2e3d4c5b6a79881f2e3d4c5b6a7988" },
};
const _realFetch = window.fetch;
window.fetch = async (url, options = {}) => {
  const path = String(url).replace("http://127.0.0.1:8765", "");
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

html = html.replace(
    '<script src="popup.js"></script>',
    STUB + "<script>\n" + js + "\n</script>",
)
# Mở rộng khung nhìn cho dễ xem trong Review pane.
html = html.replace(
    "body { width: 340px; max-height: 600px; overflow-y: auto;",
    "body { width: 340px; max-height: none; overflow: visible; margin: 0 auto;",
)

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(html, encoding="utf-8")
print(f"Wrote {OUT}")
