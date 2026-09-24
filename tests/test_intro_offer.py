# tests/test_intro_offer.py
"""
Вводный тариф: «1 ₽ за 7 дней → автоматически полная цена тарифа продления».

Проверяется:
  * правила витрины и права на покупку (intro_offer.py — чистые функции);
  * оплата вводного: неделя выдана, intro_used проставлен, но это НЕ первая
    оплата — ни флага is_first_payment_made, ни бонуса рефереру; карта
    сохраняется с тарифом продления, а не с самим вводным (иначе автопродление
    вечно списывало бы 1 ₽);
  * переход: charge_renewal списывает обычную цену тарифа продления, а не
    лоялти-цену, и не списывает за вводный/скрытый тариф.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from real_intro_offer import real_intro_offer
from test_stars_payment import load_payment_service_module
from test_payment_slots import _build_service, _payment, _tariff
from test_stale_payment_cancel import load_scheduler_module

intro = real_intro_offer()


def _user(**overrides):
    defaults = {"user_id": 42, "is_first_payment_made": False, "intro_used": False,
                "extra_devices": 0, "email": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


MONTH = _tariff(id=2, name="Месяц", price=99, loyalty_price=79, duration_days=30)
WEEK = _tariff(id=1, name="Пробная неделя", price=1, duration_days=7,
               is_intro=True, renew_tariff_id=2)
BY_ID = {1: WEEK, 2: MONTH}


# =============================================================================
# --- Правила (чистые функции) ---
# =============================================================================

class EligibilityTests(unittest.TestCase):
    def test_new_user_is_eligible(self):
        self.assertTrue(intro.is_intro_eligible(_user()))

    def test_paid_user_is_not_eligible(self):
        self.assertFalse(intro.is_intro_eligible(_user(is_first_payment_made=True)))

    def test_repeat_intro_is_not_allowed(self):
        self.assertFalse(intro.is_intro_eligible(_user(intro_used=True)))

    def test_in_intro_only_until_first_full_payment(self):
        self.assertTrue(intro.is_in_intro(_user(intro_used=True)))
        self.assertFalse(intro.is_in_intro(_user(intro_used=True, is_first_payment_made=True)))
        self.assertFalse(intro.is_in_intro(_user()))


class ShowcaseTests(unittest.TestCase):
    def test_intro_goes_first_for_eligible_user(self):
        shown = intro.visible_tariffs([MONTH, WEEK], _user(), BY_ID)
        self.assertEqual([t.id for t in shown], [1, 2])

    def test_intro_hidden_for_paid_user(self):
        shown = intro.visible_tariffs([MONTH, WEEK], _user(is_first_payment_made=True), BY_ID)
        self.assertEqual([t.id for t in shown], [2])

    def test_intro_without_renew_tariff_is_not_sold(self):
        orphan = _tariff(id=3, price=1, duration_days=7, is_intro=True, renew_tariff_id=None)
        self.assertFalse(intro.is_sellable_intro(orphan, BY_ID))
        self.assertEqual(intro.visible_tariffs([orphan, MONTH], _user(), BY_ID), [MONTH])

    def test_intro_renewing_into_hidden_tariff_is_not_sold(self):
        hidden_month = _tariff(id=2, price=99, duration_days=30, is_active=False)
        self.assertFalse(intro.is_sellable_intro(WEEK, {1: WEEK, 2: hidden_month}))


class BlockReasonTests(unittest.TestCase):
    def test_regular_active_tariff_is_allowed(self):
        self.assertIsNone(intro.intro_block_reason(MONTH, _user(is_first_payment_made=True), BY_ID))

    def test_hidden_tariff_is_blocked(self):
        hidden = _tariff(is_active=False)
        self.assertIsNotNone(intro.intro_block_reason(hidden, _user(), BY_ID))

    def test_intro_blocked_for_paid_user(self):
        reason = intro.intro_block_reason(WEEK, _user(is_first_payment_made=True), BY_ID)
        self.assertIn("первой покупке", reason)

    def test_consent_mentions_both_prices(self):
        text = intro.consent_text(WEEK, MONTH)
        self.assertIn("1 ₽ за 7 дн.", text)
        self.assertIn("99 ₽", text)  # обычная цена, не лоялти 79
        self.assertNotIn("79", text)


# =============================================================================
# --- Оплата вводного тарифа (вебхук) ---
# =============================================================================

class IntroPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def _process(self, *, user, tariff, card_saved=True):
        module = load_payment_service_module()
        pm_service = SimpleNamespace(save_from_yookassa=AsyncMock(return_value=card_saved))
        service, payment_repo = _build_service(
            module, tariff=tariff, payment=_payment(final_amount=tariff.price, tariff_id=tariff.id),
            user=user, payment_method_service=pm_service,
        )
        service._user_repo.set_intro_used = AsyncMock()
        service._tariff_repo.get_by_id = AsyncMock(side_effect=lambda tid: BY_ID.get(tid))
        result = await service.process_successful_payment(
            "yk-1", float(tariff.price), payment_method=SimpleNamespace(saved=card_saved)
        )
        return service, pm_service, result

    async def test_intro_is_not_a_first_payment(self):
        service, _, result = await self._process(user=_user(), tariff=WEEK)

        service._user_repo.set_intro_used.assert_awaited_once_with(42)
        service._user_repo.set_first_payment_done.assert_not_awaited()
        service._referral_service.process_first_payment_bonus.assert_not_awaited()
        self.assertFalse(result.is_first_payment)
        self.assertTrue(result.is_intro)

    async def test_week_is_granted(self):
        service, _, _ = await self._process(user=_user(), tariff=WEEK)
        service._subscription_service.extend.assert_awaited_once_with(42, 7, data_limit_gb=0)

    async def test_card_renews_into_monthly_tariff(self):
        _, pm_service, result = await self._process(user=_user(), tariff=WEEK)

        self.assertEqual(pm_service.save_from_yookassa.await_args.kwargs["renew_tariff_id"], 2)
        self.assertEqual(result.renew_tariff.id, 2)
        self.assertTrue(result.card_saved)

    async def test_unsaved_card_is_reported(self):
        _, _, result = await self._process(user=_user(), tariff=WEEK, card_saved=False)
        self.assertFalse(result.card_saved)

    async def test_full_price_after_intro_is_the_first_payment(self):
        service, pm_service, result = await self._process(user=_user(intro_used=True), tariff=MONTH)

        service._user_repo.set_first_payment_done.assert_awaited_once_with(42)
        service._referral_service.process_first_payment_bonus.assert_awaited_once_with(42)
        self.assertTrue(result.is_first_payment)
        self.assertEqual(pm_service.save_from_yookassa.await_args.kwargs["renew_tariff_id"], 2)


# =============================================================================
# --- Переход на полную цену (автосписание) ---
# =============================================================================

class ConversionChargeTests(unittest.IsolatedAsyncioTestCase):
    async def _charge(self, *, user, renew_tariff):
        module = load_payment_service_module()
        # Настоящая логика лоялти: непрерывное продление идёт по loyalty_price.
        module.effective_price = lambda t, user_has_active_sub=False: (
            t.loyalty_price if user_has_active_sub and t.loyalty_price else t.price
        )
        repo = SimpleNamespace(
            get_by_user=AsyncMock(return_value=SimpleNamespace(
                user_id=42, auto_renew_enabled=True, renew_tariff_id=renew_tariff.id,
                yookassa_payment_method_id="pm-1", fail_count=0,
            )),
            increment_fail=AsyncMock(),
        )
        service, payment_repo = _build_service(
            module, tariff=renew_tariff, payment=_payment(), user=user,
            payment_method_service=SimpleNamespace(_repo=repo),
        )
        recurring = Mock(return_value=("yk-new", "pending"))
        with patch.dict("sys.modules", {"tgbot.services.payment": SimpleNamespace(create_recurring_payment=recurring)}):
            status = await service.charge_renewal(42)
        return status, recurring

    async def test_intro_user_pays_regular_price_not_loyalty(self):
        status, recurring = await self._charge(user=_user(intro_used=True), renew_tariff=MONTH)
        self.assertEqual(status, "pending")
        self.assertEqual(recurring.call_args.kwargs["amount"], 99)

    async def test_regular_renewal_keeps_loyalty_price(self):
        status, recurring = await self._charge(
            user=_user(intro_used=True, is_first_payment_made=True), renew_tariff=MONTH
        )
        self.assertEqual(recurring.call_args.kwargs["amount"], 79)

    async def test_never_charges_for_intro_tariff(self):
        status, recurring = await self._charge(user=_user(intro_used=True), renew_tariff=WEEK)
        self.assertEqual(status, "skipped")
        recurring.assert_not_called()

    async def test_never_charges_for_hidden_tariff(self):
        hidden = _tariff(id=2, price=99, duration_days=30, is_active=False)
        status, recurring = await self._charge(user=_user(intro_used=True), renew_tariff=hidden)
        self.assertEqual(status, "skipped")
        recurring.assert_not_called()



# =============================================================================
# --- Планировщик: джоб перехода и отказы карты ---
# =============================================================================

def _card(**overrides):
    defaults = {"user_id": 42, "fail_count": 1, "card_last4": "1234",
                "renew_tariff_id": 2, "auto_renew_enabled": True}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class ConversionJobTests(unittest.IsolatedAsyncioTestCase):
    def test_job_is_registered_hourly(self):
        module, _ = load_scheduler_module()
        scheduler = Mock()
        module.schedule_jobs(scheduler, Mock())

        calls = {c.args[0]: c.kwargs for c in scheduler.add_job.call_args_list}
        self.assertIn(module.convert_intro_subscriptions, calls)
        self.assertNotIn("hour", calls[module.convert_intro_subscriptions])

    async def test_attempt_is_marked_before_charge(self):
        module, stubs = load_scheduler_module()
        repo = stubs["database"].payment_method_repo
        repo.get_intro_due_for_conversion = AsyncMock(return_value=[_card()])
        order = []
        repo.mark_attempt = AsyncMock(side_effect=lambda uid: order.append("mark"))
        payment_service = stubs["tgbot.services"].payment_service
        payment_service.charge_renewal = AsyncMock(side_effect=lambda uid: order.append("charge") or "pending")

        with patch.dict("sys.modules", stubs):
            await module.convert_intro_subscriptions(Mock())

        self.assertEqual(order, ["mark", "charge"])

    async def test_intro_decline_notifies_user_right_away(self):
        module, stubs = load_scheduler_module()
        db = stubs["database"]
        db.payment_method_repo.get_by_user = AsyncMock(return_value=_card(fail_count=1))
        db.user_repo.get = AsyncMock(return_value=_user(intro_used=True))
        db.tariff_repo.get_by_id = AsyncMock(return_value=MONTH)
        module.send_reminder = AsyncMock(return_value=True)

        disabled = await module.handle_renewal_failure(Mock(), 42)

        self.assertFalse(disabled)
        text = module.send_reminder.await_args.args[2]
        self.assertIn("99 ₽", text)
        self.assertIn("*1234", text)
        self.assertIn("1/3", text)

    async def test_third_decline_disables_auto_renew(self):
        module, stubs = load_scheduler_module()
        db = stubs["database"]
        db.payment_method_repo.get_by_user = AsyncMock(return_value=_card(fail_count=3))
        db.payment_method_repo.set_auto_renew = AsyncMock()
        db.user_repo.get = AsyncMock(return_value=_user(intro_used=True))
        module.send_reminder = AsyncMock(return_value=True)

        disabled = await module.handle_renewal_failure(Mock(), 42)

        self.assertTrue(disabled)
        db.payment_method_repo.set_auto_renew.assert_awaited_once_with(42, False)


if __name__ == "__main__":
    unittest.main()
