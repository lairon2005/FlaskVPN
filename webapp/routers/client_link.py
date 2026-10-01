# webapp/routers/client_link.py
"""
Личный кабинет офлайн-клиента по ссылке из чека: /c/<token>.

У клиента, которого завёл менеджер, нет ни Telegram, ни email, ни пароля. Ссылка с
длинным случайным токеном — его «ключ от кабинета»: по ней он продлевает подписку и
управляет автопродлением. В БД лежит только sha256 токена. Утерянную ссылку
перевыпускает менеджер или админ — старая сразу перестаёт работать.
"""
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from database import user_repo
from tgbot.services.manager_security import hash_token
from webapp.core.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token
from webapp.templating import render

router = APIRouter()

_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@router.get("/c/{token}")
async def open_cabinet(request: Request, token: str):
    """Токен → обычная сессия кабинета (cookie access_token) → /profile/."""
    user = await user_repo.get_by_cabinet_token_hash(hash_token(token)) if 20 <= len(token) <= 100 else None
    if user is None:
        response = render(
            request, "login.html",
            {"error": "Ссылка недействительна или устарела. Попросите менеджера выдать новую."},
            status_code=404,
        )
        response.headers.update(_HEADERS)
        return response

    access_token = create_access_token(
        data={"sub": str(user.user_id), "email": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    response = RedirectResponse("/profile/", status_code=303)
    response.set_cookie(
        "access_token", f"Bearer {access_token}", httponly=True,
        secure=request.url.scheme == "https", samesite="lax",
    )
    response.headers.update(_HEADERS)
    return response
