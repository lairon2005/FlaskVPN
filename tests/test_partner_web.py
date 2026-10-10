"""
Реферальная система и кабинет партнёра на сайте — по HTTP, на настоящей БД (SQLite),
настоящих PartnerService, ReferralService и PaymentService.

Регистрация: `/register?ref=<id>` → код из письма → `/verify-email` → друг привязан к
рефереру тем же referral_service.attach_referrer, что и /start в боте; друг активного
партнёра помечен partner_referred, и с его оплат партнёру идут проценты.

Кабинет партнёра (`/profile/partner` на сайте, `/tma/partner` в Mini App): баланс, ссылки,
реквизиты СБП, заявка на вывод, оплата подписки с баланса, история — с проверкой Origin,
без отражения параметров в разметку и с идемпотентной оплатой.
"""
import datetime
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from jose import jwt

from manager_env import build_env
from test_partner import _load, ps, rules

# Всё, что роутеры тянут сами, импортируем ДО patch.dict(sys.modules): модуль, впервые
# загруженный внутри блока, при выходе из него выгружается, и следующий импорт создаёт
# вторую копию классов (так jose/cryptography падали на «Expected instance of HashAlgorithm»).
jwt.encode({"warm": 1}, "k", algorithm="HS256")
import config  # noqa: E402,F401
import webapp.core.origin  # noqa: E402,F401
import webapp.core.security  # noqa: E402,F401
import webapp.core.verification  # noqa: E402,F401
import webapp.dependencies  # noqa: E402,F401
import webapp.templating  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://example.com"
ORIGIN = {"origin": BASE}
PARTNER, FRIEND, REGULAR = 100, 200, 300
SURFACES = (("/profile/partner", "/profile/", "/login"), ("/tma/partner", "/tma/referral", None))


def _pkg(name):
    module = types.ModuleType(name)
    module.__path__ = []
    return module


class WebEnv(unittest.IsolatedAsyncioTestCase):
    """SQLite + сервисы + FastAPI с роутерами регистрации и кабинета партнёра."""

    async def asyncSetUp(self):
        from database.repositories.partner import PartnerRepository
        self.env = await build_env(settings={"partner_hold_days": 14})
        self.users = self.env.repos.users
        self.repo = PartnerRepository(self.env.session_maker)
        self.notifier = SimpleNamespace(
            accrual=AsyncMock(), reversal=AsyncMock(), released=AsyncMock(),
            withdrawal_created=AsyncMock(return_value=None), withdrawal_resolved=AsyncMock(),
            balance_payment=AsyncMock(),
        )
        self.partners = ps.PartnerService(self.repo, self.users, self.env.repos.settings,
                                          tariff_repo=self.env.repos.tariffs, payment_service=self.env.payments)
        self.partners.notifier = self.notifier
        self.env.payments.partner_service = self.partners

        referral_module = _load("referral_service_web_test", ROOT / "tgbot" / "services" / "referral_service.py", {
            "database.repositories.user": SimpleNamespace(UserRepository=object),
            "tgbot.services.subscription_service": SimpleNamespace(SubscriptionService=object),
            "loader": SimpleNamespace(logger=Mock()),
        })
        self.referral = referral_module.ReferralService(self.users, self.env.subscription, partner_repo=self.repo)

        self.mail = []
        self.app = self.build_app(referral_module)

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    def build_app(self, referral_module):
        services = _pkg("tgbot.services")
        services.partner_service = self.partners
        services.referral_service = self.referral
        services.user_service = SimpleNamespace(get_user=self.users.get)

        async def send_verification_email(email, code):
            self.mail.append((email, code))

        mail = types.ModuleType("webapp.core.mail")
        mail.send_verification_email = send_verification_email
        mail.send_reset_code = AsyncMock()
        mail.MailSendError = type("MailSendError", (Exception,), {})

        stubs = {
            "tgbot": _pkg("tgbot"), "tgbot.services": services,
            "tgbot.services.partner_service": ps, "tgbot.services.partner_rules": rules,
            "tgbot.services.referral_service": referral_module,
            "tgbot.services.utils": _load("tgbot.services.utils", ROOT / "tgbot" / "services" / "utils.py"),
            "webapp.core.mail": mail,
        }
        with patch.dict(sys.modules, stubs):
            import webapp.dependencies as dependencies
            auth = _load("webapp_auth_under_test", ROOT / "webapp" / "routers" / "auth.py")
            partner = _load("webapp_partner_under_test", ROOT / "webapp" / "routers" / "partner.py")

        # Обе «настоящие» точки входа в БД смотрят в SQLite этого теста.
        self._patches = [
            patch.object(dependencies, "async_session_maker", self.env.session_maker),
            patch.object(auth, "async_session_maker", self.env.session_maker),
            patch.object(partner, "config", SimpleNamespace(
                tg_bot=SimpleNamespace(tg_bot_username="flaskbot", tma_app_name="app"))),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.partner_module = partner

        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(partner.router)
        return app

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE, follow_redirects=False)

    # --- данные ---------------------------------------------------------------------

    async def make_partner(self, user_id=PARTNER):
        await self.env.make_telegram_client(user_id)
        await self.partners.add_partner(str(user_id), admin_id=1)

    async def login_as(self, client, user_id):
        from webapp.core.security import create_access_token
        client.cookies.set("access_token", f"Bearer {create_access_token({'sub': str(user_id)})}")

    async def pay(self, user_id, amount, yk_id, source="web"):
        tariff = (await self.env.repos.tariffs.get_active())[1]
        await self.env.payments.create_payment_record(
            yookassa_payment_id=yk_id, user_id=user_id, tariff_id=tariff.id, original_amount=amount,
            final_amount=amount, source=source, kind="subscription",
        )
        await self.env.payments.process_successful_payment(yk_id, amount)

    async def release_holds(self):
        from sqlalchemy import update
        from db import PartnerLedger
        async with self.env.session_maker() as session:
            await session.execute(update(PartnerLedger).values(
                available_at=datetime.datetime.now() - datetime.timedelta(minutes=1)))
            await session.commit()
        await self.partners.release_holds()

    async def partner_with_balance(self, rub=3000):
        """Партнёр, друг которого заплатил `rub` ₽: на балансе 30% после холда."""
        await self.make_partner()
        await self.env.make_telegram_client(FRIEND, referrer_id=PARTNER, partner_referred=True)
        await self.pay(FRIEND, rub, "yk-friend")
        await self.release_holds()

    async def balance(self):
        return (await self.repo.get(PARTNER)).balance_kop


