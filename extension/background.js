const SERVER = "http://127.0.0.1:8765";
const REQUEST_TIMEOUT_MS = 90000;

async function callServer(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${SERVER}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      ...options,
      signal: controller.signal,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
      return { ok: false, error: detail, status: res.status, data };
    }
    return { ok: true, data, status: res.status };
  } catch (error) {
    const message = error?.name === "AbortError"
      ? `Request timeout sau ${timeoutMs / 1000}s`
      : String(error?.message || error);
    return { ok: false, error: message, status: 0, data: {} };
  } finally {
    clearTimeout(timeout);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "translate_batch") {
    (async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const res = await fetch(`${SERVER}/translate-batch`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ texts: msg.texts, target: "vi" }),
          signal: controller.signal,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
          sendResponse({ ok: false, error: detail, status: res.status, retryAfter: Number(res.headers.get("Retry-After")) || 0 });
          return;
        }
        sendResponse({ ok: true, items: data.items || [] });
      } catch (error) {
        const message = error?.name === "AbortError" ? "Batch timeout" : String(error.message || error);
        sendResponse({ ok: false, error: message, status: 0, retryAfter: 0 });
      } finally {
        clearTimeout(timeout);
      }
    })();
    return true;
  }

  // Tóm tắt bài học + xuất ra Google Docs / Notion.
  if (msg?.type === "summarize") {
    (async () => {
      const result = await callServer("/summarize", {
        body: JSON.stringify({
          title: msg.title,
          vi_cues: msg.vi_cues || [],
          export_to: msg.export_to || "none",
          target_id: msg.target_id || "",
        }),
      });
      if (!result.ok) {
        sendResponse({ ok: false, error: result.error, status: result.status });
        return;
      }
      sendResponse({ ok: true, ...result.data });
    })();
    return true;
  }

  // Lấy URL đăng nhập Google (popup sẽ mở tab).
  if (msg?.type === "google_auth_url") {
    (async () => {
      const result = await callServer("/google-auth/url", { method: "GET" }, 15000);
      if (!result.ok) {
        sendResponse({ ok: false, error: result.error });
        return;
      }
      sendResponse({ ok: true, url: result.data.url || "" });
    })();
    return true;
  }

  // Trạng thái đăng nhập Google / Notion.
  if (msg?.type === "google_auth_status") {
    (async () => {
      const result = await callServer("/google-auth/status", { method: "GET" }, 15000);
      sendResponse(result.ok ? { ok: true, ...result.data } : { ok: false, error: result.error });
    })();
    return true;
  }

  if (msg?.type === "notion_status") {
    (async () => {
      const result = await callServer("/notion/status", { method: "GET" }, 15000);
      sendResponse(result.ok ? { ok: true, ...result.data } : { ok: false, error: result.error });
    })();
    return true;
  }

  // Tải file phụ đề (VTT) từ host của video (Udemy) — content script không tự fetch
  // cross-origin được nên nhờ service worker đi giúp.
  if (msg?.type === "fetch_text") {
    (async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        const res = await fetch(msg.url, { credentials: "include", signal: controller.signal });
        if (!res.ok) {
          sendResponse({ ok: false, error: `HTTP ${res.status}` });
          return;
        }
        const text = await res.text();
        sendResponse({ ok: true, text });
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message || error) });
      } finally {
        clearTimeout(timeout);
      }
    })();
    return true;
  }

  if (msg?.type === "voices") {
    (async () => {
      try {
        const res = await fetch(`${SERVER}/voices`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          sendResponse({ ok: false, error: `HTTP ${res.status}`, status: res.status });
          return;
        }
        sendResponse({ ok: true, voices: data.voices || [], default: data.default || "" });
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message || error) });
      }
    })();
    return true;
  }

  if (msg?.type !== "speak") return;
  (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const res = await fetch(`${SERVER}/speak`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: msg.text, target: "vi", voice: msg.voice || "" }),
        signal: controller.signal,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
        const retryAfter = Number(res.headers.get("Retry-After")) || 0;
        sendResponse({ ok: false, error: detail, status: res.status, retryAfter });
        return;
      }
      if (!data.audio) throw new Error("Máy chủ không trả về âm thanh.");
      sendResponse({
        ok: true,
        text: data.text,
        audio: data.audio,
        translationCache: data.translation_cache,
        audioCache: data.audio_cache,
      });
    } catch (error) {
      const message = error?.name === "AbortError"
        ? `Request timeout sau ${REQUEST_TIMEOUT_MS / 1000}s`
        : String(error.message || error);
      sendResponse({ ok: false, error: message, status: 0, retryAfter: 0 });
    } finally {
      clearTimeout(timeout);
    }
  })();
  return true;
});
