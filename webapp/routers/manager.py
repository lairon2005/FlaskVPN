# webapp/routers/manager.py
"""
Панель менеджера на сайте (/manager): та же логика, что в боте, поверх ManagerService.

Роутер тонкий: все права, лимиты и приватность проверяет сервис. Здесь — только
сессия (webapp/core/manager_auth.py), CSRF, отображение и JSON-ручки для страницы выдачи.
Менеджер видит только свои операции и клиентов с рабочим доступом; ничего,
что принадлежит админке (тарифы, цены, формула, настройки, другие менеджеры), здесь нет.
"""
import base64
import dataclasses
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_dt, fmt_money
from tgbot.services.manager_service import ManagerError
from tgbot.services.qr_generator import create_qr_code
from webapp.core.manager_auth import (
    COOKIE_NAME, COOKIE_PATH, SESSION_HOURS, ManagerSession, create_session_token,
    get_session, require_api_session, verify_csrf, origin_allowed,
)
from webapp.templating import render

router = APIRouter(prefix="/manager")
logger = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_FORBIDDEN_CODES = {"not_manager", "blocked", "no_right"}
HISTORY_PAGE = 20
CLIENTS_PAGE = 50


def _error(e: ManagerError) -> JSONResponse:
    status = 403 if e.code in _FORBIDDEN_CODES else 400
    return JSONResponse({"error": e.code, "message": e.message}, status_code=status, headers=_NO_STORE)


def _page(request: Request, name: str, session: ManagerSession, title: str, **ctx) -> HTMLResponse:
    response = render(request, name, {
        "manager": session.manager, "csrf": session.csrf, "title": title,
        "fmt_dt": fmt_dt, "fmt_money": fmt_money, **ctx,
    })
    response.headers.update(_NO_STORE)
    return response


async def _page_session(request: Request) -> ManagerSession | RedirectResponse:
    session = await get_session(request)
    return session or RedirectResponse("/manager/login", status_code=303)


# --- Вход / выход ----------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, t: str | None = None):
    """
    Страница подтверждения входа. Саму ссылку здесь НЕ гасим: превью ссылок в
    мессенджерах открывает их GET-запросом и сожгло бы одноразовый токен раньше
    человека. Гасит только POST, который делает кнопка.
    """
    response = render(request, "manager/login.html", {
        "token": t or "", "title": "Вход для менеджера",
        "error": None if t else "Откройте ссылку из бота: «Панель менеджера» → «Панель на сайте».",
    })
    response.headers.update(_NO_STORE)
    return response


@router.post("/login")
async def login_submit(request: Request, token: str = Form(...)):
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Bad origin")
    manager = await manager_service.consume_login_token(token)
    if manager is None:
        response = render(request, "manager/login.html", {
            "token": "", "title": "Вход для менеджера",
            "error": "Ссылка недействительна или устарела. Получите новую в боте.",
        }, status_code=400)
        response.headers.update(_NO_STORE)
        return response

    jwt_token, _csrf = create_session_token(manager.id, manager.session_version)
    response = RedirectResponse("/manager/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, jwt_token, max_age=SESSION_HOURS * 3600, path=COOKIE_PATH,
        httponly=True, secure=request.url.scheme == "https", samesite="strict",
    )
    response.headers.update(_NO_STORE)
    logger.info(f"Manager #{manager.id} logged in to the web panel")
    return response


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    session = await get_session(request)
    if session is not None:
        verify_csrf(request, session, csrf)
    response = RedirectResponse("/manager/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


# --- Страницы -----------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    manager = session.manager
    stats = await manager_service.stats(manager.id)
    recent = await manager_service.history(manager.id, 0, 6)
    return _page(request, "manager/dashboard.html", session, "Панель менеджера", stats=stats, recent=recent)


@router.get("/issue", response_class=HTMLResponse)
async def issue_page(request: Request, client: str | None = None, temp: int | None = None):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    manager = session.manager
    tariffs = await manager_service.list_tariffs()
    settings = await manager_service.custom_settings()
    clients, _total = await manager_service.list_clients(manager.id, 0, 200)
    return _page(
        request, "manager/issue.html", session, "Выдать ключ", tariffs=tariffs, max_days=settings.max_days,
        clients=clients, preselect_client=client or "", temp_key_id=temp or 0,
    )


@router.get("/temp", response_class=HTMLResponse)
async def temp_page(request: Request):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    minutes, gb, devices = await manager_service.temp_settings()
    keys = await manager_service.list_temp_keys(session.manager.id)
    return _page(request, "manager/temp.html", session, "Временный ключ",
                 minutes=minutes, gb=gb, devices=devices, keys=keys)


@router.get("/clients", response_class=HTMLResponse)
async def clients_page(request: Request, page: int = 0):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    page = max(0, page)
    rows, total = await manager_service.list_clients(session.manager.id, page, CLIENTS_PAGE)
    return _page(request, "manager/clients.html", session, "Мои клиенты", rows=rows, total=total,
                 page=page, has_next=(page + 1) * CLIENTS_PAGE < total)


@router.get("/clients/{code}", response_class=HTMLResponse)
async def client_page(request: Request, code: str):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    try:
        card = await manager_service.get_client_card(session.manager.id, code)
    except ManagerError as e:
        raise HTTPException(status_code=404 if e.code == "client_not_found" else 403, detail=e.message)
    return _page(request, "manager/client.html", session, f"Клиент {card.client_code}", card=card)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, page: int = 0):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    page = max(0, page)
    rows = await manager_service.history(session.manager.id, page, HISTORY_PAGE + 1)
    return _page(request, "manager/history.html", session, "История операций", rows=rows[:HISTORY_PAGE],
                 page=page, has_next=len(rows) > HISTORY_PAGE)


