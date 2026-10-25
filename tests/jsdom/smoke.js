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
    __store: store,
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
    if (msg.type === "google_auth_status") {
      return Promise.resolve({ ok: true, connected: true, email: "a@b.c" });
    }
    if (msg.type === "notion_status") {
      return Promise.resolve({ ok: true, configured: false });
    }
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
    check(summarize.textContent.includes("Lưu tóm tắt"), "nhãn nút trở lại bình thường",
      summarize.textContent);
    finish(testSeekRecovery);
  }, 300);
}

function testSeekRecovery() {
  console.log("[seek - phục hồi lồng tiếng]");
  const html = `<!doctype html><html><body>
      <div id="movie_player"><video class="html5-main-video"></video></div>
    </body></html>`;
  const hook = `
    globalThis.__vd = {
      prime() {
        scriptLoaded = true;
        enabled = true;
        autoPaused = false;
        bindVideo();
        const v = getVideo();
        let t = 0;
        Object.defineProperty(v, "currentTime", {
          get: () => t, set: (x) => { t = x; }, configurable: true
        });
        cues = [
          { start: 0, end: 2, en: "da qua", vi: "", state: "ready", audio: null, duration: 1, batched: true },
          { start: 2, end: 4, en: "da qua 2", vi: "", state: "ready", audio: null, duration: 1, batched: true },
          { start: 180, end: 182, en: "sau khi tua", vi: "", state: "new", audio: null, duration: 0,
            batched: true, batchPromise: Promise.resolve() },
          { start: 183, end: 185, en: "ket loading", vi: "", state: "loading", audio: null, duration: 0,
            loadingSince: Date.now() - 60000 },
          { start: 186, end: 188, en: "moi loading", vi: "", state: "loading", audio: null, duration: 0,
            loadingSince: Date.now() }
        ];
      },
      seek(seconds) {
        const v = getVideo();
        v.currentTime = seconds;
        v.dispatchEvent(new window.Event("seeking"));
      },
      cues: () => cues,
      heal: (index) => healStuckCue(cues[index]),
      playCorrupt() {
        const cue = { start: 0, end: 2, en: "audio hong", vi: "", state: "ready",
          audio: "!!!khong-phai-base64!!!", duration: 0 };
        cues.push(cue);
        playCue(cue, 0);
        return cue.state;
      }
    };
  `;
  const window = setupDom(html, "https://www.youtube.com/watch?v=abc123", hook);
  window.__vd.prime();
  window.__vd.seek(180);
  const cues = window.__vd.cues();

  check(cues[0].state === "skipped" && cues[1].state === "skipped",
    "tua qua 3:00 -> câu cũ bị đánh dấu skipped", cues[0].state + "/" + cues[1].state);
  check(cues[2].state === "new", "câu sau vị trí tua vẫn ở hàng đợi", cues[2].state);
  check(cues[2].batched === false && cues[2].batchPromise === null,
    "cờ batched/batchPromise cũ được xóa để gom dịch lại");

  check(window.__vd.heal(3) === true && cues[3].state === "new",
    "câu kẹt loading quá 30s được đưa về hàng đợi", cues[3].state);
  check(window.__vd.heal(4) === false && cues[4].state === "loading",
    "câu mới loading chưa quá hạn thì không bị đụng", cues[4].state);

  let corruptThrew = null;
  let corruptState = "";
  try { corruptState = window.__vd.playCorrupt(); } catch (error) { corruptThrew = error; }
  check(corruptThrew === null, "audio hỏng không làm ném lỗi ra vòng phát",
    corruptThrew && corruptThrew.message);
  check(corruptState === "skipped", "câu có audio hỏng được đánh dấu skipped",
    corruptState);

  finish(testVoiceSwitch);
}

