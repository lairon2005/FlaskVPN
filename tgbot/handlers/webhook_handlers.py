# tgbot/handlers/webhook_handlers.py

import json
from datetime import datetime
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiohttp import web
from aiogram import Bot, Dispatcher

from tgbot.services import payment_service
from tgbot.services.payment import parse_webhook_notification
from database import user_repo
from loader import logger, config
from remnawave.client import RemnawaveClient
from tgbot.handlers.user.profile import show_profile_logic


async def _notify_tg_user(user_id: int, tariff, marzban: RemnawaveClient, bot: Bot, request: web.Request,
                          is_auto: bool = False, payment_method=None):
    """Уведомление ТОЛЬКО для Telegram пользователей."""
    if user_id < 0:
        return

    try:
        dp: Dispatcher = request.app['dp']
        storage = dp.storage
        state = FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id))

        fsm_data = await state.get_data()
        msg_id = fsm_data.get("payment_message_id")
        if msg_id:
            await bot.edit_message_text(chat_id=user_id, message_id=msg_id, text="✅ <i>Счет оплачен.</i>", reply_markup=None)
        await state.clear()
    except Exception:
        pass

    try:
        if is_auto:
            success_text = (
                f"🔄 Подписка автоматически продлена! Тариф '<b>{tariff.name}</b>' — "
                f"{tariff.duration_days} дней."
            )
        else:
            success_text = f"✅ Оплата успешна! Тариф '<b>{tariff.name}</b>' активирован."
            if payment_method is not None and getattr(payment_method, 'saved', False):
                success_text += (
                    "\n💳 Карта сохранена. Автопродление можно отключить в профиле → «Моя карта»."
                )
        await bot.send_message(user_id, success_text)

        from aiogram.types import User, Chat, Message
        fake_msg = Message(
            message_id=0, date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="User")
        )
        # show_referral_cta=True — момент счастья (§7.5): сразу после успешной
        # оплаты/продления показываем кнопку "Поделиться и получить дни".
        await show_profile_logic(fake_msg, marzban, bot, show_referral_cta=True)
    except Exception as e:
        logger.error(f"Failed to notify TG user {user_id}: {e}")


