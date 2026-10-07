from database.repositories.user import UserRepository
from tgbot.services.subscription_service import SubscriptionService
from loader import logger

# Реферальная программа (§7.5) — длительность начислений в днях. Единый источник
# правды: значения переиспользуются в текстах tgbot/handlers/user/start.py, чтобы
# UI не расходился с фактическим начислением.
REFERRAL_TRIAL_DAYS = 3          # другу — пробный период за переход по ссылке
REFERRER_LAUNCH_BONUS_DAYS = 3   # рефереру — сразу за факт приглашения
REFERRER_PAYMENT_BONUS_DAYS = 7  # рефереру — после первой оплаты друга


class ReferralService:
    def __init__(self, user_repo: UserRepository, subscription_service: SubscriptionService,
                 partner_repo=None):
        self._user_repo = user_repo
        self._subscription_service = subscription_service
        # Партнёры (денежная рефералка) получают не дни, а процент с оплат друзей —
        # см. partner_service.py. None — только в тестах: все рефереры обычные.
        self._partner_repo = partner_repo

    async def is_partner(self, user_id: int) -> bool:
        """Активный партнёр: вместо бонусных дней получает процент с оплат друзей."""
        return self._partner_repo is not None and await self._partner_repo.is_active(user_id)

    async def attach_referrer(self, user_id: int, referrer_id: int) -> bool:
        """
        Привязка друга к рефереру сразу по переходу по ссылке — ДО выдачи триала.

        Раньше referrer_id жил только в FSM (MemoryStorage) до нажатия «Проверить
        подписку»: перезапуск бота или брошенный онбординг терял приглашение, и у
        партнёра оставалось «Приглашено: 0». Теперь реферер сразу в БД.

        Привязываем только «нетронутого» пользователя: без реферера, без триала,
        без подписки и оплат — иначе по чужой ссылке можно было бы переманить
        уже действующего клиента. True — привязали.
        """
        if referrer_id == user_id:
            logger.warning(f"Referral: user {user_id} tried to refer themselves, skip")
            return False
        user = await self._user_repo.get(user_id)
        if user is None:
            logger.warning(f"Referral: user {user_id} not found, skip attach")
            return False
        if (user.referrer_id is not None or user.has_received_trial
                or user.subscription_end_date is not None or user.is_first_payment_made):
            logger.info(f"Referral: user {user_id} is not new (referrer={user.referrer_id}), skip attach to {referrer_id}")
            return False
        if await self._user_repo.get(referrer_id) is None:
            logger.warning(f"Referral: referrer {referrer_id} not found, skip attach for {user_id}")
            return False

        await self._user_repo.set_referrer(user_id, referrer_id)
        if await self.is_partner(referrer_id):
            # Друг партнёра: партнёру дней не даём, с оплат друга капают проценты.
            await self._user_repo.set_partner_referred(user_id)
            logger.info(f"Referral: user {user_id} attributed to partner {referrer_id}")
        else:
            logger.info(f"Referral: user {user_id} attached to referrer {referrer_id}")
        return True

    async def activate_new_user_referral(self, user_id: int, referrer_id: int | None = None,
                                          bonus_days: int = REFERRAL_TRIAL_DAYS,
                                          referrer_launch_bonus_days: int = REFERRER_LAUNCH_BONUS_DAYS):
        """
        Активация реферала: выдать другу пробную подписку (bonus_days) + начислить
        рефереру бонус за сам факт привлечения (§7.5, «+3 дня за запуск»).

        Реферер берётся из БД (привязан в attach_referrer при /start). referrer_id
        в аргументах — для входа без /start (Mini App): привязываем на месте.

        Идемпотентность: триал по рефералке выдаётся один раз (has_received_trial) —
        повторное нажатие «Проверить подписку» не даст ни второго триала, ни второго
        бонуса рефереру.
        """
        user = await self._user_repo.get(user_id)
        if user is None:
            logger.warning(f"Referral: user {user_id} not found, skip referral activation")
            return None
        if user.has_received_trial:
            logger.info(f"Referral: user {user_id} already received trial, skip activation")
            return None

        if user.referrer_id is None and referrer_id is not None:
            await self.attach_referrer(user_id, referrer_id)
            user = await self._user_repo.get(user_id)
        referrer_id = user.referrer_id

        # Другу — пробная подписка
        result = await self._subscription_service.extend(user_id, bonus_days)
        await self._user_repo.set_trial_received(user_id)
        logger.info(f"Referral bonus: user {user_id} got {bonus_days} days via referrer {referrer_id}")

        # Рефереру — бонус за сам факт привлечения (партнёрам — не днями, а процентами)
        if referrer_id is not None and not await self.is_partner(referrer_id):
            try:
                await self._subscription_service.extend(referrer_id, referrer_launch_bonus_days, data_limit_gb=None)
                await self._user_repo.add_bonus_days(referrer_id, days=referrer_launch_bonus_days)
                logger.info(
                    f"Referral bonus: referrer {referrer_id} got {referrer_launch_bonus_days} days "
                    f"for inviting {user_id} (launch bonus)"
                )
            except Exception as e:
                logger.error(f"Referral: failed to award launch bonus to referrer {referrer_id}: {e}")

        return result

    async def process_first_payment_bonus(self, user_who_paid_id: int,
                                           bonus_days: int = REFERRER_PAYMENT_BONUS_DAYS) -> int | None:
        """
        Бонус рефереру при первой оплате друга (§7.5 — 7 дней).
        Возвращает referrer_id если бонус начислен, None иначе.
        """
        user = await self._user_repo.get(user_who_paid_id)
        if not (user and user.referrer_id and not user.is_first_payment_made):
            return None

        referrer = await self._user_repo.get(user.referrer_id)
        if not referrer:
            return None
        if getattr(user, "partner_referred", False) or await self.is_partner(user.referrer_id):
            return None   # партнёр получает процент (partner_service), а не дни

        referrer_id = user.referrer_id

        try:
            # extend() сам синхронизирует Remnawave (создаст пользователя, если его ещё нет)
            # и НЕ трогает квоту трафика (data_limit_gb=None) — не затирает купленную квоту.
            await self._subscription_service.extend(referrer_id, bonus_days, data_limit_gb=None)
            await self._user_repo.add_bonus_days(referrer_id, days=bonus_days)

            logger.info(f"Referral bonus: Granted {bonus_days} days to referrer {referrer_id}")
            return referrer_id
        except Exception as e:
            logger.error(f"Error handling referral bonus for {referrer_id}: {e}")
            return None
