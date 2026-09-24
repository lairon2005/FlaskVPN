from sqlalchemy import select, update, delete

from db import Tariff


class TariffRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def get_active(self) -> list[Tariff]:
        """Активные обычные тарифы. Вводный сюда не входит: он продаётся
        только через get_active_for_user и не может быть тарифом продления."""
        async with self._session_maker() as session:
            stmt = (
                select(Tariff)
                .where(Tariff.is_active == True, Tariff.is_intro == False)
                .order_by(Tariff.price.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_active_for_user(self, user, allow_intro: bool = True) -> list[Tariff]:
        """Витрина конкретного пользователя: активные тарифы, где вводный
        показан первым и только тем, кому он положен (intro_offer.visible_tariffs).

        allow_intro=False — вводный не показывать никому (магазин не сохраняет
        карты: без карты перехода на полную цену не будет)."""
        from tgbot.services.intro_offer import visible_tariffs

        all_tariffs = await self.get_all()
        by_id = {t.id: t for t in all_tariffs}
        active = sorted(
            (t for t in all_tariffs if t.is_active and (allow_intro or not t.is_intro)),
            key=lambda t: t.price,
        )
        return visible_tariffs(active, user, by_id)

    async def get_by_id_map(self) -> dict[int, Tariff]:
        return {t.id: t for t in await self.get_all()}

    async def is_referenced(self, tariff_id: int) -> bool:
        """На тариф ссылается вводный тариф или карта автопродления — удалять нельзя."""
        from db import UserPaymentMethod
        async with self._session_maker() as session:
            by_tariff = await session.execute(
                select(Tariff.id).where(Tariff.renew_tariff_id == tariff_id).limit(1)
            )
            if by_tariff.first() is not None:
                return True
            by_card = await session.execute(
                select(UserPaymentMethod.id).where(UserPaymentMethod.renew_tariff_id == tariff_id).limit(1)
            )
            return by_card.first() is not None

    async def get_all(self) -> list[Tariff]:
        async with self._session_maker() as session:
            result = await session.execute(select(Tariff))
            return result.scalars().all()

    async def get_by_id(self, tariff_id: int) -> Tariff | None:
        async with self._session_maker() as session:
            return await session.get(Tariff, tariff_id)

    async def get_by_name_and_price(self, name: str, price: float) -> Tariff | None:
        async with self._session_maker() as session:
            stmt = select(Tariff).where(Tariff.name == name, Tariff.price == price)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def add(
        self,
        name: str,
        price: float,
        duration_days: int,
        data_limit_gb: int | None = None,
        loyalty_price: float | None = None,
        is_highlighted: bool = False,
        is_intro: bool = False,
        renew_tariff_id: int | None = None,
    ) -> Tariff:
        async with self._session_maker() as session:
            new_tariff = Tariff(
                name=name,
                price=price,
                duration_days=duration_days,
                is_active=True,
                data_limit_gb=data_limit_gb,
                loyalty_price=loyalty_price,
                is_highlighted=is_highlighted,
                is_intro=is_intro,
                renew_tariff_id=renew_tariff_id,
            )
            session.add(new_tariff)
            await session.commit()
            return new_tariff

    async def update_field(self, tariff_id: int, field: str, value):
        async with self._session_maker() as session:
            stmt = update(Tariff).where(Tariff.id == tariff_id).values({field: value})
            await session.execute(stmt)
            await session.commit()

    async def delete_by_id(self, tariff_id: int):
        async with self._session_maker() as session:
            stmt = delete(Tariff).where(Tariff.id == tariff_id)
            await session.execute(stmt)
            await session.commit()
