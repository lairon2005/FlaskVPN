"""
Партнёрская (денежная) рефералка на настоящей БД (SQLite) и настоящем PaymentService:
начисление с оплаты друга, холд, возврат без ухода в минус, вывод, оплата подписки с баланса.
"""
import datetime
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from manager_env import build_env  # noqa: E402  (заглушки окружения до импорта db)

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path, stubs: dict | None = None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs or {}):
        spec.loader.exec_module(module)
    return module


rules = _load("partner_rules_under_test", ROOT / "tgbot" / "services" / "partner_rules.py")


def load_partner_service_module():
    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    return _load("partner_service_under_test", ROOT / "tgbot" / "services" / "partner_service.py", {
        "tgbot": tgbot, "tgbot.services": services,
        "loader": SimpleNamespace(logger=Mock()),
        "tgbot.services.partner_rules": rules,
        "tgbot.services.pricing": _load("pricing_for_partner", ROOT / "tgbot" / "services" / "pricing.py"),
    })


ps = load_partner_service_module()


class RulesTests(unittest.TestCase):
    def test_commission_rounds_down(self):
        self.assertEqual(rules.commission_kop(14900, 30), 4470)
        self.assertEqual(rules.commission_kop(100, 30), 30)       # 1 ₽ пробной недели → 30 коп.
        self.assertEqual(rules.commission_kop(333, 30), 99)       # 99,9 → 99
        self.assertEqual(rules.commission_kop(0, 30), 0)
        self.assertEqual(rules.commission_kop(-500, 30), 0)

    def test_which_payments_count(self):
        self.assertTrue(rules.counts_for_partner("subscription", "bot"))
        self.assertTrue(rules.counts_for_partner("subscription", "auto"))
        self.assertTrue(rules.counts_for_partner("custom", "cash"))
        self.assertTrue(rules.counts_for_partner("subscription", "manager"))
        self.assertFalse(rules.counts_for_partner("devices", "bot"))
        self.assertFalse(rules.counts_for_partner("traffic", "web"))
        self.assertFalse(rules.counts_for_partner("subscription", "stars"))
        self.assertFalse(rules.counts_for_partner("subscription", "balance"))

    def test_money_format_and_parse(self):
        self.assertEqual(rules.fmt_rub(50000), "500 ₽")
        self.assertEqual(rules.fmt_rub(4470), "44,70 ₽")
        self.assertEqual(rules.fmt_rub(123456789), "1 234 567,89 ₽")
        self.assertEqual(rules.parse_rub("1 500"), 150000)
        self.assertEqual(rules.parse_rub("1500,5 ₽"), 150050)
        self.assertIsNone(rules.parse_rub("абв"))
        self.assertIsNone(rules.parse_rub("-5"))
        self.assertIsNone(rules.parse_rub("1.234"))

    def test_phone_and_bank(self):
        self.assertEqual(rules.normalize_phone("8 (999) 123-45-67"), "+79991234567")
        self.assertEqual(rules.normalize_phone("+7 999 123 45 67"), "+79991234567")
        self.assertEqual(rules.normalize_phone("9991234567"), "+79991234567")
        self.assertEqual(rules.normalize_phone("+375 29 123 45 67"), "+375291234567")
        self.assertIsNone(rules.normalize_phone("12345"))
        self.assertEqual(rules.normalize_bank("  Т-Банк  "), "Т-Банк")
        self.assertIsNone(rules.normalize_bank("x"))