function testVoiceSwitch() {
  console.log("[đổi giọng - không cắt câu đang đọc]");
  const html = `<!doctype html><html><body>
      <div id="movie_player"><video class="html5-main-video"></video></div>
    </body></html>`;
  const hook = `
    globalThis.__vd = {
      prime() {
        scriptLoaded = true;
        enabled = true;
        autoPaused = false;
        const v = getVideo();
        let t = 10;
        Object.defineProperty(v, "currentTime", {
          get: () => t, set: (x) => { t = x; }, configurable: true
        });
        cues = [
          { start: 8, end: 12, en: "dang doc", vi: "đang đọc", state: "played", audio: "AAAA", audioVoice: "vi-VN-HoaiMyNeural", duration: 4 },
          { start: 13, end: 16, en: "sap toi", vi: "sắp tới", state: "ready", audio: "BBBB", audioVoice: "vi-VN-HoaiMyNeural", duration: 3 },
          { start: 1, end: 5, en: "da qua", vi: "đã qua", state: "played", audio: "CCCC", audioVoice: "vi-VN-HoaiMyNeural", duration: 4 },
          { start: 17, end: 19, en: "da co giong moi", vi: "đã có giọng mới", state: "ready", audio: "DDDD", audioVoice: "vi-VN-NamMinhNeural", duration: 2 }
        ];
        globalThis.__fakeAudio = { paused: false, pause() { this.paused = true; } };
        currentAudio = globalThis.__fakeAudio;
        currentCue = cues[0];
      },
      changeVoice() {
        fatalError = true; // chặn prefetch/playTick trong test
        setVoice("vi-VN-NamMinhNeural");
        fatalError = false;
      },
      cues: () => cues,
      currentAudio: () => currentAudio,
      voice: () => voiceId
    };
  `;
  const window = setupDom(html, "https://www.youtube.com/watch?v=abc123", hook);
  window.__vd.prime();
  window.__vd.changeVoice();
  const cues = window.__vd.cues();

  check(window.__vd.voice() === "vi-VN-NamMinhNeural", "giọng đọc đã đổi",
    window.__vd.voice());
  check(window.__vd.currentAudio() === window.__fakeAudio && window.__fakeAudio.paused === false,
    "câu đang đọc KHÔNG bị cắt khi đổi giọng", String(window.__fakeAudio.paused));
  check(cues[0].state === "played" && cues[0].audio === "AAAA",
    "câu đang đọc giữ nguyên audio giọng cũ", cues[0].state);
  check(cues[1].state === "new" && cues[1].audio === null,
    "câu chưa phát được đưa về hàng đợi tải giọng mới", cues[1].state);
  check(cues[1].vi === "sắp tới",
    "bản dịch được giữ lại (không phụ thuộc giọng đọc)", cues[1].vi);
  check(cues[2].state === "played" && cues[2].audio === "CCCC",
    "câu đã phát trước đó không bị đụng", cues[2].state);
  check(cues[3].state === "ready" && cues[3].audio === "DDDD",
    "câu đã có audio đúng giọng mới được giữ nguyên", cues[3].state);

  finish(testSkipfailRetry);
}

function testSkipfailRetry() {
  console.log("[tự thử lại câu lỗi]");
  const html = `<!doctype html><html><body>
      <div id="movie_player"><video class="html5-main-video"></video></div>
    </body></html>`;
  const hook = `
    globalThis.__vd = {
      prime() {
        cues = [
          { start: 10, end: 12, en: "loi sap toi", vi: "", state: "skipfail" },
          { start: 20, end: 22, en: "loi xa", vi: "", state: "skipfail" },
          { start: 0, end: 2, en: "loi da qua", vi: "", state: "skipfail" },
          { start: 21, end: 23, en: "het luot", vi: "", state: "skipfail", retryCount: MAX_AUTO_RETRIES }
        ];
        skipfailBackoffMs = 0;
        skipfailRetryAt = 0;
      },
      fail() {
        cues[0].state = "skipfail";
        scheduleSkipfailRetry();
      },
      run(now, ahead, nowMs) { return retrySkippedCues(now, ahead, nowMs); },
      retryAt: () => skipfailRetryAt,
      backoff: () => skipfailBackoffMs,
      cues: () => cues
    };
  `;
  const window = setupDom(html, "https://www.youtube.com/watch?v=abc123", hook);
  window.__vd.prime();
  window.__vd.fail();

  check(window.__vd.backoff() === 5000, "lỗi lần đầu: hẹn tự thử lại sau 5s",
    String(window.__vd.backoff()));
  check(window.__vd.run(10, 60, Date.now()) === 0,
    "chưa tới giờ hẹn thì chưa thử lại");
  check(window.__vd.run(10, 60, Date.now() + 60000) === 2,
    "tới giờ: chỉ thử các câu lỗi còn trong vùng phát");

  const cues = window.__vd.cues();
  check(cues[0].state === "new" && cues[0].retryCount === 1,
    "câu lỗi sắp phát được đưa lại hàng đợi", cues[0].state);
  check(cues[1].state === "new" && cues[1].retryCount === 1,
    "câu lỗi phía sau cũng được thử lại", cues[1].state);
  check(cues[2].state === "skipfail", "câu đã trôi qua quá lâu không thử lại",
    cues[2].state);
  check(cues[3].state === "skipfail", "câu hết lượt tự thử vẫn giữ skipfail",
    cues[3].state);

  window.__vd.fail();
  check(window.__vd.backoff() === 10000, "lỗi lại: giãn cách tăng lên 10s",
    String(window.__vd.backoff()));

  finish(testVoicePicker);
}

