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
from starlette.background import BackgroundTask

from tgbot.services import manager_service
from tgbot.services.manager_guide import CABINET_HINT, GUIDE, INSTALL_STEPS
from tgbot.services.manager_receipts import fmt_dt, fmt_money, fmt_time
from tgbot.services.manager_service import ManagerError
from tgbot.services.qr_generator import create_qr_code
from webapp.core.manager_auth import (
    COOKIE_NAME, COOKIE_PATH, ManagerSession, client_ip, decode_session_token, describe_device,
    get_session, ip_blocked, origin_allowed, register_ip_failure, require_api_session,
    set_session_cookie, verify_csrf,
)
from webapp.templating import render

router = APIRouter(prefix="/manager")
logger = logging.getLogger(__name__)

# same-origin, а не no-referrer: при no-referrer браузер шлёт формы и fetch с `Origin: null`,
# и проверка Origin отклоняла бы каждый POST панели (так ломался вход по ссылке из бота).
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin"}
_FORBIDDEN_CODES = {"not_manager", "blocked", "no_right"}
HISTORY_PAGE = 20
CLIENTS_PAGE = 50


def _error(e: ManagerError) -> JSONResponse:
    status = 403 if e.code in _FORBIDDEN_CODES else 400
    return JSONResponse({"error": e.code, "message": e.message}, status_code=status, headers=_NO_STORE)


def _page(request: Request, name: str, session: ManagerSession, title: str, **ctx) -> HTMLResponse:
    response = render(request, name, {
        "manager": session.manager, "csrf": session.csrf, "title": title,
        "fmt_dt": fmt_dt, "fmt_money": fmt_money, "fmt_time": fmt_time,
        "install_steps": INSTALL_STEPS, "cabinet_hint": CABINET_HINT, **ctx,
    })
    response.headers.update(_NO_STORE)
    return response


async def _page_session(request: Request) -> ManagerSession | RedirectResponse:
    session = await get_session(request)
    return session or RedirectResponse("/manager/login", status_code=303)


# --- Вход / выход ----------------------------------------------------------------

def _login_page(request: Request, *, error: str | None = None, message: str | None = None,
                login: str = "", status_code: int = 200) -> HTMLResponse:
    response = render(request, "manager/login.html", {
        "title": "Вход для менеджера", "error": error, "message": message, "login": login,
    }, status_code=status_code)
    response.headers.update(_NO_STORE)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, done: int = 0):
    if await get_session(request) is not None:
        return RedirectResponse("/manager/", status_code=303)
    return _login_page(request, message="Пароль сохранён. Войдите с новым паролем." if done else None)


@router.post("/login")
async def login_submit(request: Request, login: str = Form(""), password: str = Form(""),
                       remember: str | None = Form(None)):
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Bad origin")
    ip = client_ip(request)
    if ip_blocked(ip):
        return _login_page(request, login=login, status_code=429,
                           error="Слишком много неудачных попыток входа. Попробуйте через 15 минут.")
    try:
        manager = await manager_service.authenticate(login, password)
    except ManagerError as e:
        if e.code in ("bad_credentials", "login_locked"):
            register_ip_failure(ip)
        status = 429 if e.code == "login_locked" else 403 if e.code == "blocked" else 400
        return _login_page(request, login=login, error=e.message, status_code=status)

    response = RedirectResponse("/manager/", status_code=303)
    set_session_cookie(response, request, manager.id, await manager_service.session_version(manager.id),
                       remember=bool(remember))
    response.headers.update(_NO_STORE)
    response.background = BackgroundTask(
        manager_service.notify_web_login, manager, ip or None, describe_device(request.headers.get("user-agent")),
    )
    logger.info(f"Manager #{manager.id} logged in to the web panel")
    return response


def _password_page(request: Request, token: str, owner, *, error: str | None = None,
                   status_code: int = 200) -> HTMLResponse:
    response = render(request, "manager/password.html", {
        "title": "Пароль для входа", "token": token if owner else "", "owner": owner, "error": error, "message": None,
    }, status_code=status_code)
    response.headers.update(_NO_STORE)
    return response


@router.get("/password", response_class=HTMLResponse)
async def password_page(request: Request, t: str = ""):
    """
    Форма «задать пароль» по ссылке из бота. GET ссылку не гасит: превью ссылок
    в мессенджерах открывают её сами и сожгли бы её раньше человека.
    """
    owner = await manager_service.password_link_owner(t)
    error = None if owner else ManagerError("invalid_password_link").message
    return _password_page(request, t, owner, error=error, status_code=200 if owner else 400)