class PartnerCase(unittest.IsolatedAsyncioTestCase):
    PARTNER = 100
    FRIEND = 200

    async def asyncSetUp(self):
        from database.repositories.partner import PartnerRepository
        self.env = await build_env(settings={"partner_hold_days": 14})
        self.repo = PartnerRepository(self.env.session_maker)
        self.notifier = SimpleNamespace(
            accrual=AsyncMock(), reversal=AsyncMock(), released=AsyncMock(),
            withdrawal_created=AsyncMock(return_value=(-100, 77)), withdrawal_resolved=AsyncMock(),
            balance_payment=AsyncMock(),
        )
        self.service = ps.PartnerService(self.repo, self.env.repos.users, self.env.repos.settings,
                                         tariff_repo=self.env.repos.tariffs, payment_service=self.env.payments)
        self.service.notifier = self.notifier
        self.env.payments.partner_service = self.service
        await self.env.make_telegram_client(self.PARTNER)
        await self.service.add_partner(str(self.PARTNER), admin_id=1)

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def make_friend(self, user_id=FRIEND, *, via_partner=True):
        return await self.env.make_telegram_client(user_id, referrer_id=self.PARTNER, partner_referred=via_partner)

    async def tariff(self, name="Месяц"):
        return next(t for t in await self.env.repos.tariffs.get_active() if t.name == name)

    async def pay(self, user_id, amount, *, yk_id, kind="subscription", source="bot", manager_id=None):
        tariff = await self.tariff()
        await self.env.payments.create_payment_record(
            yookassa_payment_id=yk_id, user_id=user_id, tariff_id=tariff.id if kind == "subscription" else None,
            original_amount=amount, final_amount=amount, source=source, kind=kind, manager_id=manager_id,
            days=30 if kind == "custom" else None, extra_devices=1 if kind == "devices" else 0,
        )
        return await self.env.payments.process_successful_payment(yk_id, amount)

    async def balance(self):
        return (await self.repo.get(self.PARTNER)).balance_kop

    async def age_holds(self):
        """Сдвигает холд в прошлое — как будто прошло 14 дней."""
        from sqlalchemy import update
        from db import PartnerLedger
        async with self.env.session_maker() as session:
            await session.execute(update(PartnerLedger).values(
                available_at=datetime.datetime.now() - datetime.timedelta(minutes=1)))
            await session.commit()


class AccrualTests(PartnerCase):
    async def test_friend_payment_goes_to_hold_then_to_balance(self):
        await self.make_friend()
        await self.pay(self.FRIEND, 149, yk_id="yk-1")

        ov = await self.service.overview(self.PARTNER)
        self.assertEqual((ov.balance_kop, ov.hold_kop, ov.earned_kop, ov.paid_friends), (0, 4470, 4470, 1))
        self.notifier.accrual.assert_awaited_once()
        self.assertGreater(ov.next_release, datetime.datetime.now() + datetime.timedelta(days=13))

        self.assertEqual(await self.service.release_holds(), 0)   # холд ещё не прошёл
        await self.age_holds()
        self.assertEqual(await self.service.release_holds(), 1)
        self.assertEqual(await self.balance(), 4470)
        self.notifier.released.assert_awaited_once_with(self.PARTNER, 4470, 4470)
        self.assertEqual(await self.service.release_holds(), 0)   # повторно не переводится

    async def test_every_payment_counts_and_webhook_repeat_does_not(self):
        await self.make_friend()
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        await self.pay(self.FRIEND, 399, yk_id="yk-2", source="auto")
        await self.service.on_payment_succeeded(await self.env.payments.get_payment("yk-2"))   # повтор
        self.assertEqual((await self.service.overview(self.PARTNER)).hold_kop, 4470 + 11970)

    async def test_excluded_payments(self):
        await self.make_friend()
        await self.pay(self.FRIEND, 99, yk_id="yk-dev", kind="devices")
        await self.pay(self.FRIEND, 50, yk_id="yk-traffic", kind="traffic")
        stars = SimpleNamespace(kind="subscription", source="stars", user_id=self.FRIEND,
                                final_amount=100, yookassa_payment_id="stars:1", manager_id=None)
        self.assertIsNone(await self.service.on_payment_succeeded(stars))
        self.assertEqual((await self.service.overview(self.PARTNER)).earned_kop, 0)

    async def test_friends_invited_before_partnership_do_not_count(self):
        await self.make_friend(via_partner=False)
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        self.assertEqual((await self.service.overview(self.PARTNER)).earned_kop, 0)

    async def test_disabled_partner_gets_nothing_but_keeps_balance(self):
        await self.make_friend()
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        await self.age_holds()
        await self.service.release_holds()
        await self.service.disable(self.PARTNER, admin_id=1)

        await self.pay(self.FRIEND, 149, yk_id="yk-2")
        ov = await self.service.overview(self.PARTNER)
        self.assertFalse(ov.active)
        self.assertEqual((ov.balance_kop, ov.hold_kop), (4470, 0))

    async def test_personal_percent_and_global_setting(self):
        await self.make_friend()
        await self.env.repos.settings.set("partner_percent", 10)
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        await self.service.set_percent(self.PARTNER, 50)
        await self.pay(self.FRIEND, 100, yk_id="yk-2")
        self.assertEqual((await self.service.overview(self.PARTNER)).hold_kop, 1490 + 5000)
        with self.assertRaises(ps.PartnerError):
            await self.service.set_percent(self.PARTNER, 0)

    async def test_manager_fee_is_not_commissioned(self):
        from db import ManagerOperation
        await self.make_friend()
        manager = await self.env.make_manager(service_fee=250)
        async with self.env.session_maker() as session:
            session.add(ManagerOperation(manager_id=manager.id, op_type="issue_tariff", status="completed",
                                         client_user_id=self.FRIEND, price=399, service_fee=250,
                                         payment_id="cash:1"))
            await session.commit()
        await self.pay(self.FRIEND, 399, yk_id="cash:1", source="cash", manager_id=manager.id)
        self.assertEqual((await self.service.overview(self.PARTNER)).hold_kop, 4470)   # 30% от 149

    async def test_qr_sale_with_fee_in_cash_counts_the_whole_payment(self):
        """По QR услуга идёт наличными мимо ЮKassa: в оплате только подписка — вычитать нечего."""
        from db import ManagerOperation
        await self.make_friend()
        manager = await self.env.make_manager(service_fee=250)
        async with self.env.session_maker() as session:
            session.add(ManagerOperation(manager_id=manager.id, op_type="issue_tariff", status="pending_payment",
                                         client_user_id=self.FRIEND, price=399, service_fee=250, fee_in_cash=True,
                                         payment_method="online", payment_id="yk-qr"))
            await session.commit()
        await self.pay(self.FRIEND, 149, yk_id="yk-qr", source="manager", manager_id=manager.id)
        self.assertEqual((await self.service.overview(self.PARTNER)).hold_kop, 4470)   # 30% от 149, не от −101

    async def test_zero_hold_goes_straight_to_balance(self):
        await self.env.repos.settings.set("partner_hold_days", 0)
        await self.make_friend()
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        self.assertEqual(await self.balance(), 4470)


