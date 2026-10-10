"""
Партнёры (денежная рефералка): баланс, журнал движения денег, заявки на вывод.

Деньги двигаются только здесь и только атомарно: условный UPDATE баланса
(`balance_kop >= сумма`) и запись в журнал — в одной транзакции. Повторная доставка
вебхука или двойное нажатие кнопки упираются в UNIQUE(kind, payment_id) журнала.
"""
import datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError

from db import ManagerOperation, Partner, PartnerLedger, PartnerWithdrawal, User

ACTIVE = "active"
DISABLED = "disabled"

HOLD = "hold"
AVAILABLE = "available"
CANCELLED = "cancelled"
REVERSED = "reversed"
DONE = "done"

PENDING = "pending"
PAID = "paid"
REJECTED = "rejected"


class PartnerRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    # --- партнёры -----------------------------------------------------------

    async def get(self, user_id: int) -> Partner | None:
        async with self._session_maker() as session:
            return await session.get(Partner, user_id)

    async def is_active(self, user_id: int) -> bool:
        async with self._session_maker() as session:
            status = (await session.execute(
                select(Partner.status).where(Partner.user_id == user_id)
            )).scalar_one_or_none()
            return status == ACTIVE

    async def enable(self, user_id: int, admin_id: int) -> Partner:
        """Делает пользователя активным партнёром (новым или снова включённым)."""
        async with self._session_maker() as session:
            partner = await session.get(Partner, user_id)
            if partner is None:
                partner = Partner(user_id=user_id, status=ACTIVE, created_by=admin_id)
                session.add(partner)
            else:
                partner.status = ACTIVE
                partner.disabled_at = None
            await session.commit()
            await session.refresh(partner)
            return partner

    async def disable(self, user_id: int) -> bool:
        async with self._session_maker() as session:
            result = await session.execute(
                update(Partner).where(Partner.user_id == user_id, Partner.status == ACTIVE)
                .values(status=DISABLED, disabled_at=datetime.datetime.now())
            )
            await session.commit()
            return result.rowcount > 0

    async def set_percent(self, user_id: int, percent: int | None) -> None:
        async with self._session_maker() as session:
            await session.execute(update(Partner).where(Partner.user_id == user_id).values(percent=percent))
            await session.commit()

    async def set_requisites(self, user_id: int, phone: str, bank: str) -> None:
        async with self._session_maker() as session:
            await session.execute(
                update(Partner).where(Partner.user_id == user_id).values(sbp_phone=phone, sbp_bank=bank)
            )
            await session.commit()

    async def list_with_users(self) -> list[tuple[Partner, User]]:
        async with self._session_maker() as session:
            rows = await session.execute(
                select(Partner, User).join(User, User.user_id == Partner.user_id)
                .order_by(Partner.status, Partner.created_at)
            )
            return [(p, u) for p, u in rows.all()]

    # --- начисления ---------------------------------------------------------

    async def add_accrual(self, *, partner_id: int, friend_id: int, payment_id: str, base_kop: int,
                          percent: int, amount_kop: int, available_at: datetime.datetime) -> PartnerLedger | None:
        """
        Начисление с оплаты друга. None — эта оплата уже начислена (повтор вебхука).
        Холд 0 дней — сразу на баланс, той же транзакцией.
        """
        immediate = available_at <= datetime.datetime.now()
        async with self._session_maker() as session:
            entry = PartnerLedger(
                partner_user_id=partner_id, kind="accrual", amount_kop=amount_kop,
                status=AVAILABLE if immediate else HOLD, friend_user_id=friend_id, payment_id=payment_id,
                base_amount_kop=base_kop, percent=percent, available_at=available_at,
            )
            session.add(entry)
            if immediate:
                await session.execute(
                    update(Partner).where(Partner.user_id == partner_id)
                    .values(balance_kop=Partner.balance_kop + amount_kop)
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(entry)
            return entry

    async def release_due(self, now: datetime.datetime) -> list[PartnerLedger]:
        """Переводит начисления с истёкшим холдом на баланс. Возвращает переведённые."""
        async with self._session_maker() as session:
            due = (await session.execute(
                select(PartnerLedger).where(
                    PartnerLedger.kind == "accrual", PartnerLedger.status == HOLD,
                    PartnerLedger.available_at <= now,
                ).order_by(PartnerLedger.id)
            )).scalars().all()

        released = []
        for entry in due:
            async with self._session_maker() as session:
                # Статус — условием: параллельный возврат мог отменить начисление по пути.
                result = await session.execute(
                    update(PartnerLedger).where(PartnerLedger.id == entry.id, PartnerLedger.status == HOLD)
                    .values(status=AVAILABLE)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.execute(
                    update(Partner).where(Partner.user_id == entry.partner_user_id)
                    .values(balance_kop=Partner.balance_kop + entry.amount_kop)
                )
                await session.commit()
                released.append(entry)
        return released

    async def reverse_payment(self, payment_id: str) -> tuple[str, int, PartnerLedger] | None:
        """
        Возврат оплаты друга. ('cancelled', сумма, начисление) — было в холде, просто гасим;
        ('reversed', списано, начисление) — уже на балансе: списываем, но баланс не уходит ниже нуля.
        None — с этой оплаты ничего не начислялось (или возврат уже учтён).
        """
        async with self._session_maker() as session:
            entry = (await session.execute(
                select(PartnerLedger).where(PartnerLedger.kind == "accrual", PartnerLedger.payment_id == payment_id)
            )).scalar_one_or_none()
            if entry is None:
                return None

            result = await session.execute(
                update(PartnerLedger).where(PartnerLedger.id == entry.id, PartnerLedger.status == HOLD)
                .values(status=CANCELLED)
            )
            if result.rowcount == 1:
                await session.commit()
                return "cancelled", entry.amount_kop, entry

            if entry.status != AVAILABLE:
                return None

            partner = (await session.execute(
                select(Partner).where(Partner.user_id == entry.partner_user_id).with_for_update()
            )).scalar_one()
            deduct = max(0, min(entry.amount_kop, partner.balance_kop))
            partner.balance_kop -= deduct
            entry.status = REVERSED
            session.add(PartnerLedger(
                partner_user_id=entry.partner_user_id, kind="reversal", amount_kop=-deduct,
                friend_user_id=entry.friend_user_id, payment_id=payment_id,
            ))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return "reversed", deduct, entry

    # --- списания и зачисления ---------------------------------------------

    async def debit(self, partner_id: int, amount_kop: int, kind: str, payment_id: str | None = None) -> bool:
        """Списывает с баланса, если хватает. False — не хватает денег или списание уже было."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(Partner).where(Partner.user_id == partner_id, Partner.balance_kop >= amount_kop)
                .values(balance_kop=Partner.balance_kop - amount_kop)
            )
            if result.rowcount != 1:
                await session.rollback()
                return False
            session.add(PartnerLedger(partner_user_id=partner_id, kind=kind, amount_kop=-amount_kop,
                                      payment_id=payment_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def credit(self, partner_id: int, amount_kop: int, kind: str, payment_id: str | None = None) -> bool:
        """Зачисляет на баланс (возврат несостоявшейся оплаты). False — уже зачислено."""
        async with self._session_maker() as session:
            await session.execute(
                update(Partner).where(Partner.user_id == partner_id)
                .values(balance_kop=Partner.balance_kop + amount_kop)
            )
            session.add(PartnerLedger(partner_user_id=partner_id, kind=kind, amount_kop=amount_kop,
                                      payment_id=payment_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    # --- вывод ----------------------------------------------------------------

    async def create_withdrawal(self, partner_id: int, amount_kop: int,
                                phone: str, bank: str) -> PartnerWithdrawal | None:
        """Заявка на вывод: сумма сразу списывается с баланса. None — не хватает денег."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(Partner).where(Partner.user_id == partner_id, Partner.balance_kop >= amount_kop)
                .values(balance_kop=Partner.balance_kop - amount_kop)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            withdrawal = PartnerWithdrawal(partner_user_id=partner_id, amount_kop=amount_kop,
                                           sbp_phone=phone, sbp_bank=bank, status=PENDING)
            session.add(withdrawal)
            await session.flush()
            session.add(PartnerLedger(partner_user_id=partner_id, kind="withdrawal", amount_kop=-amount_kop,
                                      withdrawal_id=withdrawal.id))
            await session.commit()
            await session.refresh(withdrawal)
            return withdrawal

    async def resolve_withdrawal(self, withdrawal_id: int, *, paid: bool, admin_id: int,
                                 reason: str | None = None) -> PartnerWithdrawal | None:
        """Решение по заявке. None — заявка уже решена (второй админ, двойное нажатие)."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(PartnerWithdrawal)
                .where(PartnerWithdrawal.id == withdrawal_id, PartnerWithdrawal.status == PENDING)
                .values(status=PAID if paid else REJECTED, admin_id=admin_id,
                        reject_reason=None if paid else reason, processed_at=datetime.datetime.now())
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            withdrawal = await session.get(PartnerWithdrawal, withdrawal_id)
            if not paid:
                await session.execute(
                    update(Partner).where(Partner.user_id == withdrawal.partner_user_id)
                    .values(balance_kop=Partner.balance_kop + withdrawal.amount_kop)
                )
                session.add(PartnerLedger(partner_user_id=withdrawal.partner_user_id, kind="withdrawal_return",
                                          amount_kop=withdrawal.amount_kop, withdrawal_id=withdrawal.id))
            await session.commit()
            await session.refresh(withdrawal)
            return withdrawal

    async def get_withdrawal(self, withdrawal_id: int) -> PartnerWithdrawal | None:
        async with self._session_maker() as session:
            return await session.get(PartnerWithdrawal, withdrawal_id)

    async def pending_withdrawal(self, partner_id: int) -> PartnerWithdrawal | None:
        async with self._session_maker() as session:
            return (await session.execute(
                select(PartnerWithdrawal).where(
                    PartnerWithdrawal.partner_user_id == partner_id, PartnerWithdrawal.status == PENDING,
                ).limit(1)
            )).scalar_one_or_none()

    async def list_pending_withdrawals(self) -> list[PartnerWithdrawal]:
        async with self._session_maker() as session:
            return (await session.execute(
                select(PartnerWithdrawal).where(PartnerWithdrawal.status == PENDING).order_by(PartnerWithdrawal.id)
            )).scalars().all()

    async def set_withdrawal_message(self, withdrawal_id: int, chat_id: int, message_id: int) -> None:
        async with self._session_maker() as session:
            await session.execute(
                update(PartnerWithdrawal).where(PartnerWithdrawal.id == withdrawal_id)
                .values(notify_chat_id=chat_id, notify_message_id=message_id)
            )
            await session.commit()

    # --- отчёты -----------------------------------------------------------------

    async def history(self, partner_id: int, limit: int = 15) -> list[PartnerLedger]:
        async with self._session_maker() as session:
            return (await session.execute(
                select(PartnerLedger).where(PartnerLedger.partner_user_id == partner_id)
                .order_by(PartnerLedger.id.desc()).limit(limit)
            )).scalars().all()

    async def summary(self, partner_id: int) -> dict:
        """Цифры для карточки: в холде, ближайшее зачисление, заработано, выведено, приглашено, оплатили."""
        async with self._session_maker() as session:
            hold_kop, next_release = (await session.execute(
                select(func.coalesce(func.sum(PartnerLedger.amount_kop), 0), func.min(PartnerLedger.available_at))
                .where(PartnerLedger.partner_user_id == partner_id, PartnerLedger.kind == "accrual",
                       PartnerLedger.status == HOLD)
            )).one()
            earned_kop, paid_friends = (await session.execute(
                select(func.coalesce(func.sum(PartnerLedger.amount_kop), 0),
                       func.count(func.distinct(PartnerLedger.friend_user_id)))
                .where(PartnerLedger.partner_user_id == partner_id, PartnerLedger.kind == "accrual",
                       PartnerLedger.status.in_((HOLD, AVAILABLE)))
            )).one()
            withdrawn_kop = (await session.execute(
                select(func.coalesce(func.sum(PartnerWithdrawal.amount_kop), 0))
                .where(PartnerWithdrawal.partner_user_id == partner_id, PartnerWithdrawal.status == PAID)
            )).scalar_one()
            invited = (await session.execute(
                select(func.count()).select_from(User)
                .where(and_(User.referrer_id == partner_id, User.partner_referred.is_(True)))
            )).scalar_one()
        if isinstance(next_release, str):   # SQLite отдаёт агрегат по дате строкой
            next_release = datetime.datetime.fromisoformat(next_release)
        return {
            "hold_kop": int(hold_kop), "next_release": next_release, "earned_kop": int(earned_kop),
            "paid_friends": int(paid_friends), "withdrawn_kop": int(withdrawn_kop), "invited": int(invited),
        }

    async def debt_summary(self) -> dict:
        """
        Сколько магазин должен партнёрам (для админской статистики), в копейках:
        balance_kop — на балансах (можно вывести или потратить), hold_kop — начисления в холде
        (ещё могут сгореть при возврате), pending_kop — заявки на вывод, которые админ ещё
        не выплатил (с баланса уже списаны). Долг — сумма всех трёх.
        """
        async with self._session_maker() as session:
            balance_kop, partners = (await session.execute(
                select(func.coalesce(func.sum(Partner.balance_kop), 0),
                       func.count().filter(Partner.balance_kop > 0))
            )).one()
            hold_kop = (await session.execute(
                select(func.coalesce(func.sum(PartnerLedger.amount_kop), 0))
                .where(PartnerLedger.kind == "accrual", PartnerLedger.status == HOLD)
            )).scalar_one()
            pending_kop, pending_count = (await session.execute(
                select(func.coalesce(func.sum(PartnerWithdrawal.amount_kop), 0), func.count())
                .where(PartnerWithdrawal.status == PENDING)
            )).one()
        balance_kop, hold_kop, pending_kop = int(balance_kop), int(hold_kop), int(pending_kop)
        return {
            "total_kop": balance_kop + hold_kop + pending_kop, "balance_kop": balance_kop,
            "hold_kop": hold_kop, "pending_kop": pending_kop, "pending_count": int(pending_count),
            "partners_with_balance": int(partners),
        }

    async def manager_fee_for_payment(self, payment_id: str) -> float:
        """
        Услуга менеджера внутри оплаты (это деньги менеджера — процент с неё не считается).
        Если услугу взяли наличными мимо ЮKassa (fee_in_cash), в сумме оплаты её нет — 0.
        """
        async with self._session_maker() as session:
            fee = (await session.execute(
                select(ManagerOperation.service_fee).where(
                    ManagerOperation.payment_id == payment_id, ManagerOperation.fee_in_cash.is_(False),
                ).limit(1)
            )).scalar_one_or_none()
            return float(fee or 0)
