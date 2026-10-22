// Smoke test DOM: nạp content.js vào jsdom và kiểm tra widget mount + nút Tóm tắt.
//
// Chạy:
//   cd tests/jsdom
//   npm install
//   npm test
//
// File này KHÔNG nằm trong thư mục extension nên không bị đóng gói vào tiện ích.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const EXT_DIR = path.resolve(__dirname, "..", "..", "extension");
const code = fs.readFileSync(path.join(EXT_DIR, "content.js"), "utf8");

let failed = 0;
function check(cond, message, extra = "") {
  if (cond) console.log("  OK   " + message);
  else {
    console.error("  FAIL " + message + (extra ? " -> " + extra : ""));
    failed++;
  }
}

function makeChrome() {
  const store = { enabled: false };
  return {
    storage: {
      local: {
        get: (keys) => {
          const out = {};
          for (const k of [].concat(keys)) out[k] = store[k];
          return Promise.resolve(out);
        },
        set: (obj) => { Object.assign(store, obj); return Promise.resolve(); },
        remove: (keys) => { for (const k of [].concat(keys)) delete store[k]; return Promise.resolve(); },
      },
      onChanged: { addListener() {} },
    },
    runtime: {
      sendMessage: () => Promise.resolve({ ok: false, error: "stub" }),
      onMessage: { addListener() {} },
    },
  };
}

