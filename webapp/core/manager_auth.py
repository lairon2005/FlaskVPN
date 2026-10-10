# webapp/core/manager_auth.py
"""
Вход менеджера на сайт: логин и пароль → сессионная cookie.

Логин назначает админ, пароль менеджер задаёт сам по одноразовой ссылке из бота
(после приглашения, по кнопке в панели или после сброса админом). От подбора:
5 неверных паролей подряд закрывают вход по логину на 15 минут (ManagerService),
плюс здесь — лимит неудачных попыток с одного IP по всем логинам сразу.
О каждом входе менеджеру приходит уведомление в бот с кнопкой «завершить все входы».

Сессия — JWT в отдельной cookie `mgr_session` (путь /manager, HttpOnly, SameSite=Strict),
НЕ в `access_token`: пользовательская сессия кабинета и менеджерская не взаимозаменяемы.
В токене — версия сессии: блокировка менеджера или смена его прав увеличивает версию в БД,
и все выданные ранее сессии мгновенно перестают действовать.

CSRF: токен лежит в самой сессии и дублируется в заголовке X-CSRF-Token (fetch) или поле формы.
Плюс проверка Origin — вторая линия поверх SameSite.
"""
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from tgbot.services import manager_service
from tgbot.services.manager_service import ManagerView
from webapp.core.origin import origin_allowed  # noqa: F401  (реэкспорт: роутер панели берёт отсюда)
from webapp.core.security import ALGORITHM, SECRET_KEY

COOKIE_NAME = "mgr_session"
COOKIE_PATH = "/manager"
SESSION_HOURS = 12
# «Запомнить меня»: сессия живёт месяц и переживает закрытие браузера.
REMEMBER_DAYS = 30
TOKEN_TYPE = "manager"

# С одного IP — не больше стольких неудачных входов за окно (по всем логинам).
IP_MAX_FAILURES = 20
IP_WINDOW_SECONDS = 15 * 60
_ip_failures: dict[str, deque] = {}


@dataclass(frozen=True)
class ManagerSession:
    manager: ManagerView
    csrf: str


def session_lifetime(remember: bool) -> timedelta:
    return timedelta(days=REMEMBER_DAYS) if remember else timedelta(hours=SESSION_HOURS)


def create_session_token(manager_id: int, version: int, remember: bool = False) -> tuple[str, str]:
    """Возвращает (jwt, csrf)."""
    csrf = secrets.token_urlsafe(24)
    token = jwt.encode(
        {
            "type": TOKEN_TYPE, "sub": str(manager_id), "ver": version, "csrf": csrf, "rem": bool(remember),
            "exp": datetime.utcnow() + session_lifetime(remember),
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )
    return token, csrf


def set_session_cookie(response, request: Request, manager_id: int, version: int, remember: bool) -> None:
    """Без «запомнить» cookie сессионная (уходит с закрытием браузера), JWT всё равно живёт не дольше 12 ч."""
    token, _csrf = create_session_token(manager_id, version, remember)
    response.set_cookie(
        COOKIE_NAME, token, max_age=int(session_lifetime(True).total_seconds()) if remember else None,
        path=COOKIE_PATH, httponly=True, secure=request.url.scheme == "https", samesite="strict",
    )


def client_ip(request: Request) -> str:
    """Адрес клиента: за nginx — из X-Real-IP (его ставит наш nginx), иначе адрес соединения."""
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else "") or "").strip()


def ip_blocked(ip: str) -> bool:
    attempts = _ip_failures.get(ip)
    if not attempts:
        return False
    cutoff = time.monotonic() - IP_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    if not attempts:
        _ip_failures.pop(ip, None)
        return False
    return len(attempts) >= IP_MAX_FAILURES


def register_ip_failure(ip: str) -> None:
    if len(_ip_failures) > 10_000:  # память не должна расти от перебора с тысяч адресов
        _ip_failures.clear()
    _ip_failures.setdefault(ip, deque()).append(time.monotonic())


def describe_device(user_agent: str | None) -> str | None:
    """«Chrome · Windows» из User-Agent — для уведомления о входе. Точность не важна, важно узнать «не моё»."""
    ua = user_agent or ""
    if not ua:
        return None
    browser = next((name for token, name in (
        ("YaBrowser", "Яндекс Браузер"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
        ("Chrome/", "Chrome"), ("Safari/", "Safari"),
    ) if token in ua), "браузер")
    system = next((name for token, name in (
        ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
        ("Mac OS X", "macOS"), ("Linux", "Linux"),
    ) if token in ua), None)
    return f"{browser} · {system}" if system else browser


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
