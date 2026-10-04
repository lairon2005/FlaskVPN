"""
Чек менеджерской операции картинкой (PNG) — тот же документ, что текстовый чек.

В Telegram чек уходит одним сообщением: картинка + текст чека подписью. На сайте —
картинка, которую менеджер скачивает или пересылает клиенту.

Адресаты те же, что у текста (manager_receipts):
  * group   — служебная группа: без пометки клиента и без QR (ссылка подписки — секрет),
              зато с именем ключа и отпечатком для сверки;
  * manager — с пометкой клиента и QR установки;
  * client  — без служебного: продукт, срок, сумма, QR установки.

Оформление — бренд-бук FLASK (docs/brand.md): кремовый фон, бумажная лента с зубчатым
краем и печатным офсетом вместо тени, синие чернила, оранжевый акцент, штамп статуса.
Шрифты — tools/fonts (Nunito Black, JetBrains Mono SemiBold): кириллица и «₽» в них есть,
эмодзи — нет, поэтому в картинке только текст.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

from tgbot.services.manager_receipts import (
    KIND_TEMP, ReceiptData, fmt_dt, fmt_money, fmt_time, money_lines, receipt_number,
)

AUDIENCES = ("group", "manager", "client")

_FONTS = Path(__file__).resolve().parents[2] / "tools" / "fonts"
_DISPLAY = _FONTS / "Nunito-Black.ttf"
_MONO = _FONTS / "JetBrainsMono-SemiBold.ttf"

# Палитра бренд-бука.
CREAM = "#F7F1E8"
PAPER = "#FFFCF6"
INK = "#17203A"
MUTED = "#6B7389"
BLUE = "#2457C5"
SKY_SOFT = "#DCEDFD"
ORANGE = "#FF7627"
RED = "#C8372D"
GREEN = "#1E8A55"

WIDTH = 760
PAPER_W = 640
MARGIN = (WIDTH - PAPER_W) // 2
PAD = 36                    # поля внутри ленты
INNER = PAPER_W - 2 * PAD
OFFSET = 8                  # печатный офсет ленты
TOOTH = 12                  # зубцы оторванного края

METHODS = {"cash": "Наличные", "online": "Онлайн (ЮKassa)", "free": "Бесплатно"}

# Штамп статуса: (надпись, цвет).
def _stamp(data: ReceiptData) -> tuple[str, str]:
    if data.status == "completed":
        if data.kind == KIND_TEMP and data.temp_deleted_at:
            return "КЛЮЧ УДАЛЁН", MUTED
        return "ВЫДАН", GREEN
    return {
        "pending_payment": ("ЖДЁМ ОПЛАТУ", ORANGE),
        "cancelled": ("ОТМЕНЁН", MUTED),
        "refunded": ("ВОЗВРАТ", RED),
    }.get(data.status, ("НЕ ВЫДАН", RED))


@lru_cache(maxsize=None)
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _display(size: int) -> ImageFont.FreeTypeFont:
    return _font(_DISPLAY, size)


def _mono(size: int) -> ImageFont.FreeTypeFont:
    return _font(_MONO, size)


def _width(font, text: str) -> int:
    return int(font.getlength(text))


def _wrap(font, text: str, width: int) -> list[str]:
    """Перенос по словам; слово длиннее строки режется по символам (ссылки, коды)."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if _width(font, candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while _width(font, word) > width:
            cut = len(word)
            while cut > 1 and _width(font, word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def _short_name(name: str) -> str:
    parts = (name or "").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0] if parts else "—"


def _qr_image(url: str, size: int) -> Image.Image:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color=BLUE, back_color=PAPER).convert("RGB")
    return image.resize((size, size), Image.NEAREST)


