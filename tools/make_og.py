#!/usr/bin/env python3
"""Генерация og-обложки 1200×630 для ссылок в Telegram и соцсетях.

    python3 tools/make_og.py

Результат — webapp/static/img/og-cover.png (коммитится). Шрифты лежат в
tools/fonts/ и скачиваются один раз из Google Fonts (Nunito, JetBrains Mono —
оба SIL OFL). Когда придёт фирменный леттеринг FLASK, слово-марку здесь нужно
заменить на него: строка WORDMARK ниже.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "tools" / "fonts"
OUT = ROOT / "webapp" / "static" / "img" / "og-cover.png"

W, H = 1200, 630
CREAM = (247, 241, 232)
BLUE = (36, 87, 197)
SKY = (115, 185, 245)
ORANGE = (255, 118, 39)
INK = (23, 32, 58)

WORDMARK = ("Flask", "VPN")
HEADLINE = "Безопасный интернет\nбез границ"
TAGLINE = "FREEDOM CONNECTS PEOPLE"
FOOTNOTE = "VLESS-Reality · без лимита скорости и трафика"


def main() -> None:
    img = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(img, "RGBA")

    # Голубая органическая форма справа — фоновый элемент бренда.
    d.ellipse([W - 430, -170, W + 190, 450], fill=SKY + (70,))
    d.ellipse([W - 330, 210, W + 90, 630], fill=SKY + (55,))

    # Полутон в нижнем левом углу — ниже текста, чтобы не спорить с ним.
    for row in range(4):
        for col in range(9):
            x, y = 70 + col * 26, 528 + row * 24
            d.ellipse([x, y, x + 7, y + 7], fill=SKY + (150,))

    nunito = lambda size: ImageFont.truetype(str(FONTS / "Nunito-Black.ttf"), size)
    mono = lambda size: ImageFont.truetype(str(FONTS / "JetBrainsMono-SemiBold.ttf"), size)

    # Слово-марка.
    f_mark = nunito(62)
    x = 80
    y = 74
    d.text((x, y), WORDMARK[0], font=f_mark, fill=BLUE)
    x += d.textlength(WORDMARK[0], font=f_mark)
    d.text((x, y), WORDMARK[1], font=f_mark, fill=ORANGE)

    # Надзаголовок моно-капсом с разрядкой.
    f_tag = mono(19)
    tx = 84
    for ch in TAGLINE:
        d.text((tx, 162), ch, font=f_tag, fill=BLUE)
        tx += d.textlength(ch, font=f_tag) + 5

    # Заголовок.
    f_head = nunito(78)
    d.multiline_text((80, 232), HEADLINE, font=f_head, fill=INK, spacing=12)

    # Оранжевое «рукописное» подчёркивание под словом «интернет».
    first_line = HEADLINE.split("\n")[0]
    before = first_line.split("интернет")[0]
    ux0 = 80 + d.textlength(before, font=f_head)
    ux1 = ux0 + d.textlength("интернет", font=f_head)
    d.rounded_rectangle([ux0 - 6, 337, ux1 + 6, 357], radius=9, fill=ORANGE + (225,))

    # Подпись снизу.
    d.text((80, 452), FOOTNOTE, font=nunito(30), fill=(74, 88, 120))

    # Оранжевая искра-астериск.
    cx, cy, r = 1035, 455, 46
    for angle in range(0, 180, 45):
        from math import cos, radians, sin
        dx, dy = cos(radians(angle)) * r, sin(radians(angle)) * r
        d.line([cx - dx, cy - dy, cx + dx, cy + dy], fill=ORANGE, width=13)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"Готово: {OUT.relative_to(ROOT)} ({OUT.stat().st_size} байт)")


if __name__ == "__main__":
    main()
