"""Временные ключи: выдача, список живых, оформление подписки на такой ключ."""
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from tgbot.handlers.manager.common import (
    current_manager, new_nonce, qr_file, show, show_error,
)
from tgbot.handlers.manager.issue import _show_what
from tgbot.handlers.manager.menu import manager_router
from tgbot.keyboards.manager import temp_confirm_keyboard, temp_list_keyboard, temp_result_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_time
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
        "⏱ <b>Временный ключ</b>\n\n"
        f"Работает <b>{minutes} мин</b>, до <b>{gb} ГБ</b>, <b>{devices}</b> устройство. "
        "Потом удаляется автоматически и перестаёт действовать.\n"
        "Подходит, чтобы клиент попробовал VPN на месте. Бесплатно, но каждый ключ записывается "
        f"в журнал и в чат чеков. Лимит: {manager.temp_keys_per_day} в день.\n\n"
        "Понравилось — оформите подписку на этот же ключ, переустанавливать ничего не придётся.",
        temp_confirm_keyboard(),
    )


@manager_router.callback_query(F.data == "mgr:temp_go")
async def temp_go(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    nonce = data.get("temp_nonce")
    if not nonce:
        await call.answer("Сессия устарела — откройте «Временный ключ» заново.", show_alert=True)
        return
    await call.answer("⏳ Создаю ключ…")
    manager = await current_manager(call)
    try:
        result = await manager_service.issue_temp(manager.id, nonce)
    except ManagerError as e:
        await show_error(call, e)
        return
    await state.clear()

    text = (
        f"✅ <b>Временный ключ выдан</b> · чек № {result.operation_id}\n\n"
        f"⏱ Действует до <b>{fmt_time(result.expires_at)}</b> МСК, затем удаляется.\n\n"
        f"🔗 Ссылка для установки:\n<code>{result.subscription_url}</code>"
    )
    await show(call, text, temp_result_keyboard(result.key_id))
    if result.subscription_url:
        await call.message.answer_photo(
            qr_file(result.subscription_url, "temp.png"),
            caption="📲 QR для установки — клиент сканирует его в приложении",
        )


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
        await show(call, "Активных временных ключей нет.", temp_list_keyboard([]))
        return
    lines = [f"• …{k['fingerprint']} — до {fmt_time(k['expires_at'])}"
             + (" (оформляется)" if k["status"] == "converting" else "") for k in keys]
    await show(call, "📋 <b>Активные временные ключи</b>\n\n" + "\n".join(lines),
               temp_list_keyboard([k for k in keys if k["status"] == "active"]))


@manager_router.callback_query(F.data.startswith("mgr:conv:"))
async def convert_start(call: CallbackQuery, state: FSMContext):
    """Подписка на временный ключ: дальше обычный выбор «что выдаём» — клиентом станет владелец ключа."""
    await call.answer()
    await state.clear()
    await state.update_data(temp_key_id=int(call.data.rsplit(":", 1)[1]), client_code=None)
    await _show_what(call, state)
