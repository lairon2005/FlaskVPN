"""
Менеджеры и их рабочий доступ к клиентам.

Все выборки «чужих» данных (клиенты менеджера) отдают ORM-объекты только
сервису, который собирает из них DTO с белым списком полей, — репозиторий
решений о приватности не принимает.
"""
import datetime

from sqlalchemy import select, update, func, and_, or_

from db import Manager, ManagerClient, User

ACTIVE = "active"
INVITED = "invited"
BLOCKED = "blocked"
DELETED = "deleted"

# Права, которые админ может менять тумблерами / числами.
RIGHTS_FIELDS = (
    "can_issue_tariff", "can_issue_custom", "can_issue_temp", "can_accept_cash",
    "can_view_global_stats", "temp_keys_per_day", "cash_limit",
)


class ManagerRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    # --- менеджеры ---------------------------------------------------------

    async def create_invited(self, display_name: str, created_by: int,
                             invite_token_hash: str, invite_expires_at: datetime.datetime) -> Manager:
        async with self._session_maker() as session:
            manager = Manager(
                display_name=display_name[:64],
                status=INVITED,
                created_by=created_by,
                invite_token_hash=invite_token_hash,
                invite_expires_at=invite_expires_at,
            )
            session.add(manager)
            await session.commit()
            await session.refresh(manager)
            return manager

    async def get(self, manager_id: int) -> Manager | None:
        async with self._session_maker() as session:
            return await session.get(Manager, manager_id)

    async def get_by_telegram_id(self, telegram_id: int) -> Manager | None:
        async with self._session_maker() as session:
            result = await session.execute(select(Manager).where(Manager.telegram_id == telegram_id))
            return result.scalar_one_or_none()

    async def list_all(self, include_deleted: bool = False) -> list[Manager]:
        async with self._session_maker() as session:
            stmt = select(Manager).order_by(Manager.id)
            if not include_deleted:
                stmt = stmt.where(Manager.status != DELETED)
            return (await session.execute(stmt)).scalars().all()

    async def set_invite(self, manager_id: int, token_hash: str, expires_at: datetime.datetime) -> bool:
        """Новая ссылка-приглашение (старая перестаёт работать). Только для ещё не принявших."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(Manager)
                .where(Manager.id == manager_id, Manager.status == INVITED)
                .values(invite_token_hash=token_hash, invite_expires_at=expires_at)
            )
            await session.commit()
            return result.rowcount > 0

    async def accept_invite(self, token_hash: str, telegram_id: int) -> Manager | None:
        """
        Принимает приглашение: привязывает Telegram ID и делает менеджера активным.

        Атомарно (UPDATE ... WHERE ... RETURNING): одну ссылку нельзя принять
        дважды и нельзя принять после истечения. Один Telegram-аккаунт — один менеджер.
        """
        async with self._session_maker() as session:
            taken = await session.execute(
                select(Manager.id).where(Manager.telegram_id == telegram_id, Manager.status != DELETED)
            )
            if taken.first() is not None:
                return None
            result = await session.execute(
                update(Manager)
                .where(
                    Manager.invite_token_hash == token_hash,
                    Manager.status == INVITED,
                    Manager.invite_expires_at > datetime.datetime.now(),
                )
                .values(telegram_id=telegram_id, status=ACTIVE,
                        invite_token_hash=None, invite_expires_at=None)
                .returning(Manager)
            )
            manager = result.scalar_one_or_none()
            await session.commit()
            return manager

    async def set_status(self, manager_id: int, status: str) -> bool:
        """Меняет статус и сбрасывает веб-сессии (session_version + 1)."""
        async with self._session_maker() as session:
            values = {"status": status, "session_version": Manager.session_version + 1}
            if status in (BLOCKED, DELETED):
                values.update(login_token_hash=None, login_token_expires_at=None)
            if status == DELETED:
                values.update(invite_token_hash=None, invite_expires_at=None)
            result = await session.execute(update(Manager).where(Manager.id == manager_id).values(**values))
            await session.commit()
            return result.rowcount > 0

    async def update_rights(self, manager_id: int, **fields) -> bool:
        unknown = set(fields) - set(RIGHTS_FIELDS)
        if unknown:
            raise ValueError(f"Неизвестные поля прав: {sorted(unknown)}")
        async with self._session_maker() as session:
            result = await session.execute(
                update(Manager).where(Manager.id == manager_id)
                .values(**fields, session_version=Manager.session_version + 1)
            )
            await session.commit()
            return result.rowcount > 0

    async def rename(self, manager_id: int, display_name: str) -> bool:
        async with self._session_maker() as session:
            result = await session.execute(
                update(Manager).where(Manager.id == manager_id).values(display_name=display_name[:64])
            )
            await session.commit()
            return result.rowcount > 0

    # --- вход на сайт ------------------------------------------------------

    async def set_login_token(self, manager_id: int, token_hash: str, expires_at: datetime.datetime) -> None:
        async with self._session_maker() as session:
            await session.execute(
                update(Manager).where(Manager.id == manager_id, Manager.status == ACTIVE)
                .values(login_token_hash=token_hash, login_token_expires_at=expires_at)
            )
            await session.commit()

    async def consume_login_token(self, token_hash: str) -> Manager | None:
        """Одноразовая ссылка входа: гасится атомарно в момент использования."""
        async with self._session_maker() as session:
            result = await session.execute(
                update(Manager)
                .where(
                    Manager.login_token_hash == token_hash,
                    Manager.login_token_expires_at > datetime.datetime.now(),
                    Manager.status == ACTIVE,
                )
                .values(login_token_hash=None, login_token_expires_at=None)
                .returning(Manager)
            )
            manager = result.scalar_one_or_none()
            await session.commit()
            return manager

    async def bump_session(self, manager_id: int) -> None:
        async with self._session_maker() as session:
            await session.execute(
                update(Manager).where(Manager.id == manager_id)
                .values(session_version=Manager.session_version + 1)
            )
            await session.commit()


class ManagerClientRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    @staticmethod
    def _active_filter(now: datetime.datetime):
        return and_(
            ManagerClient.revoked_at.is_(None),
            or_(ManagerClient.access_until.is_(None), ManagerClient.access_until > now),
        )

    async def grant(self, manager_id: int, client_user_id: int, via: str,
                    access_until: datetime.datetime | None, label: str | None = None) -> ManagerClient:
        """Выдаёт или продлевает доступ. Отозванный доступ при повторной выдаче оживает."""
        async with self._session_maker() as session:
            result = await session.execute(
                select(ManagerClient).where(
                    ManagerClient.manager_id == manager_id,
                    ManagerClient.client_user_id == client_user_id,
                )
            )
            record = result.scalar_one_or_none()
            if record:
                record.revoked_at = None
                record.granted_via = via
                record.granted_at = datetime.datetime.now()
                record.access_until = access_until
                if label:
                    record.label = label[:64]
            else:
                record = ManagerClient(
                    manager_id=manager_id, client_user_id=client_user_id, granted_via=via,
                    access_until=access_until, label=label[:64] if label else None,
                )
                session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get_active(self, manager_id: int, client_user_id: int) -> ManagerClient | None:
        async with self._session_maker() as session:
            result = await session.execute(
                select(ManagerClient).where(
                    ManagerClient.manager_id == manager_id,
                    ManagerClient.client_user_id == client_user_id,
                    self._active_filter(datetime.datetime.now()),
                )
            )
            return result.scalar_one_or_none()

    async def extend(self, manager_id: int, client_user_id: int, access_until: datetime.datetime) -> None:
        """Продлевает действующий доступ (никогда не сокращает)."""
        async with self._session_maker() as session:
            await session.execute(
                update(ManagerClient)
                .where(
                    ManagerClient.manager_id == manager_id,
                    ManagerClient.client_user_id == client_user_id,
                    ManagerClient.revoked_at.is_(None),
                    or_(ManagerClient.access_until.is_(None), ManagerClient.access_until < access_until),
                )
                .values(access_until=access_until)
            )
            await session.commit()

    async def set_label(self, manager_id: int, client_user_id: int, label: str | None) -> bool:
        async with self._session_maker() as session:
            result = await session.execute(
                update(ManagerClient)
                .where(ManagerClient.manager_id == manager_id, ManagerClient.client_user_id == client_user_id)
                .values(label=label[:64] if label else None)
            )
            await session.commit()
            return result.rowcount > 0

    async def list_for_manager(self, manager_id: int, limit: int = 8, offset: int = 0
                               ) -> list[tuple[ManagerClient, User]]:
        async with self._session_maker() as session:
            stmt = (
                select(ManagerClient, User)
                .join(User, User.user_id == ManagerClient.client_user_id)
                .where(ManagerClient.manager_id == manager_id,
                       self._active_filter(datetime.datetime.now()))
                .order_by(ManagerClient.granted_at.desc())
                .limit(limit).offset(offset)
            )
            return [(c, u) for c, u in (await session.execute(stmt)).all()]

    async def count_for_manager(self, manager_id: int) -> int:
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(ManagerClient).where(
                ManagerClient.manager_id == manager_id,
                self._active_filter(datetime.datetime.now()),
            )
            return (await session.execute(stmt)).scalar_one()

    async def revoke(self, client_user_id: int, manager_id: int | None = None) -> int:
        """Отзывает доступ клиентом (всех менеджеров или одного). Возвращает число отозванных."""
        async with self._session_maker() as session:
            stmt = (
                update(ManagerClient)
                .where(ManagerClient.client_user_id == client_user_id, ManagerClient.revoked_at.is_(None))
                .values(revoked_at=datetime.datetime.now())
            )
            if manager_id is not None:
                stmt = stmt.where(ManagerClient.manager_id == manager_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    async def managers_with_access(self, client_user_id: int) -> list[int]:
        async with self._session_maker() as session:
            stmt = select(ManagerClient.manager_id).where(
                ManagerClient.client_user_id == client_user_id,
                self._active_filter(datetime.datetime.now()),
            )
            return list((await session.execute(stmt)).scalars().all())
