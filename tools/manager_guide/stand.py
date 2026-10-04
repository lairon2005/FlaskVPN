#!/usr/bin/env python3
"""
Стенд для руководства менеджера: панель на сайте с демо-данными + экраны бота.

Запускается обычным Python проекта (нужны зависимости из requirements.txt, .env не нужен):

    python3 tools/manager_guide/stand.py --port 8765 --out /tmp/guide

Что делает:
  1. Поднимает то же окружение, что интеграционные тесты (tests/manager_env.py: SQLite,
     фейковая VPN-панель и ЮKassa) и наполняет его демо-клиентами.
  2. Прогоняет сценарии бота менеджера через настоящий Dispatcher (tests/bot_env.py) и
     пишет каждое сообщение бота — текст, подпись, кнопки — в <out>/bot.json.
  3. Пишет логин/пароль демо-менеджера и ссылку «задать пароль» в <out>/stand.json.
  4. Отдаёт панель менеджера на http://127.0.0.1:<port>/manager/ до остановки процесса.

Снимки и PDF собирает tools/manager_guide/capture.py (см. build.sh).
"""
import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]

# jinja2 / starlette — до patch.dict тестовых загрузчиков: иначе вторая копия jinja2
# и «missing» вместо пустых переменных в шаблонах.
import jinja2  # noqa: E402,F401
import starlette.templating  # noqa: E402,F401
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from bot_env import build_bot_env  # noqa: E402
from manager_env import build_env  # noqa: E402
from test_manager_web import _load_web  # noqa: E402

LOGIN, PASSWORD = "ivan", "Demo-Guide-2026"
MANAGER_TG = 7001


# --- Экраны бота ----------------------------------------------------------------------

class Recorder:
    """Собирает «экраны»: последовательности сообщений бота (и реплик менеджера) после действия."""

    def __init__(self, session, out: Path):
        self.session = session
        self.out = out
        self.screens: dict[str, list[dict]] = {}
        self._photos = 0

    def mark(self):
        return len(self.session.calls)

    def take(self, name: str, since: int, *, user_text: str | None = None, last: int | None = None,
             only: tuple[str, ...] = ("SendMessage", "EditMessageText", "SendPhoto")):
        messages = [{"from": "user", "text": user_text}] if user_text else []
        for call, data in self.session.calls[since:]:
            if call not in only:
                continue
            markup = data.get("reply_markup") or {}
            rows = [[b.get("text") for b in row] for row in (markup.get("inline_keyboard") or [])]
            message = {
                "from": "bot", "kind": "photo" if call == "SendPhoto" else "text",
                "text": data.get("text") or data.get("caption") or "", "buttons": rows,
            }
            # Настоящая картинка сообщения (чек, QR) — в макет бота вместо условного QR.
            photo = getattr(data.get("photo"), "data", None)
            if photo:
                self._photos += 1
                file_name = f"photo_{self._photos}.png"
                (self.out / file_name).write_bytes(photo)
                message["image"] = file_name
            messages.append(message)
        if last:
            messages = ([m for m in messages if m["from"] == "user"] + [m for m in messages if m["from"] == "bot"][-last:])
        self.screens[name] = messages
        return messages