@router.post("/password")
async def password_submit(request: Request, token: str = Form(""), password: str = Form(""),
                          password2: str = Form("")):
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Bad origin")
    owner = await manager_service.password_link_owner(token)
    if owner is None:
        return _password_page(request, "", None, error=ManagerError("invalid_password_link").message, status_code=400)
    if password != password2:
        return _password_page(request, token, owner, error="Пароли не совпадают.", status_code=400)
    try:
        await manager_service.set_password_by_link(token, password)
    except ManagerError as e:
        still_valid = await manager_service.password_link_owner(token)
        return _password_page(request, token if still_valid else "", still_valid, error=e.message, status_code=400)
    # Прежние сессии сброшены вместе со сменой пароля — входим заново, уже паролем.
    response = RedirectResponse("/manager/login?done=1", status_code=303)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    response.headers.update(_NO_STORE)
    return response


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    session = await get_session(request)
    if session is not None:
        verify_csrf(request, session, csrf)
    response = RedirectResponse("/manager/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, ok: int = 0):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    return _page(request, "manager/account.html", session, "Аккаунт",
                 message="Пароль изменён. Остальные входы на сайте завершены." if ok else None, error=None)


@router.post("/account/password")
async def account_password(request: Request, csrf: str = Form(""), old_password: str = Form(""),
                           password: str = Form(""), password2: str = Form("")):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    verify_csrf(request, session, csrf)

    def fail(text: str) -> HTMLResponse:
        response = _page(request, "manager/account.html", session, "Аккаунт", message=None, error=text)
        response.status_code = 400
        return response

    if password != password2:
        return fail("Новые пароли не совпадают.")
    try:
        version = await manager_service.change_password(session.manager.id, old_password, password)
    except ManagerError as e:
        return fail(e.message)
    # Текущий вход сохраняем (новая версия сессии), остальные устройства выкинуло.
    payload = decode_session_token(request.cookies.get(COOKIE_NAME)) or {}
    response = RedirectResponse("/manager/account?ok=1", status_code=303)
    set_session_cookie(response, request, session.manager.id, version, remember=bool(payload.get("rem")))
    response.headers.update(_NO_STORE)
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
    tariffs = await manager_service.list_tariffs() if manager.can_issue_tariff else []
    return _page(request, "manager/dashboard.html", session, "Панель менеджера", stats=stats, recent=recent,
                 tariffs=tariffs)


@router.get("/issue", response_class=HTMLResponse)
async def issue_page(request: Request, client: str | None = None, temp: int | None = None,
                     tariff: int | None = None, custom: int = 0):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    manager = session.manager
    tariffs = await manager_service.list_tariffs()
    settings = await manager_service.custom_settings()
    clients, _total = await manager_service.list_clients(manager.id, 0, 200)
    return _page(
        request, "manager/issue.html", session, "Продажа", tariffs=tariffs, max_days=settings.max_days,
        clients=clients, preselect_client=client or "", temp_key_id=temp or 0,
        preselect_tariff=tariff or (tariffs[0].id if tariffs else 0), preselect_custom=bool(custom),
    )


@router.get("/temp", response_class=HTMLResponse)
async def temp_page(request: Request):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    minutes, gb, devices = await manager_service.temp_settings()
    keys = await manager_service.list_temp_keys(session.manager.id)
    return _page(request, "manager/temp.html", session, "Пробный ключ",
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


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    session = await _page_session(request)
    if isinstance(session, RedirectResponse):
        return session
    return _page(request, "manager/help.html", session, "Как продавать", guide=GUIDE)


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
    # Доп. устройства и пакеты трафика на новый срок. None — как у клиента сейчас; потолки проверяет сервис.
    slots: int | None = Field(default=None, ge=0, le=1000)
    packs: int | None = Field(default=None, ge=0, le=1000)


class IssueRequest(QuoteRequest):
    method: Literal["cash", "online"]
    label: str | None = Field(default=None, max_length=200)
    nonce: str = Field(min_length=8, max_length=40)
    expected_total: float | None = None
    temp_key_id: int | None = None


class TempRequest(BaseModel):
    nonce: str = Field(min_length=8, max_length=40)


class CodeRequest(BaseModel):
    code: str = Field(max_length=32)


class ClientRequest(BaseModel):
    client_code: str = Field(max_length=16)


class LabelRequest(ClientRequest):
    label: str | None = Field(default=None, max_length=200)


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
            tariff_id=body.tariff_id, days=body.days, slots=body.slots, packs=body.packs,
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
            expected_total=body.expected_total, temp_key_id=body.temp_key_id or None, label=body.label,
            slots=body.slots, packs=body.packs,
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
        "expires_ts": int(result.expires_at.timestamp()) if result.expires_at else None,
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
    return JSONResponse({"status": brief.status, "client_code": brief.client_code}, headers=_NO_STORE)


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


@router.post("/api/label")
async def api_label(body: LabelRequest, session: ManagerSession = Depends(require_api_session)):
    label = " ".join((body.label or "").split())[:64] or None
    try:
        await manager_service.set_client_label(session.manager.id, body.client_code, label)
    except ManagerError as e:
        return _error(e)
    return JSONResponse({"label": label}, headers=_NO_STORE)


@router.post("/api/qr")
async def api_qr(body: QrRequest, session: ManagerSession = Depends(require_api_session)):
    """QR-картинка для ссылки (оплата / установка). Только https-ссылки: это не универсальный генератор."""
    if not body.text.startswith("https://"):
        raise HTTPException(status_code=400, detail="Only https links")
    png = create_qr_code(body.text).getvalue()
    return JSONResponse({"data_url": "data:image/png;base64," + base64.b64encode(png).decode()}, headers=_NO_STORE)
