import datetime

from sqlalchemy import select, update, delete, or_

from db import User, UserPaymentMethod


class PaymentMethodRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def upsert(self, user_id: int, yookassa_payment_method_id: str,
                     card_last4: str | None, card_type: str | None,
                     renew_tariff_id: int | None) -> UserPaymentMethod:
        """Один метод платежа на пользователя: обновляет если существует, иначе создаёт."""
        async with self._session_maker() as session:
            stmt = select(UserPaymentMethod).where(UserPaymentMethod.user_id == user_id)
            result = await session.execute(stmt)
            pm = result.scalar_one_or_none()

            if pm:
                pm.yookassa_payment_method_id = yookassa_payment_method_id
                pm.card_last4 = card_last4
                pm.card_type = card_type
                pm.renew_tariff_id = renew_tariff_id
                pm.fail_count = 0
            else:
                pm = UserPaymentMethod(
                    user_id=user_id,
                    yookassa_payment_method_id=yookassa_payment_method_id,
                    card_last4=card_last4,
                    card_type=card_type,
                    renew_tariff_id=renew_tariff_id,
                    auto_renew_enabled=True,
                    fail_count=0,
                )
                session.add(pm)

            await session.commit()
            await session.refresh(pm)
            return pm

    async def get_by_user(self, user_id: int) -> UserPaymentMethod | None:
        async with self._session_maker() as session:
            stmt = select(UserPaymentMethod).where(UserPaymentMethod.user_id == user_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def set_auto_renew(self, user_id: int, enabled: bool) -> bool:
        async with self._session_maker() as session:
            values = {'auto_renew_enabled': enabled}
            if enabled:
                # Включение вручную = новый шанс: иначе карта, отключённая после
                # трёх отказов, никогда бы не попала в выборку (fail_count < 3).
                values['fail_count'] = 0
            stmt = (
                update(UserPaymentMethod)
                .where(UserPaymentMethod.user_id == user_id)
                .values(**values)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def set_renew_tariff(self, user_id: int, tariff_id: int) -> bool:
        async with self._session_maker() as session:
            stmt = (
                update(UserPaymentMethod)
                .where(UserPaymentMethod.user_id == user_id)
                .values(renew_tariff_id=tariff_id)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def increment_fail(self, user_id: int) -> int:
        """Увеличивает счётчик ошибок автопродления, возвращает новое значение."""
        async with self._session_maker() as session:
            stmt = (
                update(UserPaymentMethod)
                .where(UserPaymentMethod.user_id == user_id)
                .values(fail_count=UserPaymentMethod.fail_count + 1)
            )
            await session.execute(stmt)
            await session.commit()

            stmt_select = select(UserPaymentMethod.fail_count).where(
                UserPaymentMethod.user_id == user_id
            )
            result = await session.execute(stmt_select)
            row = result.scalar_one_or_none()
            return row if row is not None else 0

    async def mark_attempt(self, user_id: int) -> None:
        async with self._session_maker() as session:
            stmt = (
                update(UserPaymentMethod)
                .where(UserPaymentMethod.user_id == user_id)
                .values(last_attempt_at=datetime.datetime.now())
            )
            await session.execute(stmt)
            await session.commit()

    async def reset_fail(self, user_id: int) -> None:
        async with self._session_maker() as session:
            stmt = (
                update(UserPaymentMethod)
                .where(UserPaymentMethod.user_id == user_id)
                .values(fail_count=0)
            )
            await session.execute(stmt)
            await session.commit()

    async def delete(self, user_id: int) -> bool:
        async with self._session_maker() as session:
            stmt = delete(UserPaymentMethod).where(UserPaymentMethod.user_id == user_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get_due_for_renewal(self) -> list[UserPaymentMethod]:
        """Возвращает записи с включённым автопродлением, где подписка истекает
        в диапазоне (now - 2 дня) .. (now + 3 дня) и попыток < 3.

        Верхняя граница +3 дня: списание стартует, когда до конца подписки
        остаётся 3 дня. Нижняя граница -2 дня — запас для ретраев и пропущенных
        запусков планировщика."""
        async with self._session_maker() as session:
            now = datetime.datetime.now()
            lower = now - datetime.timedelta(days=2)
            upper = now + datetime.timedelta(days=3)

            stmt = (
                select(UserPaymentMethod)
                .join(User, User.user_id == UserPaymentMethod.user_id)
                .where(
                    UserPaymentMethod.auto_renew_enabled == True,
                    UserPaymentMethod.fail_count < 3,
                    User.subscription_end_date >= lower,
                    User.subscription_end_date <= upper,
                    # Вводный тариф переходит на полную цену в день окончания —
                    # это делает отдельный джоб (get_intro_due_for_conversion).
                    or_(User.intro_used == False, User.is_first_payment_made == True),
                )
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_intro_due_for_conversion(self) -> list[UserPaymentMethod]:
        """Карты пользователей на вводном тарифе, которым пора списать полную цену.

        Окно: подписка кончается в ближайший час (джоб почасовой — списываем
        в день окончания, не отбирая у человека пробные дни) или уже кончилась,
        но не больше 3 дней назад (ретраи после отказа карты). Повтор — не
        чаще раза в сутки, до 3 попыток."""
        async with self._session_maker() as session:
            now = datetime.datetime.now()
            stmt = (
                select(UserPaymentMethod)
                .join(User, User.user_id == UserPaymentMethod.user_id)
                .where(
                    UserPaymentMethod.auto_renew_enabled == True,
                    UserPaymentMethod.fail_count < 3,
                    User.intro_used == True,
                    User.is_first_payment_made == False,
                    User.subscription_end_date > now - datetime.timedelta(days=3),
                    User.subscription_end_date <= now + datetime.timedelta(hours=1),
                    (UserPaymentMethod.last_attempt_at.is_(None))
                    | (UserPaymentMethod.last_attempt_at < now - datetime.timedelta(hours=23)),
                )
            )
            result = await session.execute(stmt)
            return result.scalars().all()
