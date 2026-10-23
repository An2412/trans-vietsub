# -*- coding: utf-8 -*-
"""Sinh bộ icon extension từ ảnh gốc "ghost đeo headphone" (user cung cấp).

Ảnh nguồn: tools/assets/ghost.jpg (vuông 608px, nền đen, glyph trắng).
Không vẽ lại — chỉ xoá vệt JPG (thay nền đen thật đen), bo góc nhẹ và
xuất 16/32/48/128 px vào extension/icons/ (LANCZOS).
"""
import os

from PIL import Image, ImageFilter

SRC = os.path.join(os.path.dirname(__file__), "assets", "ghost.jpg")
OUT_DIR = "extension/icons"


def glyph_crop(img: Image.Image, pad_frac: float = 0.10) -> Image.Image:
    """Cắt sát vùng glyph (điểm sáng) + viền đệm, để cỡ 16/32 không bị glyph quá bé."""
    mask = img.convert("L").point(lambda v: 255 if v > 90 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return img
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    pad = int(max(w, h) * pad_frac)
    side = max(w, h) + pad * 2
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = max(0, min(img.width - side, round(cx - side / 2)))
    top = max(0, min(img.height - side, round(cy - side / 2)))
    return img.crop((left, top, left + side, top + side))


def load_source() -> Image.Image:
    img = Image.open(SRC).convert("RGB")
    # Vuông hoá: lấy ô trung tâm.
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    return img.convert("RGBA")


def main() -> None:
    src = load_source()
    os.makedirs(OUT_DIR, exist_ok=True)
    # 128/48 giữ khung gốc; 32/16 zoom sát hình ghost cho dễ nhận ở cỡ bé.
    zoom = {32: True, 16: True}
    for size in (128, 48, 32, 16):
        frame = glyph_crop(src) if zoom.get(size) else src
        img = frame.resize((size, size), Image.LANCZOS)
        if size in zoom:
            # Kích thước nhỏ: nét hơi nhoè do thu nhỏ, thêm unsharp nhẹ.
            img = img.filter(ImageFilter.UnsharpMask(radius=0.6, percent=110, threshold=2))
        img.save(f"{OUT_DIR}/icon{size}.png")
        print("wrote", f"{OUT_DIR}/icon{size}.png", f"({img.width}x{img.height})")


if __name__ == "__main__":
    main()
