const SERVER = "http://127.0.0.1:8765";
const DEFAULTS = {
  gemini: "gemini-3.8-flash",
  openai: "gpt-6-luna",
  anthropic: "claude-haiku-5-5",
  ollama: "gpt-oss:120b",
};

const dot = document.getElementById("dot");
const statusEl = document.getElementById("status");
const toggle = document.getElementById("toggle");
const recheck = document.getElementById("recheck");
const provider = document.getElementById("provider");
const model = document.getElementById("model");
const apiKey = document.getElementById("apiKey");
const keyStatus = document.getElementById("keyStatus");
const apiStatus = document.getElementById("apiStatus");
const testApi = document.getElementById("testApi");
const saveConfig = document.getElementById("saveConfig");
const clearConfig = document.getElementById("clearConfig");
const exportTarget = document.getElementById("exportTarget");
const appendExisting = document.getElementById("appendExisting");
const googleClientId = document.getElementById("googleClientId");
const googleClientSecret = document.getElementById("googleClientSecret");
const saveGoogle = document.getElementById("saveGoogle");
const googleLogin = document.getElementById("googleLogin");
const googleLogout = document.getElementById("googleLogout");
const googleStatus = document.getElementById("googleStatus");
const notionToken = document.getElementById("notionToken");
const notionPageId = document.getElementById("notionPageId");
const saveNotion = document.getElementById("saveNotion");
const notionLogout = document.getElementById("notionLogout");
const notionStatus = document.getElementById("notionStatus");

function setServerStatus(kind, text) {
  dot.className = "dot " + kind;
  statusEl.textContent = text;
}

function setMessage(element, kind, text) {
  element.className = `message ${kind || ""}`;
  element.textContent = text || "";
}