# =============================================================================
# Регистрация на сайте по реферальной ссылке
# =============================================================================

class WebRegistrationTests(WebEnv):
    async def register(self, ref=None, email="friend@example.com"):
        """Весь путь браузера: страница → форма → код из письма → кабинет. Возвращает нового пользователя."""
        async with self.client() as client:
            page = await client.get("/register" + (f"?ref={ref}" if ref is not None else ""))
            self.assertEqual(page.status_code, 200)
            if ref is not None:
                self.assertIn(f'name="ref" value="{ref}"', page.text)
            form = {"email": email, "password": "secret-123", "full_name": "Друг"}
            if ref is not None:
                form["ref"] = str(ref)
            sent = await client.post("/register", data=form)
            self.assertEqual(sent.status_code, 200)
            token = re.search(r'name="registration_token" value="([^"]+)"', sent.text).group(1)
            self.assertEqual(self.mail[-1][0], email)
            done = await client.post("/verify-email", data={"registration_token": token, "code": self.mail[-1][1]})
            self.assertEqual(done.status_code, 302, done.text[:500])
            self.assertEqual(done.headers["location"], "/profile/")
            self.assertIn("access_token", done.headers.get("set-cookie", ""))
        from sqlalchemy import select
        from db import User
        async with self.env.session_maker() as session:
            return (await session.execute(select(User).where(User.email == email))).scalar_one()

    async def test_friend_of_partner_is_attributed_to_partner(self):
        await self.make_partner()
        user = await self.register(PARTNER)
        self.assertLess(user.user_id, 0)                        # веб-пользователь — отрицательный id
        self.assertEqual((user.referrer_id, user.partner_referred), (PARTNER, True))
        self.assertEqual((await self.partners.overview(PARTNER)).invited, 1)

    async def test_friend_of_regular_user_gets_a_regular_referrer(self):
        await self.env.make_telegram_client(REGULAR)
        user = await self.register(REGULAR)
        self.assertEqual((user.referrer_id, user.partner_referred), (REGULAR, False))

    async def test_disabled_partner_brings_a_regular_friend(self):
        await self.make_partner()
        await self.partners.disable(PARTNER, admin_id=1)
        user = await self.register(PARTNER)
        self.assertEqual((user.referrer_id, user.partner_referred), (PARTNER, False))

    async def test_web_user_can_invite_too(self):
        inviter = await self.register(None, email="inviter@example.com")
        friend = await self.register(inviter.user_id, email="friend@example.com")
        self.assertEqual(friend.referrer_id, inviter.user_id)

    async def test_unknown_and_garbage_refs_are_ignored(self):
        for i, ref in enumerate(("999999", "abc", "0")):
            with self.subTest(ref=ref):
                user = await self.register(ref, email=f"u{i}@example.com")
                self.assertIsNone(user.referrer_id)
                self.assertFalse(user.partner_referred)

    async def test_registration_survives_a_broken_referral_service(self):
        await self.make_partner()
        with patch.object(self.referral, "attach_referrer", AsyncMock(side_effect=RuntimeError("db hiccup"))):
            user = await self.register(PARTNER)
        self.assertIsNone(user.referrer_id)                     # без приглашения, но вход есть

    async def test_payment_of_web_friend_pays_the_partner(self):
        """Сквозной сценарий: регистрация по ссылке на сайте → оплата на сайте → 30% партнёру."""
        await self.make_partner()
        friend = await self.register(PARTNER)
        await self.pay(friend.user_id, 149, "yk-web-1")
        ov = await self.partners.overview(PARTNER)
        self.assertEqual((ov.hold_kop, ov.paid_friends), (4470, 1))
        self.notifier.accrual.assert_awaited_once()

    async def test_web_friend_of_regular_user_brings_payment_days_not_money(self):
        await self.env.make_telegram_client(REGULAR)
        friend = await self.register(REGULAR)
        self.assertIsNone(await self.partners.overview(REGULAR))
        # бонус +7 рефереру считает referral_service — партнёрских начислений нет
        self.assertEqual(await self.referral.process_first_payment_bonus(friend.user_id), REGULAR)


