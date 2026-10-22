"""
Lesson summarizer: takes a list of Vietnamese subtitle cues and
calls the active AI provider to produce structured lesson notes.

Long lessons are split into several transcript chunks; each chunk is summarised
separately and the partial results are merged into one document so nothing from
the middle of the video is silently dropped.
"""

from __future__ import annotations

import logging
from typing import Any

from translator import GenerationNotSupported

log = logging.getLogger("uvicorn.error")

_SUMMARY_SYSTEM = """
Bạn là trợ lý học tập thông minh. Người dùng sẽ cung cấp toàn bộ nội dung phụ đề Tiếng Việt của một video học online.

Hãy tạo bản tóm tắt bài học theo cấu trúc sau:

## 🎯 Mục tiêu bài học
(1–3 câu mô tả bài học này dạy gì)

## 📝 Nội dung chính
(Liệt kê các điểm chính dưới dạng bullet points, ngắn gọn và rõ ràng)

## 💡 Khái niệm quan trọng
(Định nghĩa hoặc giải thích các thuật ngữ / khái niệm then chốt)

## 🔧 Kỹ năng / Công cụ được đề cập
(Nếu có: kỹ năng thực hành, công cụ, lệnh, thư viện, v.v.)

## ✅ Tóm tắt một câu
(Một câu tóm gọn toàn bộ bài học)

Viết bằng Tiếng Việt. Ngắn gọn, dễ ôn lại sau. Không thêm thông tin ngoài nội dung video.
""".strip()

_CHUNK_SYSTEM = """
Bạn là trợ lý học tập. Dưới đây là MỘT PHẦN của phụ đề bài học.
Hãy liệt kê các ý chính của phần này dưới dạng bullet points bằng Tiếng Việt.
Chỉ nêu nội dung thực sự có trong phần này, không suy đoán, không thêm phần chưa học.
""".strip()

# Số ký tự xấp xỉ cho mỗi lần gọi provider.
_CHUNK_CHARS = 6000
# Tối đa số phần được tóm tắt riêng để không tràn quota.
_MAX_CHUNKS = 12


def _call_provider(translator: Any, prompt: str, system: str) -> str:
    """Gọi provider sinh văn bản; hỗ trợ cả FallbackTranslator lẫn provider đơn lẻ."""
    if hasattr(translator, "generate_text"):
        return translator.generate_text(prompt, system)
    providers = getattr(translator, "_providers", None)
    if providers:
        for name, provider in providers:
            if hasattr(provider, "generate_text"):
                try:
                    return provider.generate_text(prompt, system)
                except Exception as error:
                    log.warning("summarize_provider_failed provider=%s error=%s", name, error)
        return translator.translate(prompt)
    return translator.translate(prompt)


def _split_cues(cues: list[str], chunk_chars: int = _CHUNK_CHARS) -> list[list[str]]:
    """Chia phụ đề thành các phần, không cắt giữa một câu."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for cue in cues:
        text = cue.strip()
        if not text:
            continue
        current.append(text)
        size += len(text) + 1
        if size >= chunk_chars:
            chunks.append(current)
            current = []
            size = 0
    if current:
        chunks.append(current)
    return chunks


def summarize_lesson(
    title: str,
    vi_cues: list[str],
    translator: Any,
) -> str:
    """
    Generates a structured Vietnamese lesson summary.

    Args:
        title: Video / lesson title.
        vi_cues: List of Vietnamese translated subtitle strings.
        translator: Active translator object (must have generate_text or translate).

    Returns:
        Markdown string with the lesson summary.
    """
    cleaned = [cue.strip() for cue in vi_cues if cue and cue.strip()]
    if not cleaned:
        return "Không có nội dung phụ đề để tóm tắt."

    chunks = _split_cues(cleaned)
    if len(chunks) > _MAX_CHUNKS:
        # Bài quá dài: gộp phần đuôi lại để không vượt quota.
        head = chunks[:_MAX_CHUNKS - 1]
        tail = " ".join(" ".join(part) for part in chunks[_MAX_CHUNKS - 1:])
        head.append(tail)
        chunks = head

    if len(chunks) == 1:
        transcript = " ".join(chunks[0])
        prompt = f'Tiêu đề video: "{title}"\n\nNội dung phụ đề:\n{transcript}'
        return _call_provider(translator, prompt, _SUMMARY_SYSTEM).strip()

    # Bài dài: tóm tắt từng phần rồi ghép lại, phần đầu dùng cấu trúc đầy đủ.
    summaries: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        transcript = " ".join(chunk)
        prompt = f'Tiêu đề video: "{title}"\nPhần {index}/{len(chunks)}\n\nNội dung phụ đề:\n{transcript}'
        try:
            part = _call_provider(translator, prompt, _CHUNK_SYSTEM).strip()
        except GenerationNotSupported:
            raise
        except Exception as error:
            log.warning("summarize_chunk_failed part=%d error=%s", index, error)
            continue
        if part:
            summaries.append(part)

    if not summaries:
        # Không tóm tắt được phần nào: thử gọi một lần với toàn bộ transcript bị cắt bớt.
        transcript = " ".join(" ".join(part) for part in chunks)[:_CHUNK_CHARS]
        prompt = f'Tiêu đề video: "{title}"\n\nNội dung phụ đề:\n{transcript}'
        return _call_provider(translator, prompt, _SUMMARY_SYSTEM).strip()

    detail = "\n\n".join(summaries)
    head_prompt = (
        f'Tiêu đề video: "{title}"\n\n'
        "Dưới đây là các gạch đầu dòng đã trích xuất từ toàn bộ phụ đề bài học.\n"
        "Hãy tạo bản tóm tắt cuối cùng theo đúng cấu trúc 5 mục (Mục tiêu / Nội dung chính / "
        "Khái niệm quan trọng / Kỹ năng-Công cụ / Tóm tắt một câu), gộp các ý trùng lặp.\n\n"
        f"{detail}"
    )
    try:
        final = _call_provider(translator, head_prompt, _SUMMARY_SYSTEM).strip()
    except GenerationNotSupported:
        raise
    except Exception as error:
        log.warning("summarize_merge_failed error=%s", error)
        final = ""
    return final or detail
