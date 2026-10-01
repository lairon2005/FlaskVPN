# tgbot/handlers/user/manager_code.py
"""
Клиент и менеджер: «Код для менеджера» и закрытие доступа.

Менеджер видит клиента только после того, как тот сам продиктовал одноразовый код
(или менеджер сам создал клиента). Код живёт 15 минут и срабатывает один раз.
Доступ можно закрыть в любой момент — в том числе из уведомления «менеджер
получил доступ».
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from tgbot.keyboards.inline import back_to_main_menu_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_service import ACCESS_CODE_TTL_MINUTES
from utils.telegram_ui import replace_message_text

manager_code_router = Router()


@manager_code_router.callback_query(F.data == "mgr_client_code")
async def client_code_handler(call: CallbackQuery):
    await call.answer()
    code = await manager_service.create_access_code(call.from_user.id)
    await replace_message_text(
        call.message,
        "👔 <b>Код для менеджера</b>\n\n"
        f"<code>{code}</code>\n\n"
        f"Продиктуйте его менеджеру — он увидит срок вашей подписки, трафик и число устройств, "
        "чтобы помочь с установкой или продлить подписку. Платёжные данные и личная информация "
        f"ему недоступны. Код одноразовый и действует {ACCESS_CODE_TTL_MINUTES} минут.",
        reply_markup=back_to_main_menu_keyboard(),
    )


@manager_code_router.callback_query(F.data == "mgr_client_revoke")
async def client_revoke_handler(call: CallbackQuery):
    revoked = await manager_service.revoke_access_by_client(call.from_user.id)
    await call.answer(
        "Доступ менеджера закрыт." if revoked else "Активного доступа уже нет.", show_alert=True,
    )
