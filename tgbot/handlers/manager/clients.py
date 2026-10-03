"""Клиенты менеджера: список, карточка (белый список полей), QR для установки, пометка."""
from html import escape

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tgbot.handlers.manager.common import current_manager, format_brief, show, show_error
from tgbot.handlers.manager.menu import manager_router
from tgbot.keyboards.manager import PAGE, client_card_keyboard, clients_keyboard, receipt_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_dt
from tgbot.services.manager_guide import install_caption
from tgbot.services.manager_service import LABEL_MAX, ManagerError
from tgbot.states.manager_states import ManagerFSM
from tgbot.keyboards.manager import cancel_keyboard
from tgbot.handlers.manager.common import qr_file


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
        "🟢 подписка активна · ⚪️ закончилась\nНажмите на клиента, чтобы продлить или показать QR." if rows else
        "Пока никого. Клиент появится, когда вы продадите подписку или добавите его по коду."
    )
    await show(call, text, clients_keyboard(rows, page, total))


def _traffic(card) -> str:
    if card.traffic_limit_gb is None:
        return "нет данных"
    if card.traffic_limit_gb == 0:
        return f"{card.traffic_used_gb or 0} ГБ (безлимит)"
    return f"{card.traffic_used_gb or 0} из {card.traffic_limit_gb} ГБ"


async def _show_card(event, manager, code: str):
    try:
        card = await manager_service.get_client_card(manager.id, code)
    except ManagerError as e:
        await show_error(event, e)
        return

    status = "🟢 активна" if card.subscription_active else "🔴 закончилась"
    lines = [
        f"👤 <b>Клиент <code>{card.client_code}</code></b>" + (f" · {escape(card.label)}" if card.label else ""),
        f"📋 Подписка: {status}",
        f"📅 До: {fmt_dt(card.subscription_end)}" + (f" (ещё {card.days_left} дн.)" if card.subscription_active else ""),
        f"📱 Устройства: {card.devices_used if card.devices_used is not None else '—'} из {card.devices_limit}"
        + (f" ({', '.join(card.device_platforms)})" if card.device_platforms else ""),
        f"📊 Трафик: {_traffic(card)}",
    ]
    if card.extra_devices or card.extra_traffic_gb:
        lines.append(f"➕ Докуплено: {card.extra_devices} устр., {card.extra_traffic_gb} ГБ")
    if card.access_until:
        lines.append(f"🔒 Клиент виден вам до {fmt_dt(card.access_until)}")
    if card.operations:
        lines.append("\n<b>Ваши операции по клиенту:</b>")
        lines.extend(format_brief(op) for op in card.operations)
    await show(
        event, "\n".join(lines),
        client_card_keyboard(card.client_code, can_issue=manager.can_issue_tariff or manager.can_issue_custom,
                             has_key=card.has_key),
    )


@manager_router.callback_query(F.data.startswith("mgr:c:"))
async def client_card(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(None)
    manager = await current_manager(call)
    await _show_card(call, manager, call.data.split(":", 2)[2])


@manager_router.callback_query(F.data.startswith("mgr:lbl:"))
async def label_edit_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    code = call.data.split(":", 2)[2]
    await state.set_state(ManagerFSM.edit_label)
    await state.update_data(label_client=code)
    await show(
        call,
        f"📝 <b>Пометка о клиенте <code>{escape(code)}</code></b>\n\n"
        f"Напишите, как вы запомните клиента (до {LABEL_MAX} символов). Чтобы убрать пометку, отправьте «-».\n\n"
        "Пометку видите только вы и администратор.",
        cancel_keyboard(f"mgr:c:{code}"),
    )


@manager_router.message(ManagerFSM.edit_label)
async def label_edit_apply(message: Message, state: FSMContext):
    manager = await current_manager(message)
    code = (await state.get_data()).get("label_client")
    await state.set_state(None)
    raw = " ".join((message.text or "").split())
    try:
        await manager_service.set_client_label(manager.id, code, None if raw == "-" else raw[:LABEL_MAX] or None)
    except ManagerError as e:
        await message.answer(f"❌ {e.message}")
        return
    await _show_card(message, manager, code)


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
    await call.message.answer_photo(
        qr_file(url, "key.png"),
        caption=install_caption() + f"\n\n🔗 <code>{url}</code>",
        reply_markup=receipt_keyboard(has_key=True, client_code=code),
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
    await call.message.answer_photo(
        qr_file(url, "cabinet.png"),
        caption=f"🔗 <b>Новая ссылка на личный кабинет клиента <code>{code}</code></b>\n\n"
                "Старая больше не работает. Покажите QR клиенту сейчас — потом эту ссылку не восстановить.\n\n"
                f"<code>{url}</code>",
    )
