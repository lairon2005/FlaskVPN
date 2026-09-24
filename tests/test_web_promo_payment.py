# tests/test_web_promo_payment.py
"""
POST /payment/create (webapp/routers/payment.py) — скидка по промокоду считается
только на сервере. Раньше discount_percent брался из запроса как есть, и
подобранный запрос с discount_percent=99 получал 99% скидки без всякого промокода.

Теперь:
- discount_percent клиента игнорируется;
- promo_code проверяется promo_service.validate() и должен давать скидку;
- промокод атомарно захватывается (promo_service.apply → try_claim) перед
  выставлением счёта и возвращается (release), если счёт выставить не удалось.

Модуль грузится через importlib мимо tgbot/services/__init__.py и loader.py —
тот же приём, что и в test_stars_payment.py (load_payment_router_module).
"""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException


def _load_module(module_name: str, relative_path: str, stubs: dict):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


def _real_device_pricing():
    """Настоящий device_pricing: проверяем именно итоговую сумму счёта."""
    path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
    spec = importlib.util.spec_from_file_location("tgbot.services.device_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PromoClaimError = type("PromoClaimError", (Exception,), {})


def load_payment_router():
    """Возвращает (модуль, stubs) — stubs нужны тестам для настройки моков."""
    db_module = types.ModuleType("db")
    db_module.User = type("User", (), {})
    db_module.Tariff = type("Tariff", (), {})
    # webapp/dependencies.py импортирует его на уровне модуля.
    db_module.async_session_maker = Mock()

    tariff = SimpleNamespace(
        id=5, name="Месяц", price=100, duration_days=30, is_active=True, is_intro=False,
    )
    database_module = types.ModuleType("database")
    database_module.tariff_repo = AsyncMock()
    database_module.tariff_repo.get_by_name_and_price.return_value = tariff
    database_module.tariff_repo.get_by_id.return_value = tariff
    database_module.tariff_repo.get_by_id_map.return_value = {}

    device_pricing = _real_device_pricing()

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    services.payment_service = AsyncMock()
    services.payment_service.has_pending_payment.return_value = False
    services.promo_service = AsyncMock()
    services.subscription_service = AsyncMock()
    services.device_slot_service = AsyncMock()
    services.device_slot_service.settings.return_value = device_pricing.DeviceSettings()

    payment_module = types.ModuleType("tgbot.services.payment")
    payment_module.create_payment = Mock(return_value=("https://pay.example/1", "yk-1"))
    pricing_module = types.ModuleType("tgbot.services.pricing")
    pricing_module.effective_price = lambda tariff, user_has_active_sub=False: float(tariff.price)
    promo_code_service_module = types.ModuleType("tgbot.services.promo_code_service")
    promo_code_service_module.PromoClaimError = PromoClaimError
    # Витрина вводного тарифа тут не проверяется — пропускаем всех.
    intro_offer_module = types.ModuleType("tgbot.services.intro_offer")
    intro_offer_module.intro_block_reason = lambda tariff, user, tariffs_by_id: None

    config_module = types.ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        tg_bot=SimpleNamespace(tg_bot_username=None, tma_app_name="app"),
        webhook=SimpleNamespace(domain="example.com"),
        yookassa=SimpleNamespace(shop_id="x", secret_key="y", save_payment_method=True),
    )

    module = _load_module(
        "web_promo_payment_under_test",
        "webapp/routers/payment.py",
        {
            "db": db_module,
            "database": database_module,
            "tgbot": tgbot_module,
            "tgbot.services": services,
            "tgbot.services.intro_offer": intro_offer_module,
            "tgbot.services.payment": payment_module,
            "tgbot.services.pricing": pricing_module,
            "tgbot.services.device_pricing": device_pricing,
            "tgbot.services.promo_code_service": promo_code_service_module,
            "config": config_module,
        },
    )
    stubs = SimpleNamespace(
        tariff=tariff,
        payment_service=services.payment_service,
        promo_service=services.promo_service,
        create_payment=payment_module.create_payment,
    )
    return module, stubs


def _user():
    return SimpleNamespace(user_id=42, email=None, subscription_end_date=None)


def _promo(discount_percent=10, bonus_days=0, code="SALE10"):
    return SimpleNamespace(id=7, code=code, discount_percent=discount_percent, bonus_days=bonus_days)


def _valid(promo):
    return SimpleNamespace(is_valid=True, error_message=None, promo=promo)


