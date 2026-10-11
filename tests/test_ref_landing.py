"""
Реферальная ссылка ведёт на главную (`/?ref=<id>`), а не на форму регистрации.

Человек читает лендинг, потом жмёт «Начать» — на `/register` без параметров. Реферер
между этими страницами живёт в cookie `ref`; дальше путь прежний (форма → код из письма →
`/verify-email` → `referral_service.attach_referrer`). Проверяем по HTTP на настоящей
БД (SQLite), что приглашение не теряется, и что cookie входа переживает закрытие браузера.
"""
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from test_partner import _load
from test_partner_web import WebEnv, ROOT, _pkg

NINETY_DAYS = 90 * 24 * 3600


class _Anything:
    """Настройки лендинга: шаблону важны только имена, значения — любые числа."""
    def __getattr__(self, name):
        return 1


def load_root_route():
    """Настоящий обработчик `/` из webapp/main.py; тяжёлое окружение (loader, панель, БД тарифов) — заглушки."""
    from fastapi import APIRouter
    routers = _pkg("webapp.routers")
    stubs = {"webapp.routers": routers}
    for name in ("auth", "client_link", "dashboard", "legal", "manager", "partner", "payment", "tma"):
        module = types.ModuleType(f"webapp.routers.{name}")
        module.router = APIRouter()
        setattr(routers, name, module)
        stubs[f"webapp.routers.{name}"] = module
    stubs["webapp.routers.tma"].intro_consents = AsyncMock(return_value={})
    stubs["loader"] = SimpleNamespace(
        logger=None, remnawave_client=None, shutdown_logging=lambda: None,
        config=SimpleNamespace(yookassa=SimpleNamespace(save_payment_method=False)))
    stubs["database"] = SimpleNamespace(tariff_repo=SimpleNamespace(get_active_for_user=AsyncMock(return_value=[])))
    stubs["tgbot.services"] = SimpleNamespace(
        device_slot_service=SimpleNamespace(settings=AsyncMock(return_value=_Anything())),
        traffic_service=SimpleNamespace(settings=AsyncMock(return_value=_Anything())))
    return _load("webapp_main_under_test", ROOT / "webapp" / "main.py", stubs)


