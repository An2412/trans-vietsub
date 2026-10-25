const TAG = "[VietDub]";
const MAX_PARALLEL = 3; // Tăng lên 3 luồng tải audio song song để đệm trước không bị rỗng
const NEAR_WINDOW_S = 12; // câu sát vị trí phát được ưu tiên thêm slot
const NEAR_EXTRA_SLOTS = 2;
const STUCK_LOADING_MS = 30000; // câu kẹt "loading" quá lâu mà chưa có audio → thử lại
const PREFETCH_BASE_WALL_S = 60;
const PREFETCH_MAX_MEDIA_S = 150;
const MAX_LATE_S = 6;
const CAPTION_WAIT_MS = 15000;
const BATCH_SIZE = 8; // số câu gom vào một request dịch
const MAX_RETRIES = 2; // thử lại thêm 2 lần sau request đầu tiên
const AUDIO_GRACE_PERIOD_S = 0.4; // Vùng đệm 0.4s để tải kịp audio trước khi buộc dừng video
const MAX_SPEED_FACTOR = 1.35; // Giới hạn tăng tốc tối đa để câu đọc vừa vặn vào khung phụ đề
const MIN_SPEED_FACTOR = 0.95; // Giới hạn giảm tốc tối thiểu
const CLIENT_AUDIO_CACHE_MAX_ENTRIES = 128;
const CLIENT_AUDIO_CACHE_MAX_BYTES = 16 * 1024 * 1024;
const CLIENT_CACHE_DB_NAME = "vietdub-audio-cache-v1";

let enabled = false;
let cues = []; // { start, end, en, state, audio, vi, audioElement, audioUrl, duration }
let loadedFor = "";
let generation = 0;
let currentAudio = null;
let currentAudioUrl = null;
let currentCue = null;
let boundVideo = null;
let loadingScript = false;
let scriptLoaded = false;
let inflight = 0;
let videoRate = 1.0;
let host = null;
let ui = null;
let autoPaused = false;
let fatalError = false;
let widgetCollapsed = false;
let adActive = false;
let rateLimitUntil = 0;
let batchRetryAt = 0;
let voiceId = "vi-VN-HoaiMyNeural";
let voicePresets = [];
let voiceOptionsLoaded = false;
const clientAudioCache = new Map();
let clientAudioCacheBytes = 0;
let clientCacheDbPromise = null;
const VOICE_ID_RE = /^[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]{2,40}Neural$/;
const FALLBACK_VOICE_PRESETS = [
  { id: "vi-VN-HoaiMyNeural", label: "HoaiMy · nữ, giọng Bắc" },
  { id: "vi-VN-NamMinhNeural", label: "NamMinh · nam, giọng Bắc" },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ─── Platform detection ───────────────────────────────────────────────────
function detectPlatform() {
  const h = location.hostname;
  if (h.includes("youtube.com")) return "youtube";
  if (h.includes("udemy.com")) return "udemy";
  return "generic";
}
const PLATFORM = detectPlatform();

function getVideo() {
  if (PLATFORM === "youtube") return document.querySelector("video.html5-main-video");
  // Udemy và các web khác: chọn video đang phát, ưu tiên video to nhất trong player.
  const videos = Array.from(document.querySelectorAll("video"));
  if (!videos.length) return null;
  const visible = videos.filter((v) => v.readyState >= 1 || !v.videoWidth);
  const playing = visible.find((v) => !v.paused && !v.ended);
  if (playing) return playing;
  const sized = visible.filter((v) => v.videoWidth > 0 || v.clientWidth > 120);
  const pool = sized.length ? sized : visible;
  const biggest = pool.sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0];
  return biggest || pool[0] || null;
}

// Container để đặt widget. YouTube có #movie_player; Udemy dùng [data-purpose="player-video"].
function findPlayerContainer() {
  if (PLATFORM === "youtube") {
    const player = document.querySelector("#movie_player");
    if (player) return player;
  }
  if (PLATFORM === "udemy") {
    const player = document.querySelector(
      "[data-purpose='player-video'], [data-purpose='player-container'], .ud-player, #course_player, .player-video"
    );
    if (player) return player;
  }
  const video = getVideo();
  const generic = video?.closest("[data-purpose], [class*='player'], [class*='Player']") || null;
  return generic || document.body;
}

// true nếu container nằm trong <body> (không có khung player riêng) → widget dùng position:fixed.
function containerIsBody(container) {
  return !container || container === document.body || container === document.documentElement;
}

function videoId() {
  if (PLATFORM === "youtube") return new URLSearchParams(location.search).get("v") || "";
  if (PLATFORM === "udemy") {
    // e.g. /course/course-slug/learn/lecture/12345678
    const m = location.pathname.match(/\/lecture\/(\d+)/);
    return m ? `udemy-${m[1]}` : `udemy-${location.pathname.replace(/\//g, "_")}`;
  }
  return location.pathname;
}

function videoTitle() {
  if (PLATFORM === "youtube") {
    return document.querySelector("h1.ytd-video-primary-info-renderer, h1.style-scope.ytd-watch-metadata")
      ?.textContent?.trim() || document.title;
  }
  if (PLATFORM === "udemy") {
    return document.querySelector("[data-purpose='curriculum-item-title'], .ud-heading-xl, h1")
      ?.textContent?.trim() || document.title;
  }
  return document.title;
}

function normalizeVoiceId(value) {
  const voice = String(value || "").trim();
  if (!voice || voice.length > 64 || !VOICE_ID_RE.test(voice)) return "";
  return voice;
}

function voiceLabel(voice) {
  const found = voicePresets.find((item) => item.id === voice);
  return found ? found.label : voice;
}

function cueCacheKey(text, voice = voiceId) {
  return `${voice}\u0000${String(text || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase()}`;
}

function openClientCacheDb() {
  if (!("indexedDB" in window)) return Promise.resolve(null);
  if (!clientCacheDbPromise) {
    clientCacheDbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(CLIENT_CACHE_DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("clips")) db.createObjectStore("clips", { keyPath: "key" });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("indexeddb_open_failed"));
      request.onblocked = () => reject(new Error("indexeddb_open_blocked"));
    }).catch((error) => {
      clientCacheDbPromise = null;
      throw error;
    });
  }
  return clientCacheDbPromise;
}

function idbRequest(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("indexeddb_request_failed"));
  });
}

async function getCachedCueAudio(text, voice = voiceId) {
  const key = cueCacheKey(text, voice);
  const memoryEntry = clientAudioCache.get(key);
  if (memoryEntry) {
    clientAudioCache.delete(key);
    clientAudioCache.set(key, memoryEntry);
    return memoryEntry;
  }
  try {
    const db = await openClientCacheDb();
    if (!db) return null;
    const tx = db.transaction("clips", "readonly");
    const entry = await idbRequest(tx.objectStore("clips").get(key));
    if (!entry?.audio) return null;
    rememberCueAudio(text, entry.audio, entry.vi || "", false, voice);
    return { audio: entry.audio, vi: entry.vi || "", bytes: entry.bytes || 0 };
  } catch (error) {
    console.warn(TAG, "Không đọc được cache local của trình duyệt:", error?.message || error);
    return null;
  }
}

function rememberCueAudio(text, audio, vi, persist = true, voice = voiceId) {
  const key = cueCacheKey(text, voice);
  if (!key || !audio) return;
  const previous = clientAudioCache.get(key);
  if (previous) {
    clientAudioCacheBytes -= previous.bytes;
    clientAudioCache.delete(key);
  }
  const bytes = (audio.length + String(vi || "").length) * 2;
  if (bytes > CLIENT_AUDIO_CACHE_MAX_BYTES) return;
  clientAudioCache.set(key, { audio, vi: vi || "", bytes });
  clientAudioCacheBytes += bytes;
  while (clientAudioCache.size > CLIENT_AUDIO_CACHE_MAX_ENTRIES || clientAudioCacheBytes > CLIENT_AUDIO_CACHE_MAX_BYTES) {
    const [oldestKey, oldest] = clientAudioCache.entries().next().value;
    clientAudioCache.delete(oldestKey);
    clientAudioCacheBytes -= oldest.bytes;
  }
  if (persist) persistCueAudio(key, audio, vi || "", bytes);
}

async function persistCueAudio(key, audio, vi, bytes) {
  try {
    const db = await openClientCacheDb();
    if (!db) return;
    const readTx = db.transaction("clips", "readonly");
    const records = await idbRequest(readTx.objectStore("clips").getAll());
    const existing = records.filter((record) => record.key !== key).sort((a, b) => a.savedAt - b.savedAt);
    let totalBytes = existing.reduce((sum, record) => sum + (record.bytes || 0), bytes);
    let count = existing.length + 1;
    const evict = [];
    while (existing.length && (count > CLIENT_AUDIO_CACHE_MAX_ENTRIES || totalBytes > CLIENT_AUDIO_CACHE_MAX_BYTES)) {
      const old = existing.shift();
      evict.push(old.key);
      totalBytes -= old.bytes || 0;
      count -= 1;
    }
    const tx = db.transaction("clips", "readwrite");
    const store = tx.objectStore("clips");
    store.put({ key, audio, vi, bytes, savedAt: Date.now() });
    for (const oldKey of evict) store.delete(oldKey);
    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error || new Error("indexeddb_write_failed"));
      tx.onabort = () => reject(tx.error || new Error("indexeddb_write_aborted"));
    });
  } catch (error) {
    console.warn(TAG, "Không lưu được cache local của trình duyệt:", error?.message || error);
  }
}

