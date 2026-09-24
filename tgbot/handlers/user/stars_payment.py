# tgbot/handlers/user/stars_payment.py
"""
Telegram Stars (XTR) — оплата тарифов без выхода из Telegram
(docs/tma-roadmap.md фаза 3.2). Инвойс создаётся в webapp/routers/tma.py
(POST /tma/payment/stars-invoice) через Bot.create_invoice_link; здесь —
обязательные для платежей Bot API хендлеры: pre_checkout_query (последнее
подтверждение перед списанием) и successful_payment (зачисление по факту оплаты).
"""
from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from database import tariff_repo, user_repo
from loader import logger
from tgbot.services import payment_service

stars_payment_router = Router()


def _parse_stars_payload(payload: str) -> tuple[int, int] | None:
    """"stars:{tariff_id}:{user_id}" -> (tariff_id, user_id) или None при битом payload."""
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "stars":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


@stars_payment_router.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery):
    """Telegram ждёт ответ в течение 10 секунд, поэтому проверяем только дешёвые
    вещи (payload, наличие тарифа, совпадение цены) — без похода в Remnawave."""
    parsed = _parse_stars_payload(query.invoice_payload)
    if not parsed:
        await query.answer(ok=False, error_message="Некорректный счёт. Откройте приложение заново.")
        return

    tariff_id, payload_user_id = parsed
    if payload_user_id != query.from_user.id:
        # Инвойс выписывается с payload по user_id из TMA-сессии в момент создания
        # счёта (webapp/routers/tma.py::create_stars_invoice) — штатно всегда
        # совпадает с тем, кто платит. Несовпадение — сигнал испорченного/чужого счёта.
        await query.answer(ok=False, error_message="Этот счёт выписан для другого аккаунта.")
        return

    tariff = await tariff_repo.get_by_id(tariff_id)
    # Вводный тариф без сохранённой карты теряет смысл (переход на полную
    # цену списывается с карты), поэтому Stars для него не принимаем никогда.
    if not tariff or not tariff.price_stars or not tariff.is_active or tariff.is_intro:
        await query.answer(ok=False, error_message="Тариф больше недоступен. Откройте приложение заново.")
        return

    if tariff.price_stars != query.total_amount:
        logger.warning(
            f"Stars pre_checkout price mismatch: tariff={tariff_id} "
            f"expected={tariff.price_stars} got={query.total_amount}, user={payload_user_id}"
        )
        await query.answer(ok=False, error_message="Цена тарифа изменилась. Откройте приложение заново.")
        return

    # Доп. устройства звёздами не продаются: цена в XTR задана только у тарифа.
    # Пропустить такую оплату — значит продлить подписку, а вместе с ней и
    # слоты, за которые в этом периоде никто не заплатил.
    user = await user_repo.get(payload_user_id)
    if user and (user.extra_devices or 0):
        await query.answer(
            ok=False,
            error_message=(
                "У вас подключены дополнительные устройства — такая подписка "
                "продлевается только картой или через СБП."
            ),
        )
        return

    await query.answer(ok=True)


@stars_payment_router.message(F.successful_payment)
async def stars_successful_payment(message: Message):
    payment_info = message.successful_payment
    parsed = _parse_stars_payload(payment_info.invoice_payload)
    if not parsed:
        logger.error(f"successful_payment с нераспарсиваемым payload: {payment_info.invoice_payload!r}")
        return

    tariff_id, payload_user_id = parsed
    user_id = message.from_user.id
    if payload_user_id != user_id:
        logger.error(
            f"successful_payment payload user mismatch: payload={payload_user_id}, "
            f"from_user={user_id}, charge={payment_info.telegram_payment_charge_id}"
        )
        return

    result = await payment_service.process_stars_payment(
        user_id=user_id,
        tariff_id=tariff_id,
        total_amount=payment_info.total_amount,
        telegram_payment_charge_id=payment_info.telegram_payment_charge_id,
    )
    if result is None:
        # Дубликат апдейта (Telegram может повторно доставить тот же successful_payment)
        # или тариф пропал за время между инвойсом и оплатой — причина уже залогирована
        # в process_stars_payment. Деньги списаны Telegram-ом один раз в любом случае.
        return

    await message.answer(
        f"✅ Оплата успешна! Тариф '<b>{result.tariff.name}</b>' активирован на "
        f"{result.tariff.duration_days} дней.\n\n⭐ Списано: {payment_info.total_amount} Stars."
    )
