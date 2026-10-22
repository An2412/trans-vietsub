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
from typing import Any

try:
    import keyring  # type: ignore
except ImportError:
    keyring = None

log = logging.getLogger("uvicorn.error")

_KEYRING_SERVICE = "viet-dub-google"
_KEYRING_TOKEN_USER = "oauth_tokens"
_KEYRING_CLIENT_USER = "oauth_client"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

REDIRECT_URI = "http://127.0.0.1:8765/google-auth/callback"

# ── Public installed-app client id shipped with extension (no secret needed for installed apps) ──
# Users can override with their own in /google-auth/client-config
_BUILTIN_CLIENT_ID = ""   # left empty; user configures from popup
_BUILTIN_CLIENT_SECRET = ""

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
    keyring.set_password(_KEYRING_SERVICE, username, json.dumps(data))


def _kr_delete(username: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(_KEYRING_SERVICE, username)
    except Exception:
        pass


# ──────────────────────────── client id/secret ──────────────────────────────

def get_client_config() -> tuple[str, str]:
    """Returns (client_id, client_secret). Raises if not configured."""
    stored = _kr_get(_KEYRING_CLIENT_USER)
    if stored:
        return stored.get("client_id", ""), stored.get("client_secret", "")
    if _BUILTIN_CLIENT_ID:
        return _BUILTIN_CLIENT_ID, _BUILTIN_CLIENT_SECRET
    raise RuntimeError("Google OAuth client chưa được cấu hình. Vào popup → tab Google để thiết lập.")


def save_client_config(client_id: str, client_secret: str) -> None:
    _kr_set(_KEYRING_CLIENT_USER, {"client_id": client_id.strip(), "client_secret": client_secret.strip()})


def clear_client_config() -> None:
    _kr_delete(_KEYRING_CLIENT_USER)


# ──────────────────────────── token storage ─────────────────────────────────

def get_tokens() -> dict | None:
    return _kr_get(_KEYRING_TOKEN_USER)


def save_tokens(data: dict) -> None:
    data["saved_at"] = time.time()
    _kr_set(_KEYRING_TOKEN_USER, data)


def clear_tokens() -> None:
    _kr_delete(_KEYRING_TOKEN_USER)


def get_valid_access_token() -> str:
    """Returns a valid access token, refreshing automatically if needed."""
    tokens = get_tokens()
    if not tokens:
        raise RuntimeError("Chưa đăng nhập Google. Vào popup → tab Google để kết nối.")
    access_token = tokens.get("access_token", "")
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
    save_tokens(new_tokens)
    return new_tokens["access_token"]


def _refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> dict:
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
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
    payload = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
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
    except Exception:
        pass

    token_data["email"] = email
    save_tokens(token_data)
    log.info("google_auth_connected email=%s", email)
    return token_data


def get_auth_status() -> dict[str, Any]:
    tokens = get_tokens()
    if not tokens:
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
    folder_id = _find_or_create_folder("Viet Dub Notes", token)

    # --- Create new doc in that folder ---
    doc_meta = _api_request(
        "POST",
        "https://docs.googleapis.com/v1/documents",
        {"title": title},
        token,
    )
    doc_id = doc_meta["documentId"]

    # Move doc to folder (lỗi di chuyển không được làm hỏng nội dung đã tạo).
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


def _markdown_to_docs_requests(title: str, markdown: str) -> list[dict]:
    """Chuyển markdown của bản tóm tắt thành các request batchUpdate của Google Docs."""
    paragraphs: list[dict] = []
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
    requests: list[dict] = [{
        "insertText": {
            "location": {"index": 1},
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
                    "range": {"startIndex": index, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            })
        elif kind in ("h2", "h3"):
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": index, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": kind.upper()},
                    "fields": "namedStyleType",
                }
            })
        elif kind == "bullet":
            requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": index, "endIndex": end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })
        elif kind == "number":
            requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": index, "endIndex": end},
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                }
            })
        for start, stop in paragraph["bold"]:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": index + start, "endIndex": index + stop},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        for start, stop in paragraph["italic"]:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": index + start, "endIndex": index + stop},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            })
        index = end + 1  # +1 cho ký tự xuống dòng

    return requests
