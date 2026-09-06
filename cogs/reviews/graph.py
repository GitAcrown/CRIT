"""Histogramme compact des notes (bandeau, style Letterboxd)."""

from __future__ import annotations

import io
import os
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .providers import MediaHit

RATING_MAX = 10
GRAPH_FILENAME = "repartition.png"

_BG = (43, 45, 49)
_TEXT = (219, 222, 225)
_MUTED = (128, 132, 140)
_TRACK = (64, 66, 72)
_BAR = (196, 160, 80)

_W = 860
_H = 132
_PAD_X = 12
_PAD_TOP = 16
_PAD_BOT = 18
_LABEL_H = 14
_GAP = 5


@lru_cache(maxsize=4)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    roots = (
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
        "/usr/share/fonts/truetype/dejavu",
    )
    for root in roots:
        for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
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
    plot_h = _H - _PAD_TOP - _PAD_BOT - _LABEL_H
    bar_w = max(8, (_W - 2 * _PAD_X - _GAP * RATING_MAX) // (RATING_MAX + 1))

    image = Image.new("RGB", (_W, _H), _BG)
    draw = ImageDraw.Draw(image)
    font = _font(12)
    small = _font(11)
    base = _PAD_TOP + plot_h

    for score in range(RATING_MAX + 1):
        n = counts[score]
        x = _PAD_X + score * (bar_w + _GAP)
        h = 3 if n <= 0 else max(6, int(round(plot_h * n / peak)))
        y = base - h
        draw.rectangle((x, y, x + bar_w - 1, base), fill=_BAR if n else _TRACK)
        label = str(score)
        box = draw.textbbox((0, 0), label, font=font)
        lw = box[2] - box[0]
        draw.text(
            (x + (bar_w - lw) // 2, base + 2),
            label,
            font=font,
            fill=_TEXT if n else _MUTED,
        )
        if n:
            count = str(n)
            cbox = draw.textbbox((0, 0), count, font=small)
            cw = cbox[2] - cbox[0]
            ch = cbox[3] - cbox[1]
            draw.text((x + (bar_w - cw) // 2, y - ch - 2), count, font=small, fill=_TEXT)

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
