from dataclasses import dataclass
from datetime import datetime

from db import Tariff, Payment
from database.repositories.user import UserRepository
from database.repositories.tariff import TariffRepository
from database.repositories.payment import PaymentRepository
from tgbot.services.subscription_service import SubscriptionService, ExtensionResult
from tgbot.services.referral_service import ReferralService
from tgbot.services.pricing import effective_price
from tgbot.services.device_pricing import slots_cost_for_tariff
from loader import logger, config


@dataclass
class PaymentResult:
    # tariff/extension = None для kind='devices': докупка слотов устройств
    # не продлевает подписку и приходит без тарифа.
    tariff: Tariff | None
    extension: ExtensionResult | None
    referrer_id: int | None
    is_first_payment: bool
    payment: Payment | None = None
    kind: str = 'subscription'
    extra_devices: int = 0  # сколько доп. устройств стало у пользователя после платежа


@dataclass
class StarsPaymentResult:
    tariff: Tariff
    extension: ExtensionResult
    payment: Payment
    referrer_id: int | None = None
    is_first_payment: bool = False


class PaymentService:
    def __init__(self, subscription_service: SubscriptionService,
                 referral_service: ReferralService,
                 user_repo: UserRepository,
                 tariff_repo: TariffRepository,
                 payment_repo: PaymentRepository,
                 payment_method_service=None,
                 device_slot_service=None):
        self._subscription_service = subscription_service
        self._referral_service = referral_service
        self._user_repo = user_repo
        self._tariff_repo = tariff_repo
        self._payment_repo = payment_repo
        self._payment_method_service = payment_method_service
        self._device_slot_service = device_slot_service

    async def create_payment_record(self, yookassa_payment_id: str, user_id: int,
                                    tariff_id: int | None, original_amount: float,
                                    final_amount: float, source: str = 'bot',
                                    promo_code: str = None, discount_percent: int = 0,
                                    kind: str = 'subscription', extra_devices: int = 0) -> Payment:
        """Create a payment record in DB when payment is initiated.

        kind='devices' — докупка слотов устройств: tariff_id = None, подписка не
        продлевается, extra_devices = сколько слотов добавить после оплаты.
        kind='subscription' — extra_devices = сколько слотов человек оплатил
        на новый срок (абсолютное количество, выбранное на чекауте).
        """
        return await self._payment_repo.create(
            yookassa_payment_id=yookassa_payment_id,
            user_id=user_id,
            tariff_id=tariff_id,
            original_amount=original_amount,
            final_amount=final_amount,
            source=source,
            promo_code=promo_code,
            discount_percent=discount_percent,
            kind=kind,
            extra_devices=extra_devices,
        )

    async def has_pending_payment(self, user_id: int) -> bool:
        """Check if user already has a pending payment."""
        pending = await self._payment_repo.get_user_pending(user_id)
        return pending is not None

    async def get_pending_payment(self, user_id: int) -> Payment | None:
        """Return the pending payment for a user, if any."""
        return await self._payment_repo.get_user_pending(user_id)

    async def cancel_pending_payment(self, user_id: int) -> Payment | None:
        """Локальная отмена неоплаченного счёта по инициативе пользователя.

        Платежи создаются с capture=True, поэтому отменить их в YooKassa API
        нельзя (Payment.cancel работает только для waiting_for_capture) —
        просто помечаем запись как 'cancelled' в нашей БД (тот же статус, что
        проставляет вебхук `payment.canceled` при автоотмене YooKassa по
        таймауту — см. webhook_handlers.py), чтобы пользователь мог сразу
        выбрать тариф заново, не дожидаясь автоотмены.

        Если пользователь всё же оплатит по старой ссылке — process_successful_payment
        всё равно обработает вебхук (статус 'cancelled' там допустим), чтобы деньги
        не "проглотились" без продления подписки.
        """
        pending = await self._payment_repo.get_user_pending(user_id)
        if pending is None:
            return None

        await self._payment_repo.update_status(pending.yookassa_payment_id, 'cancelled')
        logger.info(f"Payment cancelled by user: user={user_id}, yookassa_id={pending.yookassa_payment_id}")
        return pending

    async def process_successful_payment(self, yookassa_payment_id: str,
                                         paid_amount: float,
                                         payment_method=None) -> PaymentResult | None:
        """
        Process a successful payment webhook:
        1. Verify payment exists in DB
        2. Check idempotency (status must be pending or locally-cancelled)
        3. Verify amount matches
        4. Extend subscription (DB + Remnawave)
        5. Award referral bonus if first payment
        6. Mark first payment done
        7. Update payment status to succeeded
        """
        # 1. Verify payment exists in our DB
        payment = await self._payment_repo.get_by_yookassa_id(yookassa_payment_id)
        if not payment:
            logger.error(f"Payment {yookassa_payment_id} not found in local DB — possible forged webhook")
            return None

        # 2. Idempotency: 'succeeded'/'failed'/'refunded' — уже финальны, не переобрабатываем.
        #    'cancelled' — счёт мог быть отменён локально (кнопкой) ИЛИ автоматически
        #    вебхуком payment.canceled; в обоих случаях реальный платёж в YooKassa мог
        #    всё же пройти оплату по старой ссылке, поэтому обрабатываем его как обычный
        #    успешный платёж, а не отбрасываем.
        if payment.status not in ('pending', 'cancelled'):
            logger.warning(f"Payment {yookassa_payment_id} already processed (status={payment.status}), skipping")
            return None

        if payment.status == 'cancelled':
            logger.warning(
                f"Payment {yookassa_payment_id} was cancelled (user={payment.user_id}), "
                f"but succeeded in YooKassa — processing anyway to avoid swallowing the payment"
            )

        # 3. Verify amount
        if abs(paid_amount - payment.final_amount) > 0.01:
            logger.error(
                f"Payment {yookassa_payment_id} amount mismatch: "
                f"expected {payment.final_amount}, got {paid_amount}"
            )
            await self._payment_repo.update_status(yookassa_payment_id, 'failed')
            return None

        user = await self._user_repo.get(payment.user_id)
        if not user:
            logger.error(f"User {payment.user_id} not found during payment processing")
            return None

        # Докупка слотов устройств: подписку не трогаем, ни дни, ни квоту трафика.
        if payment.kind == 'devices':
            return await self._process_device_payment(payment)

        tariff = await self._tariff_repo.get_by_id(payment.tariff_id)
        if not tariff:
            logger.error(f"Tariff {payment.tariff_id} not found during payment processing")
            return None

        is_first_payment = not user.is_first_payment_made

        # 4. Extend subscription. Квота платного тарифа ВСЕГДА передаётся явно:
        #    None (безлимитный тариф) приводим к 0 (=безлимит в Remnawave), иначе
        #    апгрейд с лимитного тарифа на безлимитный не снял бы старый лимит
        #    (None означает «не трогать квоту» — это только для бонусных продлений).
        tariff_limit_gb = tariff.data_limit_gb if tariff.data_limit_gb is not None else 0
        extension = await self._subscription_service.extend(
            payment.user_id, tariff.duration_days, data_limit_gb=tariff_limit_gb
        )

        # 4b. Слоты доп. устройств на новый срок. Количество человек выбрал на
        #     чекауте, поэтому применяем абсолютным значением — в том числе
        #     уменьшение (это его собственное решение, а не истечение срока).
        #     Сбой панели здесь не должен ронять обработку платежа: лимит
        #     догонит джоб sync_device_limits.
        extra_devices = payment.extra_devices or 0
        if self._device_slot_service is not None:
            try:
                extra_devices = await self._device_slot_service.set_slots(
                    payment.user_id, payment.extra_devices or 0
                )
            except Exception:
                logger.exception(
                    f"Не удалось применить слоты устройств для user={payment.user_id}, "
                    f"payment={yookassa_payment_id} — будет исправлено джобом сверки"
                )

        # 5. Referral bonus
        referrer_id = await self._referral_service.process_first_payment_bonus(payment.user_id)

        # 6. Mark first payment
        if is_first_payment:
            await self._user_repo.set_first_payment_done(payment.user_id)

        # 7. Update payment status
        await self._payment_repo.update_status(yookassa_payment_id, 'succeeded')

        # 8. Сохраняем метод оплаты для автопродления (не ломаем обработку при ошибке)
        if payment_method is not None and self._payment_method_service is not None:
            try:
                await self._payment_method_service.save_from_yookassa(
                    payment.user_id, payment_method, renew_tariff_id=payment.tariff_id
                )
            except Exception:
                logger.exception(
                    f"Не удалось сохранить метод оплаты для user={payment.user_id}, "
                    f"payment={yookassa_payment_id}"
                )

        logger.info(
            f"Payment processed: user={payment.user_id}, tariff={tariff.name}, "
            f"amount={payment.final_amount}, discount={payment.discount_percent}%, "
            f"promo={payment.promo_code or 'none'}, first={is_first_payment}"
        )
        return PaymentResult(
            tariff=tariff,
            extension=extension,
            referrer_id=referrer_id,
            is_first_payment=is_first_payment,
            payment=payment,
            kind='subscription',
            extra_devices=extra_devices,
        )

    async def _process_device_payment(self, payment: Payment) -> PaymentResult | None:
        """Оплачена докупка слотов устройств: начисляем слоты и поднимаем лимит в панели.

        Статус платежа проставляем ДО обращения к панели: деньги уже списаны, и
        если панель недоступна, платёж всё равно должен считаться обработанным —
        иначе повторная доставка вебхука начислит слоты второй раз. Лимит в этом
        случае доводит джоб sync_device_limits.
        """
        await self._payment_repo.update_status(payment.yookassa_payment_id, 'succeeded')

        slots = payment.extra_devices or 0
        total_extra = slots
        if self._device_slot_service is not None:
            try:
                total_extra = await self._device_slot_service.add_slots(payment.user_id, slots)
            except Exception:
                logger.exception(
                    f"Не удалось начислить слоты устройств для user={payment.user_id}, "
                    f"payment={payment.yookassa_payment_id} — будет исправлено джобом сверки"
                )

        logger.info(
            f"Device slots payment processed: user={payment.user_id}, slots=+{slots}, "
            f"total_extra={total_extra}, amount={payment.final_amount}"
        )
        return PaymentResult(
            tariff=None,
            extension=None,
            referrer_id=None,
            is_first_payment=False,
            payment=payment,
            kind='devices',
            extra_devices=total_extra,
        )

    async def process_stars_payment(
        self, user_id: int, tariff_id: int, total_amount: int, telegram_payment_charge_id: str,
    ) -> StarsPaymentResult | None:
        """
        Обработка Message.successful_payment для оплаты Telegram Stars
        (docs/tma-roadmap.md фаза 3.2). Вызывается из
        tgbot/handlers/user/stars_payment.py ПОСЛЕ pre_checkout_query.answer(ok=True) —
        валидация тарифа/суммы там уже пройдена, здесь только идемпотентное зачисление.

        Идемпотентность — по telegram_payment_charge_id: Telegram может повторно
        доставить апдейт (как и любой другой), а сумма Stars списывается платформой
        один раз при первой успешной обработке платежа, поэтому дублирующий
        successful_payment не должен продлевать подписку дважды.
        """
        existing = await self._payment_repo.get_by_telegram_charge_id(telegram_payment_charge_id)
        if existing:
            logger.warning(
                f"Stars payment {telegram_payment_charge_id} already processed "
                f"(payment.id={existing.id}), skipping duplicate successful_payment"
            )
            return None

        tariff = await self._tariff_repo.get_by_id(tariff_id)
        if not tariff:
            logger.error(
                f"Stars payment: tariff {tariff_id} not found "
                f"(user={user_id}, charge={telegram_payment_charge_id})"
            )
            return None

        # Флаг первой оплаты снимаем ДО каких-либо мутаций, чтобы он не зависел
        # от порядка шагов ниже (та же семантика, что в process_successful_payment).
        user = await self._user_repo.get(user_id)
        is_first_payment = bool(user) and not user.is_first_payment_made

        # Та же логика квоты, что и в YooKassa-флоу выше: платный тариф всегда
        # передаёт явную квоту (None → 0 безлимит), чтобы апгрейд/смена тарифа
        # гарантированно перезаписывала старый лимит в Remnawave.
        tariff_limit_gb = tariff.data_limit_gb if tariff.data_limit_gb is not None else 0
        extension = await self._subscription_service.extend(
            user_id, tariff.duration_days, data_limit_gb=tariff_limit_gb
        )

        # yookassa_payment_id — NOT NULL/UNIQUE колонка, для Stars-платежей не
        # существует, поэтому используем синтетический "stars:{charge_id}" (тот же
        # приём, что уже применяется в проекте для внешних платёжных ID). Настоящий
        # charge_id хранится отдельно — он verbatim нужен для refundStarPayment.
        payment = await self._payment_repo.create(
            yookassa_payment_id=f"stars:{telegram_payment_charge_id}",
            user_id=user_id,
            tariff_id=tariff.id,
            original_amount=total_amount,
            final_amount=total_amount,
            source='stars',
            telegram_payment_charge_id=telegram_payment_charge_id,
        )
        await self._payment_repo.update_status(payment.yookassa_payment_id, 'succeeded')

        # Пост-платёжная логика — тот же паритет, что и у YooKassa
        # (process_successful_payment шаги 5-6): реферер получает бонус за первую
        # оплату друга, затем помечаем факт первой оплаты. Порядок важен:
        # process_first_payment_bonus сам проверяет `not is_first_payment_made`,
        # поэтому set_first_payment_done строго после него.
        #
        # В отличие от YooKassa-флоу эти шаги идут ПОСЛЕ создания записи платежа:
        # запись с telegram_payment_charge_id — единственный якорь идемпотентности
        # для Stars (в YooKassa-флоу платёж уже лежит в БД к моменту вебхука).
        # Если процесс упадёт между шагами, повторный successful_payment отсечётся
        # как дубликат — потерянный бонус безопаснее, чем двойное начисление дней.
        referrer_id = await self._referral_service.process_first_payment_bonus(user_id)
        if is_first_payment:
            await self._user_repo.set_first_payment_done(user_id)

        # Слоты устройств Stars не продаёт, но подписка снова активна — значит
        # лимит в панели должен вернуться к «база + уже оплаченные слоты».
        if self._device_slot_service is not None:
            try:
                await self._device_slot_service.sync_limit(user_id)
            except Exception:
                logger.exception(f"Stars payment: не удалось синхронизировать лимит устройств, user={user_id}")

        logger.info(
            f"Stars payment processed: user={user_id}, tariff={tariff.name}, "
            f"amount={total_amount} XTR, charge={telegram_payment_charge_id}, "
            f"first={is_first_payment}"
        )
        return StarsPaymentResult(
            tariff=tariff,
            extension=extension,
            payment=payment,
            referrer_id=referrer_id,
            is_first_payment=is_first_payment,
        )

    async def process_refund(self, yookassa_payment_id: str) -> Payment | None:
        """
        Process a refund: mark payment as refunded, deduct days or disable subscription.
        """
        payment = await self._payment_repo.get_by_yookassa_id(yookassa_payment_id)
        if not payment:
            logger.error(f"Refund: Payment {yookassa_payment_id} not found in DB")
            return None

        if payment.status != 'succeeded':
            logger.warning(f"Refund: Payment {yookassa_payment_id} is not succeeded (status={payment.status})")
            return None

        # Возврат за докупку слотов: дни подписки ни при чём — снимаем слоты.
        if payment.kind == 'devices':
            await self._payment_repo.update_status(yookassa_payment_id, 'refunded')
            if self._device_slot_service is not None and payment.extra_devices:
                try:
                    await self._device_slot_service.add_slots(
                        payment.user_id, -payment.extra_devices
                    )
                except Exception:
                    logger.exception(
                        f"Refund: не удалось снять слоты устройств для user={payment.user_id}"
                    )
            logger.info(
                f"Refund processed (devices): payment={yookassa_payment_id}, "
                f"user={payment.user_id}, slots=-{payment.extra_devices}"
            )
            return payment

        tariff = await self._tariff_repo.get_by_id(payment.tariff_id) if payment.tariff_id else None
        days_to_deduct = tariff.duration_days if tariff else 0

        if days_to_deduct > 0:
            user = await self._user_repo.get(payment.user_id)
            if user and user.subscription_end_date:
                from datetime import timedelta
                new_end = user.subscription_end_date - timedelta(days=days_to_deduct)
                now = datetime.now()
                await self._user_repo.extend_subscription(payment.user_id, -days_to_deduct)

                if user.remnawave_uuid:
                    try:
                        remnawave = self._subscription_service._remnawave
                        if new_end <= now:
                            # Subscription expired after deduction — disable in Remnawave
                            await remnawave.disable_user(user.remnawave_uuid)
                        else:
                            new_expire = new_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                            await remnawave.update_user_expiry(user.remnawave_uuid, new_expire)
                    except Exception as e:
                        logger.error(f"Refund: Failed to update Remnawave for user {payment.user_id}: {e}")

        await self._payment_repo.update_status(yookassa_payment_id, 'refunded')
        logger.info(f"Refund processed: payment={yookassa_payment_id}, user={payment.user_id}, days_deducted={days_to_deduct}")
        return payment

    async def get_user_payments(self, user_id: int, limit: int = 20) -> list[Payment]:
        return await self._payment_repo.get_user_payments(user_id, limit)

    async def charge_renewal(self, user_id: int) -> str:
        """Попытка автоматического продления подписки по сохранённой карте.

        Возвращает:
          'succeeded' — платёж сразу успешен (вебхук финализирует/продлит)
          'pending'   — платёж создан, ожидаем вебхук
          'failed'    — платёж отклонён ЮKassa
          'skipped'   — нечего делать (нет карты, выключено, нет тарифа, уже есть pending)
        """
        from tgbot.services.payment import create_recurring_payment

        # 1. Получаем сохранённый метод оплаты
        if self._payment_method_service is None:
            logger.warning(f"charge_renewal: payment_method_service не подключён, user={user_id}")
            return 'skipped'

        card = await self._payment_method_service._repo.get_by_user(user_id)
        if card is None or not card.auto_renew_enabled:
            return 'skipped'

        # 2. Проверяем, нет ли уже pending-платежа (избегаем двойного списания)
        existing_pending = await self._payment_repo.get_user_pending(user_id)
        if existing_pending is not None:
            logger.info(f"charge_renewal: уже есть pending-платёж для user={user_id}, пропускаем")
            return 'skipped'

        # 3. Тариф для продления
        tariff = await self._tariff_repo.get_by_id(card.renew_tariff_id)
        if tariff is None:
            logger.warning(f"charge_renewal: тариф {card.renew_tariff_id} не найден, user={user_id}")
            return 'skipped'

        # 4. Данные пользователя (email для чека)
        user = await self._user_repo.get(user_id)
        email = user.email if user else None

        # Автопродление по определению непрерывно — если у тарифа задана лоялти-цена
        # ("цена навсегда"), списываем именно её.
        tariff_amount = effective_price(tariff, user_has_active_sub=True)

        # Доп. устройства продлеваются вместе с подпиской и стоят столько же за
        # месяц, сколько при покупке. Без этого слагаемого автосписание навсегда
        # оставалось бы в цене голого тарифа при расширенном лимите устройств —
        # то есть купленные один раз слоты становились бы бесплатными.
        slots = (user.extra_devices or 0) if user else 0
        slots_amount = 0.0
        if slots and self._device_slot_service is not None:
            device_settings = await self._device_slot_service.settings()
            slots_amount = slots_cost_for_tariff(device_settings, tariff.duration_days, slots)

        charge_amount = tariff_amount + slots_amount

        # Позиции чека: тариф и устройства — разные услуги (54-ФЗ).
        receipt_items = [{
            "description": f"Подписка VPN: {tariff.name}",
            "quantity": 1,
            "amount": tariff_amount,
        }]
        if slots_amount:
            receipt_items.append({
                "description": f"Дополнительные устройства ({tariff.duration_days} дн.)",
                "quantity": slots,
                "amount": slots_amount / slots,
            })

        # 5. Создаём платёж в ЮKassa
        try:
            yk_payment_id, status = create_recurring_payment(
                amount=charge_amount,
                description=f"Автопродление VPN: {tariff.name}",
                payment_method_id=card.yookassa_payment_method_id,
                user_id=user_id,
                user_email=email,
                metadata={'user_id': str(user_id), 'tariff_id': str(tariff.id), 'auto': '1'},
                shop_id=config.yookassa.shop_id,
                secret_key=config.yookassa.secret_key,
                items=receipt_items,
            )
        except Exception:
            logger.exception(f"charge_renewal: ошибка при создании платежа ЮKassa, user={user_id}")
            await self._payment_method_service._repo.increment_fail(user_id)
            return 'failed'

        # 6. Сохраняем запись в БД
        await self.create_payment_record(
            yookassa_payment_id=yk_payment_id,
            user_id=user_id,
            tariff_id=tariff.id,
            original_amount=charge_amount,
            final_amount=charge_amount,
            source='auto',
            extra_devices=slots,
        )

        logger.info(
            f"charge_renewal: платёж создан — user={user_id}, tariff={tariff.name}, "
            f"amount={charge_amount} (тариф {tariff_amount} + устройства {slots_amount}, "
            f"слотов {slots}), yk_id={yk_payment_id}, status={status}"
        )

        # 7. Обрабатываем итоговый статус
        if status == 'canceled':
            await self._payment_repo.update_status(yk_payment_id, 'failed')
            await self._payment_method_service._repo.increment_fail(user_id)
            return 'failed'

        if status == 'succeeded':
            return 'succeeded'

        return 'pending'
