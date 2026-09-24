# webapp/main.py
import asyncio
import mimetypes
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware 
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

from db import User
from webapp.routers import auth, dashboard, legal, payment, tma
from webapp.dependencies import get_current_user
from webapp.templating import templates
from loader import logger, remnawave_client, shutdown_logging, config
from typing import Optional
from db import Tariff
from database import tariff_repo
from tgbot.services import device_slot_service
from webapp.routers.tma import intro_consents



@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.remnawave_client = remnawave_client
    try:
        yield  # Здесь приложение работает (принимает запросы)
    finally:
        try:
            await remnawave_client.aclose()
        finally:
            await asyncio.to_thread(shutdown_logging)

app = FastAPI(lifespan=lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

# --- Статика (создаем папку, если нет) ---
# StaticFiles берёт Content-Type из mimetypes, а в python:3.11.8-alpine нет ни
# этих типов в таблице, ни /etc/mime.types — коллаж лендинга и фирменный шрифт
# уезжали как text/plain. Браузеры такое досниффивают, но с nosniff или через
# кэширующий прокси картинка превратится в текст.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

static_dir = "webapp/static"
if not os.path.exists(static_dir):
    os.makedirs(static_dir)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# --- Шаблоны ---
# Фильтры и глобалы (timestamp_to_date, site, now_year) живут в webapp/templating.py.

# --- Роутеры ---
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(legal.router)
app.include_router(payment.router)
app.include_router(tma.router)

# --- Главная ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/profile/", status_code=302)
    # Гость — потенциально новый клиент: вводный тариф показываем как новичку.
    # Право на него всё равно проверит /payment/create после входа.
    guest = SimpleNamespace(is_first_payment_made=False, intro_used=False)
    tariffs = await tariff_repo.get_active_for_user(
        guest, allow_intro=config.yookassa.save_payment_method
    )
    # Базовый лимит устройств правится из админки — на лендинге он не должен
    # расходиться с тем, что человек получит после оплаты.
    device_settings = await device_slot_service.settings()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": "Главная",
        "user": user,
        "tariffs": tariffs,
        "intro_consents": await intro_consents(tariffs),
        "device_settings": device_settings,
    })