# --- JSON API -----------------------------------------------------------------------------

class QuoteRequest(BaseModel):
    product: Literal["tariff", "custom"]
    client_code: str | None = None
    tariff_id: int | None = None
    days: int | None = None


class IssueRequest(QuoteRequest):
    method: Literal["cash", "online"]
    nonce: str = Field(min_length=8, max_length=40)
    expected_total: float | None = None
    temp_key_id: int | None = None


class TempRequest(BaseModel):
    nonce: str = Field(min_length=8, max_length=40)


class CodeRequest(BaseModel):
    code: str = Field(max_length=32)


class ClientRequest(BaseModel):
    client_code: str = Field(max_length=16)


class QrRequest(BaseModel):
    text: str = Field(max_length=700)


def _quote_json(q) -> dict:
    data = dataclasses.asdict(q)
    data["hint"] = dataclasses.asdict(q.hint) if q.hint else None
    return data


@router.post("/api/quote")
async def api_quote(body: QuoteRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        quote = await manager_service.quote(
            session.manager.id, product=body.product, client_code=body.client_code or None,
            tariff_id=body.tariff_id, days=body.days,
        )
    except ManagerError as e:
        return _error(e)
    return JSONResponse(_quote_json(quote), headers=_NO_STORE)


@router.post("/api/issue")
async def api_issue(body: IssueRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        result = await manager_service.issue(
            session.manager.id, product=body.product, method=body.method, idempotency_nonce=body.nonce,
            client_code=body.client_code or None, tariff_id=body.tariff_id, days=body.days,
            expected_total=body.expected_total, temp_key_id=body.temp_key_id or None,
        )
    except ManagerError as e:
        return _error(e)
    return JSONResponse({
        "operation_id": result.operation_id, "status": result.status, "price": result.price,
        "payment_url": result.payment_url, "subscription_url": result.subscription_url,
        "client_code": result.client_code, "cabinet_url": result.cabinet_url,
        "expires_at": fmt_dt(result.expires_at) if result.expires_at else None,
        "replayed": result.replayed,
    }, headers=_NO_STORE)


@router.post("/api/temp")
async def api_temp(body: TempRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        result = await manager_service.issue_temp(session.manager.id, body.nonce)
    except ManagerError as e:
        return _error(e)
    return JSONResponse({
        "operation_id": result.operation_id, "key_id": result.key_id,
        "subscription_url": result.subscription_url, "expires_at": fmt_dt(result.expires_at),
    }, headers=_NO_STORE)


@router.post("/api/client-code")
async def api_client_code(body: CodeRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        client_code = await manager_service.add_client_by_code(session.manager.id, body.code)
    except ManagerError as e:
        return _error(e)
    return JSONResponse({"client_code": client_code}, headers=_NO_STORE)


@router.get("/api/op/{operation_id}")
async def api_operation(operation_id: int, session: ManagerSession = Depends(require_api_session)):
    brief = await manager_service.check_operation(session.manager.id, operation_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse({"status": brief.status}, headers=_NO_STORE)


@router.post("/api/op/{operation_id}/cancel")
async def api_cancel(operation_id: int, session: ManagerSession = Depends(require_api_session)):
    ok = await manager_service.cancel_pending(session.manager.id, operation_id)
    return JSONResponse({"cancelled": ok}, headers=_NO_STORE)


@router.post("/api/link")
async def api_link(body: ClientRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        url = await manager_service.get_install_link(session.manager.id, body.client_code)
    except ManagerError as e:
        return _error(e)
    return JSONResponse({"subscription_url": url}, headers=_NO_STORE)


@router.post("/api/noauto")
async def api_noauto(body: ClientRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        done = await manager_service.disable_autorenew(session.manager.id, body.client_code)
    except ManagerError as e:
        return _error(e)
    return JSONResponse({"disabled": done}, headers=_NO_STORE)


@router.post("/api/cabinet-reset")
async def api_cabinet_reset(body: ClientRequest, session: ManagerSession = Depends(require_api_session)):
    try:
        url = await manager_service.reset_cabinet_link(session.manager.id, body.client_code)
    except ManagerError as e:
        return _error(e)
    return JSONResponse({"cabinet_url": url}, headers=_NO_STORE)


@router.post("/api/qr")
async def api_qr(body: QrRequest, session: ManagerSession = Depends(require_api_session)):
    """QR-картинка для ссылки (оплата / установка). Только https-ссылки: это не универсальный генератор."""
    if not body.text.startswith("https://"):
        raise HTTPException(status_code=400, detail="Only https links")
    png = create_qr_code(body.text).getvalue()
    return JSONResponse({"data_url": "data:image/png;base64," + base64.b64encode(png).decode()}, headers=_NO_STORE)