async def record_bot(out: Path) -> None:
    env, bot = await build_bot_env()
    svc = env.service
    rec = Recorder(bot.session, out)

    # Приглашение и пароль
    manager, token = await svc.invite("Иван Петров", admin_id=1, login=LOGIN)
    m = rec.mark(); await bot.send(MANAGER_TG, f"/start mgr_{token}")
    rec.take("invite", m, user_text="/start")
    await env.repos.managers.update_rights(manager.id, can_accept_cash=True)

    # Главный экран
    m = rec.mark(); await bot.send(MANAGER_TG, "/manager")
    rec.take("menu", m, user_text="/manager")
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:fee"); rec.take("fee", m)
    await bot.press(MANAGER_TG, "mgr:menu")

    # Быстрая продажа: тариф → пометка → наличные
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:q:2"); rec.take("quick_preview", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:label"); rec.take("label_prompt", m)
    m = rec.mark(); await bot.send(MANAGER_TG, "Анна, кофейня на Ленина")
    rec.take("label_preview", m, user_text="Анна, кофейня на Ленина")
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:go:cash"); rec.take("cash_done", m, last=1)  # чек — последним

    # Оплата по QR
    await bot.press(MANAGER_TG, "mgr:q:3")
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:go:online"); rec.take("invoice", m)
    op_id = (await svc.history(manager.id))[0].id
    paid_caption = None
    payment = await env.payments.get_payment((await env.repos.ops.get(op_id)).payment_id)
    await env.payments.process_successful_payment(payment.yookassa_payment_id, payment.final_amount)
    await svc.on_payment_succeeded(payment.yookassa_payment_id)
    if env.notifier.invoices:
        paid_caption = env.notifier.invoices[-1][2]
    rec.screens["invoice_paid"] = [{"from": "bot", "kind": "photo", "text": paid_caption or "", "buttons": []}]

    # Любой срок
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:q:custom"); rec.take("custom_days", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:days:45"); rec.take("custom_preview", m)
    await bot.press(MANAGER_TG, "mgr:menu")

    # Пробный ключ
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:temp"); rec.take("temp_intro", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:temp_go"); rec.take("temp_done", m)
    key = (await svc.list_temp_keys(manager.id))[0]
    until = env.module.fmt_time(key["expires_at"])
    rec.screens["temp_reminder"] = [{
        "from": "bot", "kind": "text",
        "text": f"⏱ <b>Пробный ключ закончится через 10 мин</b> (в {until} МСК)\n\n"
                "Самое время предложить клиенту подписку. Оформите её на этот же ключ — "
                "переустанавливать ничего не придётся.",
        "buttons": [["💳 Оформить подписку на этот ключ"]],
    }]
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:temps"); rec.take("temp_list", m)

    # Клиенты
    code = (await svc.list_clients(manager.id))[0][-1].client_code
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:clients:0"); rec.take("clients", m)
    m = rec.mark(); await bot.press(MANAGER_TG, f"mgr:c:{code}"); rec.take("client_card", m)
    m = rec.mark(); await bot.press(MANAGER_TG, f"mgr:link:{code}"); rec.take("client_qr", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:pickc:0"); rec.take("renew_pick", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:who:code"); rec.take("by_code", m)
    await bot.press(MANAGER_TG, "mgr:menu")

    # История, статистика, вход на сайт, памятка
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:hist:0"); rec.take("history", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:stats"); rec.take("stats", m)
    await svc.set_password_by_link(await svc.create_password_link(manager.id), PASSWORD)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:web"); rec.take("web_access", m)
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:pwd"); rec.take("password_link", m)
    rec.screens["login_notice"] = [{
        "from": "bot", "kind": "text",
        "text": "🔐 <b>Вход в панель менеджера на сайте</b>\n🕒 03.10.2026 14:32 МСК\n🌐 IP: <code>95.24.17.8</code>\n"
                "💻 Chrome · Android\n\nЕсли это были не вы — завершите все входы и смените пароль.",
        "buttons": [["🚪 Это не я — завершить все входы"], ["🔑 Сменить пароль"]],
    }]
    m = rec.mark(); await bot.press(MANAGER_TG, "mgr:help"); rec.take("guide", m)

    # QR для «фото» в макетах бота: тот же генератор, что у бота.
    qr = env.module.__dict__.get("create_qr_code")
    if qr is None:
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location("qr_for_guide", ROOT / "tgbot" / "services" / "qr_generator.py")
        qr_module = module_from_spec(spec); spec.loader.exec_module(qr_module)
        qr = qr_module.create_qr_code
    (out / "qr.png").write_bytes(qr("https://sub.flaskvpn.ru/demo-guide-key").getvalue())

    (out / "bot.json").write_text(json.dumps(rec.screens, ensure_ascii=False, indent=1), encoding="utf-8")
    await env.engine.dispose()


# --- Сайт ------------------------------------------------------------------------------

async def seed_site(env) -> dict:
    svc = env.service
    # service_fee=None — как у настоящего менеджера: услуга по умолчанию (250 ₽).
    manager = await env.make_manager("Иван Петров", telegram_id=MANAGER_TG, login=LOGIN, password=PASSWORD,
                                     can_accept_cash=True, service_fee=None)
    sales = [
        dict(nonce="s1", product="tariff", tariff_id=2, label="Анна, кофейня на Ленина"),
        dict(nonce="s2", product="tariff", tariff_id=3, label="Пётр, шиномонтаж", slots=1),
        dict(nonce="s3", product="custom", tariff_id=None, days=45, label="Ольга, салон «Лилия»"),
        dict(nonce="s4", product="tariff", tariff_id=1, label=None),
    ]
    codes = []
    for sale in sales:
        nonce = sale.pop("nonce")
        result = await svc.issue(manager.id, method="cash", idempotency_nonce=nonce, **sale)
        codes.append(result.client_code)
    # У одного клиента подписка закончилась — так он выглядит в списке.
    from sqlalchemy import update
    from db import User
    async with env.session_maker() as session:
        await session.execute(update(User).where(User.client_code == codes[3]).values(
            subscription_end_date=datetime.datetime.now() - datetime.timedelta(days=3)))
        await session.commit()
    await svc.issue_temp(manager.id, "t1")
    # Оплата по QR — чтобы на главной была услуга «к выплате».
    await svc.issue(manager.id, method="online", idempotency_nonce="o1", product="tariff", tariff_id=2,
                    label="Марат, автомойка")
    payment_id = f"yk-{len(env.created_payments)}"   # так фейковая ЮKassa из manager_env называет платежи
    await env.payments.process_successful_payment(payment_id, env.created_payments[-1]["amount"])
    await svc.on_payment_succeeded(payment_id)

    # Второй менеджер — ещё без пароля: для снимка страницы «Задать пароль».
    second = await env.make_manager("Саид", telegram_id=MANAGER_TG + 1, login="said", service_fee=None)
    token = await svc.create_password_link(second.id)
    return {"login": LOGIN, "password": PASSWORD, "password_link": f"/manager/password?t={token}",
            "client_code": codes[0]}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    await record_bot(args.out)

    env = await build_env()
    info = await seed_site(env)
    web = _load_web(env)
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(ROOT / "webapp" / "static")), name="static")
    app.include_router(web["router"].router)
    (args.out / "stand.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    print(f"STAND_READY http://127.0.0.1:{args.port}", flush=True)
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")).serve()


if __name__ == "__main__":
    import os
    os.chdir(ROOT)  # шаблоны и статика ищутся относительно корня проекта
    asyncio.run(main())
