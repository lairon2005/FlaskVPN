#!/usr/bin/env python3
"""Подготовка коллажных ассетов лендинга из исходников нейрогенератора.

    python3 tools/make_collage.py crt-vpn ~/Downloads/монитор.png

Обрезает прозрачные поля, ужимает до нужной ширины и кладёт webp в
webapp/static/img/collage/. Имена и размеры — в CATALOG ниже, они же
прописаны в webapp/templates/index.html; описание — в README рядом с
картинками.

Без аргументов печатает каталог.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "webapp" / "static" / "img" / "collage"

# имя: (ширина на выходе, качество webp)
# Ширина = реальный размер отрисовки на лендинге с запасом на 2× экраны.
# crt-vpn — LCP главной, поэтому качество ниже: на фактуре бумаги разницы
# с 86 не видно, а файл легче на треть.
CATALOG = {
    "crt-vpn": (950, 78),
    "flip-phone-vpn": (420, 86),
    "tape-blue": (420, 86),
    "paper-torn": (640, 86),
}

# Гало нейрогенератора: альфа 1–4 тянется на весь холст, глазу невидима,
# но по ней bbox не обрезается. Режем по заметной части.
ALPHA_FLOOR = 8


def build(name: str, src: Path) -> Path:
    target_w, quality = CATALOG[name]

    image = Image.open(src).convert("RGBA")
    ys, xs = np.where(np.array(image)[:, :, 3] >= ALPHA_FLOOR)
    if not len(xs):
        raise SystemExit(f"{src}: картинка полностью прозрачная")
    image = image.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))

    if image.width > target_w:
        height = round(image.height * target_w / image.width)
        image = image.resize((target_w, height), Image.LANCZOS)

    out = OUT_DIR / f"{name}.webp"
    image.save(out, quality=quality, method=6)
    print(f"{out.relative_to(ROOT)}  {image.width}×{image.height}  "
          f"{out.stat().st_size // 1024} КБ")
    print("Размеры изменились? Поправь width/height у этой картинки "
          "в webapp/templates/index.html и таблицу в README.")
    return out


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        print("Каталог:")
        for name, (width, quality) in CATALOG.items():
            print(f"  {name:16} ширина {width}, качество {quality}")
        raise SystemExit(1)

    name, src = sys.argv[1], Path(sys.argv[2]).expanduser()
    if name not in CATALOG:
        raise SystemExit(f"Неизвестное имя {name!r}; доступны: "
                         f"{', '.join(CATALOG)}")
    if not src.is_file():
        raise SystemExit(f"Нет файла {src}")
    build(name, src)


if __name__ == "__main__":
    main()
