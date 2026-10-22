"""
Google OAuth2 + Google Docs export for Viet Dub.

Flow:
  1. Extension requests /google-auth/url  →  server returns an auth URL.
  2. User visits the URL in browser, approves.
  3. Google redirects to http://127.0.0.1:8765/google-auth/callback with ?code=...
  4. Server exchanges code → access_token + refresh_token, stores both in keyring.
  5. Extension polls /google-auth/status  →  {"connected": true, "email": "..."}
  6. On summarise request, server uses stored token to write to Google Docs.

Credentials (client_id / client_secret) are stored in keyring under service
"viet-dub-google" with username "oauth_client".  The user must paste them once
from the Google Cloud Console (or we ship a public installed-app client id).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import keyring  # type: ignore
except ImportError:
    keyring = None

log = logging.getLogger("uvicorn.error")

_KEYRING_SERVICE = "viet-dub-google"
_KEYRING_TOKEN_USER = "oauth_tokens"
_KEYRING_CLIENT_USER = "oauth_client"
_KEYRING_NOTES_USER = "gdocs_master"  # id "sổ tay tóm tắt" (1 doc, mỗi video 1 thẻ)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

REDIRECT_URI = "http://127.0.0.1:8765/google-auth/callback"

# ── Client ID nhúng sẵn (Client ID là định danh CÔNG KHAI, luôn hiện trong URL
#    xin quyền nên không thể và không cần giấu) ──
# Client secret TUYỆT ĐỐI không đặt ở đây: nó chỉ nằm trong kho bí mật của hệ
# điều hành hoặc file cấu hình cục bộ đã gitignore (server/oauth_client.json).
_BUILTIN_CLIENT_ID = "265588708707-4dqldarqfu3tri3o6p8m1dr8k6sf7k4q.apps.googleusercontent.com"
_BUILTIN_CLIENT_SECRET = ""

# File cấu hình cục bộ (đã gitignore) — dùng để chuyển cấu hình sang máy khác
# mà không phải nhập lại trong popup.
_LOCAL_CLIENT_FILE = Path(__file__).resolve().parent / "oauth_client.json"

_lock = threading.Lock()
_pending_state: str = ""  # CSRF token for pending auth


# ─────────────────────────── keyring helpers ────────────────────────────────

def _kr_get(username: str) -> dict | None:
    if keyring is None:
        return None
    raw = keyring.get_password(_KEYRING_SERVICE, username)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _kr_set(username: str, data: dict) -> None:
    if keyring is None:
        raise RuntimeError("keyring not available")
    keyring.set_password(_KEYRING_SERVICE, username, json.dumps(data, separators=(",", ":")))


def _kr_delete(username: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(_KEYRING_SERVICE, username)
    except Exception:
        pass


# ──────────────────────────── client id/secret ──────────────────────────────

def _read_local_client_file() -> tuple[str, str] | None:
    """Đọc server/oauth_client.json (đã gitignore). Trả None nếu không có."""
    try:
        if not _LOCAL_CLIENT_FILE.is_file():
            return None
        data = json.loads(_LOCAL_CLIENT_FILE.read_text(encoding="utf-8"))
        client_id = (data.get("client_id") or "").strip()
        if not client_id:
            return None
        return client_id, (data.get("client_secret") or "").strip()
    except (OSError, ValueError) as error:
        log.warning("google_client_file_unreadable detail=%s", str(error)[:160])
        return None


def get_client_config() -> tuple[str, str]:
    """Returns (client_id, client_secret). Raises if not configured.

    Thứ tự ưu tiên: kho bí mật của hệ điều hành → file cục bộ → Client ID nhúng sẵn.
    """
    stored = _kr_get(_KEYRING_CLIENT_USER)
    if stored and stored.get("client_id"):
        return stored["client_id"], stored.get("client_secret", "")
    local = _read_local_client_file()
    if local:
        return local
    if _BUILTIN_CLIENT_ID:
        return _BUILTIN_CLIENT_ID, _BUILTIN_CLIENT_SECRET
    raise RuntimeError("Google OAuth client chưa được cấu hình. Vào popup → tab Google để thiết lập.")


def is_client_configured() -> bool:
    """True nếu đủ thông tin để chạy luồng OAuth.

    Client ID nhúng sẵn chỉ được tính khi có kèm secret, vì client loại
    "Web application" bắt buộc phải có secret khi đổi token.
    """
    stored = _kr_get(_KEYRING_CLIENT_USER)
    if stored and stored.get("client_id"):
        return True
    if _read_local_client_file():
        return True
    return bool(_BUILTIN_CLIENT_ID and _BUILTIN_CLIENT_SECRET)


def save_client_config(client_id: str, client_secret: str) -> None:
    _kr_set(_KEYRING_CLIENT_USER, {"client_id": client_id.strip(), "client_secret": client_secret.strip()})


def clear_client_config() -> None:
    _kr_delete(_KEYRING_CLIENT_USER)


# ─────────── Sổ tay tóm tắt: 1 tài liệu, mỗi video một thẻ (tab) ───────────

MASTER_DOC_TITLE = "Viet Dub — Tóm tắt bài học"


def get_master_doc_id() -> str:
    stored = _kr_get(_KEYRING_NOTES_USER)
    return (stored or {}).get("document_id", "")


def save_master_doc_id(doc_id: str) -> None:
    try:
        _kr_set(_KEYRING_NOTES_USER, {"document_id": doc_id, "saved_at": time.time()})
    except Exception as exc:  # noqa: BLE001 — lỗi kho không được làm hỏng việc xuất Docs
        log.warning("gdocs_master_id_save_failed detail=%s", str(exc)[:160])


# ──────────────────────────── token storage ─────────────────────────────────

def get_tokens() -> dict | None:
    return _kr_get(_KEYRING_TOKEN_USER)


def save_tokens(data: dict) -> None:
    """Lưu token vào Windows Credential Manager.

    Windows giới hạn blob của một credential (~512 byte) — vượt quá sẽ bị
    `CredWrite` từ chối với lỗi 1783 "The stub received bad data". Phản hồi của
    Google chứa `id_token` (JWT ~1200 ký tự) và `scope`, nhưng cả hai đều không
    bao giờ được đọc lại trong code → không lưu, chỉ giữ những trường server dùng.
    """
    keep = {
        key: data[key]
        for key in ("access_token", "refresh_token", "expires_in", "email")
        if data.get(key)
    }
    keep["saved_at"] = time.time()
    raw = json.dumps(keep, separators=(",", ":"))
    blob_size = len(raw.encode("utf-8"))
    log.info("keyring_token_save size=%d bytes", blob_size)
    try:
        _kr_set(_KEYRING_TOKEN_USER, keep)
    except Exception as exc:
        log.error("keyring_token_save_failed size=%d error=%s", blob_size, str(exc)[:160])
        raise RuntimeError(
            f"Windows Credential Manager từ chối ghi token (blob {blob_size} byte): {exc}. "
            "Đây là lỗi kho hệ thống, không phải lỗi Google. Mở "
            "'Quản lý thông tin xác thực' → 'Thông tin xác thực người dùng' của Windows, "
            "xóa mục 'viet-dub-google' rồi bấm đăng nhập lại."
        ) from exc


def clear_tokens() -> None:
    _kr_delete(_KEYRING_TOKEN_USER)


def get_valid_access_token() -> str:
    """Returns a valid access token, refreshing automatically if needed."""
    tokens = get_tokens()
    if not tokens:
        raise RuntimeError("Chưa đăng nhập Google. Vào popup → tab Google để kết nối.")
    access_token = tokens.get("access_token", "")
    if not access_token:
        clear_tokens()
        raise RuntimeError("Chưa đăng nhập Google. Vào popup → tab Google để kết nối.")
    expires_in = tokens.get("expires_in", 3600)
    saved_at = tokens.get("saved_at", 0)
    if time.time() - saved_at < expires_in - 60:
        return access_token
    # Need refresh
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        clear_tokens()
        raise RuntimeError("Phiên Google hết hạn và không có refresh token. Đăng nhập lại.")
    client_id, client_secret = get_client_config()
    new_tokens = _refresh_access_token(refresh_token, client_id, client_secret)
    # Preserve refresh_token if not returned
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = refresh_token
    # Google không trả email trong refresh response — giữ lại email đã lưu
    # (save_tokens không lưu trường trống nên thiếu email sẽ làm popup mất tên TK).
    if not new_tokens.get("email"):
        new_tokens["email"] = tokens.get("email", "")
    save_tokens(new_tokens)
    return new_tokens["access_token"]


def _refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> dict:
    form = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    # Client loại "Desktop app" không dùng client secret — chỉ gửi khi có.
    if client_secret:
        form["client_secret"] = client_secret
    payload = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Google token refresh thất bại: HTTP {e.code}") from None


# ──────────────────────────── OAuth flow ────────────────────────────────────

def build_auth_url() -> tuple[str, str]:
    """Returns (auth_url, state). State is stored for CSRF check."""
    import secrets
    global _pending_state
    client_id, _ = get_client_config()
    state = secrets.token_urlsafe(16)
    with _lock:
        _pending_state = state
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}", state


def exchange_code(code: str, state: str) -> dict:
    """Exchange auth code for tokens. Returns token dict with user email."""
    global _pending_state
    with _lock:
        if state != _pending_state:
            raise RuntimeError("OAuth state không khớp (CSRF).")
        _pending_state = ""
    client_id, client_secret = get_client_config()
    form = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    # Client loại "Desktop app" không dùng client secret — chỉ gửi khi có.
    if client_secret:
        form["client_secret"] = client_secret
    payload = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OAuth code exchange thất bại: HTTP {e.code}") from None

    # Chặn token rỗng/rác: nếu không có access token thật thì coi như thất bại.
    if not token_data.get("access_token"):
        raise RuntimeError("Google không trả về access token.")

    # Fetch user email
    email = ""
    try:
        info_req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        with urllib.request.urlopen(info_req, timeout=10) as r:
            info = json.loads(r.read().decode())
            email = info.get("email", "")
    except Exception as exc:
        # Không chặn đăng nhập, nhưng phải thấy được lý do trong log.
        log.warning("google_userinfo_failed detail=%s", str(exc)[:160])

    token_data["email"] = email
    save_tokens(token_data)
    log.info("google_auth_connected email=%s", email)
    return token_data


def get_auth_status() -> dict[str, Any]:
    tokens = get_tokens()
    # Token rỗng/rác không được tính là đã đăng nhập.
    if not tokens or not tokens.get("access_token"):
        return {"connected": False, "email": ""}
    return {"connected": True, "email": tokens.get("email", "")}


# ──────────────────────────── Google Docs helpers ───────────────────────────

def _api_request(method: str, url: str, body: dict | None = None, access_token: str = "") -> dict:
    # `body is not None` (không dùng truthiness) để body rỗng {} vẫn gửi được —
    # Drive API yêu cầu JSON body cho PATCH di chuyển file.
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"Google API error HTTP {e.code}: {body_text}") from None


def create_or_get_lesson_doc(title: str, summary_markdown: str) -> str:
    """
    Creates a Google Doc with the lesson notes inside the "Viet Dub Notes" folder.
    Returns the doc URL. Append to an existing doc with append_to_doc() instead.
    """
    token = get_valid_access_token()

    # --- Find or create "Viet Dub Notes" folder ---
    # Bước này cần Drive API; nếu API chưa bật thì bỏ qua (tài liệu vẫn được tạo
    # ở thư mục gốc của My Drive) — lỗi Drive không được làm hỏng việc xuất Docs.
    folder_id = ""
    try:
        folder_id = _find_or_create_folder("Viet Dub Notes", token)
    except RuntimeError as error:
        log.warning("gdocs_folder_skipped detail=%s", str(error)[:160])

    # --- Create new doc in that folder ---
    doc_meta = _api_request(
        "POST",
        "https://docs.googleapis.com/v1/documents",
        {"title": title},
        token,
    )
    doc_id = doc_meta["documentId"]

    # Move doc to folder (lỗi di chuyển không được làm hỏng nội dung đã tạo).
    if folder_id:
        try:
            _api_request(
                "PATCH",
                f"https://www.googleapis.com/drive/v3/files/{doc_id}?addParents={folder_id}&removeParents=root",
                {},
                token,
            )
        except RuntimeError as error:
            log.warning("gdocs_move_to_folder_failed detail=%s", str(error)[:160])

    # Insert formatted content
    requests = _markdown_to_docs_requests(title, summary_markdown)
    if requests:
        _api_request(
            "POST",
            f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
            {"requests": requests},
            token,
        )

    return f"https://docs.google.com/document/d/{doc_id}/edit"


def append_to_doc(doc_id: str, title: str, summary_markdown: str) -> str:
    """Appends a new section to an existing doc."""
    token = get_valid_access_token()
    doc = _api_request("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}", access_token=token)
    # Find end index
    content = doc.get("body", {}).get("content", [])
    end_index = max((seg.get("endIndex", 1) for seg in content), default=1) - 1

    separator = f"\n\n{'─' * 40}\n"
    full_text = separator + f"### {title}\n\n" + summary_markdown + "\n"

    requests = [{
        "insertText": {
            "location": {"index": end_index},
            "text": full_text,
        }
    }]
    _api_request(
        "POST",
        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
        {"requests": requests},
        token,
    )
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _find_or_create_folder(name: str, token: str) -> str:
    query = urllib.parse.quote(
        f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false"
    )
    result = _api_request(
        "GET",
        f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name)",
        access_token=token,
    )
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    # Create folder
    meta = _api_request(
        "POST",
        "https://www.googleapis.com/drive/v3/files",
        {"name": name, "mimeType": "application/vnd.google-apps.folder"},
        token,
    )
    return meta["id"]


# ── Mỗi video một thẻ trong MỘT sổ tay tóm tắt ──
#
# Luồng: tìm doc chủ (keyring → tìm theo tên → tạo mới). Với mỗi video, tìm thẻ
# có tiêu đề trùng tên video: có thì ghi tiếp (mục "cập nhật"), không có thì
# tạo thẻ mới qua `addDocumentTab` và ghi nội dung vào đúng thẻ đó.

def _get_document(doc_id: str, token: str) -> dict:
    """GET documents.get với includeTabsContent để thấy mọi thẻ."""
    return _api_request(
        "GET",
        f"https://docs.googleapis.com/v1/documents/{doc_id}?includeTabsContent=true",
        access_token=token,
    )


def _find_doc_by_name(token: str, name: str) -> str:
    query = urllib.parse.quote(
        f"name='{name}' and trashed=false and mimeType='application/vnd.google-apps.document'"
    )
    try:
        result = _api_request(
            "GET",
            f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name)",
            access_token=token,
        )
    except RuntimeError as error:
        log.warning("gdocs_find_by_name_failed detail=%s", str(error)[:120])
        return ""
    files = result.get("files", [])
    return files[0]["id"] if files else ""


def _find_tab_by_title(tabs: list[dict], title: str) -> dict | None:
    wanted = title.strip()
    for tab in tabs:
        props = tab.get("tabProperties", {}) or {}
        if props.get("title", "").strip() == wanted:
            return tab
        hit = _find_tab_by_title(tab.get("childTabs", []) or [], title)
        if hit:
            return hit
    return None


def _create_master_doc(token: str, folder_id: str) -> tuple[str, dict]:
    meta = _api_request(
        "POST",
        "https://docs.googleapis.com/v1/documents",
        {"title": MASTER_DOC_TITLE},
        token,
    )
    doc_id = meta["documentId"]
    if folder_id:
        try:
            _api_request(
                "PATCH",
                f"https://www.googleapis.com/drive/v3/files/{doc_id}?addParents={folder_id}&removeParents=root",
                {},
                token,
            )
        except RuntimeError as error:
            log.warning("gdocs_move_to_folder_failed detail=%s", str(error)[:160])
    save_master_doc_id(doc_id)
    doc = _get_document(doc_id, token)
    log.info("gdocs_master_created")
    return doc_id, doc


def export_summary_to_lab_doc(title: str, summary_markdown: str) -> dict:
    """Ghi bản tóm tắt vào sổ tay: một video = một thẻ mang tên video.

    Trả về {"url", "document_id"} của sổ tay.
    """
    token = get_valid_access_token()
    if not (title or "").strip():
        raise RuntimeError("Thiếu tiêu đề video để đặt tên thẻ.")

    folder_id = ""
    try:
        folder_id = _find_or_create_folder("Viet Dub Notes", token)
    except RuntimeError as error:
        log.warning("gdocs_folder_skipped detail=%s", str(error)[:160])

    master_id = get_master_doc_id()
    doc: dict | None = None
    if master_id:
        try:
            doc = _get_document(master_id, token)
        except RuntimeError as error:
            # id đã lưu bị xoá/mất quyền — bỏ, tìm lại theo tên.
            log.warning("gdocs_master_unusable detail=%s", str(error)[:120])
            master_id = ""
            doc = None
    if doc is None:
        master_id = _find_doc_by_name(token, MASTER_DOC_TITLE)
        if master_id:
            try:
                doc = _get_document(master_id, token)
                save_master_doc_id(master_id)
            except RuntimeError:
                doc = None
                master_id = ""
    if doc is None:
        master_id, doc = _create_master_doc(token, folder_id)

    tabs = doc.get("tabs", []) or []
    tab = _find_tab_by_title(tabs, title)
    if tab:
        _append_update_to_tab(token, master_id, tab, summary_markdown)
    elif _is_blank_root_only(tabs):
        # Lần đầu: giữ nguyên thẻ đầu tiên của doc và đổi tên thành tên video
        # (thẻ 1 của doc trở thành "thẻ của video đầu tiên").
        _claim_blank_root_tab(token, master_id, tabs, title, summary_markdown)
    else:
        _write_summary_to_new_tab(token, master_id, title, summary_markdown)

    return {
        "url": f"https://docs.google.com/document/d/{master_id}/edit",
        "document_id": master_id,
    }


def _is_blank_root_only(tabs: list[dict]) -> bool:
    if len(tabs) != 1 or tabs[0].get("childTabs"):
        return False
    body = ((tabs[0].get("documentTab") or {}).get("body") or {}).get("content", []) or []
    max_end = max((seg.get("endIndex", 0) for seg in body), default=0)
    # 2 = đúng một đoạn trống (mỗi body luôn kết thúc bằng \n).
    return max_end <= 2


def _claim_blank_root_tab(
    token: str, doc_id: str, tabs: list[dict], title: str, summary_markdown: str
) -> None:
    root_id = ((tabs[0].get("tabProperties") or {}).get("tabId") or "") if tabs else ""
    try:
        _api_request(
            "POST",
            f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
            {"requests": [{
                "updateDocumentTabProperties": {
                    "tabProperties": {"title": title.strip(), **({"tabId": root_id} if root_id else {})},
                    "fields": "title",
                }
            }]},
            token,
        )
    except RuntimeError as error:
        # Đổi tên thẻ thất bại thì vẫn ghi nội dung (nội dung mới tạo ra là thẻ mới).
        log.warning("gdocs_root_retitle_failed detail=%s", str(error)[:160])
    _write_tab_content(token, doc_id, summary_markdown, root_id)


def _write_summary_to_new_tab(token: str, doc_id: str, title: str, summary_markdown: str) -> None:
    reply = _api_request(
        "POST",
        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
        {"requests": [{"addDocumentTab": {"tabProperties": {"title": title.strip()}}}]},
        token,
    )
    tab_id = ""
    replies = reply.get("replies", []) or []
    if replies:
        props = ((replies[0].get("addDocumentTab") or {}).get("tabProperties")) or {}
        tab_id = props.get("tabId", "")
    if not tab_id:
        # Phản hồi không kèm tab ID thật (tuỳ phiên bản API) — đọc lại doc để tìm.
        doc = _get_document(doc_id, token)
        tab = _find_tab_by_title(doc.get("tabs", []) or [], title)
        if tab:
            tab_id = (tab.get("tabProperties") or {}).get("tabId", "")
        log.info("gdocs_new_tab_id_from_get found=%s", bool(tab_id))
    _write_tab_content(token, doc_id, summary_markdown, tab_id)
    log.info("gdocs_tab_added")


def _write_tab_content(token: str, doc_id: str, markdown: str, tab_id: str) -> None:
    requests = _markdown_to_docs_requests("", markdown, tab_id)
    if not requests:
        return
    _api_request(
        "POST",
        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
        {"requests": requests},
        token,
    )


def _plain_markdown(markdown: str) -> str:
    """Markdown → text thẳng (bỏ #, bullet, **, *) để ghi tiếp phần 'cập nhật'."""
    lines: list[str] = []
    for raw_line in (markdown or "").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^#{1,3}\s+", "", stripped)
        stripped = re.sub(r"^[*-•]\s+", "", stripped)
        plain, _bold, _italic = _inline(stripped)
        lines.append(plain)
    return "\n".join(lines)


def _append_update_to_tab(token: str, doc_id: str, tab: dict, summary_markdown: str) -> None:
    tab_id = (tab.get("tabProperties") or {}).get("tabId", "")
    stamp = time.strftime("%d/%m/%Y %H:%M")
    text = f"\n\n──── Cập nhật {stamp} ────\n" + _plain_markdown(summary_markdown) + "\n"
    _api_request(
        "POST",
        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
        {"requests": [{
            "insertText": {
                "endOfSegmentLocation": {"tabId": tab_id} if tab_id else {},
                "text": text,
            }
        }]},
        token,
    )


def _inline(text: str) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """Bỏ dấu markdown inline, trả về (text thuần, các đoạn bold, các đoạn italic)."""
    plain: list[str] = []
    bold: list[tuple[int, int]] = []
    italic: list[tuple[int, int]] = []
    cursor = 0
    pattern = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`", re.S)
    for match in pattern.finditer(text):
        if match.start() > cursor:
            plain.append(text[cursor:match.start()])
        if match.group(1) is not None:
            content = match.group(1)
            start = sum(len(p) for p in plain)
            plain.append(content)
            bold.append((start, start + len(content)))
        elif match.group(2) is not None:
            content = match.group(2)
            start = sum(len(p) for p in plain)
            plain.append(content)
            italic.append((start, start + len(content)))
        else:
            plain.append(match.group(3) or "")
        cursor = match.end()
    if cursor < len(text):
        plain.append(text[cursor:])
    return "".join(plain), bold, italic


def _markdown_to_docs_requests(title: str, markdown: str, tab_id: str = "") -> list[dict]:
    """Chuyển markdown của bản tóm tắt thành các request batchUpdate của Google Docs.

    - `title` rỗng: bỏ qua dòng tiêu đề (tiêu đề đã nằm trên tên thẻ).
    - `tab_id` khác rỗng: mọi location/range đều gắn tabId để ghi vào đúng thẻ.
    """
    paragraphs: list[dict] = []
    if title:
        paragraphs.append({"text": title, "kind": "title", "bold": [], "italic": []})

    for raw_line in (markdown or "").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        kind = "para"
        body = stripped
        if stripped in {"---", "***", "___"}:
            paragraphs.append({"text": "─" * 24, "kind": "para", "bold": [], "italic": []})
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            kind = f"h{len(heading.group(1))}"
            body = heading.group(2)
        elif re.match(r"^[-*•]\s+", stripped):
            kind = "bullet"
            body = re.sub(r"^[-*•]\s+", "", stripped)
        elif re.match(r"^\d+[.)]\s+", stripped):
            kind = "number"
            body = re.sub(r"^\d+[.)]\s+", "", stripped)
        text, bold, italic = _inline(body)
        paragraphs.append({"text": text, "kind": kind, "bold": bold, "italic": italic})

    full_text = "".join(f"{p['text']}\n" for p in paragraphs)
    location_tab = {"tabId": tab_id} if tab_id else {}
    requests: list[dict] = [{
        "insertText": {
            "location": {"index": 1, **location_tab},
            "text": full_text,
        }
    }]

    index = 1
    for paragraph in paragraphs:
        text = paragraph["text"]
        end = index + len(text)
        kind = paragraph["kind"]
        if kind == "title":
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": index, "endIndex": end, **location_tab},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            })
        elif kind in ("h2", "h3"):
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": index, "endIndex": end, **location_tab},
                    "paragraphStyle": {"namedStyleType": kind.upper()},
                    "fields": "namedStyleType",
                }
            })
        elif kind == "bullet":
            requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": index, "endIndex": end, **location_tab},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })
        elif kind == "number":
            requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": index, "endIndex": end, **location_tab},
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                }
            })
        for start, stop in paragraph["bold"]:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": index + start, "endIndex": index + stop, **location_tab},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        for start, stop in paragraph["italic"]:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": index + start, "endIndex": index + stop, **location_tab},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            })
        index = end + 1  # +1 cho ký tự xuống dòng

    return requests