function testVoicePicker() {
  console.log("[chọn giọng đọc]");
  const html = `<!doctype html><html><body>
      <div id="movie_player"><video class="html5-main-video"></video></div>
    </body></html>`;
  const hook = `
    globalThis.__vd = {
      speak: () => requestSpeak("xin chào"),
      currentVoice: () => voiceId,
      reload: () => {
        voiceOptionsLoaded = false;
        voicePresets = [];
        return loadVoiceOptions();
      }
    };
  `;
  const window = setupDom(html, "https://www.youtube.com/watch?v=abc123", hook);
  const sent = [];
  window.chrome.runtime.sendMessage = (msg) => {
    sent.push(msg);
    if (msg.type === "voices") {
      return Promise.resolve({
        ok: true,
        voices: [
          { id: "vi-VN-HoaiMyNeural", label: "HoaiMy · nữ" },
          { id: "vi-VN-NamMinhNeural", label: "NamMinh · nam" }
        ]
      });
    }
    return Promise.resolve({ ok: false });
  };

  const host = window.document.querySelector("#vietdub-widget-host");
  const shadow = host && host.shadowRoot;
  const select = shadow && shadow.getElementById("voice");
  const customRow = shadow && shadow.getElementById("voiceCustomRow");
  const customInput = shadow && shadow.getElementById("voiceCustom");
  const apply = shadow && shadow.getElementById("voiceApply");
  check(Boolean(select), "widget có ô chọn giọng đọc");

  window.__vd.reload();

  setTimeout(() => {
    check(select.options.length === 3, "có 2 giọng + mục Voice ID khác",
      String(select.options.length));
    check(select.value === "vi-VN-HoaiMyNeural", "mặc định là HoaiMy", select.value);
    check(sent.some((m) => m.type === "voices"), "widget hỏi server danh sách giọng");

    select.value = "vi-VN-NamMinhNeural";
    select.dispatchEvent(new window.Event("change"));
    check(window.__vd.currentVoice() === "vi-VN-NamMinhNeural",
      "chọn NamMinh -> voiceId đổi", window.__vd.currentVoice());
    check(window.chrome.__store.voice === "vi-VN-NamMinhNeural",
      "giọng được lưu vào chrome.storage", String(window.chrome.__store.voice));
    window.__vd.speak();
    const speak = sent.find((m) => m.type === "speak");
    check(speak && speak.voice === "vi-VN-NamMinhNeural",
      "request /speak gửi kèm giọng đã chọn", speak && speak.voice);

    select.value = "__custom__";
    select.dispatchEvent(new window.Event("change"));
    check(customRow.hidden === false, "chọn 'Khác' -> hiện ô nhập Voice ID");
    customInput.value = "en-US-AvaMultilingualNeural";
    apply.dispatchEvent(new window.Event("click"));
    check(window.__vd.currentVoice() === "en-US-AvaMultilingualNeural",
      "nhập Voice ID hợp lệ -> áp dụng", window.__vd.currentVoice());

    customInput.value = "không hợp lệ!!";
    apply.dispatchEvent(new window.Event("click"));
    check(window.__vd.currentVoice() === "en-US-AvaMultilingualNeural",
      "Voice ID sai định dạng bị từ chối", window.__vd.currentVoice());

    finish();
  }, 100);
}

function finish(next) {
  if (next) return next();
  console.log(failed === 0 ? "\nTất cả smoke test đã qua." : `\n${failed} smoke test thất bại.`);
  process.exit(failed === 0 ? 0 : 1);
}

testYoutube();
