"""
Notion export for Viet Dub.

Flow (Notion "Internal Integration"):
  1. User creates an integration at https://www.notion.so/my-integrations
     and copies the secret (secret_xxx...).
  2. User opens the parent page in Notion, shares it with the integration
     (… → Connections → <your integration>).
  3. Both values are pasted once in the extension popup and stored in the
     OS secret store (keyring) by the server, never in Chrome/SQLite/logs.

  4. On summarise request the server creates a child page under the parent
     page with the lesson notes, or appends a new section to an existing
     child page.

API docs: https://developers.notion.com/reference/intro
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

try:
    import keyring  # type: ignore
except ImportError:
    keyring = None

log = logging.getLogger("uvicorn.error")

_KEYRING_SERVICE = "viet-dub-notion"
_KEYRING_TOKEN_USER = "notion_token"
_KEYRING_PARENT_USER = "notion_parent_id"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion rejects rich_text content longer than 2000 chars per text object.
_MAX_TEXT_CHUNK = 1900
# Notion accepts at most 100 blocks per create/append request.
_MAX_BLOCKS_PER_REQUEST = 90


# ─────────────────────────── keyring helpers ────────────────────────────────

def _kr_get(username: str) -> str:
    if keyring is None:
        return ""
    try:
        return keyring.get_password(_KEYRING_SERVICE, username) or ""
    except Exception:
        return ""


def _kr_set(username: str, value: str) -> None:
    if keyring is None:
        raise RuntimeError("Kho bí mật của hệ điều hành chưa sẵn sàng.")
    keyring.set_password(_KEYRING_SERVICE, username, value)


def _kr_delete(username: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(_KEYRING_SERVICE, username)
    except Exception:
        pass


# ──────────────────────────── configuration ─────────────────────────────────

def get_token() -> str:
    return _kr_get(_KEYRING_TOKEN_USER).strip()


def get_parent_page_id() -> str:
    return _kr_get(_KEYRING_PARENT_USER).strip()


def save_config(token: str, parent_page_id: str) -> None:
    token = token.strip()
    parent = parent_page_id.strip()
    if not token:
        raise RuntimeError("Thiếu Notion integration token.")
    if not parent:
        raise RuntimeError("Thiếu Notion parent page ID.")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", parent):
        raise RuntimeError("Notion page ID phải là chuỗi 32 ký tự hex.")
    _kr_set(_KEYRING_TOKEN_USER, token)
    _kr_set(_KEYRING_PARENT_USER, parent)


def clear_config() -> None:
    _kr_delete(_KEYRING_TOKEN_USER)
    _kr_delete(_KEYRING_PARENT_USER)


def get_status() -> dict[str, Any]:
    token = get_token()
    parent = get_parent_page_id()
    return {
        "configured": bool(token and parent),
        "has_token": bool(token),
        "has_parent_page": bool(parent),
        "parent_page_id": parent,
    }


# ───────────────────────────── HTTP plumbing ────────────────────────────────

def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{NOTION_API}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode(errors="replace")[:300]
        except Exception:
            pass
        try:
            parsed = json.loads(detail)
            detail = parsed.get("message") or detail
        except Exception:
            pass
        raise RuntimeError(f"Notion API lỗi HTTP {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Không kết nối được Notion API: {error.reason}") from None


def test_connection(token: str, parent_page_id: str) -> dict:
    """Validates the token and that the parent page is shared with the integration."""
    token = token.strip()
    if not token:
        raise RuntimeError("Thiếu Notion integration token.")
    me = _request("GET", "/users/me", token)
    bot_name = me.get("name") or me.get("bot", {}).get("owner", {}).get("user", {}).get("name") or "integration"
    parent = parent_page_id.strip()
    if parent:
        _request("GET", f"/pages/{parent}", token)
    return {"bot_name": bot_name, "parent_ok": bool(parent)}


# ───────────────────────── markdown → Notion blocks ─────────────────────────

def _rich_text(text: str) -> list[dict]:
    """Splits text into Notion rich_text chunks and keeps **bold** / *italic* marks."""
    segments: list[tuple[str, dict]] = []
    pattern = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*|`.+?`)", re.S)
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            segments.append((text[cursor:match.start()], {}))
        token = match.group(0)
        if token.startswith("**") and token.endswith("**"):
            segments.append((token[2:-2], {"bold": True}))
        elif token.startswith("`") and token.endswith("`"):
            segments.append((token[1:-1], {"code": True}))
        elif token.startswith("*") and token.endswith("*"):
            segments.append((token[1:-1], {"italic": True}))
        cursor = match.end()
    if cursor < len(text):
        segments.append((text[cursor:], {}))

    rich: list[dict] = []
    for content, annotations in segments:
        if not content:
            continue
        while content:
            chunk, content = content[:_MAX_TEXT_CHUNK], content[_MAX_TEXT_CHUNK:]
            item: dict[str, Any] = {"type": "text", "text": {"content": chunk}}
            if annotations:
                item["annotations"] = dict(annotations)
            rich.append(item)
    return rich


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def markdown_to_blocks(markdown: str) -> list[dict]:
    """Converts the summariser markdown into Notion blocks."""
    blocks: list[dict] = []
    for raw_line in (markdown or "").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"---", "***", "___"}:
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            blocks.append({
                "object": "block",
                "type": f"heading_{level}",
                f"heading_{level}": {"rich_text": _rich_text(heading.group(2))},
            })
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        if bullet:
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(bullet.group(1))},
            })
            continue
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(numbered.group(1))},
            })
            continue
        blocks.append(_paragraph(stripped))
    return blocks


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# ──────────────────────────────── export ────────────────────────────────────

def create_child_page(title: str, markdown: str, parent_page_id: str = "") -> dict:
    """Creates a new page under the configured parent page and returns its info."""
    token = get_token()
    if not token:
        raise RuntimeError("Chưa cấu hình Notion. Vào popup → tab Notion để thiết lập.")
    parent = (parent_page_id or get_parent_page_id()).strip()
    if not parent:
        raise RuntimeError("Chưa cấu hình Notion parent page ID.")

    blocks = markdown_to_blocks(markdown)
    payload: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": parent},
        "properties": {
            "title": {"type": "title", "title": _rich_text(title[:200] or "Bài học")},
        },
    }
    if blocks:
        payload["children"] = blocks[:_MAX_BLOCKS_PER_REQUEST]

    page = _request("POST", "/pages", token, payload)

    # Remaining blocks (rare, only for very long summaries) are appended.
    remaining = blocks[_MAX_BLOCKS_PER_REQUEST:]
    for group in _chunks(remaining, _MAX_BLOCKS_PER_REQUEST):
        if not group:
            continue
        _request("PATCH", f"/blocks/{page['id']}/children", token, {"children": group})

    return {
        "page_id": page.get("id", ""),
        "page_url": page.get("url", ""),
    }


def append_to_page(page_id: str, title: str, markdown: str) -> dict:
    """Appends a new section to an existing Notion page."""
    token = get_token()
    if not token:
        raise RuntimeError("Chưa cấu hình Notion. Vào popup → tab Notion để thiết lập.")
    if not page_id.strip():
        raise RuntimeError("Thiếu Notion page ID.")

    blocks = markdown_to_blocks(f"## {title}\n\n{markdown}")
    for group in _chunks(blocks, _MAX_BLOCKS_PER_REQUEST):
        if not group:
            continue
        _request("PATCH", f"/blocks/{page_id.strip()}/children", token, {"children": group})

    page = _request("GET", f"/pages/{page_id.strip()}", token)
    return {
        "page_id": page.get("id", page_id.strip()),
        "page_url": page.get("url", ""),
    }
