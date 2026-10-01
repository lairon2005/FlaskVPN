# tgbot/handlers/manager/__init__.py
"""
Панель менеджера офлайн-продаж в боте.

Три роутера: `manager_invite_router` (принять приглашение, фильтра роли нет),
`manager_router` (всё остальное, фильтр IsManager на роутере) и `manager_denied_router`
(отказ на старые кнопки `mgr:` тем, кто менеджером уже не является; стоит после manager_router). Хендлеры живут в
модулях по темам и регистрируются на manager_router при импорте.
"""
from .menu import manager_router
from . import issue, clients, temp_keys  # noqa: F401  (регистрация хендлеров на manager_router)
from .invite import manager_denied_router, manager_invite_router

__all__ = ["manager_router", "manager_invite_router", "manager_denied_router"]
