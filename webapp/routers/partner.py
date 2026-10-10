# webapp/routers/partner.py
"""
Кабинет партнёра (денежная рефералка) на сайте — `/profile/partner` — и в Mini App —
`/tma/partner`. То же, что экран «🤝 Партнёрская программа» в боте: баланс и холд,
ссылки для друзей, реквизиты СБП, заявка на вывод, оплата подписки с баланса, история.

Логика целиком в partner_service (тот же, что у бота): суммы и права из формы не
верятся, сервис перепроверяет баланс на каждом шаге. Здесь только формы и PRG:
POST → редирект на страницу с кодом итога в query (?ok= / ?err=). Текст берётся из
фиксированных словарей — значение параметра в разметку не попадает.

POST защищены проверкой Origin поверх SameSite-cookie: подменить реквизиты СБП с
чужого сайта и вывести деньги на свой номер не выйдет.
"""
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from config import load_config
from db import User
from tgbot.services import partner_service, user_service
from tgbot.services.partner_rules import fmt_rub, parse_rub
from tgbot.services.partner_service import ERRORS, PartnerError
from tgbot.services.referral_service import REFERRAL_TRIAL_DAYS
from tgbot.services.utils import decline_word
from webapp.core.origin import origin_allowed
from webapp.dependencies import get_current_user
from webapp.templating import render

router = APIRouter()
config = load_config()


@dataclass(frozen=True)
class Surface:
    base: str        # адрес кабинета, от него же — адреса форм
    template: str
    home: str        # куда отправить того, кто не партнёр
    tma: bool


WEB = Surface("/profile/partner", "partner.html", "/profile/", tma=False)
TMA = Surface("/tma/partner", "tma/partner.html", "/tma/referral", tma=True)

NOTICES = {
    "requisites": "Реквизиты для вывода сохранены.",
    "withdrawal": "Заявка на вывод отправлена. Администратор переведёт деньги вручную — "
                  "мы напишем в Telegram, когда всё будет готово.",
    "paid": "Оплачено с баланса — подписка продлена.",
    "replayed": "Эта оплата уже проведена.",
}
FORM_ERRORS = {
    "bad_amount": "Нужна сумма числом, например 1500.",
    "bad_request": "Не получилось разобрать запрос — обновите страницу и попробуйте ещё раз.",
}

LEDGER_TITLES = {
    "accrual": "С оплаты друга",
    "reversal": "Возврат оплаты друга",
    "withdrawal": "Вывод по СБП",
    "withdrawal_return": "Вывод отклонён — деньги вернулись",
    "spend": "Оплата подписки",
    "spend_return": "Оплата не прошла — деньги вернулись",
}
ACCRUAL_STATUS = {"hold": "в холде", "cancelled": "отменено", "reversed": "списано"}

_NONCE = re.compile(r"[0-9a-f]{12}")


def _redirect(surface: Surface, anchor: str = "", **params) -> RedirectResponse:
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{surface.base}{query}{anchor}", status_code=303)


def _rub_input(kop: int) -> str:
    """Копейки → значение поля суммы («1500» или «1500,50»), которое понимает parse_rub."""
    rub, kop = divmod(max(0, kop), 100)
    return f"{rub},{kop:02d}" if kop else str(rub)


def _error_text(code: str | None, ov) -> str | None:
    if not code:
        return None
    if code == "below_min":
        return f"Минимальная сумма вывода — {fmt_rub(ov.settings.min_withdrawal_kop)}."
    return ERRORS.get(code) or FORM_ERRORS.get(code)


def _links(request: Request, user_id: int) -> dict:
    """Ссылки для друзей: в бота (главная), в Mini App и на регистрацию на сайте."""
    bot = config.tg_bot.tg_bot_username
    app = config.tg_bot.tma_app_name
    return {
        "bot": f"https://t.me/{bot}?start=ref{user_id}" if bot else None,
        "tma": f"https://t.me/{bot}/{app}?startapp={user_id}" if bot and app else None,
        "web": f"{str(request.base_url).rstrip('/')}/register?ref={user_id}",
    }


def _history_rows(entries) -> list[dict]:
    rows = []
    for e in entries:
        status = ACCRUAL_STATUS.get(e.status, "") if e.kind == "accrual" else ""
        rows.append({
            "date": e.created_at.strftime("%d.%m.%Y") if e.created_at else "",
            "title": LEDGER_TITLES.get(e.kind, e.kind),
            "amount": (f"+{fmt_rub(e.amount_kop)}" if e.amount_kop > 0 else fmt_rub(e.amount_kop)),
            "positive": e.amount_kop > 0,
            "status": status,
        })
    return rows