class WebPaymentPromoTests(unittest.TestCase):
    def setUp(self):
        self.module, self.stubs = load_payment_router()

    def _create(self, **fields):
        fields.setdefault("tariff_name", "Месяц")
        fields.setdefault("price", 100)
        payload = self.module.PaymentRequest(**fields)
        return asyncio.run(self.module.create_payment_route(payload, user=_user()))

    def _record_kwargs(self):
        return self.stubs.payment_service.create_payment_record.await_args.kwargs

    def test_client_discount_without_promo_is_ignored(self):
        self._create(discount_percent=99)

        self.assertEqual(self.stubs.create_payment.call_args.kwargs["amount"], 100)
        self.assertEqual(self._record_kwargs()["discount_percent"], 0)
        self.assertIsNone(self._record_kwargs()["promo_code"])
        self.stubs.promo_service.validate.assert_not_awaited()
        self.stubs.promo_service.apply.assert_not_awaited()

    def test_discount_comes_from_promo_not_from_client(self):
        promo = _promo(discount_percent=10)
        self.stubs.promo_service.validate.return_value = _valid(promo)

        self._create(promo_code=" sale10 ", discount_percent=99)

        self.stubs.promo_service.validate.assert_awaited_once_with("SALE10", 42, require_discount=True)
        self.stubs.promo_service.apply.assert_awaited_once_with(42, promo)
        self.assertEqual(self.stubs.create_payment.call_args.kwargs["amount"], 90)
        self.assertEqual(self._record_kwargs()["discount_percent"], 10)
        self.assertEqual(self._record_kwargs()["promo_code"], "SALE10")
        self.stubs.promo_service.release.assert_not_awaited()

    def test_invalid_promo_is_rejected_before_invoice(self):
        self.stubs.promo_service.validate.return_value = SimpleNamespace(
            is_valid=False, error_message="Промокод не найден.", promo=None,
        )

        with self.assertRaises(HTTPException) as ctx:
            self._create(promo_code="NOPE", discount_percent=99)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Промокод не найден.")
        self.stubs.promo_service.apply.assert_not_awaited()
        self.stubs.create_payment.assert_not_called()

    def test_promo_without_discount_is_rejected(self):
        self.stubs.promo_service.validate.return_value = _valid(_promo(discount_percent=0, bonus_days=7))

        with self.assertRaises(HTTPException) as ctx:
            self._create(promo_code="DAYS7", discount_percent=50)

        self.assertEqual(ctx.exception.status_code, 400)
        self.stubs.promo_service.apply.assert_not_awaited()
        self.stubs.create_payment.assert_not_called()

    def test_lost_claim_race_is_rejected_before_invoice(self):
        self.stubs.promo_service.validate.return_value = _valid(_promo())
        self.stubs.promo_service.apply.side_effect = PromoClaimError()

        with self.assertRaises(HTTPException) as ctx:
            self._create(promo_code="SALE10")

        self.assertEqual(ctx.exception.status_code, 400)
        self.stubs.create_payment.assert_not_called()
        self.stubs.promo_service.release.assert_not_awaited()

    def test_claim_is_released_when_gateway_fails(self):
        promo = _promo()
        self.stubs.promo_service.validate.return_value = _valid(promo)
        self.stubs.create_payment.side_effect = RuntimeError("YooKassa down")

        with self.assertRaises(HTTPException) as ctx:
            self._create(promo_code="SALE10")

        self.assertEqual(ctx.exception.status_code, 500)
        self.stubs.promo_service.release.assert_awaited_once_with(42, promo)

    def test_claim_is_released_when_payment_record_fails(self):
        promo = _promo()
        self.stubs.promo_service.validate.return_value = _valid(promo)
        self.stubs.payment_service.create_payment_record.side_effect = RuntimeError("DB down")

        with self.assertRaises(HTTPException):
            self._create(promo_code="SALE10")

        self.stubs.promo_service.release.assert_awaited_once_with(42, promo)

    def test_release_failure_does_not_mask_gateway_error(self):
        self.stubs.promo_service.validate.return_value = _valid(_promo())
        self.stubs.create_payment.side_effect = RuntimeError("YooKassa down")
        self.stubs.promo_service.release.side_effect = RuntimeError("DB down")

        with self.assertRaises(HTTPException) as ctx:
            self._create(promo_code="SALE10")

        self.assertEqual(ctx.exception.status_code, 500)

    def test_intro_tariff_ignores_promo(self):
        if not hasattr(self.module, "intro_block_reason"):
            self.skipTest("вводного тарифа в этой версии роутера нет")
        self.stubs.tariff.is_intro = True
        self.stubs.tariff.renew_tariff_id = 1
        self.stubs.promo_service.validate.return_value = _valid(_promo())

        self._create(tariff_id=5, promo_code="SALE10", discount_percent=99)

        self.stubs.promo_service.validate.assert_not_awaited()
        self.stubs.promo_service.apply.assert_not_awaited()
        self.assertEqual(self.stubs.create_payment.call_args.kwargs["amount"], 100)
        self.assertEqual(self._record_kwargs()["discount_percent"], 0)
        self.assertIsNone(self._record_kwargs()["promo_code"])


if __name__ == "__main__":
    unittest.main()
