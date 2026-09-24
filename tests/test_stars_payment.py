# tests/test_stars_payment.py
"""
Telegram Stars (XTR) — docs/tma-roadmap.md фаза 3.2. Без БД и сети:
- pre_checkout_query валидирует payload/тариф/цену дёшево (Telegram ждёт ответ 10с);
- PaymentService.process_stars_payment идемпотентен по telegram_payment_charge_id
  (Telegram может повторно доставить successful_payment);
- PaymentRequest.source (фаза 3.1, webapp/routers/payment.py) принимает только
  "web"/"tma".

Модули загружаются через importlib.util.spec_from_file_location мимо
tgbot/handlers/__init__.py и tgbot/services/__init__.py — оба на импорте тянут
loader.py -> config.load_config(), которому в тестовом окружении не хватает
REMNAWAVE_API_URL и т.п. (тот же приём, что и в test_promo_code_service.py /
test_subscription_service.py / test_payment_repository.py).
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from real_intro_offer import real_intro_offer


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
    """Настоящий tgbot/services/device_pricing.py — модуль без зависимостей.

    Стабить его нечем: расчёт цены доп. устройств и есть то, что проверяют
    тесты вокруг платежей, а подделка арифметики сделала бы их бессмысленными.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
    spec = importlib.util.spec_from_file_location("tgbot.services.device_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_stars_payment_module():
    """Загружает tgbot/handlers/user/stars_payment.py изолированно."""
    database_module = types.ModuleType("database")
    database_module.tariff_repo = AsyncMock()
    # pre_checkout смотрит на доп. устройства: их нельзя оплатить звёздами,
    # иначе продление подписки продлило бы и неоплаченные слоты.
    database_module.user_repo = AsyncMock()
    database_module.user_repo.get.return_value = SimpleNamespace(extra_devices=0)

    loader_module = types.ModuleType("loader")
    loader_module.logger = Mock()

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.payment_service = AsyncMock()

    module = _load_module(
        "stars_payment_under_test",
        "tgbot/handlers/user/stars_payment.py",
        {
            "database": database_module,
            "loader": loader_module,
            "tgbot": tgbot_module,
            "tgbot.services": tgbot_services_module,
            "tgbot.services.intro_offer": real_intro_offer(),
        },
    )
    return (module, database_module.tariff_repo, tgbot_services_module.payment_service,
            database_module.user_repo)


def load_payment_service_module():
    """Загружает tgbot/services/payment_service.py изолированно."""
    db_module = types.ModuleType("db")
    db_module.Tariff = type("Tariff", (), {})
    db_module.Payment = type("Payment", (), {})

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    user_repo_module = types.ModuleType("database.repositories.user")
    user_repo_module.UserRepository = type("UserRepository", (), {})
    tariff_repo_module = types.ModuleType("database.repositories.tariff")
    tariff_repo_module.TariffRepository = type("TariffRepository", (), {})
    payment_repo_module = types.ModuleType("database.repositories.payment")
    payment_repo_module.PaymentRepository = type("PaymentRepository", (), {})

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    subscription_service_module = types.ModuleType("tgbot.services.subscription_service")
    subscription_service_module.SubscriptionService = type("SubscriptionService", (), {})
    subscription_service_module.ExtensionResult = type("ExtensionResult", (), {})
    referral_service_module = types.ModuleType("tgbot.services.referral_service")
    referral_service_module.ReferralService = type("ReferralService", (), {})
    # Возврат/повторный захват промокода при отмене счёта ловит PromoClaimError.
    # Стаб держим на модуле, чтобы тесты могли бросить тот же класс:
    # module.PromoClaimError.
    promo_code_service_module = types.ModuleType("tgbot.services.promo_code_service")
    promo_code_service_module.PromoClaimError = type("PromoClaimError", (Exception,), {})
    pricing_module = types.ModuleType("tgbot.services.pricing")
    pricing_module.effective_price = lambda tariff, user_has_active_sub=False: getattr(tariff, "price", 0)
    device_pricing_module = _real_device_pricing()

    loader_module = types.ModuleType("loader")
    loader_module.logger = Mock()
    loader_module.config = SimpleNamespace(yookassa=SimpleNamespace(shop_id="x", secret_key="y"))

    return _load_module(
        "payment_service_under_test",
        "tgbot/services/payment_service.py",
        {
            "db": db_module,
            "database": database_module,
            "database.repositories": repositories_module,
            "database.repositories.user": user_repo_module,
            "database.repositories.tariff": tariff_repo_module,
            "database.repositories.payment": payment_repo_module,
            "tgbot": tgbot_module,
            "tgbot.services": tgbot_services_module,
            "tgbot.services.intro_offer": real_intro_offer(),
            "tgbot.services.subscription_service": subscription_service_module,
            "tgbot.services.referral_service": referral_service_module,
            "tgbot.services.promo_code_service": promo_code_service_module,
            "tgbot.services.pricing": pricing_module,
            "tgbot.services.device_pricing": device_pricing_module,
            "loader": loader_module,
        },
    )


def load_payment_router_module():
    """Загружает webapp/routers/payment.py изолированно (только PaymentRequest нужен)."""
    db_module = types.ModuleType("db")
    db_module.User = type("User", (), {})
    db_module.Tariff = type("Tariff", (), {})
    db_module.async_session_maker = Mock()

    database_module = types.ModuleType("database")
    database_module.tariff_repo = AsyncMock()

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    tgbot_services_module.payment_service = AsyncMock()
    tgbot_services_module.promo_service = AsyncMock()
    tgbot_services_module.subscription_service = AsyncMock()
    tgbot_services_module.device_slot_service = AsyncMock()
    tgbot_services_payment_module = types.ModuleType("tgbot.services.payment")
    tgbot_services_payment_module.create_payment = Mock()
    tgbot_services_pricing_module = types.ModuleType("tgbot.services.pricing")
    tgbot_services_pricing_module.effective_price = lambda tariff, user_has_active_sub=False: 100.0
    # webapp/routers/payment.py импортирует PromoClaimError из
    # tgbot.services.promo_code_service (см. security review TOCTOU-гонки
    # промокодов, 2026-07-26) — стабим отдельным модулем, как и остальные
    # tgbot.services.* submodules выше, иначе exec_module упадёт с
    # ModuleNotFoundError (tgbot.services.__path__ пуст).
    tgbot_services_promo_code_service_module = types.ModuleType("tgbot.services.promo_code_service")
    tgbot_services_promo_code_service_module.PromoClaimError = type("PromoClaimError", (Exception,), {})

    config_module = types.ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        tg_bot=SimpleNamespace(tg_bot_username=None, tma_app_name="app"),
        webhook=SimpleNamespace(domain="example.com"),
        yookassa=SimpleNamespace(shop_id="x", secret_key="y", save_payment_method=False),
    )

    return _load_module(
        "payment_router_under_test",
        "webapp/routers/payment.py",
        {
            "db": db_module,
            "database": database_module,
            "tgbot": tgbot_module,
            "tgbot.services": tgbot_services_module,
            "tgbot.services.intro_offer": real_intro_offer(),
            "tgbot.services.payment": tgbot_services_payment_module,
            "tgbot.services.pricing": tgbot_services_pricing_module,
            "tgbot.services.device_pricing": _real_device_pricing(),
            "tgbot.services.promo_code_service": tgbot_services_promo_code_service_module,
            "config": config_module,
        },
    )


