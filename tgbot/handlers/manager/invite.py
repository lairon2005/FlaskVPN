"""Принятие приглашения менеджера: /start mgr_<token>. Роутер без IsManager — человек ещё не менеджер."""
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from loader import logger
from tgbot.commands import apply_commands
from tgbot.filters.manager import forget
from tgbot.handlers.manager.common import password_link_text, site_url
from tgbot.keyboards.manager import manager_menu_keyboard, password_link_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_service import ManagerError

manager_invite_router = Router(name="manager_invite")
manager_invite_router.message.filter(F.chat.type == ChatType.PRIVATE)


@manager_invite_router.message(CommandStart(), F.text.regexp(r"^/start\s+mgr_\S+"))
async def accept_invite(message: Message, bot: Bot):
    token = message.text.split(maxsplit=1)[1].removeprefix("mgr_")
    try:
        manager = await manager_service.accept_invite(token, message.from_user.id)
    except ManagerError as e:
        await message.answer(f"❌ {e.message}")
        return
    forget(message.from_user.id)
    await apply_commands(bot, message.from_user.id, is_manager=True)
    logger.info(f"[manager] #{manager.id} ({manager.display_name}) accepted invite via bot")
    await message.answer(
        f"👔 <b>Добро пожаловать, {manager.display_name}!</b>\n\n"
        "Вы — менеджер. Здесь вы выдаёте VPN-ключи клиентам: по тарифу, на нужное число дней "
        "или временный ключ на час. Каждая операция попадает в журнал и в чат чеков.\n\n"
        "Панель всегда доступна по команде /manager.",
        reply_markup=manager_menu_keyboard(
            can_global_stats=manager.can_view_global_stats, can_temp=manager.can_issue_temp,
        ),
    )
    if not manager.login:
        return
    # Сразу — пароль для сайта: логин задал админ, пароль знает только сам менеджер.
    try:
        token = await manager_service.create_password_link(manager.id)
    except ManagerError as e:
        logger.warning(f"[manager] #{manager.id}: no password link after invite: {e.code}")
        return
    text, url = password_link_text(token)
    await message.answer(
        f"{text}\n\nВаш логин: <code>{manager.login}</code>\nВход: {site_url('/manager/login')}",
        reply_markup=password_link_keyboard(url),
    )


# Старые кнопки панели в руках человека, который менеджером быть перестал (заблокирован,
# удалён) или никогда не был. Стоит ПОСЛЕ manager_router: настоящим менеджерам кнопки обработает он.
manager_denied_router = Router(name="manager_denied")


@manager_denied_router.callback_query(F.data.startswith("mgr:"))
async def panel_closed(call: CallbackQuery):
    await call.answer("Доступ к панели менеджера закрыт. Обратитесь к администратору.", show_alert=True)
