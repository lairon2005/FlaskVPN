"""
Чеки менеджерских операций — чистое форматирование (HTML для Telegram).

Три варианта одного документа:
  * групповой — в служебную группу: БЕЗ ссылки подписки, Telegram ID и контактов
    клиента (группа читает много людей, и ссылка подписки — это секрет);
  * менеджеру — с ссылкой для установки;
  * клиенту с Telegram — короткий, с итоговой суммой (защита от завышения цены).

Чек не фискальный (54-ФЗ): фискальный выбивает ЮKassa при онлайн-оплате.
Время показываем по Москве; в БД оно хранится наивным UTC.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

_MSK = ZoneInfo("Europe/Moscow")

KIND_TARIFF = "tariff"
KIND_CUSTOM = "custom"
KIND_TEMP = "temp"

# Услуга менеджера — так она называется в чеках (наших и фискальном ЮKassa).
SERVICE_FEE_TITLE = "Подключение и настройка VPN"


def receipt_number(op_id: int) -> str:
    return f"M-{op_id:06d}"


def to_msk(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(_MSK) if value.tzinfo is None else value.astimezone(_MSK)


def fmt_dt(value: datetime | None) -> str:
    local = to_msk(value)
    return local.strftime("%d.%m.%Y %H:%M") if local else "—"


def fmt_time(value: datetime | None) -> str:
    local = to_msk(value)
    return local.strftime("%H:%M") if local else "—"


def fmt_money(amount: float) -> str:
    """1250 → '1 250 ₽', 149.5 → '149,50 ₽'."""
    if float(amount).is_integer():
        return f"{int(amount):,}".replace(",", " ") + " ₽"
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


@dataclass(frozen=True)
class ReceiptData:
    op_id: int
    created_at: datetime
    manager_id: int
    manager_name: str
    kind: str                         # tariff | custom | temp
    status: str                       # completed | pending_payment | failed | cancelled | refunded
    client_code: str | None = None
    client_is_new: bool = False
    client_label: str | None = None   # пометка менеджера — только в его чеке
    product_title: str = ""           # «Месяц» / «Любой срок»
    days: int | None = None
    custom_note: str | None = None    # разложение цены для «своих дней»
    traffic_gb: int | None = None     # 0 — безлимит
    devices_limit: int | None = None
    expires_at: datetime | None = None
    price: float = 0.0                # итого, вместе с услугой менеджера
    service_fee: float = 0.0          # услуга менеджера внутри price
    payment_method: str | None = None  # cash | online | free
    autorenew: bool | None = None
    cash_outstanding: float | None = None
    key_username: str | None = None
    key_fingerprint: str | None = None
    temp_deleted_at: datetime | None = None
    subscription_url: str | None = None  # только менеджеру


def _short_name(name: str) -> str:
    """«Иван Петров» → «Иван П.» — в группе фамилия не нужна."""
    parts = (name or "").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0] if parts else "—"


def _traffic_line(data: ReceiptData) -> str:
    parts = []
    if data.traffic_gb is not None:
        parts.append("📊 Безлимитный трафик" if data.traffic_gb == 0 else f"📊 {data.traffic_gb} ГБ/мес")
    if data.devices_limit is not None:
        parts.append(f"📱 до {data.devices_limit} устр.")
    return " · ".join(parts)


def _status_line(data: ReceiptData) -> str:
    if data.status == "completed":
        if data.kind == KIND_TEMP and data.temp_deleted_at:
            return f"🗑 Ключ удалён {fmt_time(data.temp_deleted_at)} — больше не действует"
        return "✅ Ключ выдан"
    if data.status == "pending_payment":
        return "⏳ Ждём оплату"
    if data.status == "cancelled":
        return "🚫 Отменено"
    if data.status == "refunded":
        return "↩️ Оплата возвращена клиенту"
    return "❌ Не выдан (ошибка)"


def _payment_line(data: ReceiptData) -> str | None:
    if data.payment_method == "online":
        renew = " · автопродление ✅" if data.autorenew else (" · автопродление ❌" if data.autorenew is False else "")
        return f"💳 Онлайн (ЮKassa){renew}"
    if data.payment_method == "cash":
        outstanding = (
            f" · к сдаче у менеджера: {fmt_money(data.cash_outstanding)}"
            if data.cash_outstanding is not None else ""
        )
        return f"💵 Наличные{outstanding}"
    return None


def money_lines(data: ReceiptData) -> list[tuple[str, float]]:
    """Из чего сложилась сумма: [(название, ₽)]. Без услуги менеджера — пусто, хватает «Итого»."""
    if not data.service_fee:
        return []
    return [("Подписка", data.price - data.service_fee), (SERVICE_FEE_TITLE, data.service_fee)]


def _money_block(data: ReceiptData) -> list[str]:
    lines = [f"▫️ {title}: {fmt_money(amount)}" for title, amount in money_lines(data)]
    lines.append(f"💰 <b>Итого: {fmt_money(data.price)}</b>")
    return lines


def _body(data: ReceiptData, *, for_group: bool) -> list[str]:
    client = escape(data.client_code) if data.client_code else "—"
    new_mark = " (новый)" if data.client_is_new else ""
    lines = [
        f"🧾 <b>Чек № {receipt_number(data.op_id)}</b> · {fmt_dt(data.created_at)} МСК",
        f"👔 Менеджер: {escape(_short_name(data.manager_name))} (#{data.manager_id})",
        f"👤 Клиент: <code>{client}</code>{new_mark}"
        # Пометка менеджера («Анна, кофейня») — только ему самому, в общий чат не уходит.
        + (f" · {escape(data.client_label)}" if data.client_label and not for_group else ""),
    ]

    if data.kind == KIND_TEMP:
        lines.append("⏱ <b>Пробный ключ</b>")
        lines.append(
            f"Действует {fmt_time(data.created_at)} → {fmt_time(data.expires_at)}, "
            "затем удаляется автоматически · бесплатно"
        )
    else:
        title = escape(data.product_title)
        days = f" · {data.days} дн." if data.days else ""
        lines.append(f"📦 {title}{days}")
        if data.custom_note:
            lines.append(f"<i>{escape(data.custom_note)}</i>")

    extras = _traffic_line(data)
    if extras:
        lines.append(extras)
    if data.kind != KIND_TEMP and data.expires_at:
        lines.append(f"📅 Действует до {fmt_dt(data.expires_at)}")

    if data.kind != KIND_TEMP:
        lines.extend(_money_block(data))
        payment = _payment_line(data)
        if payment:
            lines.append(payment)

    # Имя ключа и отпечаток — для сверки админом в общем чате; менеджеру это ничего не говорит.
    if data.key_username and for_group:
        fingerprint = f" · …{escape(data.key_fingerprint)}" if data.key_fingerprint else ""
        lines.append(f"🔑 Ключ: <code>{escape(data.key_username)}</code>{fingerprint}")
    lines.append(_status_line(data))
    return lines


def format_group_receipt(data: ReceiptData) -> str:
    """Чек для служебной группы. Ссылки подписки здесь быть не может — даже если её передали."""
    return "\n".join(_body(data, for_group=True))


def format_manager_receipt(data: ReceiptData) -> str:
    """Чек менеджеру: то же + ссылка для установки."""
    lines = _body(data, for_group=False)
    if data.subscription_url and data.status == "completed" and not data.temp_deleted_at:
        lines.append(f"🔗 Ссылка для установки:\n<code>{escape(data.subscription_url)}</code>")
    return "\n".join(lines)


def format_client_receipt(data: ReceiptData) -> str:
    """Короткий чек клиенту с Telegram."""
    title = escape(data.product_title)
    days = f" · {data.days} дн." if data.days else ""
    lines = [
        f"🧾 <b>Чек № {receipt_number(data.op_id)}</b> · {fmt_dt(data.created_at)} МСК",
        f"📦 {title}{days}",
    ]
    if data.expires_at:
        lines.append(f"📅 Действует до {fmt_dt(data.expires_at)}")
    lines.extend(f"▫️ {title}: {fmt_money(amount)}" for title, amount in money_lines(data))
    lines.append(f"💰 Сумма: <b>{fmt_money(data.price)}</b>")
    if data.service_fee:
        lines.append("<i>Автопродление списывает только стоимость подписки.</i>")
    return "\n".join(lines)