# =============================================================================
# --- _parse_stars_payload ---
# =============================================================================

class ParseStarsPayloadTests(unittest.TestCase):
    def test_valid_payload(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertEqual(module._parse_stars_payload("stars:5:123"), (5, 123))

    def test_wrong_prefix_rejected(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertIsNone(module._parse_stars_payload("other:5:123"))

    def test_wrong_parts_count_rejected(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertIsNone(module._parse_stars_payload("stars:5"))

    def test_non_numeric_rejected(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertIsNone(module._parse_stars_payload("stars:abc:123"))


# =============================================================================
# --- pre_checkout_query ---
# =============================================================================

class PreCheckoutTests(unittest.IsolatedAsyncioTestCase):
    def _query(self, payload, from_user_id, total_amount):
        return SimpleNamespace(
            invoice_payload=payload,
            from_user=SimpleNamespace(id=from_user_id),
            total_amount=total_amount,
            answer=AsyncMock(),
        )

    async def test_malformed_payload_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        query = self._query("garbage", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])
        tariff_repo.get_by_id.assert_not_awaited()

    async def test_payload_for_other_user_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        # payload выписан на user 999, а платит user 123 — не должно происходить
        # штатно, но проверяем явно (переслаемый/скопированный счёт).
        query = self._query("stars:5:999", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])
        tariff_repo.get_by_id.assert_not_awaited()

    async def test_missing_tariff_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = None
        query = self._query("stars:5:123", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])

    async def test_price_mismatch_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = SimpleNamespace(price_stars=150, is_active=True, is_intro=False)
        # Клиент прислал total_amount=100, а тариф стоит 150 XTR.
        query = self._query("stars:5:123", 123, 100)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])

    async def test_matching_price_accepted(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = SimpleNamespace(price_stars=150, is_active=True, is_intro=False)
        query = self._query("stars:5:123", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertTrue(query.answer.await_args.kwargs["ok"])

    async def test_user_with_extra_devices_rejected(self):
        """Слоты звёздами не оплачиваются: пропустив платёж, мы продлили бы их даром."""
        module, tariff_repo, _, user_repo = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = SimpleNamespace(price_stars=150, is_active=True, is_intro=False)
        user_repo.get.return_value = SimpleNamespace(extra_devices=2)
        query = self._query("stars:5:123", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])
        self.assertIn("картой", query.answer.await_args.kwargs["error_message"])


# =============================================================================
# --- PaymentService.process_stars_payment: идемпотентность ---
# =============================================================================

class StarsServiceMixin:
    def _service(self, module, tariff, payment_repo, user=None, referrer_id=None):
        """user=None означает «пользователь не найден» — тогда is_first_payment=False."""
        tariff_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=tariff))
        subscription_service = SimpleNamespace(
            extend=AsyncMock(return_value=SimpleNamespace(is_new_user=False, username="user_1"))
        )
        referral_service = SimpleNamespace(
            process_first_payment_bonus=AsyncMock(return_value=referrer_id)
        )
        user_repo = SimpleNamespace(
            get=AsyncMock(return_value=user),
            set_first_payment_done=AsyncMock(),
        )
        service = module.PaymentService(
            subscription_service=subscription_service,
            referral_service=referral_service,
            user_repo=user_repo,
            tariff_repo=tariff_repo,
            payment_repo=payment_repo,
        )
        return service, SimpleNamespace(
            subscription=subscription_service,
            referral=referral_service,
            user_repo=user_repo,
        )


