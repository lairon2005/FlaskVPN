"""Журнал операций менеджеров и инкассация. Записи не удаляются."""
import datetime

from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError

from db import ManagerOperation, ManagerPayout, ManagerSettlement

# Поля, которые можно менять после создания: журнал — это история, а не рабочая
# таблица, поэтому снимки (клиент, тариф, цена) намеренно не перезаписываются.
MUTABLE_FIELDS = {
    "status", "error_code", "payment_id", "payment_method", "key_username",
    "key_fingerprint", "key_expires_at", "completed_at", "receipt_chat_id",
    "receipt_message_id", "client_user_id", "client_code", "price", "price_details",
    "traffic_gb", "days", "tariff_name", "tariff_id", "extra_devices",
    "invoice_chat_id", "invoice_message_id", "service_fee", "fee_in_cash",
}

# Сколько из цены операции — деньги магазина (без услуги менеджера).
_SHOP_PART = ManagerOperation.price - ManagerOperation.service_fee


class ManagerOperationRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create_idempotent(self, idempotency_key: str, **fields) -> tuple[ManagerOperation, bool]:
        """
        Создаёт операцию либо возвращает уже существующую с тем же ключом.

        Двойное нажатие «подтвердить» не должно выдать два ключа и два чека:
        уникальный idempotency_key — единственный надёжный замок (проверка
        «есть ли такая» без него проигрывает гонке двух запросов).
        """
        async with self._session_maker() as session:
            operation = ManagerOperation(idempotency_key=idempotency_key, **fields)
            session.add(operation)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.execute(
                    select(ManagerOperation).where(ManagerOperation.idempotency_key == idempotency_key)
                )
                return existing.scalar_one(), False
            await session.refresh(operation)
            return operation, True

    async def add_event(self, manager_id: int, op_type: str, **fields) -> ManagerOperation:
        """Запись без идемпотентности — для лёгких событий (просмотр ключа, выдача доступа)."""
        async with self._session_maker() as session:
            operation = ManagerOperation(
                manager_id=manager_id, op_type=op_type, status='completed',
                completed_at=datetime.datetime.now(), **fields,
            )
            session.add(operation)
            await session.commit()
            await session.refresh(operation)
            return operation

    async def get(self, operation_id: int) -> ManagerOperation | None:
        async with self._session_maker() as session:
            return await session.get(ManagerOperation, operation_id)

    async def get_by_payment_id(self, payment_id: str) -> ManagerOperation | None:
        async with self._session_maker() as session:
            result = await session.execute(
                select(ManagerOperation).where(ManagerOperation.payment_id == payment_id)
            )
            return result.scalar_one_or_none()

    async def update(self, operation_id: int, **fields) -> bool:
        unknown = set(fields) - MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Эти поля журнала менять нельзя: {sorted(unknown)}")
        async with self._session_maker() as session:
            result = await session.execute(
                update(ManagerOperation).where(ManagerOperation.id == operation_id).values(**fields)
            )
            await session.commit()
            return result.rowcount > 0

    async def transition(self, operation_id: int, from_status: str, to_status: str, **fields) -> bool:
        """Условная смена статуса: срабатывает, только если операция ещё в from_status."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(ManagerOperation)
                .where(ManagerOperation.id == operation_id, ManagerOperation.status == from_status)
                .values(status=to_status, **fields)
            )
            await session.commit()
            return result.rowcount > 0

    async def transition_any(self, operation_id: int, from_statuses: tuple[str, ...], to_status: str,
                             **fields) -> bool:
        """Условная смена статуса из любого из перечисленных."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(ManagerOperation)
                .where(ManagerOperation.id == operation_id, ManagerOperation.status.in_(from_statuses))
                .values(status=to_status, **fields)
            )
            await session.commit()
            return result.rowcount > 0

    async def list_for_manager(self, manager_id: int, limit: int = 10, offset: int = 0,
                               op_types: tuple[str, ...] | None = None) -> list[ManagerOperation]:
        async with self._session_maker() as session:
            stmt = (
                select(ManagerOperation)
                .where(ManagerOperation.manager_id == manager_id)
                .order_by(ManagerOperation.id.desc())
                .limit(limit).offset(offset)
            )
            if op_types:
                stmt = stmt.where(ManagerOperation.op_type.in_(op_types))
            return (await session.execute(stmt)).scalars().all()

    async def list_for_client(self, manager_id: int, client_user_id: int, limit: int = 10) -> list[ManagerOperation]:
        async with self._session_maker() as session:
            stmt = (
                select(ManagerOperation)
                .where(ManagerOperation.manager_id == manager_id,
                       ManagerOperation.client_user_id == client_user_id)
                .order_by(ManagerOperation.id.desc()).limit(limit)
            )
            return (await session.execute(stmt)).scalars().all()

    async def list_for_client_any(self, client_user_id: int, limit: int = 10) -> list[ManagerOperation]:
        """Операции по клиенту у любых менеджеров — для чеков в его кабинете."""
        async with self._session_maker() as session:
            stmt = (
                select(ManagerOperation)
                .where(ManagerOperation.client_user_id == client_user_id)
                .order_by(ManagerOperation.id.desc()).limit(limit)
            )
            return (await session.execute(stmt)).scalars().all()

    async def list_all(self, limit: int = 50, offset: int = 0,
                       manager_id: int | None = None) -> list[ManagerOperation]:
        async with self._session_maker() as session:
            stmt = select(ManagerOperation).order_by(ManagerOperation.id.desc()).limit(limit).offset(offset)
            if manager_id is not None:
                stmt = stmt.where(ManagerOperation.manager_id == manager_id)
            return (await session.execute(stmt)).scalars().all()

    async def list_pending_online(self) -> list[ManagerOperation]:
        async with self._session_maker() as session:
            stmt = select(ManagerOperation).where(ManagerOperation.status == 'pending_payment')
            return (await session.execute(stmt)).scalars().all()

    async def count_today(self, manager_id: int, op_type: str,
                          since: datetime.datetime | None = None) -> int:
        """Сколько операций типа сегодня (для дневного лимита временных ключей)."""
        since = since or datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(ManagerOperation).where(
                ManagerOperation.manager_id == manager_id,
                ManagerOperation.op_type == op_type,
                ManagerOperation.created_at >= since,
                ManagerOperation.status != 'failed',
            )
            return (await session.execute(stmt)).scalar_one()

    async def cash_outstanding(self, manager_id: int) -> tuple[float, int]:
        """
        «К сдаче»: сумма и число проведённых наличных операций без инкассации.
        Услуга менеджера в сумму не входит — её он оставляет себе.
        """
        async with self._session_maker() as session:
            stmt = select(func.coalesce(func.sum(_SHOP_PART), 0), func.count()).where(
                ManagerOperation.manager_id == manager_id,
                ManagerOperation.payment_method == 'cash',
                ManagerOperation.status == 'completed',
                ManagerOperation.settled_at.is_(None),
            )
            total, count = (await session.execute(stmt)).one()
            return float(total), count

    async def settle(self, manager_id: int, admin_id: int) -> ManagerSettlement | None:
        """
        Инкассация: помечает все несданные наличные операции менеджера сданными.

        Одна транзакция: сумма берётся из тех же строк, которые помечаются, так что
        операция, проведённая за миг до нажатия, либо входит в сумму, либо остаётся
        несданной — но не теряется.
        """
        async with self._session_maker() as session:
            now = datetime.datetime.now()
            settlement = ManagerSettlement(manager_id=manager_id, amount=0, operations_count=0, admin_id=admin_id)
            session.add(settlement)
            await session.flush()

            result = await session.execute(
                update(ManagerOperation)
                .where(
                    ManagerOperation.manager_id == manager_id,
                    ManagerOperation.payment_method == 'cash',
                    ManagerOperation.status == 'completed',
                    ManagerOperation.settled_at.is_(None),
                )
                .values(settled_at=now, settlement_id=settlement.id)
                .returning(_SHOP_PART)
            )
            prices = [row[0] for row in result.all()]
            if not prices:
                await session.rollback()
                return None
            settlement.amount = float(sum(prices))
            settlement.operations_count = len(prices)
            await session.commit()
            await session.refresh(settlement)
            return settlement

    async def fees_outstanding(self, manager_id: int) -> tuple[float, int]:
        """
        «К выплате менеджеру»: его услуги из онлайн-продаж, которые пришли магазину
        через ЮKassa. Сейчас по QR услугу берут наличными (fee_in_cash) — копятся только
        старые продажи. Возвраты (status='refunded') не считаются.
        """
        async with self._session_maker() as session:
            stmt = select(func.coalesce(func.sum(ManagerOperation.service_fee), 0), func.count()).where(
                ManagerOperation.manager_id == manager_id,
                ManagerOperation.payment_method == 'online',
                ManagerOperation.status == 'completed',
                ManagerOperation.service_fee > 0,
                ManagerOperation.fee_in_cash.is_(False),
                ManagerOperation.fee_paid_at.is_(None),
            )
            total, count = (await session.execute(stmt)).one()
            return float(total), count

    async def payout_fees(self, manager_id: int, admin_id: int) -> ManagerPayout | None:
        """Выплата менеджеру: как инкассация, но в обратную сторону — одна транзакция, сумма из тех же строк."""
        async with self._session_maker() as session:
            now = datetime.datetime.now()
            payout = ManagerPayout(manager_id=manager_id, amount=0, operations_count=0, admin_id=admin_id)
            session.add(payout)
            await session.flush()

            result = await session.execute(
                update(ManagerOperation)
                .where(
                    ManagerOperation.manager_id == manager_id,
                    ManagerOperation.payment_method == 'online',
                    ManagerOperation.status == 'completed',
                    ManagerOperation.service_fee > 0,
                    ManagerOperation.fee_in_cash.is_(False),
                    ManagerOperation.fee_paid_at.is_(None),
                )
                .values(fee_paid_at=now, fee_payout_id=payout.id)
                .returning(ManagerOperation.service_fee)
            )
            fees = [row[0] for row in result.all()]
            if not fees:
                await session.rollback()
                return None
            payout.amount = float(sum(fees))
            payout.operations_count = len(fees)
            await session.commit()
            await session.refresh(payout)
            return payout

    async def stats(self, manager_id: int | None, since: datetime.datetime) -> list[tuple]:
        """
        Сводка за период: [(op_type, payment_method, count, revenue, fees)] только по
        завершённым операциям; fees — услуга менеджера внутри revenue. manager_id=None — по всем (для админа).
        """
        async with self._session_maker() as session:
            stmt = (
                select(
                    ManagerOperation.op_type, ManagerOperation.payment_method,
                    func.count(), func.coalesce(func.sum(ManagerOperation.price), 0),
                    func.coalesce(func.sum(ManagerOperation.service_fee), 0),
                )
                .where(ManagerOperation.status == 'completed', ManagerOperation.created_at >= since)
                .group_by(ManagerOperation.op_type, ManagerOperation.payment_method)
            )
            if manager_id is not None:
                stmt = stmt.where(ManagerOperation.manager_id == manager_id)
            return [(t, m, c, float(r), float(f)) for t, m, c, r, f in (await session.execute(stmt)).all()]

    async def settlements(self, manager_id: int, limit: int = 10) -> list[ManagerSettlement]:
        async with self._session_maker() as session:
            stmt = (
                select(ManagerSettlement).where(ManagerSettlement.manager_id == manager_id)
                .order_by(ManagerSettlement.id.desc()).limit(limit)
            )
            return (await session.execute(stmt)).scalars().all()
