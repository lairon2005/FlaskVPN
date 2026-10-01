"""Одноразовые коды доступа клиента для менеджера и учёт неверных вводов."""
import datetime

from sqlalchemy import select, update, func

from db import ClientAccessCode


class ClientAccessRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create_code(self, user_id: int, code_hash: str, expires_at: datetime.datetime) -> None:
        """Новый код клиента; прежние неиспользованные коды этого клиента гасятся."""
        async with self._session_maker() as session:
            await session.execute(
                update(ClientAccessCode)
                .where(ClientAccessCode.user_id == user_id, ClientAccessCode.used_at.is_(None))
                .values(used_at=datetime.datetime.now())
            )
            session.add(ClientAccessCode(user_id=user_id, code_hash=code_hash, expires_at=expires_at))
            await session.commit()

    async def consume(self, code_hash: str, manager_id: int) -> int | None:
        """
        Погашает код и возвращает user_id клиента. Атомарно: один код — один
        менеджер, повторное и просроченное использование отклоняются.
        """
        async with self._session_maker() as session:
            result = await session.execute(
                update(ClientAccessCode)
                .where(
                    ClientAccessCode.code_hash == code_hash,
                    ClientAccessCode.used_at.is_(None),
                    ClientAccessCode.expires_at > datetime.datetime.now(),
                )
                .values(used_at=datetime.datetime.now(), used_by_manager_id=manager_id)
                .returning(ClientAccessCode.user_id)
            )
            user_id = result.scalar_one_or_none()
            await session.commit()
            return user_id

    async def log_failed_attempt(self, manager_id: int) -> None:
        """Неверный ввод — строка без кода и клиента, только менеджер и время."""
        async with self._session_maker() as session:
            session.add(ClientAccessCode(used_by_manager_id=manager_id))
            await session.commit()

    async def count_failed(self, manager_id: int, since: datetime.datetime) -> int:
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(ClientAccessCode).where(
                ClientAccessCode.used_by_manager_id == manager_id,
                ClientAccessCode.code_hash.is_(None),
                ClientAccessCode.created_at >= since,
            )
            return (await session.execute(stmt)).scalar_one()