function youtubeAdShowing() {
  const player = document.querySelector("#movie_player");
  if (!player) return false;
  if (player.classList.contains("ad-showing") || player.classList.contains("ad-interrupting")) return true;
  const adText = player.querySelector(".ytp-ad-text");
  if (!adText || !adText.textContent.trim()) return false;
  const style = getComputedStyle(adText);
  return style.display !== "none" && style.visibility !== "hidden" && adText.getBoundingClientRect().width > 0;
}

function syncAdState() {
  const showing = youtubeAdShowing();
  if (showing === adActive) return showing;
  adActive = showing;
  if (showing) {
    if (currentCue) currentCue.state = currentCue.audio ? "ready" : "new";
    if (currentAudio) {
      currentAudio.onended = null;
      currentAudio.onerror = null;
      currentAudio.pause();
      currentAudio.removeAttribute("src");
      currentAudio.load();
    }
    if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl);
    currentAudio = null;
    currentAudioUrl = null;
    currentCue = null;
    setStatus("Quảng cáo YouTube đang phát; Viet Dub tạm ngưng và sẽ tiếp tục sau quảng cáo.", "idle");
  } else {
    if (enabled && scriptLoaded) {
      setStatus("Quảng cáo kết thúc; tiếp tục lồng tiếng.", "success");
      setTimeout(() => prefetchTick(true), 0);
    }
  }
  return showing;
}