# =============================================================================
# Кабинет партнёра: сайт и Mini App
# =============================================================================

class AccessTests(WebEnv):
    async def test_anonymous(self):
        async with self.client() as client:
            web = await client.get("/profile/partner")
            self.assertEqual((web.status_code, web.headers["location"]), (302, "/login"))
            tma = await client.get("/tma/partner")
            self.assertEqual(tma.status_code, 200)              # сплэш авторизации Mini App
            self.assertNotIn("Баланс", tma.text)
            for base, _, _ in SURFACES:
                response = await client.post(f"{base}/withdraw", data={"amount": "500"}, headers=ORIGIN)
                self.assertEqual(response.status_code, 401)

    async def test_not_a_partner_goes_back_to_the_regular_screens(self):
        await self.env.make_telegram_client(REGULAR)
        async with self.client() as client:
            await self.login_as(client, REGULAR)
            for base, home, _ in SURFACES:
                page = await client.get(base)
                self.assertEqual((page.status_code, page.headers["location"]), (302, home))
                response = await client.post(f"{base}/requisites", data={"phone": "+79991234567", "bank": "Т-Банк"},
                                             headers=ORIGIN)
                self.assertIn("err=not_partner", response.headers["location"])

    async def test_unknown_action(self):
        await self.make_partner()
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            self.assertEqual((await client.post("/profile/partner/steal", headers=ORIGIN)).status_code, 404)

    async def test_posts_from_another_site_are_rejected(self):
        await self.partner_with_balance()
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            for headers in ({}, {"origin": "https://evil.example"}, {"referer": "https://evil.example/x"},
                            {"origin": "null"}):
                for base, _, _ in SURFACES:
                    response = await client.post(f"{base}/requisites",
                                                 data={"phone": "+79990000000", "bank": "Злой банк"}, headers=headers)
                    self.assertEqual(response.status_code, 403, (base, headers))
        partner = await self.repo.get(PARTNER)
        self.assertIsNone(partner.sbp_phone)


class PageTests(WebEnv):
    async def test_page_shows_balance_links_and_history(self):
        await self.partner_with_balance()
        await self.pay(FRIEND, 1000, "yk-friend-2")            # ещё 300 ₽ — в холде
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            for base, _, _ in SURFACES:
                page = await client.get(base)
                self.assertEqual(page.status_code, 200, base)
                text = page.text
                self.assertIn("900 ₽", text)                    # баланс
                self.assertIn("300 ₽", text)                    # холд
                self.assertIn("30% с оплат друзей", text)
                self.assertIn("https://t.me/flaskbot?start=ref100", text)
                self.assertIn(f"{BASE}/register?ref=100", text)
                self.assertIn("3 дня бесплатно", text)
                self.assertIn("С оплаты друга", text)
                self.assertIn("в холде", text)
                self.assertIn(f'action="{base}/requisites"', text)
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            tma = (await client.get("/tma/partner")).text
            self.assertIn('id="tmaShareRefBtn"', tma)
            self.assertIn("tma-tabbar", tma)
            web = (await client.get("/profile/partner")).text
            self.assertNotIn('id="tmaShareRefBtn"', web)
            self.assertIn('href="/profile/"', web)

    async def test_disabled_partner_sees_balance_but_no_links(self):
        await self.partner_with_balance()
        await self.partners.disable(PARTNER, admin_id=1)
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            page = (await client.get("/profile/partner")).text
        self.assertIn("Программа отключена", page)
        self.assertIn("900 ₽", page)
        self.assertNotIn("start=ref100", page)

    async def test_query_params_are_never_reflected(self):
        await self.make_partner()
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            page = await client.get("/profile/partner", params={"ok": "<script>x</script>", "err": "<b>boom</b>"})
        self.assertNotIn("<script>x</script>", page.text)
        self.assertNotIn("boom", page.text)
        self.assertNotIn('class="alert alert-error', page.text)


