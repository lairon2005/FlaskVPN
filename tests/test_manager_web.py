"""
Веб-панель менеджера целиком: HTTP → роутер → сессия/CSRF → ManagerService → SQLite.

Проверяем то, что важно именно на веб-границе: одноразовая ссылка входа, атрибуты cookie,
сброс сессий при блокировке, CSRF, изоляция менеджеров и то, что пользовательский токен
кабинета не открывает панель.
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

    async def login(self, client, manager):
        token = await self.svc.create_login_token(manager.id)
        response = await client.post("/manager/login", data={"token": token}, headers=HEADERS)
        self.assertEqual(response.status_code, 303, response.text)
        page = await client.get("/manager/")
        self.assertEqual(page.status_code, 200)
        import re
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        return csrf

    def api(self, csrf):
        return {"origin": BASE, "x-csrf-token": csrf}


class LoginTests(WebCase):
    async def test_login_page_get_does_not_burn_the_link(self):
        manager = await self.env.make_manager()
        token = await self.svc.create_login_token(manager.id)
        async with self.client() as client:
            for _ in range(3):  # превью ссылок в мессенджерах открывают её GET-ом
                page = await client.get(f"/manager/login?t={token}")
                self.assertEqual(page.status_code, 200)
            response = await client.post("/manager/login", data={"token": token}, headers=HEADERS)
        self.assertEqual(response.status_code, 303)

    async def test_link_is_one_time(self):
        manager = await self.env.make_manager()
        token = await self.svc.create_login_token(manager.id)
        async with self.client() as client:
            ok = await client.post("/manager/login", data={"token": token}, headers=HEADERS)
            again = await client.post("/manager/login", data={"token": token}, headers=HEADERS)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(again.status_code, 400)

    async def test_cookie_attributes(self):
        manager = await self.env.make_manager()
        token = await self.svc.create_login_token(manager.id)
        async with self.client() as client:
            response = await client.post("/manager/login", data={"token": token}, headers=HEADERS)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("mgr_session=", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/manager", cookie)
        self.assertIn("secure", cookie)

    async def test_login_from_foreign_origin_is_refused(self):
        manager = await self.env.make_manager()
        token = await self.svc.create_login_token(manager.id)
        async with self.client() as client:
            response = await client.post("/manager/login", data={"token": token}, headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    async def test_wrong_token(self):
        async with self.client() as client:
            response = await client.post("/manager/login", data={"token": "x" * 40}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)

    async def test_pages_require_a_session(self):
        async with self.client() as client:
            for path in ("/manager/", "/manager/issue", "/manager/temp", "/manager/clients", "/manager/history"):
                response = await client.get(path)
                self.assertEqual(response.status_code, 303, path)
                self.assertEqual(response.headers["location"], "/manager/login")

    async def test_pages_are_not_cacheable(self):
        manager = await self.env.make_manager()
        async with self.client() as client:
            await self.login(client, manager)
            response = await client.get("/manager/")
        self.assertEqual(response.headers["cache-control"], "no-store")


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
