from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update, func, case, or_, and_

from db import Payment, Tariff

_MSK = ZoneInfo("Europe/Moscow")


def _msk_period_start(kind: str, now_utc: datetime | None = None) -> datetime:
    """Начало календарного периода в МСК, приведённое к наивному UTC —
    в той же конвенции, в какой хранится Payment.completed_at (наивный UTC).

    kind: 'day' | 'week' (с понедельника) | 'month' (с 1-го числа) | 'year' (с 1 января).
    now_utc: aware-datetime в UTC; для тестов. По умолчанию datetime.now(timezone.utc).
    Границы периода вычисляются по московским календарным суткам, затем
    переводятся обратно в наивный UTC для сравнения с completed_at.
    """
    base = now_utc or datetime.now(timezone.utc)
    now_msk = base.astimezone(_MSK)
    if kind == "day":
        start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "week":
        start = (now_msk - timedelta(days=now_msk.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    elif kind == "month":
        start = now_msk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif kind == "year":
        start = now_msk.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period kind: {kind}")
    # aware(MSK) -> aware(UTC) -> наивный UTC (как хранится completed_at)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


class PaymentRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create(self, yookassa_payment_id: str, user_id: int, tariff_id: int | None,
                     original_amount: float, final_amount: float, source: str = 'bot',
                     promo_code: str = None, discount_percent: int = 0,
                     telegram_payment_charge_id: str | None = None,
                     kind: str = 'subscription', extra_devices: int = 0) -> Payment:
        async with self._session_maker() as session:
            payment = Payment(
                yookassa_payment_id=yookassa_payment_id,
                user_id=user_id,
                tariff_id=tariff_id,
                original_amount=original_amount,
                final_amount=final_amount,
                source=source,
                promo_code=promo_code,
                discount_percent=discount_percent,
                telegram_payment_charge_id=telegram_payment_charge_id,
                kind=kind,
                extra_devices=extra_devices,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            return payment

    async def get_by_yookassa_id(self, yookassa_payment_id: str) -> Payment | None:
        async with self._session_maker() as session:
            stmt = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_by_telegram_charge_id(self, telegram_payment_charge_id: str) -> Payment | None:
        """Идемпотентность Stars-платежей (docs/tma-roadmap.md фаза 3.2): Telegram может
        повторно доставить Message.successful_payment (ретрай доставки апдейта) — по
        этому charge_id проверяем, не зачислили ли мы его уже."""
        async with self._session_maker() as session:
            stmt = select(Payment).where(
                Payment.telegram_payment_charge_id == telegram_payment_charge_id
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def update_status(self, yookassa_payment_id: str, status: str) -> bool:
        async with self._session_maker() as session:
            completed_at = datetime.now() if status in ('succeeded', 'failed', 'refunded') else None
            stmt = (
                update(Payment)
                .where(Payment.yookassa_payment_id == yookassa_payment_id)
                .values(status=status, completed_at=completed_at)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get_user_payments(self, user_id: int, limit: int = 20) -> list[Payment]:
        async with self._session_maker() as session:
            stmt = (
                select(Payment)
                .where(Payment.user_id == user_id)
                .order_by(Payment.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_user_pending(self, user_id: int) -> Payment | None:
        async with self._session_maker() as session:
            stmt = select(Payment).where(
                Payment.user_id == user_id,
                Payment.status == 'pending'
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def has_paid_with_promo(self, user_id: int, promo_code: str) -> bool:
        """Есть ли у пользователя оплаченный (в т.ч. потом возвращённый) счёт с этим промокодом.

        Страховка перед возвратом промокода при отмене счёта: бот держит скидку
        в FSM и после оплаты, так что отменяемый счёт может оказаться уже
        вторым с тем же промокодом — тогда промокод израсходован первым и
        возвращать его нельзя.
        """
        async with self._session_maker() as session:
            stmt = select(Payment.id).where(
                Payment.user_id == user_id,
                func.upper(Payment.promo_code) == promo_code.upper(),
                Payment.status.in_(('succeeded', 'refunded')),
            ).limit(1)
            result = await session.execute(stmt)
            return result.first() is not None

    async def get_pending_older_than(self, minutes: int) -> list[Payment]:
        async with self._session_maker() as session:
            cutoff = datetime.now() - timedelta(minutes=minutes)
            # Автосписание (source='auto') может подтверждаться банком дольше —
            # отменив его локально через час, мы бы на следующий день списали
            # второй раз. Для них порог — сутки.
            auto_cutoff = datetime.now() - timedelta(hours=24)
            stmt = select(Payment).where(
                Payment.status == 'pending',
                or_(
                    and_(Payment.source != 'auto', Payment.created_at < cutoff),
                    and_(Payment.source == 'auto', Payment.created_at < auto_cutoff),
                ),
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def cancel_if_pending(self, yookassa_payment_id: str) -> bool:
        """Атомарно помечает счёт 'cancelled' — но только если он ВСЁ ЕЩЁ 'pending'.

        Нужно джобу автоотмены (tgbot/services/scheduler.py): между выборкой
        зависших счетов и их отметкой вебхук мог принести оплату по старой
        ссылке, и безусловный update_status затёр бы 'succeeded' вместе с
        completed_at — платёж пропал бы из выручки. Условие в WHERE закрывает
        это окно на стороне БД.

        Возвращает False, если строка уже сменила статус (и значит трогать её
        не надо) либо её нет вовсе.
        """
        async with self._session_maker() as session:
            stmt = (
                update(Payment)
                .where(
                    Payment.yookassa_payment_id == yookassa_payment_id,
                    Payment.status == 'pending',
                )
                .values(status='cancelled', completed_at=None)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get_revenue_stats(self, days: int) -> dict:
        async with self._session_maker() as session:
            since = datetime.now() - timedelta(days=days)
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(
                Payment.status == 'succeeded',
                Payment.completed_at >= since,
                # Stars-платежи (docs/tma-roadmap.md фаза 3.2) хранят сумму в XTR,
                # а не в рублях — не смешиваем их с рублёвым доходом (см. get_stars_revenue_total).
                Payment.source != 'stars',
            )
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1])}

    async def get_revenue_for_period(self, kind: str) -> dict:
        """Доход за КАЛЕНДАРНЫЙ период (kind: 'day'|'week'|'month'|'year'),
        границы считаются по МСК. Возвращает {"count", "revenue", "since"}.
        Stars-платежи исключены (сумма в XTR, не в рублях) — см. get_stars_revenue_total."""
        async with self._session_maker() as session:
            since = _msk_period_start(kind)
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(
                Payment.status == 'succeeded',
                Payment.completed_at >= since,
                Payment.source != 'stars',
            )
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1]), "since": since}

    async def get_total_revenue(self) -> dict:
        async with self._session_maker() as session:
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(Payment.status == 'succeeded', Payment.source != 'stars')
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1])}

    async def get_stars_revenue_total(self) -> dict:
        """Доход Stars (XTR) за всё время — отдельно от рублёвого дохода
        (docs/tma-roadmap.md фаза 3.2, п.7). final_amount для source='stars'
        хранит сумму в XTR (столько же, сколько price_stars у тарифа на момент оплаты)."""
        async with self._session_maker() as session:
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(Payment.status == 'succeeded', Payment.source == 'stars')
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "stars": float(row[1])}

    async def get_mrr(self) -> dict:
        """
        Метрика №1 — MRR: сумма успешных платежей за ТЕКУЩИЙ календарный месяц
        (с 1-го числа месяца по текущий момент), а не скользящее окно в днях.
        Stars-платежи исключены (сумма в XTR, не в рублях).
        """
        async with self._session_maker() as session:
            month_start = _msk_period_start("month")
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(
                Payment.status == 'succeeded',
                Payment.completed_at >= month_start,
                Payment.source != 'stars',
            )
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1]), "month_start": month_start}

    async def count_first_payments_for_period(self, days: int) -> int:
        """
        Метрика №3 — «первые оплаты»: количество НОВЫХ платящих пользователей
        за период, т.е. пользователей, чей самый первый успешный платёж за всю
        историю пришёлся именно на этот период (а не просто любой их платёж).
        """
        async with self._session_maker() as session:
            since = datetime.now() - timedelta(days=days)
            first_payment_subq = (
                select(
                    Payment.user_id.label("user_id"),
                    func.min(Payment.completed_at).label("first_completed_at"),
                )
                .where(Payment.status == "succeeded", Payment.completed_at.is_not(None))
                .group_by(Payment.user_id)
                .subquery()
            )
            stmt = select(func.count()).select_from(first_payment_subq).where(
                first_payment_subq.c.first_completed_at >= since
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_long_tariff_share(self, days: int, min_duration_days: int = 90) -> dict:
        """
        Метрика №6 — доля платежей за «длинные» тарифы (duration_days >= 90)
        среди всех успешных платежей за период.
        """
        async with self._session_maker() as session:
            since = datetime.now() - timedelta(days=days)
            stmt = (
                select(
                    func.count(Payment.id),
                    func.coalesce(
                        func.sum(case((Tariff.duration_days >= min_duration_days, 1), else_=0)),
                        0,
                    ),
                )
                .select_from(Payment)
                .join(Tariff, Tariff.id == Payment.tariff_id)
                .where(
                    Payment.status == 'succeeded',
                    Payment.completed_at >= since,
                    # Докупка слотов устройств идёт без тарифа и метрику «длинных
                    # тарифов» разбавлять не должна (JOIN её и так отсекает —
                    # условие оставлено явным, чтобы смысл не терялся).
                    Payment.kind == 'subscription',
                )
            )
            result = await session.execute(stmt)
            row = result.one()
            total = row[0] or 0
            long_count = row[1] or 0
            share = (long_count / total) if total else 0.0
            return {"total": total, "long": long_count, "share": share}