function mountWidget() {
  const player = findPlayerContainer();
  if (!player) return false;
  if (host && host.parentElement === player) return true;
  if (host) host.remove();

  host = document.createElement("div");
  host.id = "vietdub-widget-host";
  const floating = containerIsBody(player);
  host.style.cssText = [
    floating ? "position:fixed" : "position:absolute",
    "top:76px", "right:16px", "z-index:2147483647",
    "width:280px", "max-width:calc(100% - 24px)", "pointer-events:auto",
  ].join(";");

  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; }
      .panel {
        color:#f7f7f8; background:rgba(20,22,26,.94); border:1px solid rgba(255,255,255,.16);
        border-radius:12px; box-shadow:0 8px 28px rgba(0,0,0,.38); padding:12px;
        font:13px/1.4 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        backdrop-filter:blur(8px);
      }
      .head { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; cursor:grab; touch-action:none; user-select:none; }
      .brand { font-weight:700; letter-spacing:.2px; }
      .badge { color:#b6c4d5; font-size:11px; }
      .tools { display:flex; align-items:center; gap:7px; }
      .iconButton { flex:0 0 25px; min-height:25px; padding:0; border-radius:6px; background:#3b414b; font-size:17px; }
      .bubble { width:44px; height:44px; min-height:44px; padding:0; border-radius:50%; box-shadow:0 4px 18px rgba(0,0,0,.45); cursor:grab; touch-action:none;
        transition: opacity 1.6s ease; }
      .bubble[data-idle="true"] { opacity: 0.25; }
      .bubble:hover, .bubble:focus-visible { opacity: 1; transition: opacity .2s ease; }
      .status { min-height:36px; color:#d5d9df; margin:4px 0 8px; overflow-wrap:anywhere; }
      .status[data-kind="success"] { color:#80e1a4; }
      .status[data-kind="error"] { color:#ffaaaa; }
      .status[data-kind="working"] { color:#ffd982; }
      .status[data-kind="working"]::before {
        content:""; display:inline-block; width:10px; height:10px; margin:0 7px 0 1px;
        border:2px solid rgba(255,217,130,.35); border-top-color:#ffd982; border-radius:50%;
        vertical-align:-2px; animation:vdspin .75s linear infinite;
      }
      @keyframes vdspin { to { transform:rotate(360deg); } }
      .actions { display:flex; gap:7px; }
      .actions + .actions { margin-top:7px; }
      .summaryLink { display:none; margin-top:8px; font-size:11px; }
      .summaryLink a { color:#8ab4f8; }
      .summaryBox { margin-top:8px; font-size:11px; color:#d5d9df; }
      .summaryBox summary { cursor:pointer; color:#8ab4f8; }
      .summaryBox pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:220px; overflow:auto;
        background:rgba(255,255,255,.06); border-radius:8px; padding:8px; margin:7px 0 0;
        font:11px/1.5 ui-monospace,Consolas,monospace; color:#e6e9ef; }
      button {
        appearance:none; border:0; border-radius:8px; padding:8px 9px; background:#2e78e6;
        color:#fff; font:600 12px system-ui,sans-serif; cursor:pointer; flex:1; min-height:34px;
      }
      button.secondary { background:#3b414b; }
      button.tertiary { background:#1f6f43; }
      button:disabled { opacity:.48; cursor:not-allowed; }
      .progress { height:4px; width:100%; margin:0 0 9px; accent-color:#53bd79; }
      .hint { color:#aeb5bf; font-size:10px; margin-top:7px; }
      .videoSpeed { display:flex; align-items:center; gap:8px; margin-top:10px; color:#d5d9df; }
      .videoSpeed label { white-space:nowrap; }
      .videoRateValue { min-width:38px; text-align:right; color:#fff; font-variant-numeric:tabular-nums; }
      .voiceRow { display:flex; align-items:center; gap:8px; margin-top:10px; color:#d5d9df; }
      .voiceRow label { white-space:nowrap; }
      .voiceRow select, .voiceRow input {
        flex:1; min-width:0; background:#1b1f26; color:#f7f7f8; border:1px solid rgba(255,255,255,.18);
        border-radius:7px; padding:5px 6px; font:12px system-ui,sans-serif;
      }
      .voiceRow input::placeholder { color:#8b93a1; }
      .voiceRow button { flex:0 0 auto; min-height:28px; padding:4px 8px; font-size:11px; }
    </style>
    <section id="panel" class="panel" aria-label="Viet Dub">
      <div id="dragHandle" class="head">
        <span class="brand">Viet Dub</span>
        <span class="tools"><span class="badge">BETA</span><button id="collapse" class="iconButton" type="button" title="Thu nhỏ" aria-label="Thu nhỏ">−</button></span>
      </div>
      <div id="status" class="status" data-kind="idle" role="status" aria-live="polite">Tải script phụ đề để bắt đầu.</div>
      <progress id="progress" class="progress" max="100" value="0" hidden></progress>
      <div class="actions">
        <button id="load" type="button">Tải script</button>
        <button id="start" class="secondary" type="button" disabled hidden>▶ Phát &amp; lồng tiếng</button>
      </div>
      <div class="actions">
        <button id="summarize" class="tertiary" type="button" disabled hidden>💾 Lưu tóm tắt để học</button>
      </div>
      <div id="summaryLink" class="summaryLink"><a id="summaryLinkAnchor" href="#" target="_blank" rel="noopener"></a></div>
      <details id="summaryBox" class="summaryBox" hidden>
        <summary>Xem bản tóm tắt</summary>
        <pre id="summaryText"></pre>
      </details>
      <div class="voiceRow">
        <label for="voice">Giọng đọc</label>
        <select id="voice" title="Giọng lồng tiếng"></select>
      </div>
      <div class="voiceRow" id="voiceCustomRow" hidden>
        <input id="voiceCustom" type="text" spellcheck="false" placeholder="Voice ID, vd: en-US-AvaMultilingualNeural" />
        <button id="voiceApply" class="secondary" type="button">Dùng</button>
      </div>
      <div class="videoSpeed">
        <span>Tốc độ đọc video</span>
        <span id="videoRateValue" class="videoRateValue" aria-live="polite">1.00×</span>
      </div>
      <div class="hint">Âm thanh tự bám tốc độ phát của video. Đổi tốc độ trong trình phát.</div>
    </section>
    <button id="bubble" class="bubble" type="button" title="Mở Viet Dub" aria-label="Mở Viet Dub" hidden>VD</button>`;

  player.appendChild(host);
  ui = {
    panel: shadow.getElementById("panel"),
    bubble: shadow.getElementById("bubble"),
    collapse: shadow.getElementById("collapse"),
    dragHandle: shadow.getElementById("dragHandle"),
    status: shadow.getElementById("status"),
    progress: shadow.getElementById("progress"),
    load: shadow.getElementById("load"),
    start: shadow.getElementById("start"),
    summarize: shadow.getElementById("summarize"),
    summaryLink: shadow.getElementById("summaryLink"),
    summaryLinkAnchor: shadow.getElementById("summaryLinkAnchor"),
    summaryBox: shadow.getElementById("summaryBox"),
    summaryText: shadow.getElementById("summaryText"),
    videoRateValue: shadow.getElementById("videoRateValue"),
    voice: shadow.getElementById("voice"),
    voiceCustomRow: shadow.getElementById("voiceCustomRow"),
    voiceCustom: shadow.getElementById("voiceCustom"),
    voiceApply: shadow.getElementById("voiceApply"),
  };
  ui.load.addEventListener("click", onLoadButton);
  ui.start.addEventListener("click", toggleDubbing);
  ui.summarize.addEventListener("click", summarizeAndExport);
  ui.voice.addEventListener("change", () => {
    if (ui.voice.value === "__custom__") {
      ui.voiceCustomRow.hidden = false;
      ui.voiceCustom.focus();
      return;
    }
    ui.voiceCustomRow.hidden = true;
    setVoice(ui.voice.value);
  });
  ui.voiceApply.addEventListener("click", () => {
    const voice = normalizeVoiceId(ui.voiceCustom.value);
    if (!voice) {
      setStatus("Voice ID không hợp lệ. Đúng dạng: vi-VN-NamMinhNeural.", "error");
      return;
    }
    setVoice(voice);
  });
  ui.voiceCustom.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      ui.voiceApply.click();
    }
  });
  loadVoiceOptions();
  ui.collapse.addEventListener("click", () => setWidgetCollapsed(true, true));
  ui.bubble.addEventListener("click", (event) => {
    if (ui.bubble.dataset.dragged === "true") {
      event.preventDefault();
      event.stopPropagation();
      ui.bubble.dataset.dragged = "false";
      return;
    }
    setWidgetCollapsed(false, true);
  });
  initWidgetDrag(player, ui.dragHandle);
  initWidgetDrag(player, ui.bubble, true);
  ui.bubble.addEventListener("pointerenter", wakeBubble);
  ui.bubble.addEventListener("pointerdown", wakeBubble);
  ui.bubble.addEventListener("pointerleave", () => {
    if (widgetCollapsed) armBubbleIdleFade();
  });
  updateVideoRateUi(getVideo()?.playbackRate || 1);
  const thisHost = host;
  chrome.storage.local.get(["widgetPosition", "widgetCollapsed"]).then((saved) => {
    if (host !== thisHost || !ui) return;
    setWidgetCollapsed(Boolean(saved.widgetCollapsed), false);
    if (saved.widgetPosition) applyWidgetPosition(saved.widgetPosition);
  });
  return true;
}

// Bubble thu gọn: 10s không ai đụng → mờ còn ~25% cho dễ thấy; đụng vào là rõ lại.
const BUBBLE_IDLE_MS = 10000;
let bubbleIdleTimer = null;

function armBubbleIdleFade() {
  if (bubbleIdleTimer) {
    clearTimeout(bubbleIdleTimer);
    bubbleIdleTimer = null;
  }
  if (!widgetCollapsed) return;
  bubbleIdleTimer = setTimeout(() => {
    bubbleIdleTimer = null;
    if (ui && widgetCollapsed) ui.bubble.dataset.idle = "true";
  }, BUBBLE_IDLE_MS);
}

function wakeBubble() {
  if (!ui || !widgetCollapsed) return;
  delete ui.bubble.dataset.idle;
  armBubbleIdleFade();
}

function setWidgetCollapsed(value, persist = false) {
  widgetCollapsed = Boolean(value);
  if (widgetCollapsed) {
    armBubbleIdleFade();
  } else if (bubbleIdleTimer) {
    clearTimeout(bubbleIdleTimer);
    bubbleIdleTimer = null;
    if (ui) delete ui.bubble.dataset.idle;
  }
  if (ui) {
    ui.panel.hidden = widgetCollapsed;
    ui.bubble.hidden = !widgetCollapsed;
    if (host) host.style.width = widgetCollapsed ? "44px" : "280px";
  }
  if (!widgetCollapsed && host?.parentElement) {
    const widgetRect = host.getBoundingClientRect();
    const floating = containerIsBody(host.parentElement);
    const playerRect = floating ? { left: 0, top: 0 } : host.parentElement.getBoundingClientRect();
    applyWidgetPosition({ left: widgetRect.left - playerRect.left, top: widgetRect.top - playerRect.top });
  }
  if (persist) chrome.storage.local.set({ widgetCollapsed }).catch(() => {});
}

function applyWidgetPosition(position) {
  const player = host?.parentElement;
  if (!player || !position || !Number.isFinite(position.left) || !Number.isFinite(position.top)) return;
  const floating = containerIsBody(player);
  const maxLeft = Math.max(0, (floating ? window.innerWidth : player.clientWidth) - host.offsetWidth);
  const maxTop = Math.max(0, (floating ? window.innerHeight : player.clientHeight) - host.offsetHeight);
  host.style.left = `${Math.max(0, Math.min(maxLeft, position.left))}px`;
  host.style.top = `${Math.max(0, Math.min(maxTop, position.top))}px`;
  host.style.right = "auto";
}

function saveWidgetPosition() {
  const player = host?.parentElement;
  if (!player || !host) return;
  const widgetRect = host.getBoundingClientRect();
  const floating = containerIsBody(player);
  const baseRect = floating ? { left: 0, top: 0 } : player.getBoundingClientRect();
  chrome.storage.local.set({
    widgetPosition: { left: widgetRect.left - baseRect.left, top: widgetRect.top - baseRect.top },
  }).catch(() => {});
}

function initWidgetDrag(player, handle, allowButtonDrag = false) {
  let drag = null;
  handle.addEventListener("pointerdown", (event) => {
    if (!allowButtonDrag && event.target.closest("button")) return;
    const rect = host.getBoundingClientRect();
    drag = {
      dx: event.clientX - rect.left,
      dy: event.clientY - rect.top,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
    handle.setPointerCapture(event.pointerId);
    event.stopPropagation();
    if (!allowButtonDrag) event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!drag || !host || host.parentElement !== player) return;
    event.stopPropagation();
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) >= 6) {
      drag.moved = true;
    }
    if (!drag.moved) return;
    const bounds = player.getBoundingClientRect();
    const left = Math.max(0, Math.min(player.clientWidth - host.offsetWidth, event.clientX - bounds.left - drag.dx));
    const top = Math.max(0, Math.min(player.clientHeight - host.offsetHeight, event.clientY - bounds.top - drag.dy));
    host.style.left = `${left}px`;
    host.style.top = `${top}px`;
    host.style.right = "auto";
  });
  const finishDrag = () => {
    if (!drag) return;
    if (allowButtonDrag && drag.moved) {
      handle.dataset.dragged = "true";
      setTimeout(() => { handle.dataset.dragged = "false"; }, 500);
    }
    if (drag.moved) saveWidgetPosition();
    drag = null;
  };
  handle.addEventListener("pointerup", finishDrag);
  handle.addEventListener("pointercancel", finishDrag);
}

function setStatus(text, kind = "idle") {
  if (!ui) return;
  ui.status.textContent = text;
  ui.status.dataset.kind = kind;
}

function setProgress(done, total, visible = true) {
  if (!ui) return;
  ui.progress.hidden = !visible;
  ui.progress.value = total ? Math.round((done / total) * 100) : 0;
}

function updateVideoRateUi(value) {
  videoRate = Math.max(0.25, Number(value) || 1);
  if (ui) {
    ui.videoRateValue.textContent = `${videoRate.toFixed(2)}×`;
  }
}

function b64ToUrl(b64) {
  if (typeof b64 !== "string" || !b64) throw new Error("invalid_audio_data");
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
}

function preloadCueAudio(cue) {
  if (!cue || !cue.audio || cue.audioElement) return;
  try {
    const url = b64ToUrl(cue.audio);
    const audio = new Audio(url);
    audio.preload = "auto";
    cue.audioUrl = url;
    cue.audioElement = audio;
    audio.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        cue.duration = audio.duration;
      }
    }, { once: true });
  } catch (error) {
    console.warn(TAG, "Không thể preload audio cho câu:", error);
  }
}

function cleanupCueAudio(cue) {
  if (!cue) return;
  if (cue.audioElement) {
    cue.audioElement.onended = null;
    cue.audioElement.onerror = null;
    cue.audioElement.pause();
    cue.audioElement.removeAttribute("src");
    cue.audioElement.load();
    cue.audioElement = null;
  }
  if (cue.audioUrl) {
    URL.revokeObjectURL(cue.audioUrl);
    cue.audioUrl = null;
  }
}

function calculateAdaptivePlaybackRate(cue, naturalDuration, videoRate) {
  const baseRate = Math.max(0.25, Number(videoRate) || 1);
  if (!cue || !naturalDuration || naturalDuration <= 0) return baseRate;

  const currentIndex = cues.indexOf(cue);
  const nextCue = currentIndex >= 0
    ? cues.slice(currentIndex + 1).find((c) => !isConsumedState(c.state))
    : null;

  let windowSeconds = 0;
  if (nextCue && Number.isFinite(nextCue.start) && nextCue.start > cue.start) {
    windowSeconds = Math.max(0.6, nextCue.start - cue.start - 0.15);
  } else if (cue.end > cue.start) {
    windowSeconds = Math.max(0.6, cue.end - cue.start + 0.4);
  } else {
    return baseRate;
  }

  const requiredRate = (naturalDuration / windowSeconds) * baseRate;
  const minRate = baseRate * MIN_SPEED_FACTOR;
  const maxRate = baseRate * MAX_SPEED_FACTOR;

  const rate = Math.max(minRate, Math.min(maxRate, requiredRate));
  // playbackRate = NaN/Infinity sẽ ném TypeError và làm chết vòng phát.
  return Number.isFinite(rate) && rate > 0 ? rate : baseRate;
}

function applyEffectiveSpeechRate() {
  if (!currentAudio || currentAudio.ended || !currentCue) return;
  const vRate = getVideo()?.playbackRate || 1;
  const rate = calculateAdaptivePlaybackRate(currentCue, currentCue.duration || currentAudio.duration, vRate);
  if (Number.isFinite(rate) && rate > 0) {
    currentAudio.playbackRate = Math.min(16, Math.max(0.0625, rate));
  }
}

function prefetchWindow(video) {
  const videoRate = Math.max(0.5, video.playbackRate || 1);
  return Math.min(PREFETCH_MAX_MEDIA_S, Math.max(45, PREFETCH_BASE_WALL_S * videoRate));
}

// ---------- Nạp script phụ đề; thao tác này không tạo audio cho toàn bộ video ----------
// YouTube làm đầy bộ đệm resource-timing (250 mục) nên entry timedtext có thể bị mất;
// PerformanceObserver ghi nhớ URL phụ đề ngay khi nó xuất hiện.
const captionUrlLog = [];
try {
  performance.setResourceTimingBufferSize?.(1000);
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (entry.name.includes("/api/timedtext")) captionUrlLog.push(entry.name);
    }
    if (captionUrlLog.length > 50) captionUrlLog.splice(0, captionUrlLog.length - 50);
  }).observe({ type: "resource", buffered: true });
} catch (_) {}

function timedtextUrl() {
  const fromTimeline = performance.getEntriesByType("resource")
    .map((r) => r.name).filter((name) => name.includes("/api/timedtext"));
  const entries = [...fromTimeline, ...captionUrlLog].map((name) => ({ name }));
  const currentId = videoId();
  for (let i = entries.length - 1; i >= 0; i--) {
    try {
      const u = new URL(entries[i].name);
      const captionVideoId = u.searchParams.get("v");
      if (captionVideoId && currentId && captionVideoId !== currentId) continue;
      u.searchParams.set("fmt", "json3");
      return u;
    } catch (_) {}
  }
  return null;
}

function buildCues(events) {
  const words = [];
  for (const ev of events) {
    if (!ev.segs) continue;
    const text = ev.segs.map((s) => s.utf8 || "").join("").replace(/\s+/g, " ").trim();
    if (!text) continue;
    words.push({
      t: Number(ev.tStartMs || 0) / 1000,
      d: Number(ev.dDurationMs || 0) / 1000,
      text,
    });
  }
  words.sort((a, b) => a.t - b.t);

  const out = [];
  let cur = null;
  let prevWord = null;
  for (const word of words) {
    const pauseGap = prevWord ? (word.t - (prevWord.t + prevWord.d)) : 0;
    if (!cur) {
      cur = { start: word.t, end: word.t + word.d, en: word.text };
    } else {
      // Nếu giữa 2 từ có khoảng lặng tự nhiên >= 0.45s và câu đã đủ dài, tách
      // thành câu mới: khoảng nghỉ của giọng đọc gốc giữ nguyên thành khoảng
      // nghỉ thật giữa hai câu lồng tiếng (sát nhịp giọng đọc hơn).
      if (pauseGap >= 0.45 && cur.en.length >= 14) {
        out.push(cur);
        cur = { start: word.t, end: word.t + word.d, en: word.text };
      } else {
        cur.en += ` ${word.text}`;
        cur.end = Math.max(cur.end, word.t + word.d);
      }
    }
    prevWord = word;
    if (/[.?!]["')\]]?$/.test(word.text) || cur.en.length > 200) {
      out.push(cur);
      cur = null;
      prevWord = null;
    }
  }
  if (cur) out.push(cur);

  const seen = new Set();
  return out.filter((cue) => {
    const key = `${Math.round(cue.start * 2)}:${cue.en.toLocaleLowerCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map((cue) => ({ ...cue, state: "new", audio: null, vi: "", audioElement: null, audioUrl: null, duration: 0 }));
}

async function loadCues() {
  const id = videoId();
  if (!id) throw new Error("Không xác định được video hiện tại.");
  if (loadedFor === id && cues.length) return true;

  if (PLATFORM === "udemy") {
    return await loadCuesUdemy(id);
  }
  return await loadCuesYouTube(id);
}

// ─── WebVTT parser (Udemy / generic) ─────────────────────────────────────
function parseVtt(vttText) {
  const blocks = vttText.split(/\n\n+/);
  const items = [];
  for (const block of blocks) {
    const lines = block.trim().split("\n");
    const timeLine = lines.find((l) => l.includes("-->"));
    if (!timeLine) continue;
    const [startStr, endStr] = timeLine.split("-->").map((s) => s.trim().split(" ")[0]);
    const parseTime = (t) => {
      const parts = t.split(":").map(Number);
      if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
      if (parts.length === 2) return parts[0] * 60 + parts[1];
      return Number(parts[0]) || 0;
    };
    const text = lines.filter((l) => !l.includes("-->") && !/^\d+$/.test(l.trim()))
      .join(" ")
      .replace(/<[^>]+>/g, "")
      .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ")
      .replace(/\s+/g, " ").trim();
    if (!text) continue;
    items.push({ t: parseTime(startStr), d: parseTime(endStr) - parseTime(startStr), text });
  }
  return buildCues(items);
}

// ─── Udemy subtitle loader ────────────────────────────────────────────────

// <track> cues chỉ được nạp khi track.mode khác "disabled" → ép "hidden" để lấy được.
function forceTrackMode(video) {
  try {
    for (const track of Array.from(video.textTracks || [])) {
      if (track.kind === "metadata" || track.kind === "chapters") continue;
      if (track.mode === "disabled") {
        try {
          track.mode = "hidden";
        } catch (_) {}
      }
    }
  } catch (_) {}
}

function pickEnglishTrack(video) {
  const tracks = Array.from(video.textTracks || []);
  if (!tracks.length) return null;
  return tracks.find((t) => t.language?.toLowerCase().startsWith("en"))
    || tracks.find((t) => t.label?.toLowerCase().includes("english"))
    || tracks.find((t) => t.kind === "subtitles" || t.kind === "captions")
    || tracks[0];
}

function findTrackElement(video) {
  const selector = "track[kind='subtitles'], track[kind='captions'], track[srclang]";
  return video.querySelector?.(selector) || document.querySelector(selector) || null;
}

// Content script không fetch cross-origin được → nhờ service worker tải giúp.
async function fetchTextViaBackground(url) {
  try {
    const result = await chrome.runtime.sendMessage({ type: "fetch_text", url });
    if (result?.ok && typeof result.text === "string") return result.text;
    console.warn(TAG, "Tải phụ đề thất bại:", result?.error);
  } catch (error) {
    console.warn(TAG, "Không gọi được service worker:", error?.message || error);
  }
  return "";
}

async function loadCuesUdemy(id) {
  setStatus("Đang tìm phụ đề Udemy…", "working");
  const deadline = Date.now() + CAPTION_WAIT_MS;
  let triedSrc = "";
  while (Date.now() < deadline) {
    const video = getVideo();
    if (video) {
      forceTrackMode(video);
      const track = pickEnglishTrack(video);
      if (track?.cues && track.cues.length) {
        const items = Array.from(track.cues).map((c) => ({
          t: c.startTime,
          d: c.endTime - c.startTime,
          text: String(c.text || "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim(),
        })).filter((i) => i.text);
        const next = buildCues(items);
        if (next.length) {
          cues = next;
          loadedFor = id;
          return true;
        }
      }
      // Đường dự phòng: tải file VTT của <track> (có thể là blob: hoặc URL khác host).
      const trackEl = findTrackElement(video);
      const src = trackEl?.src || trackEl?.getAttribute?.("src") || "";
      if (src && src !== triedSrc && !src.startsWith("blob:")) {
        triedSrc = src;
        const text = await fetchTextViaBackground(src);
        if (text) {
          const next = parseVtt(text);
          if (next.length) {
            cues = next;
            loadedFor = id;
            return true;
          }
        }
      }
    }
    await sleep(500);
  }
  throw new Error(
    "Không tìm thấy phụ đề trên Udemy. Bật CC/Subtitles trong player, đợi phụ đề hiện lên rồi bấm Tải script lại."
  );
}

// ─── YouTube subtitle loader (original logic) ─────────────────────────────
async function loadCuesYouTube(id) {
  const deadline = Date.now() + CAPTION_WAIT_MS;
  const nudgeAt = Date.now() + 2500;
  let nudged = false;
  let triedUrl = "";
  let ccWasOn = false;
  while (Date.now() < deadline) {
    // Chưa thấy request phụ đề: tắt/bật lại nút CC để YouTube gửi lại request.
    if (!nudged && Date.now() >= nudgeAt && !timedtextUrl()) {
      nudged = true;
      const button = document.querySelector(".ytp-subtitles-button");
      if (button) {
        ccWasOn = button.getAttribute("aria-pressed") === "true";
        setStatus("Chưa thấy request phụ đề; đang bật/tắt lại CC…", "working");
        if (ccWasOn) { button.click(); await sleep(500); }
        button.click();
      }
    }
    const url = timedtextUrl();
    if (url && url.href !== triedUrl) {
      triedUrl = url.href;
      try {
        const response = await fetch(url.href, { credentials: "include" });
        if (response.ok) {
          const raw = await response.text();
          if (!raw.trim()) {
            console.warn(TAG, "Request phụ đề trả về nội dung rỗng (có thể thiếu token); sẽ thử lại.");
            triedUrl = "";
            await sleep(800);
            continue;
          }
          const data = JSON.parse(raw);
          const next = buildCues(data.events || []);
          if (next.length) {
            if (id !== videoId()) throw new Error("Video đã đổi trong lúc tải phụ đề.");
            cues = next;
            loadedFor = id;
            // Dò phụ đề phải bật CC thật; nếu vốn tắt thì tắt lại để phụ đề
            // không "tự mở" trên màn hình sau khi tải script.
            restoreCcButton(ccWasOn);
            return true;
          }
        }
      } catch (error) {
        if (id !== videoId()) throw error;
        console.warn(TAG, "Không đọc được phụ đề:", error);
      }
    }
    await sleep(400);
  }
  throw new Error("Không tìm thấy phụ đề. Bật CC, chờ phụ đề hiện trên video rồi thử lại.");
}

// Mẹo dò URL phụ đề phải bật CC thật; nếu CC vốn tắt thì tắt lại sau khi đã lấy script.
async function restoreCcButton(ccWasOn) {
  if (ccWasOn) return;
  await sleep(400); // đợi YouTube cập nhật trạng thái nút sau lần click cuối
  const button = document.querySelector(".ytp-subtitles-button");
  if (button && button.getAttribute("aria-pressed") === "true") button.click();
}

async function onLoadButton() {
  if (loadingScript) return;
  if (syncAdState()) {
    setStatus("Hãy đợi quảng cáo kết thúc rồi tải script.", "idle");
    return;
  }
  if (cues.some((cue) => cue.state === "skipfail")) {
    await retryFailedCues();
    return;
  }
  if (scriptLoaded && loadedFor === videoId()) return;

  loadingScript = true;
  const myGeneration = ++generation;
  if (ui) {
    ui.load.disabled = true;
    ui.start.disabled = true;
    ui.start.hidden = true;
  }
  setStatus("Đang tải script phụ đề…", "working");
  setProgress(0, 0, true);
  try {
    await loadCues();
    if (myGeneration !== generation) return;
    scriptLoaded = true;
    enabled = false;
    fatalError = false;
    autoPaused = false;
    setProgress(cues.length, cues.length, false);
    setStatus(`Đã tải ${cues.length} câu phụ đề. Bấm Phát & lồng tiếng; giọng sẽ được tạo cuốn chiếu.`, "success");
    if (ui) {
      ui.load.disabled = true;
      ui.load.textContent = "Đã tải script";
      ui.start.hidden = false;
      ui.start.disabled = false;
      ui.start.textContent = "▶ Phát & lồng tiếng";
      // Hiện nút Tóm tắt nếu script đã tải (nhưng chỉ bấm được khi đã đăng nhập).
      if (ui.summarize) {
        ui.summarize.hidden = false;
        ui.summarize.disabled = false;
        applySummarizeGate();
      }
    }
  } catch (error) {
    if (myGeneration === generation) {
      setProgress(0, 0, false);
      setStatus(error?.message || "Không tải được script phụ đề.", "error");
      if (ui) {
        ui.load.disabled = false;
        ui.load.textContent = "Tải script";
      }
    }
  } finally {
    if (myGeneration === generation) loadingScript = false;
  }
}

// Gom nhiều câu sắp tới thành một request dịch; server lưu cache nên /speak sau đó không gọi LLM nữa.
function startBatchTranslate(now, ahead) {
  if (Date.now() < rateLimitUntil || Date.now() < batchRetryAt) return;
  const group = [];
  for (const cue of cues) {
    if (cue.state !== "new" || cue.batched) continue;
    if (cue.end + MAX_LATE_S < now) continue;
    if (cue.start > now + ahead) break;
    group.push(cue);
    if (group.length >= BATCH_SIZE) break;
  }
  if (!group.length) return;
  const myGeneration = generation;
  for (const cue of group) cue.batched = true;
  const promise = chrome.runtime
    .sendMessage({ type: "translate_batch", texts: group.map((cue) => cue.en) })
    .then((result) => {
      if (myGeneration !== generation || result?.ok) return;
      for (const cue of group) cue.batched = false;
      if (result?.status === 429) {
        rateLimitUntil = Math.max(rateLimitUntil, Date.now() + Math.max(1000, Number(result.retryAfter || 0) * 1000));
      } else {
        batchRetryAt = Date.now() + 5000;
      }
    })
    .catch(() => { batchRetryAt = Date.now() + 5000; })
    .finally(() => { for (const cue of group) if (cue.batchPromise === promise) cue.batchPromise = null; });
  for (const cue of group) cue.batchPromise = promise;
}

function requestSpeak(text, voice = voiceId) {
  return chrome.runtime.sendMessage({ type: "speak", text, voice });
}

// Đích xuất tóm tắt: "gdocs" | "notion" | "none" (đặt trong popup).
async function getExportTarget() {
  try {
    const { exportTarget } = await chrome.storage.local.get(["exportTarget"]);
    return ["gdocs", "notion", "none"].includes(exportTarget) ? exportTarget : "gdocs";
  } catch (_) {
    return "gdocs";
  }
}

// Dịch phần phụ đề chưa có bản Tiếng Việt để tóm tắt được toàn bộ bài.
async function ensureVietnameseCues() {
  const pending = cues.filter((cue) => cue.en && !cue.vi);
  if (!pending.length) return true;
  const size = Math.max(1, Math.min(BATCH_SIZE || 16, 16));
  for (let index = 0; index < pending.length; index += size) {
    const group = pending.slice(index, index + size);
    const result = await chrome.runtime.sendMessage({
      type: "translate_batch",
      texts: group.map((cue) => cue.en),
    });
    if (!result?.ok) return false;
    const items = result.items || [];
    items.forEach((item, offset) => {
      const cue = group[offset];
      if (cue && typeof item?.text === "string" && item.text.trim()) cue.vi = item.text.trim();
    });
  }
  return cues.some((cue) => cue.vi);
}

function showSummary(summary, url, target) {
  if (!ui) return;
  if (summary) {
    ui.summaryText.textContent = summary;
    ui.summaryBox.hidden = false;
  }
  if (url) {
    ui.summaryLinkAnchor.href = url;
    ui.summaryLinkAnchor.textContent = target === "notion" ? "Mở trang Notion ↗" : "Mở Google Docs ↗";
    ui.summaryLink.style.display = "block";
  } else {
    ui.summaryLink.style.display = "none";
  }
}

// Tóm tắt chỉ dùng được khi đã đăng nhập Google hoặc đã cấu hình Notion.
async function summarizeUnlocked() {
  try {
    const google = await chrome.runtime.sendMessage({ type: "google_auth_status" });
    if (google?.ok && google.connected) return true;
    const notion = await chrome.runtime.sendMessage({ type: "notion_status" });
    return Boolean(notion?.ok && notion.configured);
  } catch (_) {
    return false;
  }
}

// Khoá/mở nút Tóm tắt theo trạng thái đăng nhập.
async function applySummarizeGate() {
  if (!ui?.summarize) return;
  const unlocked = await summarizeUnlocked();
  ui.summarize.hidden = false;
  ui.summarize.disabled = !unlocked;
  ui.summarize.textContent = unlocked ? "💾 Lưu tóm tắt để học" : "🔒 Lưu tóm tắt: đăng nhập Google";
  ui.summarize.title = unlocked
    ? ""
    : "Mở popup Viet Dub và bấm “Tiếp tục với Google” để dùng tóm tắt bài học.";
}

async function summarizeAndExport() {
  if (!scriptLoaded || !cues.length) {
    setStatus("Tải script phụ đề trước khi tóm tắt.", "error");
    return;
  }
  if (!(await summarizeUnlocked())) {
    setStatus("Đăng nhập Google trong popup Viet Dub để dùng tóm tắt bài học.", "error");
    applySummarizeGate();
    return;
  }
  const target = await getExportTarget();
  if (ui?.summarize) {
    ui.summarize.disabled = true;
    ui.summarize.textContent = "Đang xử lý…";
  }
  try {
    if (!cues.some((cue) => cue.vi)) {
      setStatus("Đang dịch toàn bộ phụ đề sang Tiếng Việt…", "working");
      const translated = await ensureVietnameseCues();
      if (!translated) {
        setStatus("Không dịch được phụ đề. Kiểm tra API key và thử lại.", "error");
        return;
      }
    }
    const viCues = cues.map((cue) => cue.vi).filter(Boolean);
    if (viCues.length < 3) {
      setStatus("Phụ đề quá ngắn để tóm tắt (cần ít nhất 3 câu).", "error");
      return;
    }
    setStatus("Đang tóm tắt bài học bằng AI…", "working");
    const { exportTargets = {}, appendExisting = true } = await chrome.storage.local.get([
      "exportTargets",
      "appendExisting",
    ]);
    const result = await chrome.runtime.sendMessage({
      type: "summarize",
      title: videoTitle(),
      vi_cues: viCues,
      export_to: target,
      target_id: appendExisting ? (exportTargets[target] || "") : "",
    });
    if (!result?.ok) {
      setStatus(result?.error || "Tóm tắt thất bại.", "error");
      return;
    }
    if (result.target_id && result.target && result.target !== "none") {
      exportTargets[result.target] = result.target_id;
      await chrome.storage.local.set({ exportTargets });
    }
    showSummary(result.summary || "", result.target_url || "", result.target || target);
    setStatus(result.message || "Đã tóm tắt bài học.", "success");
    console.log(TAG, "Tóm tắt:", result.summary);
  } catch (error) {
    console.warn(TAG, "Lỗi tóm tắt:", error);
    setStatus("Lỗi kết nối server khi tóm tắt.", "error");
  } finally {
    if (ui?.summarize) {
      ui.summarize.disabled = false;
      ui.summarize.textContent = "💾 Lưu tóm tắt để học";
    }
  }
}

async function waitForRetry(ms, attempt, cue, myGeneration, rateLimited = false) {
  const endAt = Date.now() + Math.max(0, ms);
  while (Date.now() < endAt) {
    if (myGeneration !== generation) return false;
    const seconds = Math.max(1, Math.ceil((endAt - Date.now()) / 1000));
    const reason = rateLimited ? "Dịch vụ đang giới hạn request" : "Đang chờ request phản hồi";
    setStatus(`${reason}; thử lại sau ${seconds}s (lần ${attempt}/${MAX_RETRIES})…`, "working");
    await sleep(Math.min(1000, endAt - Date.now()));
  }
  return myGeneration === generation;
}

function refreshWorkingStatus() {
  if (fatalError || !enabled || !scriptLoaded) return;
  const ready = cues.filter((cue) => cue.audio).length;
  setStatus(`Đang lồng tiếng cuốn chiếu · ${ready}/${cues.length} câu đã sẵn sàng.`, "working");
}

function skipFailedCue(cue, error) {
  // Chính sách "video không tự dừng vì lồng tiếng": bỏ qua câu lỗi, video chạy
  // tiếp. Người dùng vẫn có thể bấm “Thử lại lỗi” để dịch lại các câu đã bỏ qua.
  cue.state = "skipfail";
  setStatus(`Bỏ qua câu lỗi (${error}); video vẫn chạy. Bấm “Thử lại lỗi” để dịch lại các câu bị bỏ qua.`, "error");
  if (ui) {
    ui.load.disabled = false;
    ui.load.textContent = "Thử lại lỗi";
  }
  console.warn(TAG, "Bỏ qua câu không tạo được giọng:", cue.en, error);
}

function handleCueFailure(cue, error) {
  skipFailedCue(cue, error);
}

async function processCue(cue, myGeneration = generation) {
  if (!cue || cue.state === "loading" || cue.audio) return;
  cue.state = "loading";
  cue.loadingSince = Date.now();
  inflight++;
  let lastError = "unknown_error";
  let previousFailure = null;
  try {
    const cachedVoice = voiceId;
    const cached = await getCachedCueAudio(cue.en, cachedVoice);
    if (myGeneration !== generation) return;
    // Người dùng đổi giọng trong lúc chờ cache → bỏ kết quả cũ, tải lại giọng mới.
    if (cachedVoice !== voiceId) {
      cue.state = "new";
      return;
    }
    if (cached) {
      cue.audio = cached.audio;
      cue.vi = cached.vi;
      cue.state = "ready";
      preloadCueAudio(cue);
      if (!fatalError) refreshWorkingStatus();
      resumeIfReady();
      return;
    }
    setStatus("Đang dịch và tạo giọng cho các câu sắp tới…", "working");
    if (cue.batchPromise) {
      await cue.batchPromise;
      if (myGeneration !== generation) return;
    }
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      if (myGeneration !== generation) return;
      const sharedWait = Math.max(0, rateLimitUntil - Date.now());
      if (sharedWait > 0) {
        if (!await waitForRetry(sharedWait, attempt || 1, cue, myGeneration, true)) return;
      }
      if (attempt > 0) {
        const exponential = Math.min(30000, 1500 * (2 ** (attempt - 1)));
        const serverWait = previousFailure?.status === 429
          ? (sharedWait > 0 ? 0 : Math.max(exponential, Number(previousFailure.retryAfter || 0) * 1000))
          : exponential;
        const jitter = previousFailure?.status === 429 ? Math.random() * 1000 : Math.random() * Math.min(1000, exponential * 0.2);
        if (!await waitForRetry(serverWait + jitter, attempt, cue, myGeneration, previousFailure?.status === 429)) return;
      }
      try {
        const attemptVoice = voiceId;
        const result = await requestSpeak(cue.en, attemptVoice);
        if (myGeneration !== generation) return;
        // Đổi giọng giữa lúc request đang chạy → không dùng audio của giọng cũ.
        if (attemptVoice !== voiceId) {
          cue.state = "new";
          cue.loadingSince = 0;
          return;
        }
        if (!result?.ok) {
          const error = new Error(result?.error || "request_failed");
          error.status = Number(result?.status || 0);
          error.retryAfter = Number(result?.retryAfter || 0);
          throw error;
        }
        if (!result.audio) throw new Error("no_audio_received");
        cue.audio = result.audio;
        cue.vi = result.text || "";
        rememberCueAudio(cue.en, cue.audio, cue.vi, true, attemptVoice);
        cue.state = "ready";
        preloadCueAudio(cue);
        if (!fatalError) refreshWorkingStatus();
        resumeIfReady();
        return;
      } catch (error) {
        lastError = String(error?.message || error);
        previousFailure = error;
        if (error?.status === 429) {
          const waitMs = Math.max(1000, Number(error.retryAfter || 0) * 1000);
          rateLimitUntil = Math.max(rateLimitUntil, Date.now() + waitMs);
        }
        console.warn(TAG, `Câu lỗi, lần thử ${attempt + 1}/${MAX_RETRIES + 1}:`, lastError);
      }
    }
    if (myGeneration === generation) {
      cue.state = "failed";
      handleCueFailure(cue, lastError);
    }
  } finally {
    inflight = Math.max(0, inflight - 1);
    // Request bị huỷ giữa đường (đổi generation, seek, lỗi mạng): trả câu về
    // hàng đợi thay vì để nó kẹt "loading" vĩnh viễn và chặn cả đoạn phía sau.
    if (cue.state === "loading" && !cue.audio) {
      cue.state = "new";
    }
    if (!cue.audio) cue.loadingSince = 0;
  }
}

// Câu bị kẹt "loading" (request chết/treo, tab bị treo, seek giữa chừng) sẽ chặn
// cả đoạn phía sau nếu không được đưa về hàng đợi. Trả về true nếu vừa chữa.
function healStuckCue(cue, nowMs = Date.now()) {
  if (!cue || cue.audio || cue.state !== "loading") return false;
  if (!cue.loadingSince || nowMs - cue.loadingSince <= STUCK_LOADING_MS) return false;
  cue.state = "new";
  cue.loadingSince = 0;
  return true;
}

function prefetchTick(force = false) {
  if (syncAdState()) return;
  if (!enabled || !scriptLoaded || fatalError) return;
  const video = getVideo();
  if (!video || (video.paused && !autoPaused && !force)) return;
  const now = video.currentTime;
  const ahead = prefetchWindow(video);
  startBatchTranslate(now, ahead);
  const nowMs = Date.now();
  const softCap = MAX_PARALLEL + NEAR_EXTRA_SLOTS;
  for (const cue of cues) {
    if (cue.end + MAX_LATE_S < now) continue;
    if (cue.start > now + ahead) break;
    // Tự chữa câu kẹt "loading" (request chết/treo) để không mất tiếng cả đoạn.
    if (healStuckCue(cue, nowMs)) {
      console.warn(TAG, "Câu kẹt khi tải giọng, thử lại:", cue.en);
    }
    if (cue.state !== "new") continue;
    // Cờ batched cũ còn sót (batch bị huỷ khi seek) không được chặn dịch lại.
    if (cue.batched && !cue.batchPromise) cue.batched = false;
    // Câu sát vị trí đang phát (nhất là ngay sau khi tua) được ưu tiên slot,
    // để không phải chờ các câu xa đang tải dở.
    const near = cue.start <= now + NEAR_WINDOW_S;
    if (inflight >= (near ? softCap : MAX_PARALLEL)) {
      if (!near) break;
      continue;
    }
    processCue(cue, generation);
  }
}

async function retryFailedCues() {
  const failed = cues.filter((cue) => cue.state === "skipfail");
  if (!failed.length || loadingScript) return;
  fatalError = false;
  loadingScript = true;
  const myGeneration = generation;
  for (const cue of failed) cue.state = "new";
  if (ui) {
    ui.load.disabled = true;
    ui.load.textContent = "Đang thử lại…";
    ui.start.disabled = true;
  }
  setStatus(`Đang thử lại ${failed.length} câu bị lỗi…`, "working");
  let index = 0;
  const worker = async () => {
    while (index < failed.length && myGeneration === generation && !fatalError) {
      const cue = failed[index++];
      await processCue(cue, myGeneration);
    }
  };
  await Promise.all(Array.from({ length: Math.min(MAX_PARALLEL, failed.length) }, worker));
  if (myGeneration === generation) {
    loadingScript = false;
    const remainingFailures = cues.filter((cue) => cue.state === "skipfail").length;
    if (!fatalError && remainingFailures) {
      if (ui) {
        ui.load.disabled = false;
        ui.load.textContent = "Thử lại lỗi";
        ui.start.disabled = false;
      }
      setStatus(`Còn ${remainingFailures} câu lỗi đã bỏ qua. Video vẫn chạy; bấm “Thử lại lỗi” để thử tiếp.`, "error");
    } else if (!fatalError) {
      if (ui) {
        ui.load.disabled = true;
        ui.load.textContent = "Đã tải script";
        ui.start.disabled = false;
      }
      setStatus("Đã khôi phục câu lỗi. Tiếp tục phát để lồng tiếng.", "success");
      resumeIfReady();
    } else if (ui) {
      ui.load.disabled = false;
    }
  }
}

function resumeIfReady() {
  if (!enabled || fatalError || !autoPaused) return;
  if (currentAudio && !currentAudio.ended) return;
  const video = getVideo();
  if (!video) return;
  const now = video.currentTime;

  // Kiểm tra xem có câu nào đang đến lượt (hoặc trễ) mà chưa có audio không
  const dueCue = cues.find((cue) => !isConsumedState(cue.state) && cue.start <= now + 0.25 && cue.end + MAX_LATE_S >= now);
  if (dueCue) {
    if (dueCue.state === "failed") {
      skipFailedCue(dueCue, "translation_or_tts_failed");
      return;
    }
    if (!dueCue.audio) {
      prefetchTick(true);
      return;
    }
  }

  // Không có câu nào đang kẹt chờ audio hoặc câu đến hạn đã sẵn sàng: mở lại video
  autoPaused = false;
  video.play().catch(() => {
    autoPaused = true;
    setStatus("Đã sẵn sàng nhưng không tự phát được. Bấm Play trong YouTube để tiếp tục.", "error");
  });
}

function toggleDubbing() {
  if (!scriptLoaded) return;
  if (enabled) {
    enabled = false;
    autoPaused = false;
    if (currentAudio) currentAudio.pause();
    chrome.storage.local.set({ enabled: false });
    setStatus("Đã tắt lồng tiếng.", "idle");
    if (ui) ui.start.textContent = "▶ Bật lồng tiếng";
    return;
  }
  if (fatalError) return;

  enabled = true;
  chrome.storage.local.set({ enabled: true });
  if (ui) ui.start.textContent = "Tắt lồng tiếng";
  refreshWorkingStatus();
  const video = getVideo();
  if (video?.paused) {
    video.play().catch(() => setStatus("Không phát được video. Hãy bấm Play trên YouTube.", "error"));
  }
  prefetchTick(true);
}

// ---------- Chọn giọng đọc ----------
// Danh sách lấy từ server (/voices); nếu server tắt thì dùng 2 giọng Việt mặc định.
// Người dùng vẫn nhập được Voice ID bất kỳ đúng định dạng Edge TTS.
async function loadVoiceOptions() {
  if (voiceOptionsLoaded) return;
  try {
    const result = await chrome.runtime.sendMessage({ type: "voices" });
    if (result?.ok && Array.isArray(result.voices) && result.voices.length) {
      voicePresets = result.voices
        .map((item) => ({
          id: normalizeVoiceId(item?.id),
          label: String(item?.label || item?.id || ""),
        }))
        .filter((item) => item.id);
    }
  } catch (_) {
    /* Server chưa chạy: vẫn cho chọn 2 giọng Việt. */
  }
  if (!voicePresets.length) voicePresets = FALLBACK_VOICE_PRESETS.slice();
  voiceOptionsLoaded = true;
  renderVoiceOptions();
}

function renderVoiceOptions() {
  if (!ui || !ui.voice) return;
  const options = voicePresets.length ? voicePresets : FALLBACK_VOICE_PRESETS;
  ui.voice.textContent = "";
  for (const preset of options) {
    const option = document.createElement("option");
    option.value = preset.id;
    option.textContent = preset.label || preset.id;
    ui.voice.appendChild(option);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "Khác — nhập Voice ID…";
  ui.voice.appendChild(custom);
  syncVoiceUi();
}

function syncVoiceUi() {
  if (!ui || !ui.voice) return;
  const options = voicePresets.length ? voicePresets : FALLBACK_VOICE_PRESETS;
  const known = options.some((preset) => preset.id === voiceId);
  ui.voice.value = known ? voiceId : "__custom__";
  ui.voiceCustomRow.hidden = known;
  if (!known) ui.voiceCustom.value = voiceId;
}

function setVoice(value) {
  const voice = normalizeVoiceId(value);
  if (!voice) {
    setStatus("Voice ID không hợp lệ. Đúng dạng: vi-VN-NamMinhNeural.", "error");
    syncVoiceUi();
    return;
  }
  if (voice === voiceId) {
    syncVoiceUi();
    return;
  }
  voiceId = voice;
  chrome.storage.local.set({ voice });
  syncVoiceUi();
  invalidateCueAudio();
  setStatus(`Đã đổi giọng đọc: ${voiceLabel(voice)}. Câu tiếp theo dùng giọng mới.`, "success");
}

// Đổi giọng: bỏ audio đã tạo bằng giọng cũ (cache key có voice nên không lẫn)
// và trả các câu chưa phát về hàng đợi để tải lại bằng giọng mới.
function invalidateCueAudio() {
  if (currentAudio) cutCurrentAudio();
  for (const cue of cues) {
    if (isConsumedState(cue.state)) continue;
    cleanupCueAudio(cue);
    cue.audio = null;
    cue.vi = "";
    cue.state = "new";
    cue.batched = false;
    cue.batchPromise = null;
    cue.loadingSince = 0;
  }
  if (enabled && scriptLoaded && !fatalError) prefetchTick(true);
}

// ---------- Phát đúng thời điểm ----------
// Câu đã phát/bỏ qua/lỗi-đã-bỏ-qua: không được phát lại cũng không chặn câu sau.
function isConsumedState(state) {
  return state === "played" || state === "skipped" || state === "skipfail";
}

// Cắt nốt câu đang đọc (khi câu sau đã tới giờ) nhưng KHÔNG dừng video.
function cutCurrentAudio() {
  const audio = currentAudio;
  const cue = currentCue;
  if (!audio) return;
  audio.onended = null;
  audio.onerror = null;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  currentAudio = null;
  if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl);
  currentAudioUrl = null;
  currentCue = null;
  if (cue) cleanupCueAudio(cue);
}

// Tỷ lệ thời lượng audio cần bỏ qua để khớp thời điểm hiện tại của video.
// 0 = phát cả câu (video chưa vào sâu trong câu).
function midCueResumeFraction(cue, now) {
  if (!cue || !(cue.end > cue.start)) return 0;
  const into = now - cue.start;
  if (into <= 0.6) return 0; // vào câu chưa sâu: đọc từ đầu cho tự nhiên
  return Math.min(0.92, into / (cue.end - cue.start));
}

function playCue(cue, now) {
  if (!cue || !cue.audio) {
    if (cue) cue.state = "skipped";
    return;
  }
  try {
    playCueSafe(cue, now);
  } catch (error) {
    // Audio hỏng/giải mã lỗi/blob đã bị thu hồi: bỏ qua câu này thay vì ném lỗi
    // mỗi 100ms và làm đứt cả vòng phát lồng tiếng.
    console.warn(TAG, "Bỏ qua câu không phát được:", error?.message || error);
    cue.state = "skipped";
    cue.audio = null;
    cleanupCueAudio(cue);
    if (currentCue === cue) {
      currentAudio = null;
      currentAudioUrl = null;
      currentCue = null;
    }
  }
}

function playCueSafe(cue, now) {
  let audio = cue.audioElement;
  let url = cue.audioUrl;
  if (!audio) {
    url = b64ToUrl(cue.audio);
    audio = new Audio(url);
    audio.preload = "auto";
    cue.audioUrl = url;
    cue.audioElement = audio;
  }

  currentAudioUrl = url;
  const vRate = getVideo()?.playbackRate || 1;
  const naturalDuration = cue.duration || (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0);
  const effectiveRate = calculateAdaptivePlaybackRate(cue, naturalDuration, vRate);

  if (Number.isFinite(effectiveRate) && effectiveRate > 0) {
    audio.playbackRate = Math.min(16, Math.max(0.0625, effectiveRate));
  }
  audio.preservesPitch = true;

  const finish = () => {
    if (currentAudio !== audio) return;
    currentAudio = null;
    currentAudioUrl = null;
    currentCue = null;
    try {
      cleanupCueAudio(cue);
      resumeIfReady();

      // Chuyển câu mượt không độ trễ: nếu câu tiếp theo đã tới hạn thì phát luôn
      const v = getVideo();
      if (v && !v.paused && enabled && scriptLoaded && !fatalError) {
        const nextNow = v.currentTime;
        const nextDue = cues.find((c) => !isConsumedState(c.state) && c.start <= nextNow && c.end + MAX_LATE_S >= nextNow);
        if (nextDue && nextDue.audio) {
          playCue(nextDue, nextNow);
        }
      }
    } catch (error) {
      console.warn(TAG, "Lỗi khi chuyển câu lồng tiếng:", error?.message || error);
    }
  };

  audio.onended = finish;
  audio.onerror = finish;
  currentAudio = audio;
  currentCue = cue;
  cue.state = "played";

  // Đang phát trễ hoặc vừa tua/reload: tiếp nối giữa câu thay vì đọc lại từ đầu —
  // giọng đọc nhảy tới đúng vị trí khớp với thời điểm hiện tại của video.
  const resumeFraction = midCueResumeFraction(cue, now);
  if (resumeFraction > 0) {
    const applyOffset = () => {
      try {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          audio.currentTime = Math.min(audio.duration - 0.05, resumeFraction * audio.duration);
        }
      } catch (_) {
        /* Chưa nhảy được (audio chưa mở) thì phát từ đầu câu. */
      }
    };
    if (audio.readyState >= 1) applyOffset();
    else audio.addEventListener("loadedmetadata", applyOffset, { once: true });
  }

  try {
    const playing = audio.play();
    if (playing && typeof playing.catch === "function") {
      playing.catch((error) => {
        console.warn(TAG, "Không phát được âm thanh:", error?.message || error);
        finish();
      });
    }
  } catch (error) {
    console.warn(TAG, "Không phát được âm thanh:", error?.message || error);
    finish();
  }
}

function playTick() {
  if (syncAdState()) return;
  if (!enabled || !scriptLoaded || fatalError) return;
  const video = getVideo();
  if (!video || video.paused) return;
  const now = video.currentTime;
  const vRate = video.playbackRate || 1;

  if (currentAudio && !currentAudio.ended) {
    if (currentAudio.paused) {
      currentAudio.play().catch(() => {});
      return;
    }
    const currentIndex = cues.indexOf(currentCue);
    const nextCue = cues.slice(currentIndex + 1).find((cue) => !isConsumedState(cue.state));
    if (nextCue && nextCue.start <= now + 0.3) {
      const dur = currentAudio.duration;
      const curTime = currentAudio.currentTime;
      const currentRate = currentAudio.playbackRate || vRate;
      const remainingAudioSeconds = (Number.isFinite(dur) && dur > 0)
        ? Math.max(0, (dur - curTime) / currentRate)
        : 0.5;

      // Nếu chỉ còn một chút audio (<= 0.75s), tăng tốc nhẹ câu hiện tại để hoàn tất êm thay vì dừng video
      if (remainingAudioSeconds <= 0.75) {
        currentAudio.playbackRate = Math.min(vRate * 1.45, currentRate * 1.15);
        return;
      }

      // Câu sau đã tới giờ: KHÔNG dừng video nữa — cắt nốt câu trước và chuyển
      // ngay sang câu sau để video luôn chạy liên tục.
      if (now >= nextCue.start + 0.25) {
        cutCurrentAudio();
        playCue(nextCue, now);
        return;
      }
    }
    return;
  }

  for (const cue of cues) {
    if (isConsumedState(cue.state)) continue;
    if (cue.start > now) break;
    if (cue.end + MAX_LATE_S < now) {
      cue.state = "skipped";
      continue;
    }
    if (cue.state === "failed") {
      skipFailedCue(cue, "dịch/tts thất bại");
      continue;
    }
    if (!cue.audio) {
      // Câu hiện tại đã trễ mà chưa có giọng, trong khi câu kế đã sẵn sàng
      // (thường gặp ngay sau khi tua): nhường cho câu sau để không im cả đoạn.
      if (now - cue.start > 1.2) {
        const idx = cues.indexOf(cue);
        const readyNext = idx >= 0
          ? cues.slice(idx + 1).find((c) => !isConsumedState(c.state) && c.audio)
          : null;
        if (readyNext && readyNext.start <= now + 0.3) {
          cue.state = "skipped";
          continue;
        }
      }
      // Câu kẹt "loading" quá lâu → trả về hàng đợi để vòng prefetch thử lại.
      healStuckCue(cue);
    }
    if (cue.audio) {
      playCue(cue, now);
      return;
    }
    // Cho phép vùng đệm 0.4s để tải audio trước khi phải đọc trễ
    if (now - cue.start < AUDIO_GRACE_PERIOD_S) {
      if (cue.state === "new") processCue(cue, generation);
      return;
    }
    // Quá vùng đệm mà audio chưa xong: video vẫn chạy (không tự dừng nữa) —
    // audio xong lúc nào thì đọc dạt lúc đó; quá trễ (end + MAX_LATE_S) thì bị bỏ qua.
    if (cue.state === "new") processCue(cue, generation);
    return;
  }
}

function resetPlayback() {
  autoPaused = false;
  fatalError = false;
  if (currentAudio) {
    currentAudio.onended = null;
    currentAudio.onerror = null;
    currentAudio.pause();
    currentAudio.removeAttribute("src");
    currentAudio.load();
    currentAudio = null;
    if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl);
    currentAudioUrl = null;
    currentCue = null;
  }
  const video = getVideo();
  const now = video ? video.currentTime : 0;
  for (const cue of cues) {
    if (cue.end + MAX_LATE_S < now) {
      cue.state = "skipped";
      cleanupCueAudio(cue);
    } else {
      cue.state = cue.audio ? "ready" : (cue.state === "loading" ? "loading" : "new");
      // Vùng mới sau khi tua phải được gom dịch lại từ đầu.
      cue.batched = false;
      cue.batchPromise = null;
      if (cue.audio && !cue.audioElement) {
        preloadCueAudio(cue);
      }
    }
  }
  if (ui && scriptLoaded) ui.start.disabled = false;
}

function bindVideo() {
  const video = getVideo();
  if (!video || video === boundVideo) return;
  boundVideo = video;
  updateVideoRateUi(video.playbackRate);
  video.addEventListener("pause", () => {
    if (!autoPaused && currentAudio) currentAudio.pause();
  });
  video.addEventListener("play", () => currentAudio && currentAudio.play().catch(() => {}));
  video.addEventListener("seeking", () => enabled && resetPlayback());
  // Sau khi YouTube hoàn tất việc tua: khởi động lại pipeline ngay tại vị trí mới
  // (đệm trước các câu sắp tới) thay vì chờ tick 300ms và tiếp tục việc cũ.
  video.addEventListener("seeked", () => {
    if (!enabled) return;
    prefetchTick(true);
    playTick();
  });
  video.addEventListener("ratechange", () => {
    updateVideoRateUi(video.playbackRate);
    applyEffectiveSpeechRate();
    prefetchTick(true);
  });
}

function resetForNavigation() {
  generation++;
  enabled = false;
  loadingScript = false;
  scriptLoaded = false;
  inflight = 0;
  autoPaused = false;
  fatalError = false;
  adActive = false;
  for (const cue of cues) {
    cleanupCueAudio(cue);
  }
  cues = [];
  loadedFor = "";
  boundVideo = null;
  if (currentAudio) {
    currentAudio.onended = null;
    currentAudio.onerror = null;
    currentAudio.pause();
    currentAudio.removeAttribute("src");
    currentAudio.load();
    currentAudio = null;
    if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl);
    currentAudioUrl = null;
    currentCue = null;
  }
  chrome.storage.local.set({ enabled: false });
  setStatus("Tải script phụ đề để bắt đầu.", "idle");
  setProgress(0, 0, false);
  if (ui) {
    ui.load.disabled = false;
    ui.load.textContent = "Tải script";
    ui.start.disabled = true;
    ui.start.hidden = true;
    if (ui.summarize) {
      ui.summarize.hidden = true;
      ui.summarize.disabled = true;
    }
    if (ui.summaryBox) ui.summaryBox.hidden = true;
    if (ui.summaryLink) ui.summaryLink.style.display = "none";
  }
  setTimeout(mountWidget, 500);
}

setInterval(() => {
  mountWidget();
  bindVideo();
}, 1000);
setInterval(prefetchTick, 300);
setInterval(playTick, 100);
document.addEventListener("yt-navigate-finish", resetForNavigation);

// Udemy và các web SPA khác đổi bài bằng History API (không có sự kiện
// "yt-navigate-finish") → theo dõi id video để reset khi chuyển bài.
let lastVideoId = PLATFORM === "youtube" ? "" : videoId();
function watchSpaNavigation() {
  if (PLATFORM === "youtube") return;
  const id = videoId();
  if (!id || id === lastVideoId) return;
  const changed = lastVideoId !== "";
  lastVideoId = id;
  if (changed) {
    console.log(TAG, "đổi bài mới:", id);
    resetForNavigation();
  }
}
setInterval(watchSpaNavigation, 700);

chrome.storage.local.get(["enabled", "voice"]).then(({ enabled: savedEnabled = false, voice = "" }) => {
  enabled = savedEnabled;
  const savedVoice = normalizeVoiceId(voice);
  if (savedVoice) voiceId = savedVoice;
  syncVoiceUi();
  console.log(TAG, "sẵn sàng; lồng tiếng", enabled ? "đang bật" : "đang tắt", "· giọng", voiceId);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.enabled) {
    enabled = Boolean(changes.enabled.newValue);
    if (!enabled && currentAudio) currentAudio.pause();
    if (ui && scriptLoaded) ui.start.textContent = enabled ? "Tắt lồng tiếng" : "▶ Bật lồng tiếng";
  }
  if (changes.voice) {
    const next = normalizeVoiceId(changes.voice.newValue);
    if (next && next !== voiceId) {
      voiceId = next;
      syncVoiceUi();
      invalidateCueAudio();
    }
  }
});

mountWidget();
