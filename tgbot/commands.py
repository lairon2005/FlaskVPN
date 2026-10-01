# tgbot/commands.py
"""Наборы команд меню бота: обычные, админские, менеджерские — и их персональная установка."""
from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

from loader import config, logger

USER_COMMANDS = [
    BotCommand(command='start', description='🏠 Главное меню'),
    BotCommand(command='profile', description='👤 Мой профиль'),
    BotCommand(command='support', description='💬 Поддержка'),
    BotCommand(command='referral', description='🤝 Реф. программа'),
    BotCommand(command='instruction', description='📲 Инструкция'),
    BotCommand(command='promo', description='🎁Ввести промокод'),
]
ADMIN_EXTRA = [
    BotCommand(command='admin', description='👑 Админ-панель'),
    BotCommand(command='cancel', description='❌ Отменить действие'),
]
MANAGER_EXTRA = [BotCommand(command='manager', description='👔 Панель менеджера')]


def commands_for(telegram_id: int, is_manager: bool) -> list[BotCommand]:
    commands = list(USER_COMMANDS)
    if telegram_id in config.tg_bot.admin_ids:
        commands += ADMIN_EXTRA
    if is_manager:
        commands += MANAGER_EXTRA
    return commands


async def apply_commands(bot: Bot, telegram_id: int, is_manager: bool) -> None:
    """Выставляет личное меню команд человеку. Сбой Telegram не должен ломать операцию админа."""
    try:
        await bot.set_my_commands(commands_for(telegram_id, is_manager), BotCommandScopeChat(chat_id=telegram_id))
    except Exception as e:
        logger.warning(f"Could not set commands for {telegram_id}: {e}")
