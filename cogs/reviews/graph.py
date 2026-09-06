"""Histogramme de notes, style sombre proche de l'UI Discord."""

from __future__ import annotations

import io
import os
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .providers import MediaHit

RATING_MAX = 10
GRAPH_FILENAME = "repartition.png"

_BG = (43, 45, 49)
_TEXT = (242, 243, 245)
_MUTED = (148, 155, 164)
_TRACK = (55, 57, 63)
_BAR = (232, 176, 74)
_PAD = 36
_TITLE_H = 52
_ROW_H = 36
_BAR_H = 14
_SCORE_W = 44
_COUNT_W = 48
_WIDTH = 720
_SCALE = 2


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        ("segoeuib.ttf", "segoeui.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    )
    roots = (
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/liberation",
    )
    files = names + (("DejaVuSans-Bold.ttf", "DejaVuSans.ttf") if bold else ("DejaVuSans.ttf",))
    for root in roots:
        for name in files:
            path = os.path.join(root, name)
            if os.path.isfile(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def rating_counts(entries: list[tuple[MediaHit, Any]]) -> list[int]:
    counts = [0] * (RATING_MAX + 1)
    for _hit, row in entries:
        points = int(round(max(0.0, min(float(RATING_MAX), float(row["rating"])))))
        counts[points] += 1
    return counts


def render_rating_graph_png(
    entries: list[tuple[MediaHit, Any]],
    average: float | None = None,
) -> bytes | None:
    if not entries:
        return None
    counts = rating_counts(entries)
    peak = max(counts) or 1
    if average is None:
        average = sum(score * n for score, n in enumerate(counts)) / len(entries)

    width = _WIDTH * _SCALE
    height = (_PAD * 2 + _TITLE_H + _ROW_H * (RATING_MAX + 1)) * _SCALE
    pad = _PAD * _SCALE
    title_h = _TITLE_H * _SCALE
    row_h = _ROW_H * _SCALE
    bar_h = _BAR_H * _SCALE
    score_w = _SCORE_W * _SCALE
    count_w = _COUNT_W * _SCALE

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    title_font = _font(28 * _SCALE, bold=True)
    meta_font = _font(22 * _SCALE)
    row_font = _font(22 * _SCALE)

    title = "Répartition"
    meta = f"moyenne {average:.1f}/{RATING_MAX}"
    draw.text((pad, pad), title, font=title_font, fill=_TEXT)
    meta_box = draw.textbbox((0, 0), meta, font=meta_font)
    draw.text((width - pad - (meta_box[2] - meta_box[0]), pad + 6 * _SCALE), meta, font=meta_font, fill=_MUTED)

    bar_left = pad + score_w
    bar_right = width - pad - count_w
    bar_span = max(1, bar_right - bar_left)
    origin_y = pad + title_h

    for score in range(RATING_MAX, -1, -1):
        n = counts[score]
        top = origin_y + (RATING_MAX - score) * row_h
        mid = top + row_h // 2
        bar_top = mid - bar_h // 2
        bar_bot = bar_top + bar_h
        label = str(score)
        label_box = draw.textbbox((0, 0), label, font=row_font)
        draw.text(
            (bar_left - 12 * _SCALE - (label_box[2] - label_box[0]), mid - (label_box[3] - label_box[1]) // 2),
            label,
            font=row_font,
            fill=_TEXT if n else _MUTED,
        )
        radius = bar_h // 2
        draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_bot), radius=radius, fill=_TRACK)
        if n:
            filled = max(radius * 2, int(round(bar_span * n / peak)))
            filled = min(bar_span, filled)
            draw.rounded_rectangle(
                (bar_left, bar_top, bar_left + filled, bar_bot),
                radius=radius,
                fill=_BAR,
            )
            count = str(n)
            count_box = draw.textbbox((0, 0), count, font=row_font)
            ch = count_box[3] - count_box[1]
            draw.text(
                (bar_left + filled + 10 * _SCALE, mid - ch // 2),
                count,
                font=row_font,
                fill=_TEXT,
            )
        else:
            zero_box = draw.textbbox((0, 0), "0", font=row_font)
            draw.text(
                (bar_left + 8 * _SCALE, mid - (zero_box[3] - zero_box[1]) // 2),
                "0",
                font=row_font,
                fill=_MUTED,
            )

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
