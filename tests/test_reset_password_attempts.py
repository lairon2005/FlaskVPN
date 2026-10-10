"""Счётчик попыток сброса пароля (webapp/routers/auth.py::process_reset_password)."""
import asyncio
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db_harness  # noqa: F401  (заглушки окружения до импорта db)


def _load_auth():
    """auth.py без production-синглтонов: tgbot.services (loader, бот) подменён заглушкой."""
    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    services.referral_service = None
    path = Path(__file__).resolve().parents[1] / "webapp" / "routers" / "auth.py"
    spec = importlib.util.spec_from_file_location("auth_reset_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"tgbot": tgbot, "tgbot.services": services}):
        spec.loader.exec_module(module)
    return module


auth = _load_auth()


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value


class FakeDB:
    """select -> пользователь; update ... RETURNING -> инкремент счётчика, как это делает БД."""

    def __init__(self, user):
        self.user = user
        self.commits = 0

    async def execute(self, stmt):
        if type(stmt).__name__ == "Update":
            self.user.reset_attempts += 1
            return FakeResult(self.user.reset_attempts)
        return FakeResult(self.user)

    async def commit(self):
        self.commits += 1


def fake_render(request, name, context=None, status_code=200):
    return SimpleNamespace(name=name, ctx=context or {})


def _user(code="123456", attempts=0, expire_in=timedelta(minutes=10)):
    return SimpleNamespace(
        user_id=-1, email="a@b.ru", password_hash="old",
        reset_code=code, reset_code_expire=datetime.now() + expire_in, reset_attempts=attempts,
    )


def _reset(db, code, password="newpass1"):
    with patch.object(auth, "render", fake_render), \
            patch.object(auth, "get_password_hash", lambda p: f"hash:{p}"):
        return asyncio.run(auth.process_reset_password(
            request=None, email="a@b.ru", code=code, new_password=password, db=db))


class ResetAttemptsTest(unittest.TestCase):
    def test_correct_code_changes_password_and_clears_state(self):
        user = _user(attempts=2)
        resp = _reset(FakeDB(user), "123456")
        self.assertEqual(resp.name, "login.html")
        self.assertEqual(user.password_hash, "hash:newpass1")
        self.assertIsNone(user.reset_code)
        self.assertEqual(user.reset_attempts, 0)

    def test_wrong_code_reports_remaining_attempts(self):
        user = _user()
        resp = _reset(FakeDB(user), "000000")
        self.assertEqual(resp.name, "reset_password.html")
        self.assertIn(f"Осталось попыток: {auth.MAX_RESET_ATTEMPTS - 1}", resp.ctx["error"])
        self.assertEqual(user.password_hash, "old")

    def test_code_is_burned_after_limit_even_if_next_is_correct(self):
        user = _user()
        db = FakeDB(user)
        for _ in range(auth.MAX_RESET_ATTEMPTS):
            _reset(db, "000000")
        self.assertEqual(user.reset_code, "123456")  # 5 неверных — код ещё жив
        resp = _reset(db, "123456")  # 6-я попытка отвергается, даже если код верный
        self.assertIn("Превышено", resp.ctx["error"])
        self.assertIsNone(user.reset_code)
        self.assertEqual(user.password_hash, "old")
        # и после гашения старый код больше не работает
        resp = _reset(db, "123456")
        self.assertEqual(user.password_hash, "old")
        self.assertIn("Запросите новый", resp.ctx["error"])

    def test_fifth_wrong_attempt_still_allowed_to_be_correct(self):
        user = _user(attempts=auth.MAX_RESET_ATTEMPTS - 1)
        resp = _reset(FakeDB(user), "123456")
        self.assertEqual(resp.name, "login.html")

    def test_expired_code_rejected_without_counting(self):
        user = _user(expire_in=timedelta(minutes=-1))
        resp = _reset(FakeDB(user), "123456")
        self.assertIn("Запросите новый", resp.ctx["error"])
        self.assertEqual(user.reset_attempts, 0)

    def test_non_ascii_input_does_not_crash(self):
        resp = _reset(FakeDB(_user()), "пароль")
        self.assertIn("Неверный код", resp.ctx["error"])


if __name__ == "__main__":
    unittest.main()
