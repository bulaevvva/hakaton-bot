"""QR-плакат для подъезда: печатается и вешается у входа."""
from __future__ import annotations

import io
from pathlib import Path

import segno
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1240, 1754  # A4 при 150 dpi
INK = (23, 32, 51)
MUTED = (100, 112, 138)
ACCENT = (51, 93, 245)

_FONT_CANDIDATES = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ],
}


def _font(weight: str, size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES[weight]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _centered(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill, width: int = WIDTH - 200) -> int:
    for line in _wrap(draw, text, font, width):
        line_width = draw.textlength(line, font=font)
        draw.text(((WIDTH - line_width) / 2, y), line, font=font, fill=fill)
        y += int(font.size * 1.25)
    return y


def render_poster(address: str, entrance: str, link: str, org_name: str = "") -> bytes:
    """PNG-плакат: заголовок, QR со ссылкой на бота, адрес и подъезд, короткая инструкция."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 26), fill=ACCENT)

    y = _centered(draw, 120, "Проблема в доме?", _font("bold", 96), INK)
    y = _centered(draw, y + 20, "Наведите камеру телефона — откроется бот в MAX", _font("regular", 44), MUTED)

    qr = segno.make(link, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=22, border=2, dark=INK)
    code = Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")
    side = 760
    code = code.resize((side, side), Image.NEAREST)
    image.paste(code, ((WIDTH - side) // 2, y + 50))
    y += 50 + side + 50

    y = _centered(draw, y, address, _font("bold", 60), INK)
    y = _centered(draw, y + 6, f"Подъезд {entrance}", _font("bold", 60), ACCENT)

    steps = [
        "Одна проблема — одна общая заявка: если соседи уже сообщили, просто присоединитесь.",
        "Управляющая компания отвечает всем сразу, а вы подтверждаете, что всё исправлено.",
    ]
    y += 40
    for step in steps:
        y = _centered(draw, y, step, _font("regular", 36), MUTED) + 14

    footer = f"«Дом: проблема решена»{' · ' + org_name if org_name else ''}"
    _centered(draw, HEIGHT - 110, footer, _font("regular", 30), MUTED)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