class RefLandingTests(WebEnv):
    def build_app(self, referral_module):
        app = super().build_app(referral_module)
        main = load_root_route()
        main.auth = self.auth_module                    # настоящий auth.set_ref_cookie
        app.add_api_route("/", main.read_root, methods=["GET"], response_class=HTMLResponse)
        return app

    async def register_via_landing(self, ref, email="friend@example.com"):
        """Главная с ?ref= → «Начать» (/register без параметров) → форма → письмо → кабинет."""
        async with self.client() as client:
            home = await client.get(f"/?ref={ref}")
            self.assertEqual(home.status_code, 200)
            self.assertIn("ref=", home.headers.get("set-cookie", ""))
            page = await client.get("/register")                 # без ?ref= — как по кнопке «Начать»
            self.assertEqual(page.status_code, 200)
            ref_in_form = re.search(r'name="ref" value="([^"]*)"', page.text)
            sent = await client.post("/register", data={
                "email": email, "password": "secret-123", "full_name": "friend_nick",
                **({"ref": ref_in_form.group(1)} if ref_in_form else {})})
            token = re.search(r'name="registration_token" value="([^"]+)"', sent.text).group(1)
            done = await client.post("/verify-email", data={"registration_token": token, "code": self.mail[-1][1]})
            self.assertEqual(done.status_code, 302, done.text[:500])
            cookies = done.headers.get_list("set-cookie")
            return await self.user_by_email(email), cookies, ref_in_form

    async def user_by_email(self, email):
        from sqlalchemy import select
        from db import User
        async with self.env.session_maker() as session:
            return (await session.execute(select(User).where(User.email == email))).scalar_one()

    async def test_partner_link_to_landing_still_credits_the_partner(self):
        await self.make_partner()
        user, _, form_ref = await self.register_via_landing(100)
        self.assertEqual(form_ref.group(1), "100")
        self.assertEqual((user.referrer_id, user.partner_referred), (100, True))
        self.assertEqual((await self.partners.overview(100)).invited, 1)

    async def test_regular_user_link_to_landing(self):
        await self.env.make_telegram_client(300)
        user, _, _ = await self.register_via_landing(300)
        self.assertEqual((user.referrer_id, user.partner_referred), (300, False))

    async def test_ref_cookie_is_cleared_after_registration(self):
        await self.make_partner()
        _, cookies, _ = await self.register_via_landing(100)
        cleared = [c for c in cookies if c.startswith("ref=")]
        self.assertTrue(cleared and ("Max-Age=0" in cleared[0] or "1970" in cleared[0]), cookies)

    async def test_garbage_ref_sets_no_cookie_and_registration_works(self):
        async with self.client() as client:
            for bad in ("abc", "0", "1" * 40, "<script>"):
                home = await client.get("/", params={"ref": bad})
                self.assertEqual(home.status_code, 200)
                self.assertNotIn("ref=", home.headers.get("set-cookie", ""), bad)

    async def test_later_ref_overrides_earlier(self):
        await self.make_partner()
        await self.env.make_telegram_client(300)
        async with self.client() as client:
            await client.get("/?ref=300")
            await client.get("/?ref=100")
            page = await client.get("/register")
            self.assertIn('name="ref" value="100"', page.text)

    async def test_explicit_query_beats_cookie(self):
        async with self.client() as client:
            await client.get("/?ref=300")
            page = await client.get("/register?ref=100")
            self.assertIn('name="ref" value="100"', page.text)

    async def test_old_register_links_still_work(self):
        await self.make_partner()
        async with self.client() as client:
            page = await client.get("/register?ref=100")
            self.assertIn('name="ref" value="100"', page.text)

    async def test_no_ref_means_no_hidden_field(self):
        async with self.client() as client:
            self.assertNotIn('name="ref"', (await client.get("/register")).text)
            self.assertNotIn("ref=", (await client.get("/")).headers.get("set-cookie", ""))

    async def test_wrong_cookie_value_does_not_break_registration(self):
        async with self.client() as client:
            client.cookies.set("ref", "999999999")              # реферера не существует
            page = await client.get("/register")
            sent = await client.post("/register", data={
                "email": "x@example.com", "password": "secret-123", "full_name": "x",
                "ref": re.search(r'name="ref" value="([^"]*)"', page.text).group(1)})
            token = re.search(r'name="registration_token" value="([^"]+)"', sent.text).group(1)
            done = await client.post("/verify-email", data={"registration_token": token, "code": self.mail[-1][1]})
            self.assertEqual(done.status_code, 302)
        self.assertIsNone((await self.user_by_email("x@example.com")).referrer_id)

    async def test_register_form_has_nickname_hint(self):
        async with self.client() as client:
            page = (await client.get("/register")).text
        self.assertIn("Имя или никнейм", page)
        self.assertIn('placeholder="Например, ivan_petrov"', page)
        self.assertNotIn("Ваше имя", page)


class SessionLifetimeTests(WebEnv):
    async def test_registration_login_cookie_survives_browser_restart(self):
        await self.make_partner()
        await self.env.make_telegram_client(300)
        from webapp.core.security import get_password_hash
        from sqlalchemy import update
        from db import User
        async with self.env.session_maker() as session:
            await session.execute(update(User).where(User.user_id == 300).values(
                email="web@example.com", password_hash=get_password_hash("secret-123")))
            await session.commit()
        async with self.client() as client:
            resp = await client.post("/login", data={"email": "web@example.com", "password": "secret-123"})
            self.assertEqual(resp.status_code, 302)
            cookie = next(c for c in resp.headers.get_list("set-cookie") if c.startswith("access_token="))
            self.assertIn(f"Max-Age={NINETY_DAYS}", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=lax", cookie)

    async def test_token_lives_ninety_days(self):
        from jose import jwt
        from webapp.core.security import ALGORITHM, SECRET_KEY, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES
        from datetime import timedelta
        import time
        self.assertEqual(ACCESS_TOKEN_EXPIRE_MINUTES * 60, NINETY_DAYS)
        token = create_access_token({"sub": "1"}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
        left = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["exp"] - time.time()
        self.assertGreater(left, NINETY_DAYS - 60)


if __name__ == "__main__":
    unittest.main()
