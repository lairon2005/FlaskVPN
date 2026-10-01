"""
Докупленный трафик в платёжном флоу: чекаут, автосписание, вебхук докупки, возврат.

Деньгами протекает то же, что у слотов:
  * автосписание берёт голый тариф → докупленные ГБ становятся вечными и бесплатными;
  * вебхук докупки продлевает подписку или сбрасывает счётчик → человек получает то, за что не платил;
  * квота NULL/0/N в панели: NULL → базовая 500, 0 → безлимит.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from real_traffic_pricing import real_traffic_pricing
import test_payment_slots as slots_tests
from test_payment_slots import _build_service, _device_slot_service, _payment, _tariff, _user
from test_stars_payment import load_payment_service_module

tp = real_traffic_pricing()


def _traffic_service(add_gb_result=300):
    return SimpleNamespace(
        settings=AsyncMock(return_value=tp.TrafficSettings()),
        add_gb=AsyncMock(return_value=add_gb_result),
        record_purchase=AsyncMock(),
    )


def _service(module, traffic, **kwargs):
    service, payment_repo = _build_service(module, **kwargs)
    service._traffic_service = traffic
    return service, payment_repo


class SubscriptionQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def _pay(self, tariff, payment):
        module = load_payment_service_module()
        traffic = _traffic_service()
        service, _ = _service(module, traffic, tariff=tariff, payment=payment, user=_user())
        result = await service.process_successful_payment("yk-1", payment.final_amount)
        return service, traffic, result

    async def test_tariff_without_limit_gets_base_quota(self):
        service, traffic, _ = await self._pay(_tariff(data_limit_gb=None), _payment())
        service._subscription_service.extend.assert_awaited_once_with(42, 90, data_limit_gb=500)
        traffic.record_purchase.assert_awaited_once_with(42, 500, 0)

    async def test_zero_means_unlimited(self):
        service, traffic, _ = await self._pay(_tariff(data_limit_gb=0), _payment())
        service._subscription_service.extend.assert_awaited_once_with(42, 90, data_limit_gb=0)
        traffic.record_purchase.assert_awaited_once_with(42, 0, 0)

    async def test_explicit_limit_is_kept(self):
        service, _, _ = await self._pay(_tariff(data_limit_gb=200), _payment())
        service._subscription_service.extend.assert_awaited_once_with(42, 90, data_limit_gb=200)

    async def test_paid_packs_are_added_to_panel_limit(self):
        service, traffic, result = await self._pay(
            _tariff(data_limit_gb=None), _payment(extra_traffic_gb=200)
        )
        service._subscription_service.extend.assert_awaited_once_with(42, 90, data_limit_gb=700)
        traffic.record_purchase.assert_awaited_once_with(42, 500, 200)
        self.assertEqual(result.extra_traffic_gb, 200)

    async def test_packs_are_ignored_on_unlimited_tariff(self):
        service, traffic, result = await self._pay(
            _tariff(data_limit_gb=0), _payment(extra_traffic_gb=200)
        )
        service._subscription_service.extend.assert_awaited_once_with(42, 90, data_limit_gb=0)
        self.assertEqual(result.extra_traffic_gb, 0)

    async def test_packs_are_capped_by_server_limit(self):
        """Цифра в платеже — не повод выдать больше потолка из настроек."""
        service, _, result = await self._pay(
            _tariff(data_limit_gb=None), _payment(extra_traffic_gb=5000)
        )
        service._subscription_service.extend.assert_awaited_once_with(42, 90, data_limit_gb=1500)
        self.assertEqual(result.extra_traffic_gb, 1000)

    async def test_recording_failure_does_not_break_payment(self):
        module = load_payment_service_module()
        traffic = _traffic_service()
        traffic.record_purchase.side_effect = RuntimeError("db down")
        service, payment_repo = _service(
            module, traffic, tariff=_tariff(), payment=_payment(), user=_user()
        )
        result = await service.process_successful_payment("yk-1", 399.0)
        self.assertIsNotNone(result)
        payment_repo.update_status.assert_awaited_with("yk-1", "succeeded")


class TrafficPaymentWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_traffic_payment_does_not_extend_or_reset(self):
        module = load_payment_service_module()
        traffic = _traffic_service(add_gb_result=300)
        payment = _payment(kind="traffic", tariff_id=None, extra_traffic_gb=100, final_amount=49.0)
        service, payment_repo = _service(module, traffic, payment=payment, user=_user())

        result = await service.process_successful_payment("yk-1", 49.0)

        self.assertEqual(result.kind, "traffic")
        self.assertEqual(result.extra_traffic_gb, 300)
        service._subscription_service.extend.assert_not_awaited()
        traffic.add_gb.assert_awaited_once_with(42, 100)
        payment_repo.update_status.assert_awaited_with("yk-1", "succeeded")

    async def test_paid_traffic_is_closed_even_if_panel_fails(self):
        """Деньги списаны: платёж закрывается, иначе ретрай вебхука начислит ГБ дважды."""
        module = load_payment_service_module()
        traffic = _traffic_service()
        traffic.add_gb.side_effect = RuntimeError("panel down")
        payment = _payment(kind="traffic", tariff_id=None, extra_traffic_gb=100, final_amount=49.0)
        service, payment_repo = _service(module, traffic, payment=payment, user=_user())

        result = await service.process_successful_payment("yk-1", 49.0)

        self.assertIsNotNone(result)
        payment_repo.update_status.assert_awaited_with("yk-1", "succeeded")

    async def test_amount_mismatch_is_rejected(self):
        module = load_payment_service_module()
        payment = _payment(kind="traffic", tariff_id=None, extra_traffic_gb=100, final_amount=49.0)
        service, payment_repo = _service(module, _traffic_service(), payment=payment, user=_user())

        self.assertIsNone(await service.process_successful_payment("yk-1", 1.0))
        payment_repo.update_status.assert_awaited_with("yk-1", "failed")


class TrafficRefundTests(unittest.IsolatedAsyncioTestCase):
    async def test_refund_removes_gb_and_keeps_days(self):
        module = load_payment_service_module()
        traffic = _traffic_service()
        payment = _payment(kind="traffic", tariff_id=None, extra_traffic_gb=100,
                           status="succeeded", final_amount=49.0)
        service, payment_repo = _service(module, traffic, payment=payment, user=_user())

        await service.process_refund("yk-1")

        traffic.add_gb.assert_awaited_once_with(42, -100)
        service._user_repo.extend_subscription.assert_not_awaited()
        payment_repo.update_status.assert_awaited_with("yk-1", "refunded")


class ChargeRenewalTrafficTests(unittest.IsolatedAsyncioTestCase):
    async def _charge(self, user, tariff=None, traffic=None):
        module = load_payment_service_module()
        traffic = traffic or _traffic_service()
        service, payment_repo = _service(
            module, traffic,
            tariff=tariff or _tariff(),
            payment=_payment(),
            user=user,
            slot_service=_device_slot_service(),
            payment_method_service=slots_tests.ChargeRenewalSlotsTests._card_service(None),
        )
        recurring = Mock(return_value=("yk-new", "succeeded"))
        with patch.dict("sys.modules", {"tgbot.services.payment": SimpleNamespace(create_recurring_payment=recurring)}):
            status = await service.charge_renewal(42)
        return status, recurring, payment_repo

    async def test_packs_are_added_to_the_charge(self):
        """399 ₽ тариф + 2 пакета × 49 ₽ × 3 мес = 693 ₽."""
        _, recurring, _ = await self._charge(_user(extra_traffic_gb=200))
        self.assertEqual(recurring.call_args.kwargs["amount"], 399 + 49 * 3 * 2)

    async def test_traffic_is_a_separate_receipt_line_and_sum_matches(self):
        _, recurring, _ = await self._charge(_user(extra_traffic_gb=200))
        items = recurring.call_args.kwargs["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["quantity"], 2)
        total = sum(i["amount"] * i["quantity"] for i in items)
        self.assertAlmostEqual(total, recurring.call_args.kwargs["amount"], places=2)

    async def test_payment_record_remembers_traffic(self):
        """Иначе вебхук по этому платежу молча снял бы докупленные ГБ."""
        _, _, payment_repo = await self._charge(_user(extra_traffic_gb=200))
        self.assertEqual(payment_repo.create.call_args.kwargs["extra_traffic_gb"], 200)

    async def test_no_extra_traffic_changes_nothing(self):
        _, recurring, _ = await self._charge(_user(extra_traffic_gb=0))
        self.assertEqual(recurring.call_args.kwargs["amount"], 399)
        self.assertEqual(len(recurring.call_args.kwargs["items"]), 1)

    async def test_unlimited_tariff_does_not_charge_for_packs(self):
        _, recurring, payment_repo = await self._charge(
            _user(extra_traffic_gb=200), tariff=_tariff(data_limit_gb=0)
        )
        self.assertEqual(recurring.call_args.kwargs["amount"], 399)
        self.assertEqual(payment_repo.create.call_args.kwargs["extra_traffic_gb"], 0)

    async def test_slots_and_traffic_are_charged_together(self):
        _, recurring, _ = await self._charge(_user(extra_devices=1, extra_traffic_gb=100))
        self.assertEqual(recurring.call_args.kwargs["amount"], 399 + 49 * 3 + 49 * 3)


if __name__ == "__main__":
    unittest.main()
