"""Server-side share card: one card of one user's story as a 1200x630 PNG (the size link previews use).

Drawn with Pillow from the same card dict the frontend renders, so the picture cannot say
anything the story did not. Deterministic: same card, same bytes.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630
MARGIN = 72
INK = (14, 11, 30)
PAPER = (255, 255, 255)
MUTED = (196, 192, 214)
# Accent per card family. Each is bright enough for INK text on it (contrast > 7:1) and on INK as text.
ACCENT = {
    "volume": (255, 122, 89), "consistency": (61, 220, 151), "rhythm": (178, 156, 255), "place": (79, 195, 247),
    "breadth": (255, 200, 87), "craft": (255, 128, 164), "curiosity": (249, 248, 113), "peak": (255, 159, 28),
    "origin": (123, 223, 242), "community": (155, 229, 100), "frame": (200, 182, 255),
}  # fmt: skip
FONT_PATH = Path(__file__).parent / "fonts" / "Inter.ttf"


@lru_cache(maxsize=64)
def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size)
    font.set_variation_by_axes([min(32, max(14, size)), weight])  # optical size, weight
    return font


def _fit(
    draw: ImageDraw.ImageDraw, text: str, weight: int, start: int, floor: int, max_width: int
) -> ImageFont.FreeTypeFont:
    """Largest font from `start` down to `floor` at which the text fits on one line."""
    size = start
    while size > floor and draw.textlength(text, font=_font(size, weight)) > max_width:
        size -= 4
    return _font(size, weight)


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int
) -> list[str]:
    if max_lines <= 0:
        return []
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
            continue
        lines.append(line)
        line = word
    lines.append(line)
    if len(lines) > max_lines:  # never drop words silently: mark the cut
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
        lines[-1] += "…"
    return [ln for ln in lines if ln]


def _background(accent: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), INK)
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    # Two soft discs of the accent colour; concentric steps stand in for a blur.
    for cx, cy, radius, strength in ((WIDTH - 120, 80, 520, 70), (140, HEIGHT + 120, 420, 40)):
        for step in range(24):
            r = radius * (1 - step / 24)
            g.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*accent, int(strength * (step + 1) / 24 / 3)))
    img.paste(glow, (0, 0), glow)
    return img


def render_card(card: dict[str, Any], login: str, year: int) -> bytes:
    accent = ACCENT.get(card.get("family", "frame"), ACCENT["frame"])
    img = _background(accent)
    draw = ImageDraw.Draw(img)
    inner = WIDTH - 2 * MARGIN

    draw.text((MARGIN, 56), f"WRAPPED {year}", font=_font(26, 700), fill=accent)
    handle = f"@{login}"
    draw.text(
        (WIDTH - MARGIN - draw.textlength(handle, font=_font(26, 500)), 56), handle, font=_font(26, 500), fill=MUTED
    )

    claim = card.get("claim")
    y = 132 if claim or card.get("stats") else 190  # no pill below: sit lower so the card is not top-heavy
    headline = str(card.get("headline", ""))
    draw.text((MARGIN, y), headline, font=_fit(draw, headline, 600, 44, 28, inner), fill=MUTED)
    y += 64

    value = str(card.get("value", ""))
    value_font = _fit(draw, value, 800, 92 if card.get("stats") else 132, 52, inner)
    draw.text((MARGIN, y), value, font=value_font, fill=PAPER)
    y += int(value_font.size * 1.16)
    if unit := str(card.get("unit", "")):
        draw.text((MARGIN, y), unit, font=_fit(draw, unit, 600, 40, 24, inner), fill=accent)
        y += 58

    if stats := card.get("stats"):  # summary card: a row of headline numbers
        x: float = MARGIN
        for stat in stats[:3]:
            draw.text((x, y + 6), str(stat["value"]), font=_font(46, 800), fill=PAPER)
            draw.text((x, y + 62), str(stat["label"]), font=_font(22, 500), fill=MUTED)
            x += max(draw.textlength(str(stat["value"]), font=_font(46, 800)),
                     draw.textlength(str(stat["label"]), font=_font(22, 500))) + 56  # fmt: skip
        y += 104

    footer_y = HEIGHT - 150 if claim else HEIGHT - 96
    body_font = _font(30, 400)
    for line in _wrap(draw, str(card.get("body", "")), body_font, inner, max(0, min(2, (footer_y - y - 12) // 42))):
        draw.text((MARGIN, y + 8), line, font=body_font, fill=PAPER)
        y += 42

    if claim:
        pill_font = _fit(draw, claim["text"], 800, 34, 22, inner - 56)
        pill_w = draw.textlength(claim["text"], font=pill_font) + 56
        draw.rounded_rectangle((MARGIN, HEIGHT - 142, MARGIN + pill_w, HEIGHT - 82), radius=30, fill=accent)
        draw.text((MARGIN + 28, HEIGHT - 112), claim["text"], font=pill_font, fill=INK, anchor="lm")
        basis = _wrap(draw, claim["basis"], _font(22, 500), inner, 1)
        draw.text((MARGIN, HEIGHT - 68), basis[0] if basis else "", font=_font(22, 500), fill=MUTED)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
