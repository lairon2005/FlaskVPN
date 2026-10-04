"""Пробные (временные) ключи: выдача, список живых, оформление подписки на такой ключ."""
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from tgbot.handlers.manager.common import (
    current_manager, new_nonce, send_receipt, show, show_error,
)
from tgbot.handlers.manager.issue import _show_what
from tgbot.handlers.manager.menu import manager_router
from tgbot.keyboards.manager import temp_confirm_keyboard, temp_list_keyboard, temp_result_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_time, receipt_number
from tgbot.services.manager_service import ManagerError


@manager_router.callback_query(F.data == "mgr:temp")
async def temp_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    manager = await current_manager(call)
    minutes, gb, devices = await manager_service.temp_settings()
    await state.update_data(temp_nonce=new_nonce())
    await show(
        call,
        "⏱ <b>Пробный ключ</b>\n\n"
        "Дайте клиенту попробовать VPN прямо сейчас — бесплатно.\n\n"
        f"• работает <b>{minutes} мин</b>, затем удаляется сам\n"
        f"• до <b>{gb} ГБ</b> трафика, <b>{devices}</b> устройство\n"
        f"• не больше {manager.temp_keys_per_day} в день, каждый — в журнале\n\n"
        "Понравилось — оформите подписку на этот же ключ, переустанавливать ничего не придётся. "
        "За 10 минут до конца бот напомнит.",
        temp_confirm_keyboard(),
    )


@manager_router.callback_query(F.data == "mgr:temp_go")
async def temp_go(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    nonce = data.get("temp_nonce")
    if not nonce:
        await call.answer("Сессия устарела — откройте «Пробный ключ» заново.", show_alert=True)
        return
    await call.answer("⏳ Создаю ключ…")
    manager = await current_manager(call)
    try:
        result = await manager_service.issue_temp(manager.id, nonce)
    except ManagerError as e:
        await show_error(call, e)
        return
    await state.clear()

    # Чек одним сообщением: картинка с QR установки — её и показывают клиенту.
    text = result.receipt_text or (
        f"✅ <b>Пробный ключ выдан</b> · чек № {receipt_number(result.operation_id)}\n\n"
        f"⏱ Работает до <b>{fmt_time(result.expires_at)}</b> МСК, затем удаляется сам.\n\n"
        f"🔗 Ссылка для установки:\n<code>{result.subscription_url}</code>"
    )
    await send_receipt(call, result.operation_id, text, result.receipt_image, temp_result_keyboard(result.key_id))


@manager_router.callback_query(F.data == "mgr:temps")
async def temp_list(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    try:
        keys = await manager_service.list_temp_keys(manager.id)
    except ManagerError as e:
        await show_error(call, e)
        return
    if not keys:
        await show(call, "Сейчас нет действующих пробных ключей.", temp_list_keyboard([]))
        return
    for k in keys:
        k["until"] = fmt_time(k["expires_at"])
    lines = [f"• выдан, работает до {k['until']}" + (" — оформляется подписка" if k["status"] == "converting" else "")
             for k in keys]
    await show(call, "📋 <b>Мои пробные ключи</b>\n\n" + "\n".join(lines),
               temp_list_keyboard([k for k in keys if k["status"] == "active"]))


@manager_router.callback_query(F.data.startswith("mgr:conv:"))
async def convert_start(call: CallbackQuery, state: FSMContext):
    """Подписка на временный ключ: дальше обычный выбор «что выдаём» — клиентом станет владелец ключа."""
    await call.answer()
    await state.clear()
    await state.update_data(temp_key_id=int(call.data.rsplit(":", 1)[1]), client_code=None)
    await _show_what(call, state)
