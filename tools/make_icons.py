# -*- coding: utf-8 -*-
"""Sinh bộ icon extension từ thiết kế "trans-vietsub".

Thiết kế: vuông bo tròn xanh đậm (gradient nhẹ), tam giác phát trắng viền tròn
kèm thanh dọc bên trong, đèn nền xanh sáng quanh glyph. Xuất 16/32/48/128 px
để dùng trong manifest (action + icons).
"""
import os

from PIL import Image, ImageDraw, ImageFilter

S = 2048  # master size (supersample)

TOP_COLOR = (47, 63, 82)      # #2f3f52 — phần trên mặt vuông
BOTTOM_COLOR = (26, 38, 54)   # #1a2636 — phần dưới
GLOW_COLOR = (168, 210, 255)  # #a8d2ff — ánh sáng quanh glyph
WHITE = (255, 255, 255, 255)

STROKE = 0.055      # độ dày viền tam giác (tỷ lệ theo S)
GLOW_BLUR = 0.045
TRI_TL = (0.250, 0.205)   # đỉnh trên trái
TRI_BL = (0.250, 0.795)   # đỉnh dưới trái
TRI_R = (0.665, 0.500)    # đỉnh phải (mũi phát)
BAR_X0, BAR_X1 = 0.430, 0.500
BAR_Y0, BAR_Y1 = 0.410, 0.625
CORNER = 0.185            # bo góc mặt vuông


def rounded_square() -> Image.Image:
    gradient = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        r = round(TOP_COLOR[0] + (BOTTOM_COLOR[0] - TOP_COLOR[0]) * t)
        g = round(TOP_COLOR[1] + (BOTTOM_COLOR[1] - TOP_COLOR[1]) * t)
        b = round(TOP_COLOR[2] + (BOTTOM_COLOR[2] - TOP_COLOR[2]) * t)
        gradient.putpixel((0, y), (r, g, b))
    gradient = gradient.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(CORNER * S), fill=255)
    icon = gradient.convert("RGBA")
    icon.putalpha(mask)
    return icon


def glyph_layer() -> Image.Image:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    p1 = (TRI_TL[0] * S, TRI_TL[1] * S)
    p2 = (TRI_BL[0] * S, TRI_BL[1] * S)
    p3 = (TRI_R[0] * S, TRI_R[1] * S)
    width = int(STROKE * S)
    # Viền tam giác: ba cạnh + chấm tròn ở đỉnh cho góc bo tròn.
    draw.line([p1, p2], fill=WHITE, width=width)
    draw.line([p2, p3], fill=WHITE, width=width)
    draw.line([p3, p1], fill=WHITE, width=width)
    r = width / 2
    for (x, y) in (p1, p2, p3):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)
    # Thanh dọc bên trong (capsule).
    bx0, bx1 = BAR_X0 * S, BAR_X1 * S
    by0, by1 = BAR_Y0 * S, BAR_Y1 * S
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=(bx1 - bx0) / 2, fill=WHITE)
    return layer


def compose() -> Image.Image:
    icon = rounded_square()
    glyph = glyph_layer()
    # Đèn nền: glyph bị blur, nhuốm xanh sáng, alpha ~55%.
    glow = glyph.filter(ImageFilter.GaussianBlur(GLOW_BLUR * S))
    a = glow.getchannel("A").point(lambda v: int(v * 0.55))
    glow_tint = Image.new("RGBA", (S, S), GLOW_COLOR + (0,))
    glow_tint.putalpha(a)
    icon = Image.alpha_composite(icon, glow_tint)
    icon = Image.alpha_composite(icon, glyph)
    return icon


def main() -> None:
    master = compose()
    out_dir = "extension/icons"
    os.makedirs(out_dir, exist_ok=True)
    for size in (128, 48, 32, 16):
        img = master.resize((size, size), Image.LANCZOS)
        path = f"{out_dir}/icon{size}.png"
        img.save(path)
        print("wrote", path)


if __name__ == "__main__":
    main()
