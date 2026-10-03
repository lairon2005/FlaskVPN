# tgbot/handlers/user/trial_sub.py

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery

from loader import logger
from database import user_repo, channel_repo
from tgbot.services import subscription_service
from tgbot.keyboards.inline import channels_subscribe_keyboard, main_menu_keyboard
from tgbot.handlers.user.start import ORGANIC_TRIAL_DAYS, _days_word, show_connect_step
from tgbot.services.subscription import check_subscription

trial_sub_router = Router()


async def give_trial_subscription(user_id: int, message):
    """Активирует триал за подписку на каналы и сразу показывает подключение (страница подписки)."""
    trial_days = ORGANIC_TRIAL_DAYS

    try:
        await subscription_service.activate_trial(user_id, trial_days)
        logger.info(f"Successfully activated trial subscription ({trial_days} days) for user {user_id}.")
    except Exception as e:
        logger.error(f"Failed to give trial subscription to user {user_id}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при активации вашего пробного периода. Пожалуйста, обратитесь в поддержку.")
        return

    await show_connect_step(
        message, user_id,
        f"🎉 <b>Поздравляем!</b> Вы получили пробную подписку на <b>{trial_days} {_days_word(trial_days)}</b>.",
        edit=True,
    )


@trial_sub_router.callback_query(F.data == "start_trial_process")
async def start_trial_process_handler(call: CallbackQuery, bot: Bot):
    """
    Запускает процесс получения пробной подписки после нажатия на кнопку.
    Включает проверку на повторное получение.
    """
    user_id = call.from_user.id

    # 1. Получаем пользователя из БД
    user = await user_repo.get(user_id)

    # 2. Проверяем, получал ли он уже триал
    if user and user.has_received_trial:
        await call.answer("Вы уже использовали свой пробный период.", show_alert=True)
        await call.message.edit_text(
            f"👋 Привет, <b>{call.from_user.full_name}</b>!",
            reply_markup=main_menu_keyboard()
        )
        return

    # 3. Проверяем подписку на каналы
    is_subscribed = await check_subscription(bot, user_id)
    if is_subscribed:
        # Если подписан, сразу выдаем триал
        await call.answer("Проверка пройдена! Активируем пробный период...", show_alert=True)
        await give_trial_subscription(user_id, call.message)
    else:
        # Если не подписан, показываем каналы
        channels = await channel_repo.get_all()
        if not channels:
            logger.warning(f"User {user_id} is starting trial, but no channels are in DB. Giving trial immediately.")
            await call.answer("Активируем пробный период...", show_alert=True)
            await give_trial_subscription(user_id, call.message)
            return

        keyboard = channels_subscribe_keyboard(channels)
        await call.message.edit_text(
            "❗️ <b>Для получения пробного периода, пожалуйста, подпишитесь на наши каналы.</b>\n\n"
            "После подписки нажмите кнопку «Проверить» ниже.",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

# --- ХЕНДЛЕР ДЛЯ КНОПКИ ПРОВЕРКИ ---
@trial_sub_router.callback_query(F.data == "check_subscription")
async def handle_check_subscription(call: CallbackQuery, bot: Bot):
    user_id = call.from_user.id

    user = await user_repo.get(user_id)
    if user and user.has_received_trial:
        await call.answer("Вы уже активировали свою подписку.", show_alert=True)
        await call.message.delete()
        await call.message.answer("Воспользуйтесь главным меню.", reply_markup=main_menu_keyboard())
        return

    is_subscribed = await check_subscription(bot, user_id)

    if is_subscribed:
        await call.answer("✅ Отлично! Спасибо за подписку. Активируем пробный период...", show_alert=True)
        await give_trial_subscription(user_id, call.message)
    else:
        await call.answer("Вы еще не подписались на все каналы. Пожалуйста, попробуйте снова.", show_alert=True)
