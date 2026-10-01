# webapp/core/manager_auth.py
"""
Вход менеджера на сайт: одноразовая ссылка из бота → сессионная cookie.

Почему не пароль: у менеджера уже есть подтверждённая личность в Telegram, а
пароль — это ещё один секрет, который нужно хранить, сбрасывать и который можно
подобрать. Ссылка живёт 5 минут и срабатывает один раз (см. ManagerService).

Сессия — JWT в отдельной cookie `mgr_session` (путь /manager, HttpOnly, SameSite=Strict),
НЕ в `access_token`: пользовательская сессия кабинета и менеджерская не взаимозаменяемы.
В токене — версия сессии: блокировка менеджера или смена его прав увеличивает версию в БД,
и все выданные ранее сессии мгновенно перестают действовать.

CSRF: токен лежит в самой сессии и дублируется в заголовке X-CSRF-Token (fetch) или поле формы.
Плюс проверка Origin — вторая линия поверх SameSite.
"""
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from tgbot.services import manager_service
from tgbot.services.manager_service import ManagerView
from webapp.core.security import ALGORITHM, SECRET_KEY

COOKIE_NAME = "mgr_session"
COOKIE_PATH = "/manager"
SESSION_HOURS = 12
TOKEN_TYPE = "manager"


@dataclass(frozen=True)
class ManagerSession:
    manager: ManagerView
    csrf: str


def create_session_token(manager_id: int, version: int) -> tuple[str, str]:
    """Возвращает (jwt, csrf)."""
    csrf = secrets.token_urlsafe(24)
    token = jwt.encode(
        {
            "type": TOKEN_TYPE, "sub": str(manager_id), "ver": version, "csrf": csrf,
            "exp": datetime.utcnow() + timedelta(hours=SESSION_HOURS),
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )
    return token, csrf


def decode_session_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    # Токены других типов (регистрация, пользовательский access) здесь не годятся.
    if payload.get("type") != TOKEN_TYPE:
        return None
    return payload


async def get_session(request: Request) -> ManagerSession | None:
    """Сессия менеджера или None. Каждый запрос сверяется с БД: статус и версия."""
    payload = decode_session_token(request.cookies.get(COOKIE_NAME))
    if payload is None:
        return None
    try:
        manager_id = int(payload["sub"])
        version = int(payload["ver"])
    except (KeyError, TypeError, ValueError):
        return None
    manager = await manager_service.get_session_manager(manager_id, version)
    if manager is None:
        return None
    return ManagerSession(manager=manager, csrf=payload.get("csrf", ""))


async def require_session(request: Request) -> ManagerSession:
    session = await get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return session


def origin_allowed(request: Request) -> bool:
    """Origin (или Referer) запроса должен совпадать с хостом сайта. Нет заголовка — отказ."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    host = urlparse(source).netloc
    return bool(host) and host == request.headers.get("host", "")


def verify_csrf(request: Request, session: ManagerSession, supplied: str | None) -> None:
    """Бросает 403, если токен не совпал или запрос пришёл с чужого источника."""
    if not supplied or not hmac.compare_digest(supplied.encode(), (session.csrf or "").encode()):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Bad origin")


async def require_api_session(request: Request) -> ManagerSession:
    """Зависимость для JSON-ручек: сессия + CSRF-заголовок."""
    session = await require_session(request)
    verify_csrf(request, session, request.headers.get("x-csrf-token"))
    return session