async def _log_transaction(bot: Bot, user_id: int, tariff_name: str, price: float,
                           is_new: bool, payment=None):
    """Логирование в админ-чат с информацией о скидке."""
    user = await user_repo.get(user_id)
    if not user:
        return

    source_icon = "🌐 WEB" if user_id < 0 else "🤖 BOT"
    action = "💎 Новая подписка" if is_new else "🔄 Продление"
    username_text = f"@{user.username}" if user.username else "Нет"

    # Формируем строку суммы с учётом скидки
    if payment and payment.discount_percent > 0:
        amount_text = (
            f"{payment.final_amount:.2f} RUB "
            f"(скидка {payment.discount_percent}%, промокод {payment.promo_code}, "
        )
    else:
        amount_text = f"{price:.2f} RUB"

    text = (
        f"{source_icon} | {action}\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"💳 <b>Тариф:</b> {tariff_name}\n"
        f"💰 <b>Сумма:</b> {amount_text}"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send transaction log: {e}")


async def _log_refund(bot: Bot, payment):
    """Логирование возврата в админ-чат."""
    user = await user_repo.get(payment.user_id)
    if not user:
        return

    username_text = f"@{user.username}" if user.username else "Нет"

    text = (
        f"💸 <b>ВОЗВРАТ</b>\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"💰 <b>Сумма возврата:</b> {payment.final_amount:.2f} RUB\n"
        f"🆔 <b>YooKassa ID:</b> <code>{payment.yookassa_payment_id}</code>"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send refund log: {e}")


async def _notify_slots_purchased(bot: Bot, result) -> None:
    """Слоты начислены — сообщаем новый лимит и ведём на экран устройств."""
    from tgbot.keyboards.inline import back_to_main_menu_keyboard

    bought = result.payment.extra_devices or 0
    try:
        await bot.send_message(
            result.payment.user_id,
            f"✅ <b>Оплата прошла</b>\n\n"
            f"Добавлено доп. устройств: <b>{bought}</b>.\n"
            f"Всего оплачено дополнительных слотов: <b>{result.extra_devices}</b>.\n\n"
            "Новый лимит уже действует — подключайте устройство, "
            "перевыпускать ключ не нужно.",
            reply_markup=back_to_main_menu_keyboard(),
        )
    except Exception as e:
        logger.error(f"Failed to notify user {result.payment.user_id} about slots: {e}")


async def _log_device_slots_purchase(bot: Bot, result) -> None:
    """Лог докупки устройств в админ-чат."""
    user = await user_repo.get(result.payment.user_id)
    if not user:
        return

    source_icon = "🌐 WEB" if result.payment.user_id < 0 else "🤖 BOT"
    username_text = f"@{user.username}" if user.username else "Нет"

    text = (
        f"{source_icon} | 📱 Докупка устройств\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"➕ <b>Слотов куплено:</b> {result.payment.extra_devices}\n"
        f"📱 <b>Всего доп. слотов:</b> {result.extra_devices}\n"
        f"💰 <b>Сумма:</b> {result.payment.final_amount:.2f} RUB"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send device slots log: {e}")


# --- ГЛАВНЫЙ ХЕНДЛЕР ---
async def yookassa_webhook_handler(request: web.Request):
    try:
        # Тело читаем сырым: на публичный /yookassa часто стучатся сканеры,
        # healthcheck-пробы и «пустые» проверки связи — у них тело пустое или
        # не-JSON. Такое не должно валиться в FATAL и провоцировать ретраи YooKassa.
        raw_body = await request.read()
        if not raw_body:
            logger.warning("YooKassa webhook: пустое тело запроса (healthcheck/проба), игнорируем")
            return web.Response(status=400)

        try:
            request_body = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "YooKassa webhook: не-JSON тело, игнорируем: %r",
                raw_body[:200],
            )
            return web.Response(status=400)

        notification = parse_webhook_notification(request_body)

        if not notification:
            return web.Response(status=400)

        event_type = notification.event
        payment_obj = notification.object
        yookassa_payment_id = payment_obj.id

        bot: Bot = request.app['bot']
        marzban: RemnawaveClient = request.app['marzban']

        # === УСПЕШНАЯ ОПЛАТА ===
        if event_type == 'payment.succeeded':
            paid_amount = float(payment_obj.amount.value)

            payment_method = getattr(payment_obj, 'payment_method', None)
            result = await payment_service.process_successful_payment(
                yookassa_payment_id, paid_amount, payment_method=payment_method
            )
            if not result:
                return web.Response(status=200)

            # Докупка слотов устройств: подписка не менялась, тарифа у платежа
            # нет — свой короткий сценарий уведомления.
            if result.kind == 'devices':
                await _log_device_slots_purchase(bot, result)
                if result.payment.user_id > 0:
                    await _notify_slots_purchased(bot, result)
                return web.Response(status=200)

            # Уведомление реферера (Telegram-специфично)
            if result.referrer_id and result.referrer_id > 0:
                try:
                    await bot.send_message(
                        result.referrer_id,
                        f"🎉 Ваш реферал совершил первую оплату! Вам начислено <b>15 бонусных дней</b>."
                    )
                except Exception as e:
                    logger.error(f"Failed to notify referrer {result.referrer_id}: {e}")

            # Лог транзакции
            await _log_transaction(
                bot, result.payment.user_id, result.tariff.name,
                result.payment.final_amount, result.extension.is_new_user,
                payment=result.payment
            )

            # Уведомление пользователя (только TG)
            if result.payment.user_id > 0:
                await _notify_tg_user(
                    result.payment.user_id, result.tariff, marzban, bot, request,
                    is_auto=(result.payment.source == 'auto'),
                    payment_method=payment_method,
                )

            return web.Response(status=200)

        # === ВОЗВРАТ ===
        elif event_type == 'refund.succeeded':
            refunded_payment = await payment_service.process_refund(yookassa_payment_id)
            if refunded_payment:
                await _log_refund(bot, refunded_payment)
                # Уведомляем пользователя
                if refunded_payment.user_id > 0:
                    try:
                        await bot.send_message(
                            refunded_payment.user_id,
                            f"💸 Произведён возврат средств: <b>{refunded_payment.final_amount:.2f} RUB</b>.\n"
                            "Дни подписки были скорректированы."
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify user about refund: {e}")

            return web.Response(status=200)

        # === ОТМЕНА ===
        elif event_type == 'payment.canceled':
            from database import payment_repo
            payment = await payment_repo.get_by_yookassa_id(yookassa_payment_id)
            await payment_repo.update_status(yookassa_payment_id, 'cancelled')
            logger.info(f"Payment {yookassa_payment_id} cancelled by YooKassa")
            if payment and payment.user_id > 0:
                try:
                    await bot.send_message(
                        payment.user_id,
                        "⏰ Ваш счёт на оплату был автоматически отменён, так как не был оплачен в течение 10 минут.\n\n"
                        "Вы можете создать новый платёж в любое время."
                    )
                except Exception:
                    pass
            return web.Response(status=200)

        else:
            logger.warning(f"Unknown webhook event: {event_type}")
            return web.Response(status=200)

    except Exception as e:
        logger.error(f"FATAL Webhook Error: {e}", exc_info=True)
        return web.Response(status=500)
