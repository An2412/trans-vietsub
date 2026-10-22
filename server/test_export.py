"""Kiểm tra nhanh phần chuyển đổi markdown và luồng tóm tắt (không gọi mạng)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import google_auth
import notion_export
from summarizer import summarize_lesson

SAMPLE = """## 🎯 Mục tiêu bài học
Học cách dựng **REST API** với FastAPI và deploy lên cloud.

## 📝 Nội dung chính
- Cài đặt FastAPI và uvicorn
- Định nghĩa route `GET /items`
- Viết test với pytest
- Deploy lên VPS

## 💡 Khái niệm quan trọng
- **Dependency injection**: tiêm phụ thuộc qua constructor
- *Middleware*: hàm chạy trước mỗi request

## ✅ Tóm tắt một câu
Bài học hướng dẫn xây REST API bằng FastAPI rồi deploy lên production.
"""


def _rich_of(block):
    if block["type"] == "divider":
        return []
    return block.get(block["type"], {}).get("rich_text", [])


def test_notion_blocks():
    blocks = notion_export.markdown_to_blocks(SAMPLE)
    kinds = [b["type"] for b in blocks]
    assert "heading_2" in kinds, kinds
    assert "bulleted_list_item" in kinds, kinds
    assert "paragraph" in kinds, kinds
    # Dấu markdown phải được bỏ khỏi text.
    for block in blocks:
        for item in _rich_of(block):
            content = item.get("text", {}).get("content", "")
            assert "**" not in content, content
            assert "`" not in content, content
    # Bold phải được gắn annotations.
    bolded = [
        item for block in blocks
        for item in _rich_of(block)
        if item.get("annotations", {}).get("bold")
    ]
    assert bolded, "không có đoạn bold nào"
    print(f"  notion blocks: {len(blocks)} block, {len(bolded)} đoạn bold")


def test_docs_requests():
    requests = google_auth._markdown_to_docs_requests("Bài học 1", SAMPLE)
    assert requests[0]["insertText"]["location"]["index"] == 1
    styles = [r for r in requests if "updateParagraphStyle" in r]
    bullets = [r for r in requests if "createParagraphBullets" in r]
    bolds = [r for r in requests if "updateTextStyle" in r]
    assert styles, "thiếu style heading"
    assert bullets, "thiếu bullet"
    assert bolds, "thiếu bold"
    # Index phải nằm trong văn bản đã chèn.
    total = len(requests[0]["insertText"]["text"])
    for request in requests[1:]:
        for key in ("updateParagraphStyle", "updateTextStyle", "createParagraphBullets"):
            if key in request:
                rng = request[key]["range"]
                assert 0 < rng["startIndex"] < rng["endIndex"] <= total, (key, rng, total)
    print(f"  docs requests: {len(requests)} request, {len(bullets)} bullet, {len(bolds)} bold")


class FakeProvider:
    def generate_text(self, prompt, system=""):
        assert "Nội dung phụ đề" in prompt or "gạch đầu dòng" in prompt
        return SAMPLE


class FakeTranslator:
    _providers = [("fake", FakeProvider())]

    def translate(self, text, target="vi"):
        return text


def test_summarize():
    cues = ["Bài 1 giới thiệu FastAPI", "Bài 2 cài đặt route", "Bài 3 viết test"] * 10
    out = summarize_lesson("Bài học FastAPI", cues, FakeTranslator())
    assert "Mục tiêu" in out
    print(f"  summary length: {len(out)} ký tự")


def test_summarize_long():
    cues = [f"Câu số {i} nội dung học thuật về lập trình." for i in range(1200)]
    out = summarize_lesson("Bài dài", cues, FakeTranslator())
    assert out
    print(f"  summary dài length: {len(out)} ký tự")


if __name__ == "__main__":
    for name, fn in [
        ("notion_blocks", test_notion_blocks),
        ("docs_requests", test_docs_requests),
        ("summarize", test_summarize),
        ("summarize_long", test_summarize_long),
    ]:
        print(f"[{name}]")
        fn()
    print("\nTất cả kiểm tra đã qua.")