async function requestJson(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(`${SERVER}${path}`, {
      ...options,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    return { response, data };
  } finally {
    clearTimeout(timer);
  }
}

function errorText(error) {
  if (error?.name === "AbortError") return "Server không phản hồi trong 12 giây.";
  return "Không kết nối được server cục bộ. Hãy khởi động Python server rồi thử lại.";
}

function currentFormConfig() {
  return {
    provider: provider.value,
    model: model.value.trim() || DEFAULTS[provider.value],
    api_key: apiKey.value,
  };
}

async function saveLocalMetadata(providerName, modelName) {
  // API keys are intentionally never written to Chrome storage.
  await chrome.storage.local.set({ provider: providerName, model: modelName });
}

async function checkServer() {
  recheck.disabled = true;
  setServerStatus("", "Đang kiểm tra...");
  try {
    const { response, data } = await requestJson("/health");
    if (!response.ok || data.status !== "ok") {
      setServerStatus("err", "Server trả lời bất thường");
      return;
    }
    setServerStatus("ok", "Server đang chạy");
    const configResult = await requestJson("/provider-config");
    if (!configResult.response.ok) {
      setMessage(keyStatus, "err", configResult.data.detail || "Không đọc được trạng thái cấu hình provider.");
      return;
    }
    const config = configResult.data;
    if (config.provider) {
      provider.value = config.provider;
      model.value = config.model || DEFAULTS[config.provider] || "";
      await saveLocalMetadata(provider.value, model.value);
    }
    if (!config.secure_store_ready) {
      setMessage(keyStatus, "err", config.store_message || "Kho bí mật của hệ điều hành chưa sẵn sàng.");
    } else if (config.has_api_key) {
      setMessage(keyStatus, "ok", `Đã lưu khóa cho ${provider.options[provider.selectedIndex].text}; key không được đọc lại vào popup.`);
    } else {
      setMessage(keyStatus, "warn", "Chưa lưu API key. Nhập key, chọn model rồi bấm Lưu cấu hình.");
    }
    setMessage(apiStatus, "", "");
  } catch (error) {
    setServerStatus("err", errorText(error));
    setMessage(keyStatus, "err", "Không kiểm tra được trạng thái khóa khi server chưa kết nối.");
  } finally {
    recheck.disabled = false;
  }
}

async function testProvider() {
  const config = currentFormConfig();
  testApi.disabled = true;
  saveConfig.disabled = true;
  setMessage(apiStatus, "warn", "Đang gửi một câu mẫu tới provider; thao tác này có thể dùng quota/chi phí API...");
  try {
    const { response, data } = await requestJson("/provider-config/test", {
      method: "POST",
      body: JSON.stringify(config),
    });
    if (!response.ok) {
      setMessage(apiStatus, "err", data.detail || `Server trả HTTP ${response.status}.`);
      return;
    }
    setMessage(apiStatus, data.ok ? "ok" : "err", data.message || (data.ok ? "Kết nối API thành công." : "Kiểm tra API thất bại."));
  } catch (error) {
    setMessage(apiStatus, "err", errorText(error));
  } finally {
    testApi.disabled = false;
    saveConfig.disabled = false;
  }
}

async function saveProvider() {
  const config = currentFormConfig();
  if (!config.model) {
    setMessage(apiStatus, "err", "Nhập Model ID trước khi lưu.");
    return;
  }
  saveConfig.disabled = true;
  testApi.disabled = true;
  setMessage(apiStatus, "warn", "Đang lưu metadata và khóa vào kho bí mật của hệ điều hành...");
  try {
    const { response, data } = await requestJson("/provider-config", {
      method: "POST",
      body: JSON.stringify(config),
    });
    if (!response.ok || !data.ok) {
      setMessage(apiStatus, "err", data.detail || "Không lưu được cấu hình.");
      return;
    }
    await saveLocalMetadata(data.provider, data.model);
    apiKey.value = "";
    setMessage(keyStatus, "ok", `Đã lưu khóa cho ${provider.options[provider.selectedIndex].text}; không thể xem lại từ extension.`);
    setMessage(apiStatus, "ok", data.message || "Đã lưu cấu hình.");
    setServerStatus("ok", "Server đang chạy");
  } catch (error) {
    setMessage(apiStatus, "err", errorText(error));
  } finally {
    saveConfig.disabled = false;
    testApi.disabled = false;
  }
}

async function removeProvider() {
  if (!window.confirm("Xóa cấu hình provider và API key khỏi kho bí mật của hệ điều hành?")) return;
  clearConfig.disabled = true;
  setMessage(apiStatus, "warn", "Đang xóa cấu hình...");
  try {
    const { response, data } = await requestJson("/provider-config/clear", { method: "POST", body: "{}" });
    if (!response.ok || !data.ok) {
      setMessage(apiStatus, "err", data.detail || "Không xóa được cấu hình.");
      return;
    }
    apiKey.value = "";
    provider.value = "gemini";
    model.value = DEFAULTS.gemini;
    await chrome.storage.local.remove(["provider", "model"]);
    setMessage(keyStatus, "warn", "Đã xóa cấu hình API và khóa khỏi kho bí mật.");
    setMessage(apiStatus, "ok", data.message);
  } catch (error) {
    setMessage(apiStatus, "err", errorText(error));
  } finally {
    clearConfig.disabled = false;
  }
}

async function init() {
  const {
    enabled = false,
    provider: savedProvider,
    model: savedModel,
    exportTarget: savedTarget = "gdocs",
    appendExisting: savedAppend = true,
  } = await chrome.storage.local.get([
    "enabled", "provider", "model", "exportTarget", "appendExisting",
  ]);
  toggle.checked = enabled;
  provider.value = savedProvider && DEFAULTS[savedProvider] ? savedProvider : "gemini";
  model.value = savedModel || DEFAULTS[provider.value];
  exportTarget.value = ["gdocs", "notion", "none"].includes(savedTarget) ? savedTarget : "gdocs";
  appendExisting.checked = savedAppend !== false;
  toggle.addEventListener("change", () => chrome.storage.local.set({ enabled: toggle.checked }));
  recheck.addEventListener("click", checkServer);
  testApi.addEventListener("click", testProvider);
  saveConfig.addEventListener("click", saveProvider);
  clearConfig.addEventListener("click", removeProvider);
  provider.addEventListener("change", () => {
    model.value = DEFAULTS[provider.value] || "";
    setMessage(keyStatus, "warn", "Provider đã đổi trong form nhưng server vẫn dùng cấu hình đã lưu. Có thể thử key đã lưu cho provider này bằng cách để trống ô key, rồi bấm Kiểm tra API; bấm Lưu cấu hình để áp dụng.");
    setMessage(apiStatus, "", "");
  });
  model.addEventListener("input", () => {
    setMessage(keyStatus, "warn", "Model đã đổi trong form nhưng server chỉ dùng model mới sau khi bạn bấm Lưu cấu hình.");
  });
  exportTarget.addEventListener("change", () => {
    chrome.storage.local.set({ exportTarget: exportTarget.value }).catch(() => {});
  });
  appendExisting.addEventListener("change", () => {
    chrome.storage.local.set({ appendExisting: appendExisting.checked }).catch(() => {});
  });
  saveGoogle.addEventListener("click", saveGoogleClient);
  googleLogin.addEventListener("click", loginGoogle);
  googleLogout.addEventListener("click", logoutGoogle);
  saveNotion.addEventListener("click", saveNotionConfig);
  notionLogout.addEventListener("click", logoutNotion);
  bootstrapStatuses();
}

// Đọc trạng thái Google/Notion một lần khi mở popup, nhưng chỉ khi server đã chạy
// để không hiện lỗi gây nhầm lẫn (giữ đúng nguyên tắc: mở popup không tự kết nối).
async function bootstrapStatuses() {
  try {
    const { response } = await requestJson("/health");
    if (!response.ok) return;
  } catch (_) {
    return;
  }
  refreshGoogleStatus();
  refreshNotionStatus();
}

// ─────────────────────────── Google Docs (OAuth2) ───────────────────────────

async function saveGoogleClient() {
  const clientId = googleClientId.value.trim();
  const clientSecret = googleClientSecret.value.trim();
  if (!clientId) {
    setMessage(googleStatus, "err", "Nhập OAuth Client ID trước.");
    return;
  }
  saveGoogle.disabled = true;
  setMessage(googleStatus, "warn", "Đang lưu client vào kho bí mật...");
  try {
    const { response, data } = await requestJson("/google-auth/client-config", {
      method: "POST",
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    });
    if (!response.ok || !data.ok) {
      setMessage(googleStatus, "err", data.detail || data.message || "Không lưu được client.");
      return;
    }
    googleClientSecret.value = "";
    setMessage(googleStatus, "ok", data.message || "Đã lưu OAuth client.");
  } catch (error) {
    setMessage(googleStatus, "err", errorText(error));
  } finally {
    saveGoogle.disabled = false;
  }
}

async function loginGoogle() {
  googleLogin.disabled = true;
  setMessage(googleStatus, "warn", "Đang mở trang đăng nhập Google...");
  try {
    // Lưu client trước nếu người dùng vừa dán vào form.
    if (googleClientId.value.trim()) await saveGoogleClient();
    const { response, data } = await requestJson("/google-auth/url", { method: "GET" });
    if (!response.ok || !data.url) {
      setMessage(googleStatus, "err", data.detail || "Chưa cấu hình OAuth client.");
      return;
    }
    await chrome.tabs.create({ url: data.url, active: true });
    setMessage(
      googleStatus,
      "warn",
      "Đang chờ bạn duyệt quyền trong tab vừa mở… Popup sẽ tự cập nhật khi đăng nhập xong."
    );
    pollGoogleStatus();
  } catch (error) {
    setMessage(googleStatus, "err", errorText(error));
  } finally {
    googleLogin.disabled = false;
  }
}

let googlePollTimer = null;
function pollGoogleStatus() {
  if (googlePollTimer) clearInterval(googlePollTimer);
  googlePollTimer = setInterval(async () => {
    const status = await refreshGoogleStatus();
    if (status?.connected) {
      clearInterval(googlePollTimer);
      googlePollTimer = null;
    }
  }, 2000);
}

async function refreshGoogleStatus() {
  try {
    const { response, data } = await requestJson("/google-auth/status", { method: "GET" });
    if (!response.ok) return null;
    if (data.connected) {
      setMessage(googleStatus, "ok", `Đã đăng nhập: ${data.email || "tài khoản Google"}.`);
    } else if (data.client_configured) {
      setMessage(
        googleStatus,
        "warn",
        "Chưa đăng nhập. Bấm “Đăng nhập Google”, chọn tài khoản và duyệt quyền Docs/Drive."
      );
    } else {
      setMessage(
        googleStatus,
        "warn",
        "Chưa có OAuth client. Mở “Cài đặt lần đầu” bên dưới để dán Client ID + Secret."
      );
    }
    return data;
  } catch (_) {
    setMessage(googleStatus, "err", "Không đọc được trạng thái Google (server chưa chạy?).");
    return null;
  }
}

async function logoutGoogle() {
  if (!window.confirm("Ngắt kết nối Google? Token sẽ bị xoá khỏi kho bí mật.")) return;
  googleLogout.disabled = true;
  try {
    const { response, data } = await requestJson("/google-auth/disconnect", { method: "POST", body: "{}" });
    if (!response.ok) {
      setMessage(googleStatus, "err", data.detail || "Không ngắt được kết nối.");
      return;
    }
    setMessage(googleStatus, "warn", data.message || "Đã ngắt kết nối Google.");
  } catch (error) {
    setMessage(googleStatus, "err", errorText(error));
  } finally {
    googleLogout.disabled = false;
  }
}

// ──────────────────────────────── Notion ──────────────────────────────────

async function saveNotionConfig() {
  const token = notionToken.value.trim();
  const parentPageId = notionPageId.value.trim();
  if (!token || !parentPageId) {
    setMessage(notionStatus, "err", "Nhập cả integration token và parent page ID.");
    return;
  }
  saveNotion.disabled = true;
  setMessage(notionStatus, "warn", "Đang kiểm tra kết nối Notion...");
  try {
    const { response, data } = await requestJson("/notion/test", {
      method: "POST",
      body: JSON.stringify({ token, parent_page_id: parentPageId }),
    });
    if (!response.ok || !data.ok) {
      setMessage(notionStatus, "err", data.detail || data.message || "Không kết nối được Notion.");
      return;
    }
    const save = await requestJson("/notion/config", {
      method: "POST",
      body: JSON.stringify({ token, parent_page_id: parentPageId }),
    });
    if (!save.response.ok || !save.data.ok) {
      setMessage(notionStatus, "err", save.data.detail || "Không lưu được cấu hình Notion.");
      return;
    }
    notionToken.value = "";
    setMessage(notionStatus, "ok", data.message || "Đã lưu cấu hình Notion.");
  } catch (error) {
    setMessage(notionStatus, "err", errorText(error));
  } finally {
    saveNotion.disabled = false;
  }
}

async function refreshNotionStatus() {
  try {
    const { response, data } = await requestJson("/notion/status", { method: "GET" });
    if (!response.ok) return null;
    if (data.configured) {
      setMessage(notionStatus, "ok", `Đã cấu hình Notion (page cha ${data.parent_page_id || ""}).`);
    } else if (data.has_token && !data.has_parent_page) {
      setMessage(notionStatus, "warn", "Đã có token nhưng thiếu parent page ID.");
    } else {
      setMessage(notionStatus, "warn", "Chưa cấu hình Notion.");
    }
    return data;
  } catch (_) {
    setMessage(notionStatus, "err", "Không đọc được trạng thái Notion (server chưa chạy?).");
    return null;
  }
}

async function logoutNotion() {
  if (!window.confirm("Xoá Notion token và parent page ID?")) return;
  notionLogout.disabled = true;
  try {
    const { response, data } = await requestJson("/notion/disconnect", { method: "POST", body: "{}" });
    if (!response.ok) {
      setMessage(notionStatus, "err", data.detail || "Không xoá được cấu hình.");
      return;
    }
    notionPageId.value = "";
    setMessage(notionStatus, "warn", data.message || "Đã xoá cấu hình Notion.");
  } catch (error) {
    setMessage(notionStatus, "err", errorText(error));
  } finally {
    notionLogout.disabled = false;
  }
}

init();