class RequisitesAndWithdrawalTests(WebEnv):
    async def post(self, client, path, **data):
        response = await client.post(path, data=data, headers=ORIGIN)
        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        follow = await client.get(location.split("#")[0])
        return location, follow.text

    async def test_requisites_then_withdrawal(self):
        await self.partner_with_balance()
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            location, page = await self.post(client, "/profile/partner/withdraw", amount="500")
            self.assertIn("err=no_requisites", location)
            self.assertIn("Сначала укажите реквизиты", page)

            location, page = await self.post(client, "/profile/partner/requisites", phone="8 (999) 123-45-67", bank="Т-Банк")
            self.assertIn("ok=requisites", location)
            self.assertIn("+79991234567", page)
            self.assertIn('action="/profile/partner/withdraw"', page)
            self.assertIn('value="900"', page)                # по умолчанию — весь баланс

            location, page = await self.post(client, "/profile/partner/withdraw", amount="600")
            self.assertIn("ok=withdrawal", location)
            self.assertIn("на рассмотрении", page)
        self.assertEqual(await self.balance(), 30000)           # 900 − 600
        pending = await self.repo.pending_withdrawal(PARTNER)
        self.assertEqual((pending.amount_kop, pending.sbp_phone, pending.sbp_bank), (60000, "+79991234567", "Т-Банк"))
        self.notifier.withdrawal_created.assert_awaited_once()

    async def test_bad_requisites(self):
        await self.make_partner()
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            location, page = await self.post(client, "/tma/partner/requisites", phone="12345", bank="Т-Банк")
            self.assertIn("err=bad_phone", location)
            self.assertIn("Не похоже на номер телефона", page)
            location, _ = await self.post(client, "/tma/partner/requisites", phone="+79991234567", bank="x")
            self.assertIn("err=bad_bank", location)
        self.assertIsNone((await self.repo.get(PARTNER)).sbp_phone)

    async def test_withdrawal_rules(self):
        await self.partner_with_balance()
        await self.partners.set_requisites(PARTNER, "+79991234567", "Т-Банк")
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            pages = {}
            for amount, code in (("abc", "bad_amount"), ("-5", "bad_amount"), ("499,99", "below_min"),
                                 ("901", "insufficient")):
                with self.subTest(amount=amount):
                    location, pages[code] = await self.post(client, "/tma/partner/withdraw", amount=amount)
                    self.assertIn(f"err={code}", location)
            self.assertIn("Минимальная сумма вывода — 500 ₽", pages["below_min"])
            self.assertIn("недостаточно средств", pages["insufficient"])
            self.assertIn("Нужна сумма числом", pages["bad_amount"])
            await self.post(client, "/tma/partner/withdraw", amount="500")
            location, _ = await self.post(client, "/tma/partner/withdraw", amount="500")
            self.assertIn("err=pending_exists", location)
        self.assertEqual(await self.balance(), 40000)           # одна заявка на 500 ₽ из 900

    async def test_small_balance_has_no_withdrawal_form(self):
        await self.partner_with_balance(rub=1000)               # 300 ₽ < минимума 500 ₽
        await self.partners.set_requisites(PARTNER, "+79991234567", "Т-Банк")
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            page = (await client.get("/profile/partner")).text
        self.assertNotIn('action="/profile/partner/withdraw"', page)
        self.assertIn("Вывод — от 500 ₽. Сейчас на балансе 300 ₽.", page)