def _rows(data: ReceiptData, audience: str) -> list[tuple[str, str]]:
    """Строки «название — значение» в середине чека."""
    rows: list[tuple[str, str]] = []
    if data.client_code:
        rows.append(("Клиент", data.client_code + (" · новый" if data.client_is_new else "")))
    if data.client_label and audience == "manager":
        rows.append(("Пометка", data.client_label))
    if audience != "client":
        rows.append(("Менеджер", f"{_short_name(data.manager_name)} (#{data.manager_id})"))

    if data.kind == KIND_TEMP:
        rows.append(("Продукт", "Пробный ключ"))
        rows.append(("Действует", f"{fmt_time(data.created_at)} – {fmt_time(data.expires_at)} МСК"))
    else:
        rows.append(("Продукт", data.product_title + (f" · {data.days} дн." if data.days else "")))
    if data.traffic_gb is not None:
        per = "" if data.kind == KIND_TEMP else "/мес"
        rows.append(("Трафик", "безлимит" if data.traffic_gb == 0 else f"{data.traffic_gb} ГБ{per}"))
    if data.devices_limit is not None:
        rows.append(("Устройства", f"до {data.devices_limit}"))
    if data.kind != KIND_TEMP and data.expires_at:
        rows.append(("Действует до", f"{fmt_dt(data.expires_at)} МСК"))

    if data.kind != KIND_TEMP and data.payment_method:
        rows.append(("Оплата", METHODS.get(data.payment_method, data.payment_method)))
        if data.payment_method == "online" and data.autorenew is not None:
            rows.append(("Автопродление", "включено" if data.autorenew else "выключено"))
    if audience == "group" and data.key_username:
        fingerprint = f" · …{data.key_fingerprint}" if data.key_fingerprint else ""
        rows.append(("Ключ", data.key_username + fingerprint))
    return rows


class _Canvas:
    """Содержимое ленты: рисуем сверху вниз, высоту узнаём по ходу и потом обрезаем."""

    def __init__(self):
        self.image = Image.new("RGBA", (PAPER_W, 4000), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)
        self.y = PAD

    def text(self, x: int, text: str, font, fill=INK, *, anchor="la"):
        self.draw.text((x, self.y), text, font=font, fill=fill, anchor=anchor)

    def gap(self, height: int):
        self.y += height

    def dashed(self):
        self.gap(14)
        x = PAD
        while x < PAPER_W - PAD:
            self.draw.line((x, self.y, min(x + 10, PAPER_W - PAD), self.y), fill=MUTED, width=2)
            x += 18
        self.gap(20)

    def row(self, label: str, value: str, *, font=None, value_fill=INK, label_fill=MUTED):
        """Название слева, значение справа; длинное значение переносится под название."""
        font = font or _mono(21)
        label_w = _width(font, label) + 24
        lines = _wrap(font, value, INNER - label_w)
        if len(lines) > 2:   # не помещается справа — пишем под названием во всю ширину
            self.text(PAD, label, font, label_fill)
            self.gap(30)
            for line in _wrap(font, value, INNER):
                self.text(PAPER_W - PAD, line, font, value_fill, anchor="ra")
                self.gap(30)
            self.gap(4)
            return
        self.text(PAD, label, font, label_fill)
        for line in lines:
            self.text(PAPER_W - PAD, line, font, value_fill, anchor="ra")
            self.gap(30)
        self.gap(4)

    def crop(self) -> Image.Image:
        return self.image.crop((0, 0, PAPER_W, self.y + PAD))


