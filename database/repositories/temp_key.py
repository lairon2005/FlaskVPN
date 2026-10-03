"""Временные ключи менеджеров."""
import datetime

from sqlalchemy import select, update, func

from db import TempKey


class TempKeyRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create(self, manager_id: int, operation_id: int, rw_username: str, rw_uuid: str | None,
                     expires_at: datetime.datetime, client_user_id: int | None = None) -> TempKey:
        async with self._session_maker() as session:
            key = TempKey(
                manager_id=manager_id, operation_id=operation_id, rw_username=rw_username,
                rw_uuid=rw_uuid, expires_at=expires_at, client_user_id=client_user_id,
            )
            session.add(key)
            await session.commit()
            await session.refresh(key)
            return key

    async def get(self, key_id: int) -> TempKey | None:
        async with self._session_maker() as session:
            return await session.get(TempKey, key_id)

    async def get_by_operation(self, operation_id: int) -> TempKey | None:
        async with self._session_maker() as session:
            result = await session.execute(select(TempKey).where(TempKey.operation_id == operation_id))
            return result.scalar_one_or_none()

    async def list_due(self, now: datetime.datetime, limit: int = 100) -> list[TempKey]:
        """Активные ключи, срок которых вышел."""
        async with self._session_maker() as session:
            stmt = (
                select(TempKey)
                .where(TempKey.status == 'active', TempKey.expires_at <= now)
                .order_by(TempKey.expires_at).limit(limit)
            )
            return (await session.execute(stmt)).scalars().all()

    async def list_expiring(self, now: datetime.datetime, until: datetime.datetime) -> list[TempKey]:
        """Активные ключи, которые закончатся до `until`, а напоминания по ним ещё не было."""
        async with self._session_maker() as session:
            stmt = (
                select(TempKey)
                .where(TempKey.status == 'active', TempKey.reminded_at.is_(None),
                       TempKey.expires_at > now, TempKey.expires_at <= until)
                .order_by(TempKey.expires_at)
            )
            return (await session.execute(stmt)).scalars().all()

    async def mark_reminded(self, key_id: int) -> bool:
        """Атомарно: напоминание уходит один раз, даже если джобы пересеклись."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(TempKey).where(TempKey.id == key_id, TempKey.reminded_at.is_(None))
                .values(reminded_at=datetime.datetime.now())
            )
            await session.commit()
            return result.rowcount > 0

    async def list_stuck_converting(self, older_than: datetime.datetime) -> list[TempKey]:
        """Ключи, зависшие в 'converting' (оплата не дошла) после истечения срока."""
        async with self._session_maker() as session:
            stmt = select(TempKey).where(TempKey.status == 'converting', TempKey.expires_at <= older_than)
            return (await session.execute(stmt)).scalars().all()

    async def list_converting_for_client(self, client_user_id: int) -> list[TempKey]:
        async with self._session_maker() as session:
            stmt = select(TempKey).where(TempKey.client_user_id == client_user_id, TempKey.status == 'converting')
            return (await session.execute(stmt)).scalars().all()

    async def list_active_for_manager(self, manager_id: int) -> list[TempKey]:
        async with self._session_maker() as session:
            stmt = (
                select(TempKey)
                .where(TempKey.manager_id == manager_id, TempKey.status.in_(('active', 'converting')))
                .order_by(TempKey.expires_at)
            )
            return (await session.execute(stmt)).scalars().all()

    async def transition(self, key_id: int, from_status: str, to_status: str, **fields) -> bool:
        """Условная смена статуса (атомарно): active → converting и т.п."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(TempKey).where(TempKey.id == key_id, TempKey.status == from_status)
                .values(status=to_status, **fields)
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_deleted(self, key_id: int) -> bool:
        return await self.transition_any(key_id, ('active', 'converting'), 'deleted',
                                         deleted_at=datetime.datetime.now())

    async def transition_any(self, key_id: int, from_statuses: tuple[str, ...], to_status: str, **fields) -> bool:
        async with self._session_maker() as session:
            result = await session.execute(
                update(TempKey).where(TempKey.id == key_id, TempKey.status.in_(from_statuses))
                .values(status=to_status, **fields)
            )
            await session.commit()
            return result.rowcount > 0

    async def bump_attempts(self, key_id: int) -> int:
        async with self._session_maker() as session:
            result = await session.execute(
                update(TempKey).where(TempKey.id == key_id)
                .values(delete_attempts=TempKey.delete_attempts + 1)
                .returning(TempKey.delete_attempts)
            )
            value = result.scalar_one_or_none()
            await session.commit()
            return value or 0

    async def conversion_stats(self, manager_id: int | None, since: datetime.datetime) -> dict:
        """Сколько временных ключей выдано и сколько стало платными подписками."""
        async with self._session_maker() as session:
            stmt = select(TempKey.status, func.count()).where(TempKey.created_at >= since).group_by(TempKey.status)
            if manager_id is not None:
                stmt = stmt.where(TempKey.manager_id == manager_id)
            counts = {status: count for status, count in (await session.execute(stmt)).all()}
        total = sum(counts.values())
        return {"total": total, "converted": counts.get("converted", 0), "by_status": counts}
