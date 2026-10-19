const SERVER = "http://127.0.0.1:8765";
const REQUEST_TIMEOUT_MS = 90000;

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
  if (msg?.type !== "speak") return;
  (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const res = await fetch(`${SERVER}/speak`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: msg.text, target: "vi" }),
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
