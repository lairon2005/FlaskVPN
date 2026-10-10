"""Общие хелперы панели менеджера."""
import datetime
import uuid

from aiogram.types import BufferedInputFile, CallbackQuery, Message

from loader import config
from tgbot.services import manager_service
from tgbot.services.manager_notifier import fit_caption
from tgbot.services.manager_receipts import fmt_dt, fmt_money, receipt_number
from tgbot.services.manager_service import ManagerError, ManagerView
from tgbot.services.qr_generator import create_qr_code
from utils.telegram_ui import replace_message_text

OP_TITLES = {
    "issue_tariff": "Подписка по тарифу", "issue_custom": "Подписка на любой срок", "issue_temp": "Пробный ключ",
    "convert_temp": "Подписка на пробный ключ", "access_grant": "Доступ к клиенту", "key_view": "Показан QR установки",
    "autorenew_off": "Автопродление выкл.", "cabinet_link_reset": "Новая ссылка на кабинет",
}
STATUS_ICONS = {"completed": "✅", "pending_payment": "⏳", "cancelled": "🚫", "failed": "❌", "processing": "⚙️",
                "refunded": "↩️"}


def site_url(path: str) -> str:
    return f"https://{config.webhook.domain}{path}"


def password_link_text(token: str) -> tuple[str, str]:
    """Текст и адрес ссылки «задать пароль» — одинаковые в меню менеджера, приглашении и сбросе админом."""
    url = site_url(f"/manager/password?t={token}")
    text = (
        "🔑 <b>Пароль для входа на сайт</b>\n\n"
        "Нажмите кнопку ниже и придумайте пароль (не короче 8 символов). "
        "Ссылка одноразовая и действует 30 минут — не пересылайте её никому."
    )
    return text, url


async def current_manager(event: Message | CallbackQuery) -> ManagerView:
    """
    Менеджер, который жмёт кнопку, — из БД прямо сейчас. Не менеджер (заблокирован или удалён
    уже ПОСЛЕ того, как фильтр IsManager его пропустил по кэшу) — ManagerError, которую
    ловит общий обработчик ошибок роутера: человек видит отказ, а не падение хендлера.
    """
    manager = await manager_service.view_by_telegram(event.from_user.id)
    if manager is None:
        raise ManagerError("blocked")
    return manager


async def show(event: Message | CallbackQuery, text: str, markup=None):
    """Показывает экран: правит сообщение кнопки либо отвечает на сообщение."""
    if isinstance(event, CallbackQuery):
        return await replace_message_text(event.message, text, reply_markup=markup)
    return await event.answer(text, reply_markup=markup)


async def show_error(event: Message | CallbackQuery, error: ManagerError, markup=None):
    await show(event, f"❌ {error.message}", markup)


def new_nonce() -> str:
    return uuid.uuid4().hex[:24]


def qr_file(data: str, name: str = "qr.png") -> BufferedInputFile:
    return BufferedInputFile(create_qr_code(data).getvalue(), filename=name)


async def send_receipt(call: CallbackQuery, operation_id: int, text: str, image: bytes | None, markup=None):
    """
    Чек одним сообщением: картинка + текст подписью. Экран, с которого жали «Подтвердить»,
    убираем — чек встаёт на его место последним сообщением, под ним кнопки «что дальше».
    """
    try:
        await call.message.delete()
    except Exception:
        pass
    if image:
        return await call.message.answer_photo(
            BufferedInputFile(image, filename=f"check-{receipt_number(operation_id)}.png"),
            caption=fit_caption(text), reply_markup=markup,
        )
    return await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def format_brief(op) -> str:
    icon = STATUS_ICONS.get(op.status, "•")
    title = OP_TITLES.get(op.op_type, op.op_type)
    parts = [f"{icon} <b>#{op.id}</b> {fmt_dt(op.created_at)} — {title}"]
    detail = []
    if op.client_code:
        detail.append(f"клиент <code>{op.client_code}</code>")
    if op.tariff_name:
        detail.append(op.tariff_name + (f" · {op.days} дн." if op.days else ""))
    if op.price:
        method = {"cash": "нал.", "online": "онлайн"}.get(op.payment_method or "", "")
        detail.append(f"{fmt_money(op.price)} {method}".strip())
    if detail:
        parts.append("   " + " · ".join(detail))
    return "\n".join(parts)


def quote_text(quote, *, client_label: str) -> str:
    """Предпросмотр выдачи: что получит клиент, из чего сложилась цена, подсказка, согласие."""
    quota = "безлимитный трафик" if quote.quota_gb == 0 else f"{quote.quota_gb + quote.extra_traffic_gb} ГБ/мес"
    lines = [
        "🧾 <b>Проверьте и выберите оплату</b>\n",
        f"👤 Клиент: {client_label}",
        f"📦 {quote.tariff_name} · <b>{quote.days} дн.</b>",
        f"📊 {quota} · 📱 до {quote.devices_limit} устр.",
    ]
    if quote.breakdown:
        lines.append(f"<i>{quote.breakdown}</i>")
    if quote.slots:
        lines.append(f"+ доп. устройства: {quote.slots} шт. — {fmt_money(quote.slots_cost)}")
    if quote.packs:
        lines.append(f"+ доп. трафик: +{quote.extra_traffic_gb} ГБ — {fmt_money(quote.traffic_cost)}")
    if quote.service_fee:
        lines.append(f"+ ваша услуга (подключение и настройка): {fmt_money(quote.service_fee)}")
    lines.append(f"\n💰 <b>К оплате: {fmt_money(quote.total)}</b>")
    if quote.service_fee:
        lines.append(f"📱 По QR: онлайн {fmt_money(quote.subscription_total)} за подписку, "
                     f"услугу {fmt_money(quote.service_fee)} клиент отдаёт вам наличными.")
    if quote.hint:
        h = quote.hint
        lines.append(
            f"\n💡 Выгоднее стандартный тариф «{h.name}» — {h.days} дн. за {fmt_money(h.price)} "
            f"(на {fmt_money(h.saving)} дешевле) и с автопродлением."
        )
    if quote.renew_text:
        lines.append(f"\n♻️ <i>{quote.renew_text}</i>")
    return "\n".join(lines)


def left_text(expires_at: datetime.datetime) -> str:
    return fmt_dt(expires_at)
