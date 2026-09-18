from database.repositories.payment_method import PaymentMethodRepository
from db import UserPaymentMethod
from loader import logger


class PaymentMethodService:
    def __init__(self, payment_method_repo: PaymentMethodRepository):
        self._repo = payment_method_repo

    async def save_from_yookassa(self, user_id: int, yk_payment_method,
                                  renew_tariff_id: int) -> None:
        """Сохраняет метод оплаты из объекта YooKassa SDK после успешного платежа.

        yk_payment_method — объект payment_method из верифицированного Payment SDK.
        Сохраняет только если getattr(yk_payment_method, 'saved', False) == True.
        """
        if not getattr(yk_payment_method, 'saved', False):
            logger.info(
                f"Метод оплаты пользователя {user_id} не помечен как saved — пропускаем сохранение"
            )
            return

        pm_id = yk_payment_method.id
        card_last4: str | None = None
        card_type: str | None = None

        card = getattr(yk_payment_method, 'card', None)
        if card:
            card_last4 = getattr(card, 'last4', None)
            card_type = getattr(card, 'card_type', None)

        if getattr(yk_payment_method, 'type', None) == 'sbp':
            card_type = 'SBP'
            card_last4 = None

        try:
            await self._repo.upsert(
                user_id=user_id,
                yookassa_payment_method_id=pm_id,
                card_last4=card_last4,
                card_type=card_type,
                renew_tariff_id=renew_tariff_id,
            )
            logger.info(
                f"Метод оплаты сохранён: user={user_id}, type={card_type or 'unknown'}, "
                f"last4={card_last4 or '—'}"
            )
        except Exception:
            logger.exception(f"Ошибка при сохранении метода оплаты для user={user_id}")

    async def get_card(self, user_id: int) -> UserPaymentMethod | None:
        """Возвращает сохранённый метод оплаты пользователя или None."""
        return await self._repo.get_by_user(user_id)

    async def toggle_auto_renew(self, user_id: int) -> bool:
        """Переключает автопродление на противоположное значение.
        Возвращает новое состояние. Если карты нет — возвращает False."""
        pm = await self._repo.get_by_user(user_id)
        if not pm:
            return False
        new_state = not pm.auto_renew_enabled
        await self._repo.set_auto_renew(user_id, new_state)
        return new_state

    async def set_renew_tariff(self, user_id: int, tariff_id: int) -> bool:
        """Меняет тариф, на который будет автоматически продлеваться подписка.
        Возвращает True, если запись существовала и была обновлена."""
        return await self._repo.set_renew_tariff(user_id, tariff_id)

    async def delete_card(self, user_id: int) -> bool:
        """Удаляет сохранённый метод оплаты пользователя. Возвращает True если запись существовала."""
        return await self._repo.delete(user_id)
