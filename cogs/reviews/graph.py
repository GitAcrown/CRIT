"""Histogramme compact des notes, barres horizontales."""

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
_TEXT = (242, 243, 245)
_MUTED = (148, 155, 164)
_BAR = (196, 160, 80)

_W = 280
_ROW = 22
_PAD = 12
_SCORE_W = 28
_GAP = 8
_BAR_H = 13
_COUNT_W = 32


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
    height = _PAD * 2 + _ROW * (RATING_MAX + 1)
    bar_left = _PAD + _SCORE_W + _GAP
    bar_max = max(24, _W - bar_left - _PAD - _COUNT_W)

    image = Image.new("RGB", (_W, height), _BG)
    draw = ImageDraw.Draw(image)
    font = _font(18)

    for score in range(RATING_MAX, -1, -1):
        n = counts[score]
        mid = _PAD + (RATING_MAX - score) * _ROW + _ROW // 2
        draw.text(
            (_PAD + _SCORE_W, mid),
            str(score),
            font=font,
            fill=_TEXT if n else _MUTED,
            anchor="rm",
        )
        if n <= 0:
            continue
        filled = max(_BAR_H, int(round(bar_max * n / peak)))
        filled = min(bar_max, filled)
        bar_top = mid - _BAR_H // 2
        draw.rectangle((bar_left, bar_top, bar_left + filled, bar_top + _BAR_H), fill=_BAR)
        draw.text(
            (bar_left + filled + 6, mid),
            str(n),
            font=font,
            fill=_TEXT,
            anchor="lm",
        )

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