function setupDom(html, url, extra) {
  const dom = new JSDOM(html, { url, runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;

  // Stub các API trình duyệt mà jsdom thiếu.
  window.chrome = makeChrome();
  window.performance.setResourceTimingBufferSize = () => {};
  window.PerformanceObserver = class { observe() {} };
  window.URL.createObjectURL = () => "blob:stub";
  window.URL.revokeObjectURL = () => {};
  window.indexedDB = { open: () => ({ addEventListener() {} }) };
  if (!window.performance.getEntriesByType) window.performance.getEntriesByType = () => [];

  for (const video of window.document.querySelectorAll("video")) {
    Object.defineProperties(video, {
      playbackRate: { get: () => 1, configurable: true },
      paused: { get: () => true, configurable: true },
      ended: { get: () => false, configurable: true },
      videoWidth: { get: () => 640, configurable: true },
      videoHeight: { get: () => 360, configurable: true },
      readyState: { get: () => 4, configurable: true },
      clientWidth: { get: () => 640, configurable: true },
      textTracks: { get: () => [], configurable: true },
    });
  }

  // `let` trong eval không chia sẻ scope giữa 2 lần eval, nên hook phải được
  // nối vào CÙNG một chuỗi eval với content.js.
  window.eval(code + "\n" + (extra || ""));
  return window;
}

function testYoutube() {
  console.log("[youtube]");
  const window = setupDom(
    `<!doctype html><html><body>
       <div id="movie_player">
         <video class="html5-main-video"></video>
       </div>
     </body></html>`,
    "https://www.youtube.com/watch?v=abc123"
  );

  const host = window.document.querySelector("#vietdub-widget-host");
  check(Boolean(host), "widget host đã được mount vào #movie_player");
  check(host && host.parentElement.id === "movie_player", "host nằm trong player container");
  check(host && host.style.position === "absolute", "YouTube dùng position:absolute", host && host.style.position);

  const shadow = host && host.shadowRoot;
  check(Boolean(shadow), "shadow root tồn tại");
  const summarize = shadow && shadow.getElementById("summarize");
  check(Boolean(summarize), "nút Tóm tắt có trong widget");
  check(summarize && summarize.hidden === true, "nút Tóm tắt ẩn khi chưa tải script");
  check(summarize && summarize.disabled === true, "nút Tóm tắt disabled khi chưa tải script");

  const summaryBox = shadow && shadow.getElementById("summaryBox");
  check(Boolean(summaryBox), "khung xem tóm tắt tồn tại");
  check(summaryBox && summaryBox.hidden === true, "khung tóm tắt ẩn mặc định");

  // Bấm nút: chưa tải script -> phải báo lỗi qua status (chứng minh listener đã nối).
  summarize.dispatchEvent(new window.Event("click"));
  const status = shadow && shadow.getElementById("status");
  setTimeout(() => {
    const text = status ? status.textContent : "";
    check(text.includes("Tải script"), "click Tóm tắt -> báo 'Tải script phụ đề trước khi tóm tắt'", text);
    finish(testUdemy);
  }, 50);
}

function testUdemy() {
  console.log("[udemy]");
  const window = setupDom(
    `<!doctype html><html><body>
       <div data-purpose="player-video">
         <div class="video-player"><video></video></div>
       </div>
     </body></html>`,
    "https://www.udemy.com/course/hoc-fastapi/learn/lecture/12345678"
  );

  const host = window.document.querySelector("#vietdub-widget-host");
  check(Boolean(host), "widget mount được trên Udemy");
  check(host && host.parentElement.getAttribute("data-purpose") === "player-video",
    "host nằm trong player Udemy");
  check(host && host.style.position === "absolute", "Udemy dùng position:absolute trong player");

  const shadow = host && host.shadowRoot;
  const summarize = shadow && shadow.getElementById("summarize");
  check(Boolean(summarize), "nút Tóm tắt có trên Udemy");

  // Test parseVtt qua hành vi công khai: nút load phải bấm được (không throw).
  const load = shadow && shadow.getElementById("load");
  check(Boolean(load), "nút Tải script tồn tại trên Udemy");
  let threw = null;
  try { load.dispatchEvent(new window.Event("click")); } catch (e) { threw = e; }
  check(threw === null, "click Tải script không throw", threw && threw.message);

  finish(testNoPlayer);
}

function testNoPlayer() {
  console.log("[generic - không có video]");
  const window = setupDom(
    `<!doctype html><html><body><p>Không có video</p></body></html>`,
    "https://example.com/page"
  );
  let host = null;
  let threw = null;
  try { host = window.document.querySelector("#vietdub-widget-host"); } catch (e) { threw = e; }
  check(threw === null, "không crash khi trang không có video");
  // Không có player riêng -> widget vẫn mount vào body ở chế độ fixed (để vẫn dùng được).
  check(Boolean(host), "widget vẫn mount vào body");
  check(host && host.style.position === "fixed", "dùng position:fixed khi không có player container",
    host && host.style.position);
  finish(testSummarizeFlow);
}

function testSummarizeFlow() {
  console.log("[luồng Tóm tắt đầy đủ]");
  const html = `<!doctype html><html><body>
      <div id="movie_player"><h1 class="style-scope ytd-watch-metadata">Bài học FastAPI</h1>
        <video class="html5-main-video"></video></div>
    </body></html>`;
  const hook = `
    globalThis.__vd = {
      prime() {
        scriptLoaded = true;
        cues = [
          { start: 0, end: 2, en: "one", vi: "", state: "new", audio: null, duration: 0 },
          { start: 2, end: 4, en: "two", vi: "", state: "new", audio: null, duration: 0 },
          { start: 4, end: 6, en: "three", vi: "", state: "new", audio: null, duration: 0 }
        ];
      }
    };
  `;
  const window = setupDom(html, "https://www.youtube.com/watch?v=abc123", hook);

  // Ghi lại message gửi lên background và trả kết quả giả lập.
  const sent = [];
  window.chrome.runtime.sendMessage = (msg) => {
    sent.push(msg);
    if (msg.type === "translate_batch") {
      return Promise.resolve({ ok: true, items: msg.texts.map((t) => ({ text: "VI:" + t })) });
    }
    if (msg.type === "summarize") {
      return Promise.resolve({
        ok: true,
        summary: "## 🎯 Mục tiêu\n- Học FastAPI\n\n## ✅ Tóm tắt một câu\n- Xong",
        target: msg.export_to,
        target_id: "doc-1",
        target_url: "https://docs.google.com/document/d/doc-1/edit",
        message: "Đã tóm tắt bài học. Đã lưu vào Google Docs.",
      });
    }
    return Promise.resolve({ ok: false });
  };

  const host = window.document.querySelector("#vietdub-widget-host");
  const shadow = host.shadowRoot;

  // Đưa content.js vào trạng thái "đã tải script".
  window.__vd.prime();

  const summarize = shadow.getElementById("summarize");
  summarize.hidden = false;
  summarize.disabled = false;
  summarize.dispatchEvent(new window.Event("click"));

  setTimeout(() => {
    const summarizeMsg = sent.find((m) => m.type === "summarize");
    check(Boolean(summarizeMsg), "đã gửi message summarize");
    check(summarizeMsg && summarizeMsg.export_to === "gdocs", "export_to lấy từ storage (gdocs)",
      summarizeMsg && summarizeMsg.export_to);
    check(summarizeMsg && summarizeMsg.vi_cues.length === 3, "gửi đủ 3 câu phụ đề đã dịch",
      summarizeMsg && String(summarizeMsg.vi_cues.length));
    check(summarizeMsg && summarizeMsg.vi_cues[0] === "VI:one", "cue được dịch tự động trước khi tóm tắt",
      summarizeMsg && summarizeMsg.vi_cues[0]);

    const box = shadow.getElementById("summaryBox");
    const text = shadow.getElementById("summaryText");
    check(box && box.hidden === false, "khung tóm tắt được mở");
    check(text && text.textContent.includes("Mục tiêu"), "nội dung tóm tắt hiển thị trong widget",
      text && text.textContent.slice(0, 40));

    const link = shadow.getElementById("summaryLink");
    const anchor = shadow.getElementById("summaryLinkAnchor");
    check(link && link.style.display === "block", "link mở tài liệu hiển thị");
    check(anchor && anchor.href.includes("docs.google.com"), "link trỏ đúng tài liệu",
      anchor && anchor.href);

    const status = shadow.getElementById("status");
    check(status && status.textContent.includes("Đã tóm tắt"), "status báo tóm tắt thành công",
      status && status.textContent);

    check(summarize.disabled === false, "nút Tóm tắt bật lại sau khi xong");
    check(summarize.textContent.includes("Tóm tắt"), "nhãn nút trở lại bình thường",
      summarize.textContent);
    finish();
  }, 300);
}

function finish(next) {
  if (next) return next();
  console.log(failed === 0 ? "\nTất cả smoke test đã qua." : `\n${failed} smoke test thất bại.`);
  process.exit(failed === 0 ? 0 : 1);
}

testYoutube();