class ProcessStarsPaymentTests(StarsServiceMixin, unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_charge_id_does_not_extend_twice(self):
        module = load_payment_service_module()
        tariff = SimpleNamespace(id=5, name="Месяц", duration_days=30, data_limit_gb=None, is_active=True, is_intro=False)
        created_payment = SimpleNamespace(id=1, yookassa_payment_id="stars:CHARGE1")
        payment_repo = SimpleNamespace(
            # Первый вызов — платежа ещё нет; второй (повторная доставка апдейта
            # Telegram) — уже есть.
            get_by_telegram_charge_id=AsyncMock(side_effect=[None, created_payment]),
            create=AsyncMock(return_value=created_payment),
            update_status=AsyncMock(),
        )
        service, deps = self._service(
            module, tariff, payment_repo,
            user=SimpleNamespace(is_first_payment_made=False),
            referrer_id=77,
        )

        first = await service.process_stars_payment(
            user_id=1, tariff_id=5, total_amount=150, telegram_payment_charge_id="CHARGE1"
        )
        second = await service.process_stars_payment(
            user_id=1, tariff_id=5, total_amount=150, telegram_payment_charge_id="CHARGE1"
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        deps.subscription.extend.assert_awaited_once()
        payment_repo.create.assert_awaited_once()
        payment_repo.update_status.assert_awaited_once_with("stars:CHARGE1", "succeeded")
        # Дубликат не должен повторно начислять реферальный бонус и повторно
        # помечать первую оплату.
        deps.referral.process_first_payment_bonus.assert_awaited_once()
        deps.user_repo.set_first_payment_done.assert_awaited_once()

    async def test_missing_tariff_returns_none_without_extend(self):
        module = load_payment_service_module()
        payment_repo = SimpleNamespace(
            get_by_telegram_charge_id=AsyncMock(return_value=None),
            create=AsyncMock(),
            update_status=AsyncMock(),
        )
        service, deps = self._service(module, tariff=None, payment_repo=payment_repo)

        result = await service.process_stars_payment(
            user_id=1, tariff_id=999, total_amount=150, telegram_payment_charge_id="CHARGE2"
        )

        self.assertIsNone(result)
        deps.subscription.extend.assert_not_awaited()
        payment_repo.create.assert_not_awaited()
        deps.referral.process_first_payment_bonus.assert_not_awaited()

    async def test_successful_payment_records_stars_source_and_charge_id(self):
        module = load_payment_service_module()
        tariff = SimpleNamespace(id=5, name="Месяц", duration_days=30, data_limit_gb=None, is_active=True, is_intro=False)
        payment_repo = SimpleNamespace(
            get_by_telegram_charge_id=AsyncMock(return_value=None),
            create=AsyncMock(return_value=SimpleNamespace(id=1, yookassa_payment_id="stars:CHARGE3")),
            update_status=AsyncMock(),
        )
        service, _ = self._service(module, tariff, payment_repo)

        result = await service.process_stars_payment(
            user_id=1, tariff_id=5, total_amount=150, telegram_payment_charge_id="CHARGE3"
        )

        self.assertIsNotNone(result)
        create_kwargs = payment_repo.create.await_args.kwargs
        self.assertEqual(create_kwargs["source"], "stars")
        self.assertEqual(create_kwargs["telegram_payment_charge_id"], "CHARGE3")
        self.assertEqual(create_kwargs["final_amount"], 150)


# =============================================================================
# --- PaymentService.process_stars_payment: паритет пост-платёжной логики ---
#     (реферальный бонус + отметка первой оплаты — как в YooKassa-флоу)
# =============================================================================

class StarsPostPaymentParityTests(StarsServiceMixin, unittest.IsolatedAsyncioTestCase):
    def _repo(self, charge_id):
        return SimpleNamespace(
            get_by_telegram_charge_id=AsyncMock(return_value=None),
            create=AsyncMock(return_value=SimpleNamespace(id=1, yookassa_payment_id=f"stars:{charge_id}")),
            update_status=AsyncMock(),
        )

    async def test_first_stars_payment_awards_referral_bonus(self):
        module = load_payment_service_module()
        tariff = SimpleNamespace(id=5, name="Месяц", duration_days=30, data_limit_gb=None, is_active=True, is_intro=False)
        payment_repo = self._repo("CHARGE4")
        service, deps = self._service(
            module, tariff, payment_repo,
            user=SimpleNamespace(is_first_payment_made=False),
            referrer_id=77,
        )

        result = await service.process_stars_payment(
            user_id=1, tariff_id=5, total_amount=150, telegram_payment_charge_id="CHARGE4"
        )

        deps.referral.process_first_payment_bonus.assert_awaited_once_with(1)
        deps.user_repo.set_first_payment_done.assert_awaited_once_with(1)
        self.assertEqual(result.referrer_id, 77)
        self.assertTrue(result.is_first_payment)

    async def test_repeat_stars_payment_does_not_remark_first_payment(self):
        module = load_payment_service_module()
        tariff = SimpleNamespace(id=5, name="Месяц", duration_days=30, data_limit_gb=None, is_active=True, is_intro=False)
        payment_repo = self._repo("CHARGE5")
        # Пользователь уже платил раньше — set_first_payment_done звать не нужно.
        # process_first_payment_bonus всё равно вызывается: он сам внутри проверяет
        # is_first_payment_made и вернёт None (см. ReferralService).
        service, deps = self._service(
            module, tariff, payment_repo,
            user=SimpleNamespace(is_first_payment_made=True),
            referrer_id=None,
        )

        result = await service.process_stars_payment(
            user_id=1, tariff_id=5, total_amount=150, telegram_payment_charge_id="CHARGE5"
        )

        deps.referral.process_first_payment_bonus.assert_awaited_once_with(1)
        deps.user_repo.set_first_payment_done.assert_not_awaited()
        self.assertIsNone(result.referrer_id)
        self.assertFalse(result.is_first_payment)


# =============================================================================
# --- PaymentRequest.source (фаза 3.1) ---
# =============================================================================

class PaymentRequestSourceTests(unittest.TestCase):
    def test_default_source_is_web(self):
        module = load_payment_router_module()
        payload = module.PaymentRequest(tariff_name="Месяц", price=100)
        self.assertEqual(payload.source, "web")

    def test_web_and_tma_are_accepted(self):
        module = load_payment_router_module()
        self.assertEqual(module.PaymentRequest(tariff_name="x", price=1, source="web").source, "web")
        self.assertEqual(module.PaymentRequest(tariff_name="x", price=1, source="tma").source, "tma")

    def test_unknown_source_is_rejected(self):
        module = load_payment_router_module()
        with self.assertRaises(Exception):
            module.PaymentRequest(tariff_name="x", price=1, source="bot")


if __name__ == "__main__":
    unittest.main()
