"""
Доп. устройства в платёжном флоу: автосписание, вебхук, возврат.

Три места, где фича протекает деньгами, если ошибиться:
  * автосписание берёт цену голого тарифа → купленные слоты становятся вечными;
  * вебхук докупки продлевает подписку → человек получает дни, за которые не платил;
  * возврат за слоты вычитает дни подписки → отбираем не то, что вернули.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_stars_payment import load_payment_service_module


def _tariff(**overrides):
    defaults = {"id": 5, "name": "Три месяца", "duration_days": 90,
                "data_limit_gb": None, "price": 399, "loyalty_price": None,
                "is_active": True, "is_intro": False, "renew_tariff_id": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _payment(**overrides):
    defaults = {
        "id": 1,
        "yookassa_payment_id": "yk-1",
        "user_id": 42,
        "tariff_id": 5,
        "final_amount": 399.0,
        "status": "pending",
        "kind": "subscription",
        "extra_devices": 0,
        "source": "bot",
        "discount_percent": 0,
        "promo_code": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _user(**overrides):
    defaults = {"user_id": 42, "email": "a@b.c", "is_first_payment_made": True,
                "extra_devices": 0, "remnawave_uuid": "uuid-1",
                "subscription_end_date": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _device_slot_service(module_settings=None, set_slots_result=2, add_slots_result=3):
    from test_device_slot_service import _real_device_pricing

    pricing = _real_device_pricing()
    service = SimpleNamespace(
        settings=AsyncMock(return_value=module_settings or pricing.DeviceSettings()),
        set_slots=AsyncMock(return_value=set_slots_result),
        add_slots=AsyncMock(return_value=add_slots_result),
        sync_limit=AsyncMock(),
    )
    return service


def _build_service(module, *, tariff=None, payment=None, user=None, slot_service=None,
                   payment_method_service=None):
    payment_repo = SimpleNamespace(
        get_by_yookassa_id=AsyncMock(return_value=payment),
        get_user_pending=AsyncMock(return_value=None),
        create=AsyncMock(return_value=payment),
        update_status=AsyncMock(),
    )
    service = module.PaymentService(
        subscription_service=SimpleNamespace(
            extend=AsyncMock(return_value=SimpleNamespace(is_new_user=False, username="user_42")),
            _remnawave=SimpleNamespace(),
        ),
        referral_service=SimpleNamespace(process_first_payment_bonus=AsyncMock(return_value=None)),
        user_repo=SimpleNamespace(
            get=AsyncMock(return_value=user),
            set_first_payment_done=AsyncMock(),
            extend_subscription=AsyncMock(),
        ),
        tariff_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=tariff)),
        payment_repo=payment_repo,
        payment_method_service=payment_method_service,
        device_slot_service=slot_service,
    )
    return service, payment_repo


# =============================================================================
# --- Автосписание: слоты обязаны попадать в сумму ---
# =============================================================================

class ChargeRenewalSlotsTests(unittest.IsolatedAsyncioTestCase):
    def _card_service(self):
        repo = SimpleNamespace(
            get_by_user=AsyncMock(return_value=SimpleNamespace(
                user_id=42, auto_renew_enabled=True, renew_tariff_id=5,
                yookassa_payment_method_id="pm-1", fail_count=0,
            )),
            increment_fail=AsyncMock(),
        )
        return SimpleNamespace(_repo=repo)

    async def _charge(self, module, user, slot_service):
        service, payment_repo = _build_service(
            module,
            tariff=_tariff(),
            payment=_payment(),
            user=user,
            slot_service=slot_service,
            payment_method_service=self._card_service(),
        )

        recurring = Mock(return_value=("yk-new", "succeeded"))
        payment_module = SimpleNamespace(create_recurring_payment=recurring)
        with patch.dict("sys.modules", {"tgbot.services.payment": payment_module}):
            status = await service.charge_renewal(42)

        return status, recurring, payment_repo

    async def test_slots_are_added_to_the_charged_amount(self):
        """399 ₽ тариф + 2 слота × 49 ₽ × 3 мес = 693 ₽."""
        module = load_payment_service_module()
        status, recurring, _ = await self._charge(
            module, _user(extra_devices=2), _device_slot_service()
        )

        self.assertEqual(status, "succeeded")
        self.assertEqual(recurring.call_args.kwargs["amount"], 399 + 49 * 3 * 2)

    async def test_slots_are_a_separate_receipt_line(self):
        module = load_payment_service_module()
        _, recurring, _ = await self._charge(
            module, _user(extra_devices=2), _device_slot_service()
        )

        items = recurring.call_args.kwargs["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["amount"], 399)
        self.assertEqual(items[1]["quantity"], 2)
        self.assertEqual(items[1]["amount"], 147.0)  # 49 × 3 месяца за штуку

    async def test_receipt_sum_matches_charged_amount(self):
        module = load_payment_service_module()
        _, recurring, _ = await self._charge(
            module, _user(extra_devices=3), _device_slot_service()
        )

        items = recurring.call_args.kwargs["items"]
        total = sum(item["amount"] * item["quantity"] for item in items)
        self.assertAlmostEqual(total, recurring.call_args.kwargs["amount"], places=2)

    async def test_payment_record_remembers_slots(self):
        """Иначе вебхук по этому платежу снял бы у человека слоты как «отказ»."""
        module = load_payment_service_module()
        _, _, payment_repo = await self._charge(
            module, _user(extra_devices=2), _device_slot_service()
        )

        self.assertEqual(payment_repo.create.call_args.kwargs["extra_devices"], 2)

    async def test_without_slots_nothing_changes(self):
        module = load_payment_service_module()
        _, recurring, _ = await self._charge(
            module, _user(extra_devices=0), _device_slot_service()
        )

        self.assertEqual(recurring.call_args.kwargs["amount"], 399)
        self.assertEqual(len(recurring.call_args.kwargs["items"]), 1)


# =============================================================================
# --- Вебхук: докупка слотов не трогает подписку ---
# =============================================================================

class DevicePaymentWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_payment_does_not_extend_subscription(self):
        module = load_payment_service_module()
        slot_service = _device_slot_service(add_slots_result=3)
        payment = _payment(kind="devices", tariff_id=None, extra_devices=1, final_amount=77.0)
        service, payment_repo = _build_service(
            module, payment=payment, user=_user(), slot_service=slot_service
        )

        result = await service.process_successful_payment("yk-1", 77.0)

        self.assertEqual(result.kind, "devices")
        self.assertEqual(result.extra_devices, 3)
        service._subscription_service.extend.assert_not_awaited()
        slot_service.add_slots.assert_awaited_once_with(42, 1)
        payment_repo.update_status.assert_awaited_with("yk-1", "succeeded")

    async def test_device_payment_is_marked_succeeded_even_if_panel_fails(self):
        """Деньги списаны: платёж обязан закрыться, иначе ретрай вебхука начислит слоты дважды."""
        module = load_payment_service_module()
        slot_service = _device_slot_service()
        slot_service.add_slots.side_effect = RuntimeError("panel down")
        payment = _payment(kind="devices", tariff_id=None, extra_devices=1, final_amount=49.0)
        service, payment_repo = _build_service(
            module, payment=payment, user=_user(), slot_service=slot_service
        )

        result = await service.process_successful_payment("yk-1", 49.0)

        self.assertIsNotNone(result)
        payment_repo.update_status.assert_awaited_with("yk-1", "succeeded")

    async def test_amount_mismatch_is_rejected(self):
        module = load_payment_service_module()
        payment = _payment(kind="devices", tariff_id=None, extra_devices=1, final_amount=49.0)
        service, payment_repo = _build_service(
            module, payment=payment, user=_user(), slot_service=_device_slot_service()
        )

        result = await service.process_successful_payment("yk-1", 10.0)

        self.assertIsNone(result)
        payment_repo.update_status.assert_awaited_with("yk-1", "failed")

    async def test_subscription_payment_applies_chosen_slots(self):
        module = load_payment_service_module()
        slot_service = _device_slot_service(set_slots_result=2)
        service, _ = _build_service(
            module,
            tariff=_tariff(),
            payment=_payment(extra_devices=2),
            user=_user(),
            slot_service=slot_service,
        )

        result = await service.process_successful_payment("yk-1", 399.0)

        slot_service.set_slots.assert_awaited_once_with(42, 2)
        self.assertEqual(result.extra_devices, 2)

    async def test_panel_failure_does_not_break_subscription_payment(self):
        module = load_payment_service_module()
        slot_service = _device_slot_service()
        slot_service.set_slots.side_effect = RuntimeError("panel down")
        service, payment_repo = _build_service(
            module,
            tariff=_tariff(),
            payment=_payment(extra_devices=2),
            user=_user(),
            slot_service=slot_service,
        )

        result = await service.process_successful_payment("yk-1", 399.0)

        self.assertIsNotNone(result)
        payment_repo.update_status.assert_awaited_with("yk-1", "succeeded")


# =============================================================================
# --- Возврат за слоты ---
# =============================================================================

class DeviceRefundTests(unittest.IsolatedAsyncioTestCase):
    async def test_refund_removes_slots_not_days(self):
        module = load_payment_service_module()
        slot_service = _device_slot_service()
        payment = _payment(kind="devices", tariff_id=None, extra_devices=2,
                           status="succeeded", final_amount=98.0)
        service, payment_repo = _build_service(
            module, payment=payment, user=_user(extra_devices=2), slot_service=slot_service
        )

        result = await service.process_refund("yk-1")

        self.assertIsNotNone(result)
        slot_service.add_slots.assert_awaited_once_with(42, -2)
        service._user_repo.extend_subscription.assert_not_awaited()
        payment_repo.update_status.assert_awaited_with("yk-1", "refunded")


if __name__ == "__main__":
    unittest.main()