def _header(c: _Canvas, data: ReceiptData):
    """Слово-марка, номер и время слева; штамп статуса справа — как печать на бланке."""
    top = c.y
    big = _display(46)
    c.text(PAD, "Flask", big, BLUE)
    c.draw.text((PAD + _width(big, "Flask"), c.y), "VPN", font=big, fill=ORANGE)
    c.gap(62)
    c.text(PAD, f"ЧЕК № {receipt_number(data.op_id)}", _mono(22), INK)
    c.gap(32)
    c.text(PAD, f"{fmt_dt(data.created_at)} МСК", _mono(19), MUTED)
    c.gap(26)
    stamp = _stamp_image(data)
    middle = (top + c.y) // 2
    c.image.alpha_composite(stamp, (PAPER_W - PAD - stamp.width + 8, max(8, middle - stamp.height // 2)))


def _totals(c: _Canvas, data: ReceiptData):
    small = _mono(21)
    for title, amount in money_lines(data):
        c.row(title, fmt_money(amount), font=small, label_fill=INK)
    if money_lines(data):
        c.gap(6)
    c.text(PAD, "ИТОГО", _display(30), INK)
    total = "Бесплатно" if data.kind == KIND_TEMP else fmt_money(data.price)
    c.draw.text((PAPER_W - PAD, c.y - 12), total, font=_display(48), fill=BLUE, anchor="ra")
    c.gap(56)


def _stamp_image(data: ReceiptData) -> Image.Image:
    text, color = _stamp(data)
    font = _display(30)
    w = _width(font, text) + 44
    h = 62
    stamp = Image.new("RGBA", (w + 8, h + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(stamp)
    d.rounded_rectangle((4, 4, w + 4, h + 4), radius=16, outline=color, width=4)
    d.text((w // 2 + 4, h // 2 + 4), text, font=font, fill=color, anchor="mm")
    return stamp.rotate(-6, resample=Image.BICUBIC, expand=True)


def _qr_block(c: _Canvas, url: str):
    size = 236
    panel_h = size + 32
    top = c.y
    c.draw.rounded_rectangle((PAD, top, PAPER_W - PAD, top + panel_h), radius=22, fill=SKY_SOFT)
    c.image.paste(_qr_image(url, size), (PAD + 16, top + 16))
    x = PAD + size + 36
    width = PAPER_W - PAD - 18 - x
    y = top + 22
    c.draw.text((x, y), "Установка VPN", font=_display(28), fill=BLUE)
    y += 44
    for step in ("Наведите камеру на QR", "Откройте ссылку", "Выберите приложение INCY", "Нажмите «Подключить»"):
        for line in _wrap(_mono(17), step, width):
            c.draw.text((x, y), line, font=_mono(17), fill=INK)
            y += 24
        y += 6
    c.y = top + panel_h
    c.gap(18)


def _footer(c: _Canvas, data: ReceiptData):
    if data.kind == KIND_TEMP:
        note = "Пробный ключ удаляется автоматически по окончании срока."
    elif data.payment_method == "online":
        note = "Не фискальный документ. Кассовый чек пришлёт ЮKassa."
    else:
        note = "Не фискальный документ."
    if data.service_fee:
        note += " Автопродление списывает только стоимость подписки."
    for line in _wrap(_mono(16), note, INNER):
        c.text(PAD, line, _mono(16), MUTED)
        c.gap(22)


def _torn_paper(height: int) -> list[tuple[int, int]]:
    """Контур ленты: ровный верх, зубчатый низ — чек оторван от рулона."""
    points = [(0, 0), (PAPER_W, 0), (PAPER_W, height)]
    x = PAPER_W
    down = False
    while x > 0:
        x -= TOOTH
        points.append((max(x, 0), height + (TOOTH if down else 0)))
        down = not down
    points.append((0, height))
    return points


def render_receipt(data: ReceiptData, audience: str = "manager") -> bytes:
    """PNG чека для адресата (group | manager | client)."""
    if audience not in AUDIENCES:
        raise ValueError(audience)
    c = _Canvas()
    _header(c, data)
    c.dashed()
    for label, value in _rows(data, audience):
        c.row(label, value)
        if label == "Продукт" and data.custom_note and audience != "client":
            # Разложение цены «любого срока» — сразу под продуктом, мелко.
            for line in _wrap(_mono(17), data.custom_note, INNER):
                c.text(PAPER_W - PAD, line, _mono(17), MUTED, anchor="ra")
                c.gap(24)
            c.gap(4)
    c.dashed()
    _totals(c, data)

    show_qr = (
        audience != "group" and data.subscription_url and data.status == "completed" and not data.temp_deleted_at
    )
    if show_qr:
        c.gap(6)
        _qr_block(c, data.subscription_url)
    else:
        c.gap(4)
    _footer(c, data)
    content = c.crop()

    paper_h = content.height
    top = 40
    canvas = Image.new("RGB", (WIDTH, top + paper_h + TOOTH + OFFSET + 40), CREAM)
    draw = ImageDraw.Draw(canvas)
    outline = _torn_paper(paper_h)
    # Офсет — по прямоугольнику ленты: под зубцами тень рябила бы.
    draw.rectangle((MARGIN + OFFSET, top + OFFSET, MARGIN + PAPER_W + OFFSET - 1, top + paper_h - 1), fill=BLUE)
    draw.polygon([(x + MARGIN, y + top) for x, y in outline], fill=PAPER)
    canvas.paste(content, (MARGIN, top), content)
    # Оранжевая «искра» в углу — фирменный акцент.
    _spark(draw, MARGIN + PAPER_W - 4, top - 6, 16)

    out = BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def _spark(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int):
    """Четырёхлучевая звёздочка-астериск из бренд-бука."""
    k = r // 4
    draw.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                  (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=ORANGE)
