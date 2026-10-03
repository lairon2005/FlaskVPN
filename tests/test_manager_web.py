"""
Веб-панель менеджера целиком: HTTP → роутер → сессия/CSRF → ManagerService → SQLite.

Проверяем то, что важно именно на веб-границе: вход по логину и паролю, ссылка «задать пароль»,
атрибуты cookie, сброс сессий при блокировке и смене пароля, CSRF, изоляция менеджеров
и то, что пользовательский токен кабинета не открывает панель.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from jose import jwt

from manager_env import build_env
from real_traffic_pricing import _load  # noqa: F401  (тот же загрузчик модулей)

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://example.com"
HEADERS = {"origin": BASE}


def _load_web(env):
    """Роутер и auth-модуль, привязанные к сервису этого окружения."""
    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    services.manager_service = env.service

    def pure(name):
        spec = importlib.util.spec_from_file_location(f"tgbot.services.{name}", ROOT / "tgbot" / "services" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    stubs = {
        "tgbot": tgbot, "tgbot.services": services,
        "tgbot.services.manager_service": env.module,
        "tgbot.services.manager_receipts": pure("manager_receipts"),
        "tgbot.services.qr_generator": pure("qr_generator"),
        "tgbot.services.manager_guide": pure("manager_guide"),
    }
    loaded = {}
    with patch.dict(sys.modules, stubs):
        sys.modules.pop("webapp.core.manager_auth", None)
        auth_spec = importlib.util.spec_from_file_location("webapp.core.manager_auth", ROOT / "webapp" / "core" / "manager_auth.py")
        auth = importlib.util.module_from_spec(auth_spec)
        sys.modules["webapp.core.manager_auth"] = auth
        auth_spec.loader.exec_module(auth)

        router_spec = importlib.util.spec_from_file_location("webapp_routers_manager_under_test", ROOT / "webapp" / "routers" / "manager.py")
        router_module = importlib.util.module_from_spec(router_spec)
        router_spec.loader.exec_module(router_module)
        loaded.update(auth=auth, router=router_module)
    sys.modules.pop("webapp.core.manager_auth", None)
    return loaded


class WebCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await build_env()
        self.svc = self.env.service
        loaded = _load_web(self.env)
        self.auth = loaded["auth"]
        app = FastAPI()
        app.include_router(loaded["router"].router)
        self.app = app

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE, follow_redirects=False)

    PASSWORD = "Секрет-2026"

    async def login(self, client, manager, remember=False):
        """Вход как в браузере: логин и пароль задаются менеджеру, если ещё не заданы."""
        view = await self.svc.view(manager.id)
        login = view.login or await self.svc.set_login(manager.id, f"mgr{manager.id}", admin_id=1)
        if not view.has_password:
            await self.svc.set_password_by_link(await self.svc.create_password_link(manager.id), self.PASSWORD)
        form = {"login": login, "password": self.PASSWORD}
        if remember:
            form["remember"] = "1"
        response = await client.post("/manager/login", data=form, headers=HEADERS)
        self.assertEqual(response.status_code, 303, response.text)
        page = await client.get("/manager/")
        self.assertEqual(page.status_code, 200)
        import re
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        return csrf

    def api(self, csrf):
        return {"origin": BASE, "x-csrf-token": csrf}


class LoginTests(WebCase):
    async def manager_with_password(self, **kw):
        return await self.env.make_manager(login="ivan", password=self.PASSWORD, **kw)

    async def post_login(self, client, login="ivan", password=None, headers=None, **extra):
        return await client.post("/manager/login", data={"login": login, "password": password or self.PASSWORD, **extra},
                                 headers=headers or HEADERS)

    async def test_login_page_renders_a_form(self):
        async with self.client() as client:
            page = await client.get("/manager/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="login"', page.text)
        self.assertIn('name="password"', page.text)
        self.assertIn('name="remember"', page.text)

    async def test_referrer_policy_keeps_origin_on_posts(self):
        """Регрессия: при no-referrer браузер шлёт `Origin: null`, и каждый POST панели получал 403."""
        manager = await self.manager_with_password()
        async with self.client() as client:
            login_page = await client.get("/manager/login")
            await self.login(client, manager)
            panel = await client.get("/manager/")
        for response in (login_page, panel):
            self.assertEqual(response.headers["referrer-policy"], "same-origin")
            self.assertNotIn('content="no-referrer"', response.text)

    async def test_successful_login(self):
        await self.manager_with_password()
        async with self.client() as client:
            response = await self.post_login(client, login="IVAN", headers={**HEADERS, "x-real-ip": "5.6.7.8",
                                                                            "user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/manager/")
            self.assertEqual((await client.get("/manager/")).status_code, 200)
            # уже вошедшего страница входа отправляет в панель
            self.assertEqual((await client.get("/manager/login")).status_code, 303)
        notice = self.env.notifier.web_logins[-1]
        self.assertEqual((notice["ip"], notice["device"]), ("5.6.7.8", "Chrome · Windows"))

    async def test_cookie_attributes_without_remember(self):
        await self.manager_with_password()
        async with self.client() as client:
            response = await self.post_login(client)
        cookie = response.headers["set-cookie"].lower()
        for part in ("mgr_session=", "httponly", "samesite=strict", "path=/manager", "secure"):
            self.assertIn(part, cookie)
        self.assertNotIn("max-age", cookie)  # сессионная cookie — уходит с закрытием браузера
        payload = jwt.decode(response.cookies["mgr_session"], self.auth.SECRET_KEY, algorithms=[self.auth.ALGORITHM])
        self.assertFalse(payload["rem"])

    async def test_remember_me_keeps_the_session_for_30_days(self):
        await self.manager_with_password()
        async with self.client() as client:
            response = await self.post_login(client, remember="1")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn(f"max-age={30 * 24 * 3600}", cookie)
        payload = jwt.decode(response.cookies["mgr_session"], self.auth.SECRET_KEY, algorithms=[self.auth.ALGORITHM])
        self.assertTrue(payload["rem"])

    async def test_wrong_password_and_unknown_login_look_the_same(self):
        await self.manager_with_password()
        async with self.client() as client:
            wrong = await self.post_login(client, password="wrong-pass")
            unknown = await self.post_login(client, login="nobody")
        self.assertEqual((wrong.status_code, unknown.status_code), (400, 400))
        self.assertIn("Неверный логин или пароль", wrong.text)
        self.assertIn("Неверный логин или пароль", unknown.text)
        self.assertNotIn("set-cookie", wrong.headers)

    async def test_lockout_is_shown(self):
        await self.manager_with_password()
        async with self.client() as client:
            for _ in range(4):
                await self.post_login(client, password="wrong-pass")
            locked = await self.post_login(client, password="wrong-pass")
            still = await self.post_login(client)
        self.assertEqual((locked.status_code, still.status_code), (429, 429))

    async def test_ip_limit_across_logins(self):
        await self.manager_with_password()
        self.auth._ip_failures.clear()
        headers = {**HEADERS, "x-real-ip": "9.9.9.9"}
        async with self.client() as client:
            for i in range(self.auth.IP_MAX_FAILURES):
                await self.post_login(client, login=f"user{i}", headers=headers)
            blocked = await self.post_login(client, headers=headers)
            other_ip = await self.post_login(client, headers={**HEADERS, "x-real-ip": "8.8.8.8"})
        self.auth._ip_failures.clear()
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(other_ip.status_code, 303)

    async def test_blocked_manager_with_the_right_password(self):
        manager = await self.manager_with_password()
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        async with self.client() as client:
            response = await self.post_login(client)
        self.assertEqual(response.status_code, 403)

    async def test_login_from_foreign_origin_is_refused(self):
        await self.manager_with_password()
        async with self.client() as client:
            response = await self.post_login(client, headers={"origin": "https://evil.example"})
            null_origin = await self.post_login(client, headers={"origin": "null"})
        self.assertEqual((response.status_code, null_origin.status_code), (403, 403))

    async def test_old_link_login_no_longer_works(self):
        async with self.client() as client:
            response = await client.post("/manager/login", data={"token": "x" * 40}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("set-cookie", response.headers)

    async def test_pages_require_a_session(self):
        async with self.client() as client:
            for path in ("/manager/", "/manager/issue", "/manager/temp", "/manager/clients", "/manager/history",
                         "/manager/account"):
                response = await client.get(path)
                self.assertEqual(response.status_code, 303, path)
                self.assertEqual(response.headers["location"], "/manager/login")

    async def test_pages_are_not_cacheable(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            response = await client.get("/manager/")
        self.assertEqual(response.headers["cache-control"], "no-store")


class PasswordLinkTests(WebCase):
    async def test_get_shows_the_form_and_does_not_burn_the_link(self):
        manager = await self.env.make_manager(login="ivan")
        token = await self.svc.create_password_link(manager.id)
        async with self.client() as client:
            for _ in range(3):  # превью ссылок в мессенджерах открывают её GET-ом
                page = await client.get(f"/manager/password?t={token}")
                self.assertEqual(page.status_code, 200)
        self.assertIn('value="ivan"', page.text)
        self.assertIn('name="password2"', page.text)

    async def test_set_password_then_log_in(self):
        manager = await self.env.make_manager(login="ivan")
        token = await self.svc.create_password_link(manager.id)
        async with self.client() as client:
            mismatch = await client.post("/manager/password", headers=HEADERS,
                                         data={"token": token, "password": "Пароль-123", "password2": "Пароль-124"})
            weak = await client.post("/manager/password", headers=HEADERS,
                                     data={"token": token, "password": "short", "password2": "short"})
            ok = await client.post("/manager/password", headers=HEADERS,
                                   data={"token": token, "password": "Пароль-123", "password2": "Пароль-123"})
            again = await client.post("/manager/password", headers=HEADERS,
                                      data={"token": token, "password": "Пароль-123", "password2": "Пароль-123"})
            done = await client.get(ok.headers["location"])
            login = await client.post("/manager/login", data={"login": "ivan", "password": "Пароль-123"}, headers=HEADERS)
        self.assertEqual((mismatch.status_code, weak.status_code), (400, 400))
        self.assertIn("не совпадают", mismatch.text)
        self.assertEqual((ok.status_code, ok.headers["location"]), (303, "/manager/login?done=1"))
        self.assertEqual(again.status_code, 400)
        self.assertIn("Пароль сохранён", done.text)
        self.assertEqual(login.status_code, 303)

    async def test_bad_link(self):
        async with self.client() as client:
            page = await client.get("/manager/password?t=garbage")
            post = await client.post("/manager/password", headers=HEADERS,
                                     data={"token": "garbage", "password": "Пароль-123", "password2": "Пароль-123"})
        self.assertEqual((page.status_code, post.status_code), (400, 400))
        self.assertNotIn('name="password"', page.text)

    async def test_foreign_origin_is_refused(self):
        manager = await self.env.make_manager(login="ivan")
        token = await self.svc.create_password_link(manager.id)
        async with self.client() as client:
            response = await client.post("/manager/password", headers={"origin": "https://evil.example"},
                                         data={"token": token, "password": "Пароль-123", "password2": "Пароль-123"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(await self.svc.password_link_owner(token))

    async def test_setting_password_kills_existing_sessions(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            await self.svc.reset_password(manager.id, admin_id=1)
            self.assertEqual((await client.get("/manager/")).status_code, 303)


class AccountTests(WebCase):
    async def test_change_password_keeps_this_session_and_ends_others(self):
        manager = await self.env.make_manager()
        async with self.client() as here, self.client() as there:
            csrf = await self.login(here, manager, remember=True)
            await self.login(there, manager)
            self.assertEqual((await here.get("/manager/account")).status_code, 200)
            bad = await here.post("/manager/account/password", headers=HEADERS, data={
                "csrf": csrf, "old_password": "wrong-pass", "password": "Новый-пароль-1", "password2": "Новый-пароль-1"})
            no_csrf = await here.post("/manager/account/password", headers=HEADERS, data={
                "csrf": "nope", "old_password": self.PASSWORD, "password": "Новый-пароль-1", "password2": "Новый-пароль-1"})
            ok = await here.post("/manager/account/password", headers=HEADERS, data={
                "csrf": csrf, "old_password": self.PASSWORD, "password": "Новый-пароль-1", "password2": "Новый-пароль-1"})
            self.assertIn(f"max-age={30 * 24 * 3600}", ok.headers["set-cookie"].lower())  # «запомнить» сохранилось
            here_after = await here.get("/manager/account?ok=1")
            there_after = await there.get("/manager/")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Текущий пароль указан неверно", bad.text)
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(here_after.status_code, 200)
        self.assertIn("Пароль изменён", here_after.text)
        self.assertEqual(there_after.status_code, 303)


class SessionTests(WebCase):
    async def test_blocking_kills_the_live_session(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            await self.svc.set_status(manager.id, "blocked", admin_id=1)
            page = await client.get("/manager/")
            api = await client.post("/manager/api/quote", json={"product": "tariff", "tariff_id": 2}, headers=self.api(csrf))
        self.assertEqual(page.status_code, 303)
        self.assertEqual(api.status_code, 401)

    async def test_rights_change_forces_a_new_login(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            await self.svc.update_rights(manager.id, admin_id=1, can_accept_cash=True)
            page = await client.get("/manager/")
        self.assertEqual(page.status_code, 303)

    async def test_deleted_manager_has_no_session(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            await self.svc.set_status(manager.id, "deleted", admin_id=1)
            self.assertEqual((await client.get("/manager/")).status_code, 303)

    async def test_user_cabinet_token_does_not_open_the_panel(self):
        from datetime import datetime, timedelta
        user_token = jwt.encode({"sub": "555", "exp": datetime.utcnow() + timedelta(hours=1)},
                                self.auth.SECRET_KEY, algorithm=self.auth.ALGORITHM)
        async with self.client() as client:
            client.cookies.set("mgr_session", user_token, path="/manager")
            client.cookies.set("access_token", f"Bearer {user_token}")
            response = await client.get("/manager/")
        self.assertEqual(response.status_code, 303)

    async def test_forged_token_with_wrong_signature(self):
        forged = jwt.encode({"type": "manager", "sub": "1", "ver": 1, "csrf": "x"}, "not-the-secret", algorithm="HS256")
        async with self.client() as client:
            client.cookies.set("mgr_session", forged, path="/manager")
            self.assertEqual((await client.get("/manager/")).status_code, 303)

    async def test_logout_clears_cookie_and_needs_csrf(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            bad = await client.post("/manager/logout", data={"csrf": "nope"}, headers=HEADERS)
            self.assertEqual(bad.status_code, 403)
            ok = await client.post("/manager/logout", data={"csrf": csrf}, headers=HEADERS)
            self.assertEqual(ok.status_code, 303)
            self.assertEqual((await client.get("/manager/")).status_code, 303)


class CsrfTests(WebCase):
    async def test_api_requires_csrf_token(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            body = {"product": "tariff", "tariff_id": 2}
            self.assertEqual((await client.post("/manager/api/quote", json=body, headers={"origin": BASE})).status_code, 403)
            self.assertEqual((await client.post("/manager/api/quote", json=body, headers={**self.api("wrong")})).status_code, 403)
            self.assertEqual((await client.post("/manager/api/quote", json=body, headers=self.api(csrf))).status_code, 200)

    async def test_api_requires_same_origin(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            body = {"product": "tariff", "tariff_id": 2}
            evil = await client.post("/manager/api/quote", json=body,
                                     headers={"origin": "https://evil.example", "x-csrf-token": csrf})
            no_origin = await client.post("/manager/api/quote", json=body, headers={"x-csrf-token": csrf})
        self.assertEqual(evil.status_code, 403)
        self.assertEqual(no_origin.status_code, 403)

    async def test_api_without_session_is_401(self):
        async with self.client() as client:
            response = await client.post("/manager/api/quote", json={"product": "tariff", "tariff_id": 2}, headers=HEADERS)
        self.assertEqual(response.status_code, 401)

    async def test_csrf_token_of_another_session_does_not_work(self):
        a = await self.env.make_manager(telegram_id=1)
        b = await self.env.make_manager(telegram_id=2)
        async with self.client() as client_a, self.client() as client_b:
            csrf_a = await self.login(client_a, a)
            await self.login(client_b, b)
            response = await client_b.post("/manager/api/quote", json={"product": "tariff", "tariff_id": 2},
                                           headers=self.api(csrf_a))
        self.assertEqual(response.status_code, 403)


class IssueApiTests(WebCase):
    async def test_cash_issue_end_to_end(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        async with self.client() as client:
            csrf = await self.login(client, manager)
            quote = (await client.post("/manager/api/quote", json={"product": "tariff", "tariff_id": 2},
                                       headers=self.api(csrf))).json()
            self.assertEqual(quote["total"], 149.0)
            response = await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "method": "cash", "nonce": "web-nonce-0001",
                "expected_total": quote["total"],
            })
            data = response.json()
            replay = (await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "method": "cash", "nonce": "web-nonce-0001",
            })).json()

        self.assertEqual(response.status_code, 200, data)
        self.assertEqual((data["status"], data["price"]), ("completed", 149.0))
        self.assertIn("SECRET", data["subscription_url"])
        self.assertTrue(data["cabinet_url"].startswith("https://example.com/c/"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["operation_id"], data["operation_id"])
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 149.0)

    async def test_cash_without_right_is_403(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            response = await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "method": "cash", "nonce": "web-nonce-0002"})
        self.assertEqual(response.status_code, 400)  # отказ по праву приёма наличных
        self.assertEqual(response.json()["error"], "cash_not_allowed")

    async def test_missing_right_is_403(self):
        manager = await self.env.make_manager(can_issue_custom=False)
        async with self.client() as client:
            csrf = await self.login(client, manager)
            response = await client.post("/manager/api/quote", json={"product": "custom", "days": 10}, headers=self.api(csrf))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "no_right")

    async def test_online_issue_and_polling(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            data = (await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "method": "online", "nonce": "web-nonce-0003"})).json()
            self.assertEqual(data["status"], "pending_payment")
            self.assertTrue(data["payment_url"].startswith("https://pay.example/"))
            status = (await client.get(f"/manager/api/op/{data['operation_id']}", headers=self.api(csrf))).json()
            self.assertEqual(status["status"], "pending_payment")
            await self.env.payments.process_successful_payment("yk-1", 149.0)
            status = (await client.get(f"/manager/api/op/{data['operation_id']}", headers=self.api(csrf))).json()
            self.assertEqual(status["status"], "completed")
            cancel = (await client.post(f"/manager/api/op/{data['operation_id']}/cancel", json={}, headers=self.api(csrf))).json()
            self.assertFalse(cancel["cancelled"])

    async def test_temp_key_api(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            data = (await client.post("/manager/api/temp", json={"nonce": "temp-nonce-0001"}, headers=self.api(csrf))).json()
        self.assertIn("SECRET", data["subscription_url"])
        self.assertIn("МСК", "МСК")  # время отдаётся строкой для показа
        self.assertTrue(data["expires_at"])

    async def test_qr_only_for_https_links(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            ok = await client.post("/manager/api/qr", json={"text": "https://pay.example/1"}, headers=self.api(csrf))
            bad = await client.post("/manager/api/qr", json={"text": "javascript:alert(1)"}, headers=self.api(csrf))
            huge = await client.post("/manager/api/qr", json={"text": "https://" + "a" * 800}, headers=self.api(csrf))
        self.assertTrue(ok.json()["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(huge.status_code, 422)

    async def test_validation_of_request_bodies(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        async with self.client() as client:
            csrf = await self.login(client, manager)
            for body in ({"product": "weird"}, {"product": "tariff", "method": "barter", "nonce": "x" * 12},
                         {"product": "tariff", "method": "cash", "nonce": "short"}):
                response = await client.post("/manager/api/issue", json=body, headers=self.api(csrf))
                self.assertEqual(response.status_code, 422, body)


class IsolationTests(WebCase):
    async def test_foreign_client_card_is_404(self):
        a = await self.env.make_manager(can_accept_cash=True, telegram_id=1)
        b = await self.env.make_manager(telegram_id=2)
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
        async with self.client() as client:
            await self.login(client, b)
            page = await client.get(f"/manager/clients/{result.client_code}")
        self.assertEqual(page.status_code, 404)

    async def test_foreign_client_api_is_refused(self):
        a = await self.env.make_manager(can_accept_cash=True, telegram_id=1)
        b = await self.env.make_manager(telegram_id=2)
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
        async with self.client() as client:
            csrf = await self.login(client, b)
            link = await client.post("/manager/api/link", json={"client_code": result.client_code}, headers=self.api(csrf))
            quote = await client.post("/manager/api/quote", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "client_code": result.client_code})
        self.assertEqual(link.status_code, 400)
        self.assertEqual(quote.status_code, 400)
        self.assertNotIn("SECRET", link.text + quote.text)

    async def test_operation_status_of_another_manager_is_404(self):
        a = await self.env.make_manager(telegram_id=1)
        b = await self.env.make_manager(telegram_id=2)
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="n1")
        async with self.client() as client:
            csrf = await self.login(client, b)
            response = await client.get(f"/manager/api/op/{result.operation_id}", headers=self.api(csrf))
        self.assertEqual(response.status_code, 404)

    async def test_history_shows_only_own_operations(self):
        a = await self.env.make_manager(can_accept_cash=True, telegram_id=1)
        b = await self.env.make_manager(can_accept_cash=True, telegram_id=2)
        ra = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="a1")
        rb = await self.svc.issue(b.id, product="tariff", tariff_id=3, method="cash", idempotency_nonce="b1")
        async with self.client() as client:
            await self.login(client, a)
            page = (await client.get("/manager/history")).text
        self.assertIn(ra.client_code, page)
        self.assertNotIn(rb.client_code, page)


class PagesTests(WebCase):
    async def test_all_pages_render(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
        await self.svc.issue_temp(manager.id, "t1")
        async with self.client() as client:
            await self.login(client, manager)
            for path in ("/manager/", "/manager/issue", f"/manager/issue?client={result.client_code}", "/manager/temp",
                         "/manager/clients", f"/manager/clients/{result.client_code}", "/manager/history"):
                response = await client.get(path)
                self.assertEqual(response.status_code, 200, path)

    async def test_card_page_has_no_telegram_identity(self):
        manager = await self.env.make_manager()
        user = await self.env.make_telegram_client(555, is_first_payment_made=True)
        code = await self.svc.add_client_by_code(manager.id, await self.svc.create_access_code(user.user_id))
        async with self.client() as client:
            await self.login(client, manager)
            page = (await client.get(f"/manager/clients/{code}")).text
        for secret in ("555", "real_username", "Настоящее Имя"):
            self.assertNotIn(secret, page)

    async def test_cash_button_only_with_the_right(self):
        async with self.client() as client:
            manager = await self.env.make_manager()
            await self.login(client, manager)
            self.assertNotIn("payCashBtn", (await client.get("/manager/issue")).text)
        async with self.client() as client:
            await self.svc.update_rights(manager.id, admin_id=1, can_accept_cash=True)
            await self.login(client, manager)
            self.assertIn("payCashBtn", (await client.get("/manager/issue")).text)

    async def test_admin_data_never_appears_in_manager_pages(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            text = ""
            for path in ("/manager/", "/manager/issue", "/manager/temp", "/manager/clients", "/manager/history"):
                text += (await client.get(path)).text
        for forbidden in ("custom_day_price", "admin_", "ADMINS", "YOOKASSA", "SECRET_KEY"):
            self.assertNotIn(forbidden, text)


class CabinetLinkTests(WebCase):
    """/c/<token>: кабинет офлайн-клиента по ссылке из чека."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        database = types.ModuleType("database")
        database.user_repo = self.env.repos.users
        pure = types.ModuleType("tgbot.services.manager_security")
        spec = importlib.util.spec_from_file_location(
            "tgbot.services.manager_security", ROOT / "tgbot" / "services" / "manager_security.py")
        security = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(security)
        tgbot = types.ModuleType("tgbot")
        tgbot.__path__ = []
        services = types.ModuleType("tgbot.services")
        services.__path__ = []
        with patch.dict(sys.modules, {"database": database, "tgbot": tgbot, "tgbot.services": services,
                                      "tgbot.services.manager_security": security}):
            spec = importlib.util.spec_from_file_location("client_link_under_test", ROOT / "webapp" / "routers" / "client_link.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.cabinet_app = FastAPI()
        self.cabinet_app.include_router(module.router)

    def cabinet_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.cabinet_app), base_url=BASE, follow_redirects=False)

    async def _issue(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
        token = result.cabinet_url.rsplit("/", 1)[1]
        return manager, result, token

    async def test_valid_link_opens_the_cabinet_session(self):
        _, result, token = await self._issue()
        async with self.cabinet_client() as client:
            response = await client.get(f"/c/{token}")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/profile/")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("access_token=", cookie)
        self.assertIn("httponly", cookie)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_session_belongs_to_the_offline_client(self):
        _, result, token = await self._issue()
        user = await self.env.repos.users.get_by_client_code(result.client_code)
        async with self.cabinet_client() as client:
            response = await client.get(f"/c/{token}")
        value = response.headers["set-cookie"].split("access_token=")[1].split(";")[0].strip('"')
        payload = jwt.decode(value.removeprefix("Bearer "), "CHANGE_THIS_TO_A_SUPER_SECRET_STRING", algorithms=["HS256"])
        self.assertEqual(payload["sub"], str(user.user_id))

    async def test_unknown_and_malformed_tokens_are_404(self):
        await self._issue()
        async with self.cabinet_client() as client:
            for token in ("A" * 43, "short", "x" * 200):
                response = await client.get(f"/c/{token}")
                self.assertEqual(response.status_code, 404, token)
                self.assertNotIn("access_token", response.headers.get("set-cookie", ""))

    async def test_reset_link_kills_the_old_token(self):
        manager, result, token = await self._issue()
        new_url = await self.svc.reset_cabinet_link(manager.id, result.client_code)
        async with self.cabinet_client() as client:
            old = await client.get(f"/c/{token}")
            new = await client.get(f"/c/{new_url.rsplit('/', 1)[1]}")
        self.assertEqual(old.status_code, 404)
        self.assertEqual(new.status_code, 303)

    async def test_token_is_not_stored_in_clear(self):
        _, result, token = await self._issue()
        user = await self.env.repos.users.get_by_client_code(result.client_code)
        self.assertNotEqual(user.cabinet_token_hash, token)
        self.assertNotIn(token, user.cabinet_token_hash)


if __name__ == "__main__":
    unittest.main()


class UxPagesTests(WebCase):
    async def test_dashboard_offers_tariffs_in_one_tap_and_a_guide_for_newcomers(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        async with self.client() as client:
            await self.login(client, manager)
            first = (await client.get("/manager/")).text
            await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
            later = (await client.get("/manager/")).text
        self.assertIn('href="/manager/issue?tariff=2"', first)
        self.assertIn('href="/manager/issue?custom=1"', first)
        self.assertIn("Впервые здесь", first)
        self.assertNotIn("Впервые здесь", later)

    async def test_issue_page_preselects_from_the_link(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            tariff = (await client.get("/manager/issue?tariff=3")).text
            custom = (await client.get("/manager/issue?custom=1")).text
        self.assertRegex(tariff, r'value="t3" checked')
        self.assertRegex(custom, r'value="custom" checked')
        self.assertIn('id="labelInput"', tariff)

    async def test_help_page(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            page = await client.get("/manager/help")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Продажа за минуту", page.text)
        self.assertIn("INCY", page.text)

    async def test_every_page_has_the_show_client_screen_and_mobile_tabs(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            for path in ("/manager/", "/manager/issue", "/manager/temp", "/manager/clients", "/manager/help"):
                text = (await client.get(path)).text
                self.assertIn('id="showClient"', text, path)
                self.assertIn('class="tabbar', text, path)
                self.assertIn('id="installSteps"', text, path)

    async def test_issue_with_label_and_label_api(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        async with self.client() as client:
            csrf = await self.login(client, manager)
            data = (await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "method": "cash", "nonce": "web-nonce-0101",
                "label": "Анна, кофейня"})).json()
            clients = (await client.get("/manager/clients")).text
            renamed = await client.post("/manager/api/label", headers=self.api(csrf),
                                        json={"client_code": data["client_code"], "label": "  Пётр  "})
            card = (await client.get(f"/manager/clients/{data['client_code']}")).text
            cleared = (await client.post("/manager/api/label", headers=self.api(csrf),
                                         json={"client_code": data["client_code"], "label": ""})).json()
        self.assertIn("Анна, кофейня", clients)
        self.assertEqual(renamed.json(), {"label": "Пётр"})
        self.assertIn("Пётр", card)
        self.assertIsNone(cleared["label"])

    async def test_label_api_refuses_foreign_clients(self):
        a = await self.env.make_manager(can_accept_cash=True, telegram_id=1)
        b = await self.env.make_manager(telegram_id=2)
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
        async with self.client() as client:
            csrf = await self.login(client, b)
            response = await client.post("/manager/api/label", headers=self.api(csrf),
                                         json={"client_code": result.client_code, "label": "чужой"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone((await self.svc.list_clients(a.id))[0][0].label)

    async def test_operation_status_returns_the_client_for_the_install_qr(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            data = (await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "method": "online", "nonce": "web-nonce-0102"})).json()
            status = (await client.get(f"/manager/api/op/{data['operation_id']}", headers=self.api(csrf))).json()
        self.assertEqual(status["client_code"], data["client_code"])

    async def test_temp_page_shows_time_not_fingerprints(self):
        manager = await self.env.make_manager()
        await self.svc.issue_temp(manager.id, "t1")
        async with self.client() as client:
            await self.login(client, manager)
            page = (await client.get("/manager/temp")).text
        self.assertIn("data-countdown=", page)
        fingerprint = (await self.svc.list_temp_keys(manager.id))[0]["fingerprint"]
        self.assertNotIn(fingerprint, page)


class ExtrasWebTests(WebCase):
    async def test_quote_and_issue_with_chosen_extras(self):
        manager = await self.env.make_manager(can_accept_cash=True)
        async with self.client() as client:
            csrf = await self.login(client, manager)
            page = (await client.get("/manager/issue")).text
            q = (await client.post("/manager/api/quote", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "slots": 2, "packs": 1})).json()
            data = (await client.post("/manager/api/issue", headers=self.api(csrf), json={
                "product": "tariff", "tariff_id": 2, "slots": 2, "packs": 1, "method": "cash",
                "nonce": "web-nonce-0201", "expected_total": q["total"]})).json()
        self.assertIn('id="extrasBox"', page)
        self.assertEqual((q["slots"], q["packs"], q["devices_limit"], q["total"]), (2, 1, 7, 149 + 3 * 49))
        self.assertEqual((q["max_slots"], q["max_packs"]), (5, 10))
        self.assertEqual((data["status"], data["price"]), ("completed", 149 + 3 * 49))

    async def test_out_of_range_extras(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            csrf = await self.login(client, manager)
            too_many = await client.post("/manager/api/quote", headers=self.api(csrf),
                                         json={"product": "tariff", "tariff_id": 2, "slots": 50})
            negative = await client.post("/manager/api/quote", headers=self.api(csrf),
                                         json={"product": "tariff", "tariff_id": 2, "packs": -1})
        self.assertEqual(too_many.status_code, 400)
        self.assertEqual(too_many.json()["error"], "bad_extras")
        self.assertEqual(negative.status_code, 422)
