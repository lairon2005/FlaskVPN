"""«Свои дни» в платёжном флоу: kind='custom' — тарифа нет, срок и квота считаются из платежа."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from real_traffic_pricing import real_traffic_pricing
from test_payment_slots import _build_service, _device_slot_service, _payment, _tariff, _user
from test_payment_traffic import _traffic_service
from test_stars_payment import load_payment_service_module


def _custom_payment(**overrides):
    defaults = dict(kind="custom", tariff_id=None, days=45, final_amount=279.0, source="cash", manager_id=7)
    defaults.update(overrides)
    return _payment(**defaults)


def _service(module, *, payment, user=None, tariffs=None, settings=None, card_service=None):
    tariffs = tariffs if tariffs is not None else [
        _tariff(id=5, duration_days=30, price=149), _tariff(id=6, duration_days=90, price=399),
    ]
    service, payment_repo = _build_service(
        module, payment=payment, user=user or _user(),
        slot_service=_device_slot_service(), payment_method_service=card_service,
    )
    service._traffic_service = _traffic_service()
    service._tariff_repo = SimpleNamespace(
        get_by_id=AsyncMock(side_effect=lambda i: next((t for t in tariffs if t.id == i), None)),
        get_active=AsyncMock(return_value=tariffs),
    )
    service._settings_repo = SimpleNamespace(get_int=AsyncMock(return_value=(settings or {}).get("renew", 0)))
    return service, payment_repo


class CustomPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_extends_by_payment_days_with_base_quota(self):
        module = load_payment_service_module()
        service, repo = _service(module, payment=_custom_payment())

        result = await service.process_successful_payment("cash:1", 279.0)

        service._subscription_service.extend.assert_awaited_once_with(42, 45, data_limit_gb=500)
        self.assertEqual(result.kind, "custom")
        self.assertEqual(result.days, 45)
        self.assertIsNone(result.tariff)
        repo.update_status.assert_awaited_with("cash:1", "succeeded")

    async def test_extras_of_the_client_are_kept(self):
        module = load_payment_service_module()
        service, _ = _service(module, payment=_custom_payment(extra_traffic_gb=200, extra_devices=2))

        await service.process_successful_payment("cash:1", 279.0)

        service._subscription_service.extend.assert_awaited_once_with(42, 45, data_limit_gb=700)
        service._device_slot_service.set_slots.assert_awaited_once_with(42, 2)

    async def test_first_payment_is_marked_and_referrer_rewarded(self):
        module = load_payment_service_module()
        service, _ = _service(module, payment=_custom_payment(), user=_user(is_first_payment_made=False))

        result = await service.process_successful_payment("cash:1", 279.0)

        self.assertTrue(result.is_first_payment)
        service._user_repo.set_first_payment_done.assert_awaited_once_with(42)
        service._referral_service.process_first_payment_bonus.assert_awaited_once_with(42)

    async def test_payment_without_days_is_refused(self):
        module = load_payment_service_module()
        service, _ = _service(module, payment=_custom_payment(days=None))

        self.assertIsNone(await service.process_successful_payment("cash:1", 279.0))
        service._subscription_service.extend.assert_not_awaited()

    async def test_card_is_saved_for_the_configured_renew_tariff(self):
        module = load_payment_service_module()
        cards = SimpleNamespace(save_from_yookassa=AsyncMock(return_value=True))
        service, _ = _service(module, payment=_custom_payment(source="manager"),
                              card_service=cards, settings={"renew": 6})

        result = await service.process_successful_payment("yk-1", 279.0, payment_method=SimpleNamespace(saved=True))

        self.assertTrue(result.card_saved)
        self.assertEqual(cards.save_from_yookassa.await_args.kwargs["renew_tariff_id"], 6)

    async def test_unset_renew_tariff_falls_back_to_closest_to_a_month(self):
        module = load_payment_service_module()
        cards = SimpleNamespace(save_from_yookassa=AsyncMock(return_value=True))
        service, _ = _service(module, payment=_custom_payment(source="manager"), card_service=cards)

        await service.process_successful_payment("yk-1", 279.0, payment_method=SimpleNamespace(saved=True))

        self.assertEqual(cards.save_from_yookassa.await_args.kwargs["renew_tariff_id"], 5)

    async def test_hidden_renew_tariff_is_not_used(self):
        module = load_payment_service_module()
        cards = SimpleNamespace(save_from_yookassa=AsyncMock(return_value=True))
        hidden = _tariff(id=9, duration_days=30, price=1, is_active=False)
        service, _ = _service(module, payment=_custom_payment(source="manager"), card_service=cards,
                              tariffs=[_tariff(id=5, duration_days=30, price=149), hidden], settings={"renew": 9})
        # get_by_id отдаёт и скрытый тариф — именно это и проверяем
        service._tariff_repo.get_by_id = AsyncMock(side_effect=lambda i: hidden if i == 9 else _tariff(id=5))

        await service.process_successful_payment("yk-1", 279.0, payment_method=SimpleNamespace(saved=True))

        self.assertEqual(cards.save_from_yookassa.await_args.kwargs["renew_tariff_id"], 5)

    async def test_cash_payment_does_not_try_to_save_a_card(self):
        module = load_payment_service_module()
        cards = SimpleNamespace(save_from_yookassa=AsyncMock())
        service, _ = _service(module, payment=_custom_payment(), card_service=cards)

        result = await service.process_successful_payment("cash:1", 279.0)  # без payment_method

        cards.save_from_yookassa.assert_not_awaited()
        self.assertFalse(result.card_saved)

    async def test_refund_takes_back_the_paid_days(self):
        module = load_payment_service_module()
        service, repo = _service(
            module, payment=_custom_payment(status="succeeded"),
            user=_user(subscription_end_date=__import__("datetime").datetime.now() + __import__("datetime").timedelta(days=60)),
        )
        service._subscription_service._remnawave = SimpleNamespace(
            update_user_expiry=AsyncMock(), disable_user=AsyncMock(),
        )

        await service.process_refund("cash:1")

        service._user_repo.extend_subscription.assert_awaited_once_with(42, -45)


if __name__ == "__main__":
    unittest.main()
