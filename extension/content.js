const TAG = "[VietDub]";
const MAX_PARALLEL = 3; // Tăng lên 3 luồng tải audio song song để đệm trước không bị rỗng
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
const clientAudioCache = new Map();
let clientAudioCacheBytes = 0;
let clientCacheDbPromise = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function getVideo() {
  return document.querySelector("video.html5-main-video");
}

function videoId() {
  return new URLSearchParams(location.search).get("v") || "";
}

function cueCacheKey(text) {
  return String(text || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
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

async function getCachedCueAudio(text) {
  const key = cueCacheKey(text);
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
    rememberCueAudio(text, entry.audio, entry.vi || "", false);
    return { audio: entry.audio, vi: entry.vi || "", bytes: entry.bytes || 0 };
  } catch (error) {
    console.warn(TAG, "Không đọc được cache local của trình duyệt:", error?.message || error);
    return null;
  }
}

function rememberCueAudio(text, audio, vi, persist = true) {
  const key = cueCacheKey(text);
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
  const player = document.querySelector("#movie_player");
  if (!player) return false;
  if (host && host.parentElement === player) return true;
  if (host) host.remove();

  host = document.createElement("div");
  host.id = "vietdub-widget-host";
  host.style.cssText = [
    "position:absolute", "top:76px", "right:16px", "z-index:2147483647",
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
      .bubble { width:44px; height:44px; min-height:44px; padding:0; border-radius:50%; box-shadow:0 4px 18px rgba(0,0,0,.45); cursor:grab; touch-action:none; }
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
      button {
        appearance:none; border:0; border-radius:8px; padding:8px 9px; background:#2e78e6;
        color:#fff; font:600 12px system-ui,sans-serif; cursor:pointer; flex:1; min-height:34px;
      }
      button.secondary { background:#3b414b; }
      button:disabled { opacity:.48; cursor:not-allowed; }
      .progress { height:4px; width:100%; margin:0 0 9px; accent-color:#53bd79; }
      .hint { color:#aeb5bf; font-size:10px; margin-top:7px; }
      .videoSpeed { display:flex; align-items:center; gap:8px; margin-top:10px; color:#d5d9df; }
      .videoSpeed label { white-space:nowrap; }
      .videoRateValue { min-width:38px; text-align:right; color:#fff; font-variant-numeric:tabular-nums; }
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
      <div class="videoSpeed">
        <span>Tốc độ đọc YouTube</span>
        <span id="videoRateValue" class="videoRateValue" aria-live="polite">1.00×</span>
      </div>
      <div class="hint">Âm thanh tự bám tốc độ phát trên YouTube. Đổi tốc độ trong trình phát.</div>
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
    videoRateValue: shadow.getElementById("videoRateValue"),
  };
  ui.load.addEventListener("click", onLoadButton);
  ui.start.addEventListener("click", toggleDubbing);
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
  updateVideoRateUi(getVideo()?.playbackRate || 1);
  const thisHost = host;
  chrome.storage.local.get(["widgetPosition", "widgetCollapsed"]).then((saved) => {
    if (host !== thisHost || !ui) return;
    setWidgetCollapsed(Boolean(saved.widgetCollapsed), false);
    if (saved.widgetPosition) applyWidgetPosition(saved.widgetPosition);
  });
  return true;
}

function setWidgetCollapsed(value, persist = false) {
  widgetCollapsed = Boolean(value);
  if (ui) {
    ui.panel.hidden = widgetCollapsed;
    ui.bubble.hidden = !widgetCollapsed;
    if (host) host.style.width = widgetCollapsed ? "44px" : "280px";
  }
  if (!widgetCollapsed && host?.parentElement) {
    const widgetRect = host.getBoundingClientRect();
    const playerRect = host.parentElement.getBoundingClientRect();
    applyWidgetPosition({ left: widgetRect.left - playerRect.left, top: widgetRect.top - playerRect.top });
  }
  if (persist) chrome.storage.local.set({ widgetCollapsed }).catch(() => {});
}

function applyWidgetPosition(position) {
  const player = host?.parentElement;
  if (!player || !position || !Number.isFinite(position.left) || !Number.isFinite(position.top)) return;
  const maxLeft = Math.max(0, player.clientWidth - host.offsetWidth);
  const maxTop = Math.max(0, player.clientHeight - host.offsetHeight);
  host.style.left = `${Math.max(0, Math.min(maxLeft, position.left))}px`;
  host.style.top = `${Math.max(0, Math.min(maxTop, position.top))}px`;
  host.style.right = "auto";
}

function saveWidgetPosition() {
  const player = host?.parentElement;
  if (!player || !host) return;
  const widgetRect = host.getBoundingClientRect();
  const playerRect = player.getBoundingClientRect();
  chrome.storage.local.set({
    widgetPosition: { left: widgetRect.left - playerRect.left, top: widgetRect.top - playerRect.top },
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
    ? cues.slice(currentIndex + 1).find((c) => !["played", "skipped"].includes(c.state))
    : null;

  let windowSeconds = 0;
  if (nextCue && nextCue.start > cue.start) {
    windowSeconds = Math.max(0.6, nextCue.start - cue.start - 0.15);
  } else if (cue.end > cue.start) {
    windowSeconds = Math.max(0.6, cue.end - cue.start + 0.4);
  } else {
    return baseRate;
  }

  const requiredRate = (naturalDuration / windowSeconds) * baseRate;
  const minRate = baseRate * MIN_SPEED_FACTOR;
  const maxRate = baseRate * MAX_SPEED_FACTOR;

  return Math.max(minRate, Math.min(maxRate, requiredRate));
}

function applyEffectiveSpeechRate() {
  if (!currentAudio || currentAudio.ended || !currentCue) return;
  const vRate = getVideo()?.playbackRate || 1;
  const rate = calculateAdaptivePlaybackRate(currentCue, currentCue.duration || currentAudio.duration, vRate);
  currentAudio.playbackRate = rate;
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
      // Nếu giữa 2 từ có khoảng lặng tự nhiên >= 0.65s và câu đã đủ dài, tách thành câu mới
      if (pauseGap >= 0.65 && cur.en.length >= 20) {
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
  if (!id) throw new Error("Không xác định được video YouTube hiện tại.");
  if (loadedFor === id && cues.length) return true;

  const deadline = Date.now() + CAPTION_WAIT_MS;
  const nudgeAt = Date.now() + 2500;
  let nudged = false;
  let triedUrl = "";
  while (Date.now() < deadline) {
    // Chưa thấy request phụ đề: tắt/bật lại nút CC để YouTube gửi lại request.
    if (!nudged && Date.now() >= nudgeAt && !timedtextUrl()) {
      nudged = true;
      const button = document.querySelector(".ytp-subtitles-button");
      if (button) {
        setStatus("Chưa thấy request phụ đề; đang bật/tắt lại CC…", "working");
        if (button.getAttribute("aria-pressed") === "true") { button.click(); await sleep(500); }
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

async function onLoadButton() {
  if (loadingScript) return;
  if (syncAdState()) {
    setStatus("Hãy đợi quảng cáo kết thúc rồi tải script.", "idle");
    return;
  }
  if (cues.some((cue) => cue.state === "failed")) {
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

function requestSpeak(text) {
  return chrome.runtime.sendMessage({ type: "speak", text });
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

function pauseForWait(cue) {
  const video = getVideo();
  if (!video) return;
  if (!video.paused) {
    autoPaused = true;
    video.pause();
  }
  setStatus("Đang chờ tạo giọng cho câu kế tiếp; video tạm dừng để giữ đồng bộ…", "working");
  if (cue.state === "new") prefetchTick(true);
}

function pauseForError(cue, error) {
  fatalError = true;
  const video = getVideo();
  if (video && !video.paused) {
    autoPaused = true;
    video.pause();
  }
  setStatus(`Dịch/TTS lỗi sau ${MAX_RETRIES + 1} lần thử (${error}). Video đã tạm dừng để tránh lệch. Bấm “Thử lại lỗi”.`, "error");
  if (ui) {
    ui.load.disabled = false;
    ui.load.textContent = "Thử lại lỗi";
    ui.start.disabled = true;
  }
  console.warn(TAG, "Tạm dừng vì không tạo được giọng cho câu:", cue.en, error);
}

function handleCueFailure(cue, error) {
  const video = getVideo();
  const secondsUntilCue = cue.start - (video?.currentTime || 0);
  const pauseThreshold = Math.max(4, (video?.playbackRate || 1) * 3);
  if (secondsUntilCue <= pauseThreshold) {
    pauseForError(cue, error);
    return;
  }
  setStatus(`Một câu phía trước bị lỗi (${error}); video vẫn chạy. Sẽ tạm dừng nếu câu đó chưa xử lý được khi sắp tới.`, "error");
  if (ui) {
    ui.load.disabled = false;
    ui.load.textContent = "Thử lại lỗi";
  }
  console.warn(TAG, "Lỗi ở câu chưa tới; chưa dừng video:", cue.en, error);
}

async function processCue(cue, myGeneration = generation) {
  if (!cue || cue.state === "loading" || cue.audio) return;
  cue.state = "loading";
  inflight++;
  let lastError = "unknown_error";
  let previousFailure = null;
  try {
    const cached = await getCachedCueAudio(cue.en);
    if (myGeneration !== generation) return;
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
        const result = await requestSpeak(cue.en);
        if (myGeneration !== generation) return;
        if (!result?.ok) {
          const error = new Error(result?.error || "request_failed");
          error.status = Number(result?.status || 0);
          error.retryAfter = Number(result?.retryAfter || 0);
          throw error;
        }
        if (!result.audio) throw new Error("no_audio_received");
        cue.audio = result.audio;
        cue.vi = result.text || "";
        rememberCueAudio(cue.en, cue.audio, cue.vi);
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
  }
}

function prefetchTick(force = false) {
  if (syncAdState()) return;
  if (!enabled || !scriptLoaded || fatalError) return;
  const video = getVideo();
  if (!video || (video.paused && !autoPaused && !force)) return;
  const now = video.currentTime;
  const ahead = prefetchWindow(video);
  startBatchTranslate(now, ahead);
  for (const cue of cues) {
    if (inflight >= MAX_PARALLEL) break;
    if (cue.state !== "new") continue;
    if (cue.end + MAX_LATE_S < now) continue;
    if (cue.start > now + ahead) break;
    processCue(cue, generation);
  }
}

async function retryFailedCues() {
  const failed = cues.filter((cue) => cue.state === "failed");
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
    const remainingFailures = cues.filter((cue) => cue.state === "failed").length;
    if (!fatalError && remainingFailures) {
      if (ui) {
        ui.load.disabled = false;
        ui.load.textContent = "Thử lại lỗi";
        ui.start.disabled = false;
      }
      setStatus(`Còn ${remainingFailures} câu lỗi. Video sẽ dừng khi sắp tới câu chưa xử lý được.`, "error");
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
  const dueCue = cues.find((cue) => !["played", "skipped"].includes(cue.state) && cue.start <= now + 0.25 && cue.end + MAX_LATE_S >= now);
  if (dueCue) {
    if (dueCue.state === "failed") {
      pauseForError(dueCue, "translation_or_tts_failed");
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

// ---------- Phát đúng thời điểm ----------
function playCue(cue, now) {
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

  audio.playbackRate = effectiveRate;
  audio.preservesPitch = true;

  const finish = () => {
    if (currentAudio === audio) {
      currentAudio = null;
      currentAudioUrl = null;
      currentCue = null;
      cleanupCueAudio(cue);
      resumeIfReady();

      // Chuyển câu mượt không độ trễ: nếu câu tiếp theo đã tới hạn thì phát luôn
      const v = getVideo();
      if (v && !v.paused && enabled && scriptLoaded && !fatalError) {
        const nextNow = v.currentTime;
        const nextDue = cues.find((c) => !["played", "skipped"].includes(c.state) && c.start <= nextNow && c.end + MAX_LATE_S >= nextNow);
        if (nextDue && nextDue.audio) {
          playCue(nextDue, nextNow);
        }
      }
    }
  };

  audio.onended = finish;
  audio.onerror = finish;
  currentAudio = audio;
  currentCue = cue;
  cue.state = "played";

  audio.play().catch((error) => {
    console.warn(TAG, "Không phát được âm thanh:", error.message);
    finish();
  });
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
    const nextCue = cues.slice(currentIndex + 1).find((cue) => !["played", "skipped"].includes(cue.state));
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

      // Chỉ dừng video khi audio câu trước còn quá dài (> 0.75s) VÀ video đã trôi qua điểm bắt đầu câu sau
      if (now >= nextCue.start + 0.25) {
        autoPaused = true;
        video.pause();
        setStatus("Đang chờ câu đọc hiện tại kết thúc; video tạm dừng để không vượt phụ đề…", "working");
      }
    }
    return;
  }

  for (const cue of cues) {
    if (cue.state === "played" || cue.state === "skipped") continue;
    if (cue.start > now) break;
    if (cue.end + MAX_LATE_S < now) {
      cue.state = "skipped";
      continue;
    }
    if (cue.state === "failed") {
      pauseForError(cue, "tts_failed");
      return;
    }
    if (cue.audio) {
      playCue(cue, now);
      return;
    }
    // Cho phép vùng đệm 0.4s để tải audio trước khi buộc dừng video
    if (now - cue.start < AUDIO_GRACE_PERIOD_S) {
      if (cue.state === "new") processCue(cue, generation);
      return;
    }
    pauseForWait(cue);
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

chrome.storage.local.get(["enabled"]).then(({ enabled: savedEnabled = false }) => {
  enabled = savedEnabled;
  console.log(TAG, "sẵn sàng; lồng tiếng", enabled ? "đang bật" : "đang tắt");
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.enabled) {
    enabled = Boolean(changes.enabled.newValue);
    if (!enabled && currentAudio) currentAudio.pause();
    if (ui && scriptLoaded) ui.start.textContent = enabled ? "Tắt lồng tiếng" : "▶ Bật lồng tiếng";
  }
});

mountWidget();