class RefundTests(PartnerCase):
    async def test_refund_during_hold_cancels_accrual(self):
        await self.make_friend()
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        await self.env.payments.process_refund("yk-1")
        ov = await self.service.overview(self.PARTNER)
        self.assertEqual((ov.hold_kop, ov.earned_kop, ov.balance_kop), (0, 0, 0))
        await self.age_holds()
        self.assertEqual(await self.service.release_holds(), 0)

    async def test_refund_after_hold_never_makes_balance_negative(self):
        await self.make_friend()
        await self.pay(self.FRIEND, 149, yk_id="yk-1")
        await self.age_holds()
        await self.service.release_holds()
        self.assertTrue(await self.repo.debit(self.PARTNER, 4000, "spend", payment_id="x"))   # потратил почти всё

        await self.env.payments.process_refund("yk-1")
        self.assertEqual(await self.balance(), 0)
        outcome = self.notifier.reversal.await_args.args
        self.assertEqual(outcome[1:3], ("reversed", 470))
        self.assertIsNone(await self.service.on_payment_refunded(await self.env.payments.get_payment("yk-1")))


class WithdrawalTests(PartnerCase):
    async def fund(self, kop: int):
        await self.repo.credit(self.PARTNER, kop, "spend_return", payment_id=f"seed-{kop}")

    async def test_full_cycle_paid(self):
        await self.fund(80000)
        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.request_withdrawal(self.PARTNER, 60000)
        self.assertEqual(ctx.exception.code, "no_requisites")
        await self.service.set_requisites(self.PARTNER, "8 999 123 45 67", "Т-Банк")

        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.request_withdrawal(self.PARTNER, 40000)
        self.assertEqual(ctx.exception.code, "below_min")

        w = await self.service.request_withdrawal(self.PARTNER, 60000)
        self.assertEqual((w.sbp_phone, w.status), ("+79991234567", "pending"))
        self.assertEqual(await self.balance(), 20000)
        self.assertEqual((await self.repo.get_withdrawal(w.id)).notify_message_id, 77)

        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.request_withdrawal(self.PARTNER, 60000)
        self.assertEqual(ctx.exception.code, "pending_exists")

        done = await self.service.resolve_withdrawal(w.id, admin_id=1, paid=True)
        self.assertEqual(done.status, "paid")
        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.resolve_withdrawal(w.id, admin_id=2, paid=False)
        self.assertEqual(ctx.exception.code, "already_resolved")
        ov = await self.service.overview(self.PARTNER)
        self.assertEqual((ov.balance_kop, ov.withdrawn_kop), (20000, 60000))

    async def test_rejected_returns_money(self):
        await self.fund(60000)
        await self.service.set_requisites(self.PARTNER, "+79991234567", "Сбер")
        w = await self.service.request_withdrawal(self.PARTNER, 60000)
        await self.service.resolve_withdrawal(w.id, admin_id=1, paid=False, reason="Неверные реквизиты")
        self.assertEqual(await self.balance(), 60000)
        self.assertEqual((await self.repo.get_withdrawal(w.id)).reject_reason, "Неверные реквизиты")

    async def test_cannot_withdraw_more_than_balance(self):
        await self.fund(50000)
        await self.service.set_requisites(self.PARTNER, "+79991234567", "Сбер")
        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.request_withdrawal(self.PARTNER, 50001)
        self.assertEqual(ctx.exception.code, "insufficient")
        self.assertEqual(await self.balance(), 50000)


