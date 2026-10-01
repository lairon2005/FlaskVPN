# tgbot/filters/manager.py
import time
from typing import Union

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from tgbot.services import manager_service

# Фильтр стоит на роутере, который проходит КАЖДОЕ обновление от КАЖДОГО пользователя.
# Поэтому кэшируются только ОТРИЦАТЕЛЬНЫЕ ответы («не менеджер»): иначе на любое сообщение
# любого человека шёл бы запрос в БД. Менеджер же проверяется свежо каждый раз — заблокированный
# или удалённый теряет панель сразу, а не через TTL кэша (экраны навигации данных не читают
# и сами бы это не заметили). Новому менеджеру кэш сбрасывает forget() при приёме приглашения.
_NEGATIVE_TTL_SECONDS = 30
_not_manager_until: dict[int, float] = {}


def forget(telegram_id: int) -> None:
    """Сбрасывает «не менеджер» — после приёма приглашения или разблокировки."""
    _not_manager_until.pop(telegram_id, None)


class IsManager(Filter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = event.from_user
        if user is None:
            return False
        now = time.monotonic()
        if _not_manager_until.get(user.id, 0.0) > now:
            return False

        if await manager_service.get_by_telegram(user.id) is not None:
            return True
        _not_manager_until[user.id] = now + _NEGATIVE_TTL_SECONDS
        return False
