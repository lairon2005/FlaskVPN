# webapp/routers/legal.py
"""Юридические страницы: оферта, политика конфиденциальности, оплата и возврат.

Ссылки на них требует площадка приёма платежей — до этого в подвале висели
мёртвые `href="#"`. Реквизиты подставляются из .env через глобал `site`:
выдуманных ИНН и адресов в шаблонах нет.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from db import User
from webapp.dependencies import get_current_user
from webapp.templating import templates

router = APIRouter(tags=["legal"])

_PAGES = {
    "offer": ("Публичная оферта", "legal/offer.html"),
    "privacy": ("Политика конфиденциальности", "legal/privacy.html"),
    "refund": ("Оплата и возврат", "legal/refund.html"),
}


def _render(request: Request, user: User | None, slug: str) -> HTMLResponse:
    title, template = _PAGES[slug]
    return templates.TemplateResponse(template, {
        "request": request,
        "title": title,
        "user": user,
    })


@router.get("/offer", response_class=HTMLResponse)
async def offer(request: Request, user: User = Depends(get_current_user)):
    return _render(request, user, "offer")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request, user: User = Depends(get_current_user)):
    return _render(request, user, "privacy")


@router.get("/refund", response_class=HTMLResponse)
async def refund(request: Request, user: User = Depends(get_current_user)):
    return _render(request, user, "refund")