class BalancePaymentTests(PartnerCase):
    async def test_pay_subscription_with_balance(self):
        await self.repo.credit(self.PARTNER, 20000, "spend_return", payment_id="seed")
        tariff = await self.tariff()
        offers = {o.tariff.name: o for o in await self.service.balance_offers(self.PARTNER)}
        self.assertTrue(offers["Месяц"].affordable)
        self.assertFalse(offers["3 месяца"].affordable)

        result, replayed = await self.service.pay_with_balance(self.PARTNER, tariff.id, "n1")
        self.assertFalse(replayed)
        self.assertIsNotNone(result)
        self.assertEqual(await self.balance(), 20000 - 14900)
        payment = await self.env.payments.get_payment(f"balance:{self.PARTNER}:n1")
        self.assertEqual((payment.status, payment.source), ("succeeded", "balance"))
        user = await self.env.repos.users.get(self.PARTNER)
        self.assertGreater(user.subscription_end_date, datetime.datetime.now() + datetime.timedelta(days=29))

        _, replayed = await self.service.pay_with_balance(self.PARTNER, tariff.id, "n1")   # двойное нажатие
        self.assertTrue(replayed)
        self.assertEqual(await self.balance(), 20000 - 14900)

    async def test_not_enough_balance(self):
        await self.repo.credit(self.PARTNER, 1000, "spend_return", payment_id="seed")
        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.pay_with_balance(self.PARTNER, (await self.tariff()).id, "n1")
        self.assertEqual(ctx.exception.code, "insufficient")
        self.assertEqual(await self.balance(), 1000)

    async def test_failed_processing_returns_money(self):
        await self.repo.credit(self.PARTNER, 20000, "spend_return", payment_id="seed")
        self.env.payments.process_successful_payment = AsyncMock(side_effect=RuntimeError("panel down"))
        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.pay_with_balance(self.PARTNER, (await self.tariff()).id, "n1")
        self.assertEqual(ctx.exception.code, "payment_failed")
        self.assertEqual(await self.balance(), 20000)
        self.assertEqual((await self.env.payments.get_payment(f"balance:{self.PARTNER}:n1")).status, "failed")

    async def test_balance_payment_of_a_friend_partner_is_not_commissioned(self):
        """Друг партнёра сам стал партнёром и платит с баланса — первому партнёру ничего (живых денег нет)."""
        await self.make_friend()
        await self.service.add_partner(str(self.FRIEND), admin_id=1)
        await self.repo.credit(self.FRIEND, 20000, "spend_return", payment_id="seed-f")
        await self.service.pay_with_balance(self.FRIEND, (await self.tariff()).id, "n1")
        self.assertEqual((await self.service.overview(self.PARTNER)).earned_kop, 0)


class AdminTests(PartnerCase):
    async def test_add_by_username_and_reject_unknown(self):
        await self.env.make_telegram_client(300)
        from sqlalchemy import update
        from db import User
        async with self.env.session_maker() as session:
            await session.execute(update(User).where(User.user_id == 300).values(username="vasya"))
            await session.commit()
        partner, user = await self.service.add_partner("@vasya", admin_id=1)
        self.assertEqual((partner.user_id, partner.status), (300, "active"))
        with self.assertRaises(ps.PartnerError) as ctx:
            await self.service.add_partner("999999", admin_id=1)
        self.assertEqual(ctx.exception.code, "user_not_found")

    async def test_reenable_keeps_balance(self):
        await self.repo.credit(self.PARTNER, 5000, "spend_return", payment_id="seed")
        await self.service.disable(self.PARTNER, 1)
        await self.service.enable(self.PARTNER, 1)
        partner = await self.repo.get(self.PARTNER)
        self.assertEqual((partner.status, partner.balance_kop), ("active", 5000))


