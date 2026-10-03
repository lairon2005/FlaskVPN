# tests/manager_env.py
"""
Сборка ManagerService для интеграционных тестов: настоящая БД (SQLite), настоящие
репозитории, настоящий PaymentService, фейковые панель и Telegram.

Деньги и права — как раз то, что мокать нельзя: проверяется, что наличная продажа
реально проходит через process_successful_payment, что журнал и платёж сходятся,
что чужой клиент недоступен.
"""
import datetime
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import db_harness  # noqa: F401  (заглушки окружения до импорта db)
from real_traffic_pricing import real_custom_pricing, real_traffic_pricing
from remnawave.client import RemnawaveAPIError
from test_stars_payment import load_payment_service_module

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3

_PURE = ("device_pricing", "pricing", "manager_receipts", "manager_security", "manager_guide")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_manager_service_module():
    """manager_service.py без production-синглтонов: чистые модули — настоящие, loader — заглушка."""
    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    stubs = {"tgbot": tgbot, "tgbot.services": services,
             "loader": SimpleNamespace(logger=Mock(), config=None)}
    for name in _PURE:
        stubs[f"tgbot.services.{name}"] = _load(f"tgbot.services.{name}", ROOT / "tgbot" / "services" / f"{name}.py")
    stubs["tgbot.services.traffic_pricing"] = real_traffic_pricing()
    stubs["tgbot.services.custom_pricing"] = real_custom_pricing()

    path = ROOT / "tgbot" / "services" / "manager_service.py"
    spec = importlib.util.spec_from_file_location("manager_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class FakePanel:
    """Минимальная Remnawave: пользователи в памяти, ошибки включаются флагами."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.fail_delete_with: Exception | None = None
        self.fail_create = False
        self.deleted: list[str] = []
        self._seq = 0

    async def create_user(self, *, username, expire_at=None, traffic_limit_bytes=None,
                          traffic_limit_strategy=None, description=None, user_uuid=None):
        if self.fail_create:
            raise RuntimeError("panel down")
        self._seq += 1
        user = {"uuid": f"u{self._seq}", "username": username, "expireAt": expire_at,
                "trafficLimitBytes": traffic_limit_bytes, "trafficLimitStrategy": traffic_limit_strategy,
                "description": description, "hwidDeviceLimit": None,
                "subscriptionUrl": f"https://sub.example/{username}-SECRET"}
        self.users[username] = user
        return dict(user)

    async def update_user(self, user_uuid, **fields):
        for user in self.users.values():
            if user["uuid"] == user_uuid:
                if fields.get("hwid_device_limit") is not None:
                    user["hwidDeviceLimit"] = fields["hwid_device_limit"]
                if fields.get("traffic_limit_bytes") is not None:
                    user["trafficLimitBytes"] = fields["traffic_limit_bytes"]
                return dict(user)
        raise RemnawaveAPIError(404, "not found")

    async def get_user_by_username(self, username):
        user = self.users.get(username)
        return dict(user) if user else None

    async def get_user_devices(self, user_uuid):
        return [{"hwid": "SECRET-HWID", "platform": "Android", "requestIp": "1.2.3.4"}]

    async def delete_user(self, user_uuid):
        if self.fail_delete_with is not None:
            raise self.fail_delete_with
        for name, user in list(self.users.items()):
            if user["uuid"] == user_uuid:
                del self.users[name]
                self.deleted.append(name)
                return
        raise RemnawaveAPIError(404, "not found")


class RecordingNotifier:
    """Вместо Telegram: запоминает, что и кому было бы отправлено."""

    def __init__(self):
        self.group: list[tuple[int, str]] = []
        self.manager_msgs: list[dict] = []
        self.client_msgs: list[tuple[int, str]] = []
        self.client_access: list[tuple[int, str]] = []
        self.manager_texts: list[tuple[int, str]] = []
        self.web_logins: list[dict] = []
        self.invoices: list[tuple[int, int, str]] = []
        self.temp_reminders: list[dict] = []
        self._next_message_id = 1000

    async def post_group_receipt(self, op_id, text, existing):
        self.group.append((op_id, text))
        if existing:
            return existing
        self._next_message_id += 1
        return (-100, self._next_message_id)

    async def notify_manager(self, telegram_id, text, *, subscription_url=None, operation_id=None):
        self.manager_msgs.append({"to": telegram_id, "text": text, "url": subscription_url, "op": operation_id})

    async def notify_client(self, user_id, text):
        self.client_msgs.append((user_id, text))

    async def notify_client_access(self, user_id, manager_name):
        self.client_access.append((user_id, manager_name))

    async def update_invoice(self, chat_id, message_id, text):
        self.invoices.append((chat_id, message_id, text))

    async def notify_temp_expiring(self, telegram_id, key_id, *, minutes_left, until):
        self.temp_reminders.append({"to": telegram_id, "key": key_id, "left": minutes_left, "until": until})

    async def notify_manager_text(self, telegram_id, text):
        self.manager_texts.append((telegram_id, text))

    async def notify_web_login(self, telegram_id, *, ip, device, when):
        self.web_logins.append({"to": telegram_id, "ip": ip, "device": device, "when": when})


class FakeSubscriptionService:
    """extend() двигает срок в настоящей БД и заводит пользователя в фейковой панели."""

    def __init__(self, user_repo, panel):
        self._users = user_repo
        self._panel = panel
        self.calls: list[tuple] = []

    async def extend(self, user_id, days, data_limit_gb=None):
        self.calls.append((user_id, days, data_limit_gb))
        user = await self._users.get(user_id)
        username = user.vpn_username or f"user_{user_id}"
        if username not in self._panel.users:
            created = await self._panel.create_user(username=username)
            await self._users.update_vpn_username(user_id, username)
            await self._users.update_remnawave_uuid(user_id, created["uuid"])
        await self._users.extend_subscription(user_id, days)
        return SimpleNamespace(is_new_user=True, username=username)


async def build_env(*, tariffs=None, settings=None, save_card=True, manager_rights=None):
    """Возвращает SimpleNamespace со всем нужным тестам."""
    from database.repositories.client_access import ClientAccessRepository
    from database.repositories.manager import ManagerClientRepository, ManagerRepository
    from database.repositories.manager_operation import ManagerOperationRepository
    from database.repositories.payment import PaymentRepository
    from database.repositories.settings import SettingsRepository
    from database.repositories.tariff import TariffRepository
    from database.repositories.temp_key import TempKeyRepository
    from database.repositories.user import UserRepository
    from db import Tariff

    sm, engine = await db_harness.make_session_maker()
    repos = SimpleNamespace(
        managers=ManagerRepository(sm), clients=ManagerClientRepository(sm),
        ops=ManagerOperationRepository(sm), temps=TempKeyRepository(sm),
        access=ClientAccessRepository(sm), users=UserRepository(sm),
        tariffs=TariffRepository(sm), payments=PaymentRepository(sm), settings=SettingsRepository(sm),
    )
    for key, value in (settings or {}).items():
        await repos.settings.set(key, value)

    seed = tariffs if tariffs is not None else [
        dict(name="Неделя", price=74, duration_days=7, data_limit_gb=None),
        dict(name="Месяц", price=149, duration_days=30, data_limit_gb=None),
        dict(name="3 месяца", price=399, duration_days=90, data_limit_gb=None),
    ]
    for t in seed:
        await repos.tariffs.add(**t)

    payments_module = load_payment_service_module()
    panel = FakePanel()
    subscription = FakeSubscriptionService(repos.users, panel)
    traffic = SimpleNamespace(
        settings=AsyncMock(return_value=real_traffic_pricing().TrafficSettings()),
        record_purchase=AsyncMock(), add_gb=AsyncMock(),
    )
    devices = SimpleNamespace(
        settings=AsyncMock(return_value=_device_settings()),
        set_slots=AsyncMock(side_effect=lambda uid, n: n), sync_limit=AsyncMock(),
    )
    cards = SimpleNamespace(
        save_from_yookassa=AsyncMock(return_value=True), get_card=AsyncMock(return_value=None),
        disable_auto_renew=AsyncMock(return_value=True),
    )
    payment_service = payments_module.PaymentService(
        subscription_service=subscription,
        referral_service=SimpleNamespace(process_first_payment_bonus=AsyncMock(return_value=None)),
        user_repo=repos.users, tariff_repo=repos.tariffs, payment_repo=repos.payments,
        payment_method_service=cards, device_slot_service=devices, traffic_service=traffic,
        settings_repo=repos.settings,
    )

    notifier = RecordingNotifier()
    created_payments: list[dict] = []

    def create_payment(**kwargs):
        created_payments.append(kwargs)
        return f"https://pay.example/{len(created_payments)}", f"yk-{len(created_payments)}"

    config = SimpleNamespace(
        yookassa=SimpleNamespace(shop_id="s", secret_key="k", save_payment_method=save_card),
        tg_bot=SimpleNamespace(tg_bot_username="bot", tma_app_name="app"),
        webhook=SimpleNamespace(domain="example.com"),
    )
    module = load_manager_service_module()
    service = module.ManagerService(
        manager_repo=repos.managers, client_repo=repos.clients, op_repo=repos.ops, temp_repo=repos.temps,
        access_repo=repos.access, user_repo=repos.users, tariff_repo=repos.tariffs,
        payment_service=payment_service, subscription_service=subscription, traffic_service=traffic,
        device_slot_service=devices, remnawave=panel, settings_repo=repos.settings,
        create_payment=create_payment, config=config, notifier=notifier,
    )

    async def make_manager(name="Иван Петров", telegram_id=1001, login=None, password=None, **rights):
        """Активный менеджер. login/password — сразу с входом на сайт (пароль ставится через ссылку, как в жизни)."""
        manager, token = await service.invite(name, admin_id=1, login=login)
        await service.accept_invite(token, telegram_id)
        merged = dict(manager_rights or {})
        merged.update(rights)
        if merged:
            await repos.managers.update_rights(manager.id, **merged)
        if password is not None:
            await service.set_password_by_link(await service.create_password_link(manager.id), password)
        return manager

    async def make_telegram_client(user_id=555, **fields):
        user, _ = await repos.users.get_or_create(user_id, "Настоящее Имя", "real_username")
        if fields:
            from sqlalchemy import update
            from db import User
            async with sm() as session:
                await session.execute(update(User).where(User.user_id == user_id).values(**fields))
                await session.commit()
        return await repos.users.get(user_id)

    return SimpleNamespace(
        service=service, module=module, repos=repos, panel=panel, notifier=notifier, payments=payment_service,
        subscription=subscription, created_payments=created_payments, make_manager=make_manager,
        make_telegram_client=make_telegram_client, engine=engine, session_maker=sm, devices=devices,
        cards=cards, traffic=traffic,
    )


def _device_settings():
    spec = importlib.util.spec_from_file_location("dp_env", ROOT / "tgbot" / "services" / "device_pricing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DeviceSettings()
