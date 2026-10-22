// Kiểm tra tĩnh: mọi getElementById trong widget phải có id tương ứng trong HTML,
// và mọi `ui.<prop>` được dùng phải được khai báo trong object `ui = {...}`.
//
// Chạy: node tests/check-wiring.js   (không cần cài thêm gì)
const fs = require("fs");
const path = require("path");

const EXT = path.resolve(__dirname, "..", "extension");
const src = fs.readFileSync(path.join(EXT, "content.js"), "utf8");

let failed = 0;
function check(cond, message) {
  if (cond) {
    console.log("  OK   " + message);
  } else {
    console.error("  FAIL " + message);
    failed++;
  }
}

// 1. HTML trong shadow root
const htmlMatch = src.match(/shadow\.innerHTML\s*=\s*`([\s\S]*?)`;/);
check(Boolean(htmlMatch), "tìm thấy shadow.innerHTML template");
const html = htmlMatch ? htmlMatch[1] : "";
const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
console.log("  ids trong template: " + [...htmlIds].join(", "));

// 2. Các lời gọi shadow.getElementById
const wanted = [...src.matchAll(/shadow\.getElementById\("([^"]+)"\)/g)].map((m) => m[1]);
check(wanted.length > 0, "có gọi shadow.getElementById");
for (const id of new Set(wanted)) {
  check(htmlIds.has(id), `id "${id}" tồn tại trong template`);
}

// 3. Object ui = { ... }
const uiBlock = src.match(/ui\s*=\s*\{([\s\S]*?)\n\s*\};/);
check(Boolean(uiBlock), "tìm thấy object ui = { ... }");
const uiKeys = new Set(
  [...(uiBlock ? uiBlock[1] : "").matchAll(/(?:^|[\s{,])([A-Za-z_$][\w$]*)\s*:/g)].map((m) => m[1])
);
console.log("  ui keys: " + [...uiKeys].join(", "));

// 4. Mọi ui.<prop> được dùng
const used = new Set([...src.matchAll(/\bui\.([A-Za-z_$][\w$]*)/g)].map((m) => m[1]));
for (const prop of used) {
  check(uiKeys.has(prop), `ui.${prop} được khai báo`);
}

// 5. Listener của các nút chính
check(/ui\.summarize\.addEventListener\("click",\s*summarizeAndExport\)/.test(src),
  "nút summarize có listener riêng");
check(/ui\.load\.addEventListener\("click",\s*onLoadButton\)/.test(src),
  "nút load vẫn còn listener");

// 6. Hàm dùng trong widget phải tồn tại
for (const fn of ["summarizeAndExport", "onLoadButton", "toggleDubbing"]) {
  check(new RegExp(`function\\s+${fn}\\s*\\(`).test(src), `có function ${fn}`);
}

// 7. summarizeAndExport phải gửi đúng field API mới
check(/type:\s*"summarize"/.test(src), "gửi message type=summarize");
check(/export_to:\s*target/.test(src), "gửi export_to=target");
check(/target_id:/.test(src), "gửi target_id");
check(!/export_to_docs:\s*true/.test(src), "không còn dùng export_to_docs kiểu cũ");
check(/result\.target_url/.test(src), "đọc result.target_url");
check(/result\.target_id/.test(src), "đọc result.target_id");

// 8. background.js phải xử lý các message mà content.js gửi
const bg = fs.readFileSync(path.join(EXT, "background.js"), "utf8");
// Chỉ lấy các "type" thực sự là message của extension (bỏ qua PerformanceObserver...).
const NON_MESSAGE_TYPES = new Set(["resource", "navigation", "mark", "measure", "longtask"]);
const sentTypes = new Set(
  [...src.matchAll(/sendMessage\(\s*\{[^}]*?type:\s*"([a-z_]+)"/g)].map((m) => m[1])
    .filter((t) => !NON_MESSAGE_TYPES.has(t))
);
console.log("  content.js gửi: " + [...sentTypes].join(", "));
for (const type of sentTypes) {
  const handled =
    bg.includes(`msg?.type === "${type}"`) || bg.includes(`msg?.type !== "${type}"`);
  check(handled, `background.js xử lý "${type}"`);
}

// 9. manifest host_permissions đủ cho các fetch của background
const manifest = JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8"));
check(manifest.host_permissions.includes("http://127.0.0.1:8765/*"), "có quyền gọi server local");
check(manifest.host_permissions.some((h) => h.includes("udemy")), "có quyền với udemy");
check(manifest.content_scripts[0].js.includes("content.js"), "content.js được inject");

// 10. popup.html id phải khớp popup.js
const popupHtml = fs.readFileSync(path.join(EXT, "popup.html"), "utf8");
const popupJs = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");
const popupIds = new Set([...popupHtml.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
const popupWanted = [...popupJs.matchAll(/getElementById\("([^"]+)"\)/g)].map((m) => m[1]);
console.log("  popup ids dùng: " + [...new Set(popupWanted)].join(", "));
for (const id of new Set(popupWanted)) {
  check(popupIds.has(id), `popup id "${id}" tồn tại trong popup.html`);
}

console.log(failed === 0 ? "\nTất cả kiểm tra tĩnh đã qua." : `\n${failed} kiểm tra thất bại.`);
process.exit(failed === 0 ? 0 : 1);
