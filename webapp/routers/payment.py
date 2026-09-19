# webapp/routers/payment.py
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tgbot.services.payment import create_payment
from tgbot.services import (
    payment_service, promo_service, subscription_service, device_slot_service,
)
from tgbot.services.pricing import effective_price
from tgbot.services.device_pricing import build_checkout, receipt_items
from tgbot.services.promo_code_service import PromoClaimError
from db import User, Tariff
from database import tariff_repo
from webapp.dependencies import get_current_user
from config import load_config

router = APIRouter(prefix="/payment")
config = load_config()
logger = logging.getLogger(__name__)


class PaymentRequest(BaseModel):
    tariff_name: str
    price: float
    promo_code: str | None = None
    discount_percent: int = 0
    # Источник запроса (docs/tma-roadmap.md фаза 3.1): "web" — сайт (дефолт,
    # обратная совместимость), "tma" — Telegram Mini App. Literal даёт FastAPI
    # автоматическую валидацию (422 на любое другое значение) без ручного validator'а.
    source: Literal["web", "tma"] = "web"
    # Сколько доп. устройств оплачивается вместе с тарифом. Скидка на них не
    # распространяется — см. build_checkout.
    extra_devices: int = 0


@router.post("/create")
async def create_payment_route(
    payload: PaymentRequest,
    user: User = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Проверка на дубликат pending-платежа
    if await payment_service.has_pending_payment(user.user_id):
        raise HTTPException(
            status_code=409,
            detail="У вас уже есть неоплаченный счёт. Дождитесь его отмены (30 мин) или завершите оплату."
        )

    # Находим тариф в БД по имени и цене
    tariff = await tariff_repo.get_by_name_and_price(payload.tariff_name, payload.price)
    if not tariff:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    # Рассчитываем финальную цену: сначала эффективная цена (лоялти при непрерывном
    # продлении, §7.1), затем поверх неё — промо-скидка.
    user_has_active_sub = bool(user.subscription_end_date and user.subscription_end_date > datetime.now())
    original_price = effective_price(tariff, user_has_active_sub)

    # Доп. устройства: количество приходит с клиента, поэтому режем его по
    # серверному потолку — иначе подобранный запрос выдал бы лимит выше продаваемого.
    device_settings = await device_slot_service.settings()
    slots = max(0, min(device_settings.max_extra, payload.extra_devices))
    checkout = build_checkout(
        device_settings, original_price, tariff.duration_days, slots, payload.discount_percent
    )
    final_price = checkout.total

    # return_url: для TMA — обратно в Telegram (юзер не должен "повиснуть" во
    # внешнем браузере, см. docs/tma-roadmap.md фаза 3.1 + риски), для сайта —
    # прежнее поведение (/profile/). Ссылка t.me доступна только если бот
    # сконфигурирован (TG_BOT_USERNAME) — иначе фолбэк на /profile/.
    if payload.source == "tma" and config.tg_bot.tg_bot_username:
        return_url = f"https://t.me/{config.tg_bot.tg_bot_username}/{config.tg_bot.tma_app_name}?startapp=paid"
    else:
        return_url = f"https://{config.webhook.domain}/profile/"

    try:
        payment_url, yookassa_payment_id = create_payment(
            amount=final_price,
            description=f"Подписка {tariff.name} (Web)" + (f" (скидка {payload.discount_percent}%)" if payload.discount_percent else ""),
            return_url=return_url,
            user_id=user.user_id,
            user_email=user.email,
            shop_id=config.yookassa.shop_id,
            secret_key=config.yookassa.secret_key,
            save_payment_method=config.yookassa.save_payment_method,
            metadata={
                "tariff_id": tariff.id,
                "source": payload.source,
                "slots": str(slots),
            },
            items=receipt_items(tariff.name, checkout, tariff.duration_days),
        )

        # Сохраняем платёж в БД
        await payment_service.create_payment_record(
            yookassa_payment_id=yookassa_payment_id,
            user_id=user.user_id,
            tariff_id=tariff.id,
            original_amount=checkout.original_total,
            final_amount=final_price,
            source=payload.source,
            promo_code=payload.promo_code,
            discount_percent=payload.discount_percent,
            extra_devices=slots,
        )

        logger.info(
            f"Web payment created: user={user.user_id}, tariff={tariff.name}, "
            f"amount={final_price}, slots={slots}, discount={payload.discount_percent}%, "
            f"promo={payload.promo_code or 'none'}, source={payload.source}"
        )

        return {"payment_url": payment_url}

    except Exception as e:
        logger.error(f"Error creating payment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка платежного шлюза")


class PromoValidateRequest(BaseModel):
    code: str


@router.post("/validate-promo")
async def validate_promo_route(
    payload: PromoValidateRequest,
    user: User = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await promo_service.validate(payload.code.upper(), user.user_id)
    if not result.is_valid:
        return {"valid": False, "error": result.error_message}

    promo = result.promo
    response = {"valid": True, "code": promo.code}

    if promo.discount_percent > 0:
        response["type"] = "discount"
        response["discount_percent"] = promo.discount_percent
    elif promo.bonus_days > 0:
        response["type"] = "bonus_days"
        response["bonus_days"] = promo.bonus_days

    return response


@router.post("/apply-bonus-promo")
async def apply_bonus_promo_route(
    payload: PromoValidateRequest,
    user: User = Depends(get_current_user),
):
    """Apply a bonus-days promo code (no payment needed)."""
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await promo_service.validate(payload.code.upper(), user.user_id)
    if not result.is_valid:
        raise HTTPException(status_code=400, detail=result.error_message)

    promo = result.promo
    if promo.bonus_days <= 0:
        raise HTTPException(status_code=400, detail="Этот промокод даёт скидку, а не бонусные дни")

    # apply_bonus_days сама атомарно захватывает промокод (try_claim) и только
    # потом продлевает подписку; при сбое extend() (например, таймаут
    # Remnawave) захват откатывается (release_claim), так что промокод не
    # "сгорает" — см. tgbot/services/promo_code_service.py.
    try:
        await promo_service.apply_bonus_days(user.user_id, promo, subscription_service)
    except PromoClaimError:
        # Гонка (двойной клик/повторный запрос) или промокод закончился между
        # validate() и try_claim() — не сбой Remnawave, а ожидаемая ситуация,
        # поэтому 400 с понятным сообщением, а не 500.
        raise HTTPException(status_code=400, detail="Этот промокод уже использован или закончился")
    except Exception as e:
        logger.error(f"Error applying bonus promo code for user {user.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось начислить бонусные дни. Попробуйте позже.")

    return {"success": True, "bonus_days": promo.bonus_days}


class DeviceSlotsRequest(BaseModel):
    """Докупка доп. устройств в середине оплаченного периода."""
    slots: int = 1
    source: Literal["web", "tma"] = "web"


@router.post("/device-slots")
async def create_device_slots_payment(
    payload: DeviceSlotsRequest,
    user: User = Depends(get_current_user),
):
    """
    Создаёт счёт на докупку слотов устройств.

    Цену считает сервер (остаток дней подписки × цена слота) — клиент присылает
    только количество. Платёж создаётся с kind='devices': вебхук по нему поднимет
    лимит устройств и НЕ тронет срок подписки.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if await payment_service.has_pending_payment(user.user_id):
        raise HTTPException(
            status_code=409,
            detail="У вас уже есть неоплаченный счёт. Завершите или дождитесь его отмены."
        )

    quote = await device_slot_service.quote(user.user_id, payload.slots)
    if not quote.ok:
        raise HTTPException(status_code=400, detail=quote.error)

    if payload.source == "tma" and config.tg_bot.tg_bot_username:
        return_url = f"https://t.me/{config.tg_bot.tg_bot_username}/{config.tg_bot.tma_app_name}?startapp=devices"
    else:
        return_url = f"https://{config.webhook.domain}/profile/"

    description = f"Дополнительные устройства ({quote.slots} шт., {quote.remaining_days} дн.)"

    try:
        payment_url, yookassa_payment_id = create_payment(
            amount=quote.price,
            description=description,
            return_url=return_url,
            user_id=user.user_id,
            user_email=user.email,
            shop_id=config.yookassa.shop_id,
            secret_key=config.yookassa.secret_key,
            metadata={"kind": "devices", "slots": str(quote.slots), "source": payload.source},
            items=[{
                "description": description,
                "quantity": quote.slots,
                "amount": quote.price / quote.slots,
            }],
        )

        await payment_service.create_payment_record(
            yookassa_payment_id=yookassa_payment_id,
            user_id=user.user_id,
            tariff_id=None,
            original_amount=quote.price,
            final_amount=quote.price,
            source=payload.source,
            kind='devices',
            extra_devices=quote.slots,
        )

        logger.info(
            f"Device slots payment created: user={user.user_id}, slots={quote.slots}, "
            f"amount={quote.price}, days_left={quote.remaining_days}, source={payload.source}"
        )

        return {"payment_url": payment_url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating device slots payment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка платежного шлюза")


@router.post("/cancel-pending")
async def cancel_pending_payment_route(user: User = Depends(get_current_user)):
    """Отменяет висящий неоплаченный счёт по просьбе пользователя.

    Пока счёт в статусе pending, новый создать нельзя (иначе вебхук по старому
    начислил бы оплаченное дважды), а YooKassa сама отменит его только минут
    через 30. Без этой ручки человек, передумавший насчёт способа оплаты,
    полчаса видел «у вас уже есть неоплаченный счёт» и ничего не мог сделать.

    Отмена локальная: платежи создаются с capture=True, и Payment.cancel в API
    YooKassa для них недоступен — помечаем запись 'cancelled' у себя. Если по
    старой ссылке всё же заплатят, вебхук обработает платёж как обычно
    (см. payment_service.process_successful_payment) — деньги не пропадут.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cancelled = await payment_service.cancel_pending_payment(user.user_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Неоплаченный счёт не найден.")

    return {"cancelled": True}