class BalancePaymentTests(WebEnv):
    async def test_pay_subscription_with_balance(self):
        await self.partner_with_balance()
        tariff = (await self.env.repos.tariffs.get_active())[1]          # «Месяц», 149 ₽
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            page = (await client.get("/profile/partner")).text
            self.assertIn(f'href="/profile/partner?pay={tariff.id}#pay"', page)

            confirm = (await client.get(f"/profile/partner?pay={tariff.id}")).text
            self.assertIn("Баланс 900 ₽ → 751 ₽", confirm)
            nonce = re.search(r'name="nonce" value="([0-9a-f]{12})"', confirm).group(1)

            response = await client.post("/profile/partner/pay", data={"tariff_id": tariff.id, "nonce": nonce},
                                         headers=ORIGIN)
            self.assertIn("ok=paid", response.headers["location"])
            again = await client.post("/profile/partner/pay", data={"tariff_id": tariff.id, "nonce": nonce},
                                      headers=ORIGIN)
            self.assertIn("ok=replayed", again.headers["location"])      # двойное нажатие — одна оплата

        self.assertEqual(await self.balance(), 90000 - 14900)
        partner_user = await self.users.get(PARTNER)
        self.assertIsNotNone(partner_user.subscription_end_date)
        payment = await self.env.payments.get_payment(f"balance:{PARTNER}:{nonce}")
        self.assertEqual((payment.status, payment.source, payment.final_amount), ("succeeded", "balance", 149.0))

    async def test_not_enough_balance_and_forged_forms(self):
        await self.partner_with_balance(rub=300)                # 90 ₽ — ни на один тариф
        expensive = (await self.env.repos.tariffs.get_active())[2]
        async with self.client() as client:
            await self.login_as(client, PARTNER)
            page = (await client.get(f"/tma/partner?pay={expensive.id}")).text
            self.assertNotIn('name="nonce"', page)               # подтверждения нет — не хватает
            self.assertIn("не хватает", page)
            for data, code in (({"tariff_id": expensive.id, "nonce": "a" * 12}, "insufficient"),
                               ({"tariff_id": expensive.id, "nonce": "<x>"}, "bad_request"),
                               ({"tariff_id": "1; drop", "nonce": "a" * 12}, "bad_request"),
                               ({"tariff_id": 999, "nonce": "b" * 12}, "tariff_unavailable")):
                with self.subTest(data=data):
                    response = await client.post("/tma/partner/pay", data=data, headers=ORIGIN)
                    self.assertIn(f"err={code}", response.headers["location"])
        self.assertEqual(await self.balance(), 9000)


# =============================================================================
# Другие экраны: карточка в кабинете и рефералка Mini App
# =============================================================================

class ScreensTests(WebEnv):
    def render(self, name, **ctx):
        from starlette.requests import Request
        from webapp.templating import templates
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
                           "server": ("example.com", 443), "scheme": "https", "root_path": ""})
        return templates.get_template(name).render(request=request, **ctx)

    async def test_dashboard_card(self):
        await self.partner_with_balance()
        ov = await self.partners.overview(PARTNER)
        user = await self.users.get(PARTNER)
        # без VPN-аккаунта (ветка «попробуйте бесплатно») карточка партнёра тоже есть
        html = self.render("dashboard.html", user=user, partner=ov, partner_balance="900 ₽", title="Кабинет")
        self.assertIn('href="/profile/partner"', html)
        self.assertIn("900 ₽", html)
        subscribed = SimpleNamespace(**{**user.__dict__, "vpn_username": "u"})
        vpn = {"status": "active", "expire": None, "used_traffic": 0, "data_limit": 0}
        html = self.render("dashboard.html", user=subscribed, vpn_data=vpn, partner=ov, partner_balance="900 ₽",
                           title="Кабинет")
        self.assertEqual(html.count('href="/profile/partner"'), 1)
        self.assertNotIn("Приглашайте друзей", html)
        regular = self.render("dashboard.html", user=subscribed, vpn_data=vpn,
                              referrer_launch_bonus="3 дня", referrer_payment_bonus="7 дней", title="Кабинет")
        self.assertNotIn('href="/profile/partner"', regular)
        self.assertIn("+3 дня", regular)
        self.assertNotIn("30 бонусных дней", regular)

    async def test_tma_referral_links_to_partner_cabinet(self):
        await self.partner_with_balance()
        ov = await self.partners.overview(PARTNER)
        user = await self.users.get(PARTNER)
        html = self.render("tma/referral.html", user=user, partner=ov, partner_balance="900 ₽", referral_count=1,
                           ref_link="https://t.me/flaskbot/app?startapp=100", title="Рефералка")
        self.assertIn('href="/tma/partner"', html)
        self.assertNotIn("в боте", html)


if __name__ == "__main__":
    unittest.main()
