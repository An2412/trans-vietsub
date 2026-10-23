# -*- coding: utf-8 -*-
"""Sinh bộ icon extension: ghost đeo headphone (thiết kế user gửi).

Nền: vuông bo tròn gần đen full-bleed. Glyph: ghost trắng viền tròn
(đầu vòm, đuôi răng cưa, hai mắt nghiêng), dải headphone phủ lên đầu và
hai earmuff (trái nhỏ, phải lớn có viên thuốc trắng bên trong).
Xuất 16/32/48/128 px vào extension/icons/.
"""
import math

from PIL import Image, ImageDraw

S = 2048  # master size (supersample)

BG_TOP = (13, 14, 17)     # #0d0e11
BG_BOTTOM = (8, 9, 11)    # #08090b
WHITE = (245, 247, 250, 255)
CORNER = 0.20             # bo góc mặt vuông
STROKE = 0.038            # độ dày nét glyph (tỷ lệ theo S)

C = (0.478, 0.435)        # tâm cụm đầu + headphone
BAND_R = 0.222            # bán kính dải headphone
DOME_R = 0.175            # bán kính vòm đầu

LEFT_CUP = (0.262, 0.445, 0.318, 0.600)   # earmuff trái (nhỏ)
RIGHT_CUP = (0.610, 0.390, 0.735, 0.640)  # earmuff phải (lớn)
PILL = (0.668, 0.462, 0.700, 0.578)       # viên thuốc trắng trong earmuff phải
EYE_L = (0.355, 0.535, 0.026, 0.013, -20)
EYE_R = (0.443, 0.515, 0.034, 0.016, -20)


def arc_points(cx, cy, r, a0, a1, steps):
    pts = []
    for i in range(steps + 1):
        t = math.radians(a0 + (a1 - a0) * i / steps)
        pts.append(((cx + r * math.cos(t)) * S, (cy - r * math.sin(t)) * S))
    return pts


def quad_points(p0, p1, p2, steps):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append((x * S, y * S))
    return pts


def rotated_ellipse(cx, cy, rx, ry, deg, steps=48):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        pts.append(((cx + ex * ca - ey * sa) * S, (cy - (ex * sa + ey * ca)) * S))
    return pts


def body_points():
    """Nét viền thân ghost: vòm đầu -> sườn phải -> đuôi răng cưa -> sườn trái (vòng kín)."""
    pts = arc_points(C[0], C[1], DOME_R, 180, 0, 72)
    pts += quad_points((0.653, 0.435), (0.695, 0.530), (0.670, 0.700), 40)[1:]
    xs = [0.670, 0.618, 0.566, 0.514, 0.462, 0.410, 0.358, 0.306]
    for i, x in enumerate(xs):
        if i == 0:
            y = 0.700
        elif i == len(xs) - 1:
            y = 0.705
        else:
            y = 0.705 if i % 2 == 0 else 0.755
        if i:
            pts.append((x * S, y * S))
    pts += quad_points((0.306, 0.705), (0.278, 0.565), (0.303, 0.435), 40)[1:]
    return pts


def rounded_square() -> Image.Image:
    gradient = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        r = round(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = round(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = round(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        gradient.putpixel((0, y), (r, g, b))
    gradient = gradient.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(CORNER * S), fill=255)
    icon = gradient.convert("RGBA")
    icon.putalpha(mask)
    return icon


def compose(stroke=STROKE, detailed=True, with_band=True) -> Image.Image:
    icon = rounded_square()
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    w = round(stroke * S)
    half = w / 2

    # Dải headphone vòm qua đầu (nét + chấm tròn hai đầu); bỏ ở bản 16/32px
    # vì sát đầu quá thành cục nhoè.
    if with_band:
        band = arc_points(C[0], C[1], BAND_R, 200, 340, 72)
        d.line(band, fill=WHITE, width=w, joint="curve")
        for p in (band[0], band[-1]):
            d.ellipse([p[0] - half, p[1] - half, p[0] + half, p[1] + half], fill=WHITE)

    # Earmuff trái (nằm sau, vẽ trước thân).
    d.rounded_rectangle([LEFT_CUP[0] * S, LEFT_CUP[1] * S, LEFT_CUP[2] * S, LEFT_CUP[3] * S],
                        radius=0.028 * S, outline=WHITE, width=w)

    # Thân ghost (nét viền kín, lòng để đen).
    d.line(body_points(), fill=WHITE, width=w, joint="curve")

    # Earmuff phải đè lên thân (+ viên thuốc trắng trong bản chi tiết).
    d.rounded_rectangle([RIGHT_CUP[0] * S, RIGHT_CUP[1] * S, RIGHT_CUP[2] * S, RIGHT_CUP[3] * S],
                        radius=0.052 * S, outline=WHITE, width=w)
    if detailed:
        d.rounded_rectangle([PILL[0] * S, PILL[1] * S, PILL[2] * S, PILL[3] * S],
                            radius=0.016 * S, fill=WHITE)
        # Hai mắt nghiên (đẩy góc nhợn).
        d.polygon(rotated_ellipse(*EYE_L), fill=WHITE)
        d.polygon(rotated_ellipse(*EYE_R), fill=WHITE)

    return Image.alpha_composite(icon, layer)


def main() -> None:
    out_dir = "extension/icons"
    import os
    os.makedirs(out_dir, exist_ok=True)
    # Kích thước nhỏ dùng nét dày hơn và bỏ chi tiết nhỏ (mắt/viên thuốc)
    # để không bị nhoè thành cục khi LANCZOS thu nhỏ.
    specs = {
        128: (0.038, True, True),
        48: (0.046, True, True),
        32: (0.062, False, False),
        16: (0.095, False, False),
    }
    for size, (stroke, detailed, with_band) in specs.items():
        master = compose(stroke=stroke, detailed=detailed, with_band=with_band)
        img = master.resize((size, size), Image.LANCZOS)
        img.save(f"{out_dir}/icon{size}.png")
        print("wrote", f"{out_dir}/icon{size}.png")


if __name__ == "__main__":
    main()