class ReferralIntegrationTests(PartnerCase):
    """Старая рефералка не платит партнёру днями, а лидерборд его не видит."""

    def referral_service(self):
        module = _load("referral_service_under_test", ROOT / "tgbot" / "services" / "referral_service.py", {
            "database.repositories.user": SimpleNamespace(UserRepository=object),
            "tgbot.services.subscription_service": SimpleNamespace(SubscriptionService=object),
            "loader": SimpleNamespace(logger=Mock()),
        })
        self.extend = AsyncMock()
        return module.ReferralService(self.env.repos.users, SimpleNamespace(extend=self.extend),
                                      partner_repo=self.repo)

    async def test_friend_of_partner_gets_trial_partner_gets_no_days(self):
        await self.env.make_telegram_client(self.FRIEND)
        svc = self.referral_service()
        await svc.activate_new_user_referral(self.FRIEND, self.PARTNER, 3)
        friend = await self.env.repos.users.get(self.FRIEND)
        self.assertEqual((friend.referrer_id, friend.partner_referred), (self.PARTNER, True))
        self.extend.assert_awaited_once_with(self.FRIEND, 3)    # только триал другу
        self.assertIsNone(await svc.process_first_payment_bonus(self.FRIEND))

    async def test_regular_referrer_still_gets_days(self):
        await self.env.make_telegram_client(300)
        await self.env.make_telegram_client(self.FRIEND)
        svc = self.referral_service()
        await svc.activate_new_user_referral(self.FRIEND, 300, 3)
        self.assertFalse((await self.env.repos.users.get(self.FRIEND)).partner_referred)
        self.assertEqual(self.extend.await_count, 2)            # триал другу + бонус рефереру

    async def test_attach_at_start_survives_until_activation(self):
        # /start по ссылке → реферер сразу в БД; триал выдаётся позже без аргумента
        # (раньше реферер жил в FSM и пропадал при перезапуске бота).
        await self.env.make_telegram_client(self.FRIEND)
        svc = self.referral_service()
        self.assertTrue(await svc.attach_referrer(self.FRIEND, self.PARTNER))
        friend = await self.env.repos.users.get(self.FRIEND)
        self.assertEqual((friend.referrer_id, friend.partner_referred), (self.PARTNER, True))
        await svc.activate_new_user_referral(self.FRIEND, None, 3)
        self.extend.assert_awaited_once_with(self.FRIEND, 3)
        self.assertTrue((await self.env.repos.users.get(self.FRIEND)).has_received_trial)

    async def test_attach_rejects_active_or_already_referred_users(self):
        await self.env.make_telegram_client(300)
        await self.env.make_telegram_client(201, has_received_trial=True)
        await self.env.make_telegram_client(202, subscription_end_date=datetime.datetime.now())
        await self.env.make_telegram_client(203, referrer_id=300)
        svc = self.referral_service()
        for uid in (201, 202, 203):
            self.assertFalse(await svc.attach_referrer(uid, self.PARTNER), uid)
        self.assertFalse(await svc.attach_referrer(300, 300))           # сам себя
        self.assertFalse(await svc.attach_referrer(300, 999))           # реферера нет
        self.assertEqual((await self.env.repos.users.get(203)).referrer_id, 300)

    async def test_repeated_activation_gives_nothing(self):
        await self.env.make_telegram_client(300)
        await self.env.make_telegram_client(self.FRIEND)
        svc = self.referral_service()
        await svc.activate_new_user_referral(self.FRIEND, 300, 3)
        await svc.activate_new_user_referral(self.FRIEND, 300, 3)
        self.assertEqual(self.extend.await_count, 2)            # не 4

    async def test_leaderboard_skips_active_partners(self):
        from database.repositories.stats import StatsRepository
        stats = StatsRepository(self.env.session_maker)
        await self.make_friend()
        await self.env.make_telegram_client(300)
        await self.env.make_telegram_client(301, referrer_id=300)
        now = datetime.datetime.now()
        board = await stats.get_monthly_referral_leaderboard(now.year, now.month)
        self.assertEqual([r for r, _ in board], [300])


if __name__ == "__main__":
    unittest.main()