def _offer_rows(offers, balance_kop: int) -> list[dict]:
    rows = []
    for o in offers:
        q = o.quote
        parts = [f"тариф {q.tariff_amount:.0f} ₽"]
        if q.slots_amount:
            parts.append(f"доп. устройства ({q.slots}) {q.slots_amount:.0f} ₽")
        if q.traffic_amount:
            parts.append(f"доп. трафик ({q.traffic_gb} ГБ/мес) {q.traffic_amount:.0f} ₽")
        rows.append({
            "tariff_id": o.tariff.id, "name": o.tariff.name, "days": o.tariff.duration_days,
            "total": fmt_rub(o.total_kop), "total_kop": o.total_kop, "affordable": o.affordable,
            "missing": fmt_rub(o.total_kop - balance_kop) if not o.affordable else None,
            "breakdown": ", ".join(parts),
            "balance_after": fmt_rub(balance_kop - o.total_kop) if o.affordable else None,
        })
    return rows


async def _page(request: Request, user: User | None, surface: Surface):
    if user is None:
        if surface.tma:
            return render(request, "tma/_auth_splash.html")
        return RedirectResponse(url="/login", status_code=302)

    ov = await partner_service.overview(user.user_id)
    if ov is None:
        return RedirectResponse(url=surface.home, status_code=302)

    offers = _offer_rows(await partner_service.balance_offers(user.user_id), ov.balance_kop)
    # Подтверждение оплаты с баланса — вторым экраном (?pay=<тариф>), как в боте:
    # nonce в форме делает повторную отправку безопасной.
    confirm = None
    pay = request.query_params.get("pay", "")
    if pay.isdigit():
        confirm = next((o for o in offers if o["tariff_id"] == int(pay) and o["affordable"]), None)

    links = _links(request, user.user_id)
    share_link = links["bot"] or links["web"]
    trial = f"{REFERRAL_TRIAL_DAYS} {decline_word(REFERRAL_TRIAL_DAYS, ('день', 'дня', 'дней'))}"
    fresh = await user_service.get_user(user.user_id)
    return render(request, surface.template, {
        "user": fresh or user,
        "ov": ov,
        "surface": surface,
        "balance": fmt_rub(ov.balance_kop),
        "hold": fmt_rub(ov.hold_kop),
        "earned": fmt_rub(ov.earned_kop),
        "withdrawn": fmt_rub(ov.withdrawn_kop),
        "min_withdrawal": fmt_rub(ov.settings.min_withdrawal_kop),
        "pending": ov.pending_withdrawal,
        "pending_amount": fmt_rub(ov.pending_withdrawal.amount_kop) if ov.pending_withdrawal else None,
        "amount_default": _rub_input(ov.balance_kop),
        "links": links,
        "share_text": (f"Пользуюсь стабильным VPN: 5 стран, без ограничения скорости. "
                       f"По моей ссылке {trial} бесплатно → {share_link}"),
        "trial_days_text": trial,
        "offers": offers,
        "confirm": confirm,
        "nonce": secrets.token_hex(6),
        "history": _history_rows(await partner_service.history(user.user_id, limit=15)),
        "notice": NOTICES.get(request.query_params.get("ok", "")),
        "error": _error_text(request.query_params.get("err"), ov),
        "title": "Партнёрская программа",
        "active_tab": "referral",
    })


async def _action(request: Request, user: User | None, surface: Surface, action: str):
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Bad origin")
    form = await request.form()
    try:
        if action == "requisites":
            await partner_service.set_requisites(user.user_id, str(form.get("phone") or ""),
                                                 str(form.get("bank") or ""))
            return _redirect(surface, "#withdraw", ok="requisites")
        if action == "withdraw":
            amount_kop = parse_rub(str(form.get("amount") or ""))
            if amount_kop is None:
                return _redirect(surface, "#withdraw", err="bad_amount")
            await partner_service.request_withdrawal(user.user_id, amount_kop)
            return _redirect(surface, "#withdraw", ok="withdrawal")
        if action == "pay":
            tariff_id = str(form.get("tariff_id") or "")
            nonce = str(form.get("nonce") or "")
            if not tariff_id.isdigit() or not _NONCE.fullmatch(nonce):
                return _redirect(surface, "#pay", err="bad_request")
            _, replayed = await partner_service.pay_with_balance(user.user_id, int(tariff_id), nonce)
            return _redirect(surface, ok="replayed" if replayed else "paid")
    except PartnerError as e:
        anchor = "#pay" if action == "pay" else "#withdraw"
        return _redirect(surface, anchor, err=e.code)
    raise HTTPException(status_code=404, detail="Not found")


# --- Сайт ------------------------------------------------------------------------

@router.get("/profile/partner")
async def web_partner(request: Request, user: User | None = Depends(get_current_user)):
    return await _page(request, user, WEB)


@router.post("/profile/partner/{action}")
async def web_partner_action(action: str, request: Request, user: User | None = Depends(get_current_user)):
    return await _action(request, user, WEB, action)


# --- Mini App ------------------------------------------------------------------------

@router.get("/tma/partner")
async def tma_partner(request: Request, user: User | None = Depends(get_current_user)):
    return await _page(request, user, TMA)


@router.post("/tma/partner/{action}")
async def tma_partner_action(action: str, request: Request, user: User | None = Depends(get_current_user)):
    return await _action(request, user, TMA, action)
