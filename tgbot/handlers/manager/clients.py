"""Клиенты менеджера: список, карточка (белый список полей), ссылка для установки."""
from aiogram import F
from aiogram.types import CallbackQuery

from tgbot.handlers.manager.common import current_manager, format_brief, show, show_error
from tgbot.handlers.manager.menu import manager_router
from tgbot.keyboards.manager import PAGE, client_card_keyboard, clients_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_dt
from tgbot.services.manager_service import ManagerError
from tgbot.services.qr_generator import create_qr_code
from aiogram.types import BufferedInputFile


@manager_router.callback_query(F.data.startswith("mgr:clients:"))
async def clients_list(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    page = int(call.data.rsplit(":", 1)[1])
    try:
        rows, total = await manager_service.list_clients(manager.id, page, PAGE)
    except ManagerError as e:
        await show_error(call, e)
        return
    text = f"👥 <b>Мои клиенты</b> ({total})\n\n" + (
        "🟢 подписка активна · ⚪️ закончилась" if rows else
        "Пока никого. Клиент появится, когда вы выдадите ключ или добавите его по коду."
    )
    await show(call, text, clients_keyboard(rows, page, total))


def _traffic(card) -> str:
    if card.traffic_limit_gb is None:
        return "нет данных"
    if card.traffic_limit_gb == 0:
        return f"{card.traffic_used_gb or 0} ГБ (безлимит)"
    return f"{card.traffic_used_gb or 0} из {card.traffic_limit_gb} ГБ"


@manager_router.callback_query(F.data.startswith("mgr:c:"))
async def client_card(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    code = call.data.split(":", 2)[2]
    try:
        card = await manager_service.get_client_card(manager.id, code)
    except ManagerError as e:
        await show_error(call, e)
        return

    status = "🟢 активна" if card.subscription_active else "🔴 закончилась"
    lines = [
        f"👤 <b>Клиент <code>{card.client_code}</code></b>" + (f" · {card.label}" if card.label else ""),
        f"📋 Подписка: {status}",
        f"📅 До: {fmt_dt(card.subscription_end)}" + (f" (ещё {card.days_left} дн.)" if card.subscription_active else ""),
        f"📱 Устройства: {card.devices_used if card.devices_used is not None else '—'} из {card.devices_limit}"
        + (f" ({', '.join(card.device_platforms)})" if card.device_platforms else ""),
        f"📊 Трафик: {_traffic(card)}",
    ]
    if card.extra_devices or card.extra_traffic_gb:
        lines.append(f"➕ Докуплено: {card.extra_devices} устр., {card.extra_traffic_gb} ГБ")
    if card.access_until:
        lines.append(f"🔒 Ваш доступ к клиенту до {fmt_dt(card.access_until)}")
    if card.operations:
        lines.append("\n<b>Ваши операции по клиенту:</b>")
        lines.extend(format_brief(op) for op in card.operations)
    await show(
        call, "\n".join(lines),
        client_card_keyboard(card.client_code, can_issue=manager.can_issue_tariff or manager.can_issue_custom,
                             has_key=card.has_key),
    )


@manager_router.callback_query(F.data.startswith("mgr:link:"))
async def client_link(call: CallbackQuery):
    manager = await current_manager(call)
    code = call.data.split(":", 2)[2]
    try:
        url = await manager_service.get_install_link(manager.id, code)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    await call.message.answer(f"🔗 Ссылка для установки клиента <code>{code}</code>:\n<code>{url}</code>")
    await call.message.answer_photo(
        BufferedInputFile(create_qr_code(url).getvalue(), filename="key.png"),
        caption="📲 QR для установки (просмотр записан в журнал)",
    )


@manager_router.callback_query(F.data.startswith("mgr:noauto:"))
async def client_disable_autorenew(call: CallbackQuery):
    manager = await current_manager(call)
    code = call.data.split(":", 2)[2]
    try:
        done = await manager_service.disable_autorenew(manager.id, code)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer("Автопродление выключено." if done else "Автопродление у клиента не включено.", show_alert=True)


@manager_router.callback_query(F.data.startswith("mgr:cab:"))
async def client_cabinet_reset(call: CallbackQuery):
    """Новая ссылка кабинета для офлайн-клиента, потерявшего ссылку из чека."""
    manager = await current_manager(call)
    code = call.data.split(":", 2)[2]
    try:
        url = await manager_service.reset_cabinet_link(manager.id, code)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        f"🔗 Новая ссылка кабинета клиента <code>{code}</code> (старая больше не работает; "
        f"покажите сейчас — потом её не восстановить):\n<code>{url}</code>"
    )
