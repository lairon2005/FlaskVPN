"""
Деньги в расширенной статистике админа на настоящей БД (SQLite):

- «Доход» — без продаж менеджеров (QR и наличные), Stars и оплат с партнёрского баланса;
- «Продажи менеджеров» — отдельно, деньги магазина без услуги менеджера: у наличных и у
  старых QR-продаж услуга сидит внутри оплаты, у новых QR её берут наличными мимо ЮKassa;
- «Долг партнёрам» — балансы + начисления в холде + невыплаченные заявки на вывод.
"""
import datetime
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from manager_env import build_env
from test_partner import ps

ROOT = Path(__file__).resolve().parents[1]


def load_admin_stats_module():
    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    spec = importlib.util.spec_from_file_location(
        "admin_stats_under_test", ROOT / "tgbot" / "services" / "admin_stats_service.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"tgbot": tgbot, "tgbot.services": services}):
        spec.loader.exec_module(module)
    return module


class MoneyStatsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from database.repositories.partner import PartnerRepository
        self.env = await build_env(settings={"partner_hold_days": 14})
        self.svc = self.env.service
        self.payments = self.env.repos.payments
        self.partner_repo = PartnerRepository(self.env.session_maker)

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def pay(self, yk_id, amount, *, source="bot", user_id=900, kind="subscription"):
        await self.env.make_telegram_client(user_id)
        tariff = (await self.env.repos.tariffs.get_active())[1]
        await self.env.payments.create_payment_record(
            yookassa_payment_id=yk_id, user_id=user_id, tariff_id=tariff.id, original_amount=amount,
            final_amount=amount, source=source, kind=kind,
        )
        await self.env.payments.process_successful_payment(yk_id, amount)

    async def manager_sales(self):
        """Три продажи «Месяца» (149 ₽) с услугой 250 ₽: наличные, QR сейчас, QR до перехода."""
        manager = await self.env.make_manager(service_fee=250, can_accept_cash=True)
        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="c1")

        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="q1")
        await self.env.payments.process_successful_payment("yk-1", 149.0)      # по QR — только подписка
        await self.svc.on_payment_succeeded("yk-1")

        # Старая QR-продажа: услуга шла через ЮKassa, в оплате 399 ₽
        legacy = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online",
                                      idempotency_nonce="q2")
        await self.env.repos.ops.update(legacy.operation_id, fee_in_cash=False)
        from sqlalchemy import update
        from db import Payment
        async with self.env.session_maker() as session:
            await session.execute(update(Payment).where(Payment.yookassa_payment_id == "yk-2")
                                  .values(original_amount=399.0, final_amount=399.0))
            await session.commit()
        await self.env.payments.process_successful_payment("yk-2", 399.0)
        await self.svc.on_payment_succeeded("yk-2")
        return manager


class RevenueSplitTests(MoneyStatsCase):
    async def test_revenue_excludes_manager_sales(self):
        await self.pay("bot-1", 149)
        await self.pay("web-1", 399, source="web", user_id=901)
        await self.pay("stars-1", 100, source="stars", user_id=902)
        await self.pay("bal-1", 149, source="balance", user_id=903)
        await self.manager_sales()

        total = await self.payments.get_total_revenue()
        self.assertEqual((total["count"], total["revenue"]), (2, 548.0))
        for kind in ("day", "week", "month", "year"):
            period = await self.payments.get_revenue_for_period(kind)
            self.assertEqual((period["count"], period["revenue"]), (2, 548.0), kind)

    async def test_manager_sales_are_shop_money_without_the_fee(self):
        await self.pay("bot-1", 149)
        await self.manager_sales()

        sales = await self.payments.get_manager_sales()
        # три продажи по 149 ₽ подписки; услуги (3 × 250) — деньги менеджера
        self.assertEqual(sales["count"], 3)
        self.assertEqual(sales["revenue"], 447.0)
        self.assertEqual((sales["qr"], sales["cash"]), (298.0, 149.0))
        self.assertEqual(sales["fees"], 750.0)
        self.assertEqual(await self.payments.get_manager_sales("day"), sales)

    async def test_failed_and_refunded_manager_sales_do_not_count(self):
        manager = await self.env.make_manager(service_fee=250)
        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="q1")
        self.assertEqual((await self.payments.get_manager_sales())["count"], 0)    # не оплачено
        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.env.payments.process_refund("yk-1")
        self.assertEqual((await self.payments.get_manager_sales())["revenue"], 0.0)

    async def test_old_sales_outside_the_period(self):
        await self.manager_sales()
        from sqlalchemy import update
        from db import Payment
        async with self.env.session_maker() as session:
            await session.execute(update(Payment).values(completed_at=datetime.datetime(2020, 1, 1)))
            await session.commit()
        self.assertEqual((await self.payments.get_manager_sales("year"))["count"], 0)
        self.assertEqual((await self.payments.get_manager_sales())["count"], 3)


class PartnerDebtTests(MoneyStatsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.partners = ps.PartnerService(self.partner_repo, self.env.repos.users, self.env.repos.settings,
                                          tariff_repo=self.env.repos.tariffs, payment_service=self.env.payments)
        self.env.payments.partner_service = self.partners

    async def partner_with_friend(self, partner_id, friend_id):
        await self.env.make_telegram_client(partner_id)
        await self.partners.add_partner(str(partner_id), admin_id=1)
        await self.env.make_telegram_client(friend_id, referrer_id=partner_id, partner_referred=True)

    async def release(self):
        from sqlalchemy import update
        from db import PartnerLedger
        async with self.env.session_maker() as session:
            await session.execute(update(PartnerLedger).values(
                available_at=datetime.datetime.now() - datetime.timedelta(minutes=1)))
            await session.commit()
        await self.partners.release_holds()

    async def test_empty(self):
        debt = await self.partner_repo.debt_summary()
        self.assertEqual((debt["total_kop"], debt["balance_kop"], debt["hold_kop"], debt["pending_kop"]), (0, 0, 0, 0))

    async def test_balance_hold_and_pending_withdrawals(self):
        await self.partner_with_friend(100, 200)
        await self.partner_with_friend(101, 201)
        await self.pay("p-1", 1000, user_id=200)            # 300 ₽ партнёру 100
        await self.pay("p-2", 2000, user_id=201)            # 600 ₽ партнёру 101
        await self.release()
        await self.pay("p-3", 500, user_id=200)             # ещё 150 ₽ партнёру 100 — в холде

        await self.partners.set_requisites(101, "+79991234567", "Т-Банк")
        await self.partners.request_withdrawal(101, 50000)   # 500 ₽ из 600 — заявка, с баланса списано

        debt = await self.partner_repo.debt_summary()
        self.assertEqual(debt["balance_kop"], 30000 + 10000)
        self.assertEqual(debt["hold_kop"], 15000)
        self.assertEqual((debt["pending_kop"], debt["pending_count"]), (50000, 1))
        self.assertEqual(debt["total_kop"], 30000 + 10000 + 15000 + 50000)
        self.assertEqual(debt["partners_with_balance"], 2)

    async def test_paid_withdrawal_and_spending_reduce_the_debt(self):
        await self.partner_with_friend(100, 200)
        await self.pay("p-1", 3000, user_id=200)            # 900 ₽
        await self.release()
        await self.partners.set_requisites(100, "+79991234567", "Т-Банк")
        withdrawal = await self.partners.request_withdrawal(100, 50000)
        await self.partners.resolve_withdrawal(withdrawal.id, admin_id=1, paid=True)
        await self.partners.pay_with_balance(100, 2, "n1")   # «Месяц» за 149 ₽ с баланса

        debt = await self.partner_repo.debt_summary()
        self.assertEqual((debt["pending_kop"], debt["hold_kop"]), (0, 0))
        self.assertEqual(debt["total_kop"], 90000 - 50000 - 14900)

    async def test_rejected_withdrawal_returns_to_the_balance(self):
        await self.partner_with_friend(100, 200)
        await self.pay("p-1", 3000, user_id=200)
        await self.release()
        await self.partners.set_requisites(100, "+79991234567", "Т-Банк")
        withdrawal = await self.partners.request_withdrawal(100, 50000)
        await self.partners.resolve_withdrawal(withdrawal.id, admin_id=1, paid=False, reason="нет")
        debt = await self.partner_repo.debt_summary()
        self.assertEqual((debt["total_kop"], debt["balance_kop"], debt["pending_kop"]), (90000, 90000, 0))


class DashboardTests(MoneyStatsCase):
    async def test_dashboard_has_manager_sales_and_partner_debt(self):
        module = load_admin_stats_module()
        stats_repo = SimpleNamespace(
            count_all_users=AsyncMock(return_value=0), count_active_subscriptions=AsyncMock(return_value=0),
            count_users_with_first_payment=AsyncMock(return_value=0), get_intro_funnel=AsyncMock(return_value={}),
            count_new_users_for_period=AsyncMock(return_value=0),
        )
        panel = SimpleNamespace(get_system_stats=AsyncMock(return_value={"online_now": 0}),
                                get_nodes=AsyncMock(return_value=[]), get_all_users=AsyncMock(return_value=[]))
        await self.pay("bot-1", 149)
        await self.manager_sales()
        service = module.AdminStatsService(stats_repo, panel, self.payments, partner_repo=self.partner_repo)

        stats = await service.get_dashboard_stats()
        self.assertEqual(stats["revenue_total"]["revenue"], 149.0)
        self.assertEqual(set(stats["manager_sales"]), {"today", "week", "month", "year", "total"})
        self.assertEqual(stats["manager_sales"]["total"]["revenue"], 447.0)
        self.assertEqual(stats["partner_debt"]["total_kop"], 0)

        without_partners = module.AdminStatsService(stats_repo, panel, self.payments)
        self.assertIsNone((await without_partners.get_dashboard_stats())["partner_debt"])


if __name__ == "__main__":
    unittest.main()


class StatsScreenTests(unittest.IsolatedAsyncioTestCase):
    """Экран «📊 Расширенная статистика» в боте: блоки дохода, менеджеров и долга партнёрам."""

    def load_handler(self, stats: dict):
        from real_traffic_pricing import _load
        tgbot = types.ModuleType("tgbot")
        tgbot.__path__ = []
        filters = types.ModuleType("tgbot.filters.admin")
        filters.IsAdmin = lambda: (lambda *_: True)
        keyboards = types.ModuleType("tgbot.keyboards.inline")
        keyboards.admin_main_menu_keyboard = lambda: None
        services = types.ModuleType("tgbot.services")
        services.__path__ = []
        services.admin_stats_service = SimpleNamespace(get_dashboard_stats=AsyncMock(return_value=stats))
        stubs = {
            "tgbot": tgbot, "tgbot.filters": types.ModuleType("tgbot.filters"), "tgbot.filters.admin": filters,
            "tgbot.keyboards": types.ModuleType("tgbot.keyboards"), "tgbot.keyboards.inline": keyboards,
            "tgbot.services": services, "loader": SimpleNamespace(logger=SimpleNamespace(debug=print, error=print)),
            "tgbot.services.partner_rules": _load("partner_rules_for_stats",
                                                  ROOT / "tgbot" / "services" / "partner_rules.py"),
        }
        with patch.dict(sys.modules, stubs):
            return _load("admin_main_under_test", ROOT / "tgbot" / "handlers" / "admin" / "main.py")

    @staticmethod
    def stats(partner_debt):
        period = {"count": 2, "revenue": 548.0}
        sales = {"count": 3, "revenue": 447.0, "qr": 298.0, "cash": 149.0, "fees": 750.0}
        return {
            "total_users": 1, "active_subs": 1, "first_payments": 1, "users_today": 0, "users_week": 0,
            "users_month": 0, "nodes": [], "online_now": 0,
            "intro_funnel": {"bought": 0, "converted": 0, "on_trial": 0, "dropped": 0},
            **{f"revenue_{k}": period for k in ("today", "week", "month", "year", "total")},
            "stars_revenue_total": {"stars": 0, "count": 0},
            "manager_sales": {k: sales for k in ("today", "week", "month", "year", "total")},
            "partner_debt": partner_debt,
        }

    async def render(self, partner_debt):
        module = self.load_handler(self.stats(partner_debt))
        message = SimpleNamespace(edit_text=AsyncMock())
        call = SimpleNamespace(answer=AsyncMock(), message=message, from_user=SimpleNamespace(id=1))
        await module.admin_stats_handler(call)
        return message.edit_text.await_args.args[0]

    async def test_text(self):
        text = await self.render({"total_kop": 115000, "balance_kop": 40000, "hold_kop": 15000,
                                  "pending_kop": 60000, "pending_count": 2, "partners_with_balance": 2})
        self.assertIn("Доход (календарь, МСК, без продаж менеджеров)", text)
        self.assertIn("└ Всего: <b>548.00 ₽</b> (2)", text)
        self.assertIn("Продажи менеджеров (в доход выше не входят)", text)
        self.assertIn("├ Сегодня: <b>447.00 ₽</b> (3) · QR 298 ₽ · нал. 149 ₽", text)
        self.assertIn("Услуги менеджеров (их деньги, не входят): 750 ₽", text)
        self.assertIn("<b>Долг партнёрам: 1 150 ₽</b>", text)
        self.assertIn("├ В холде: 150 ₽", text)
        self.assertIn("└ Заявки на вывод: 600 ₽ (2)", text)

    async def test_without_partner_repo_there_is_no_debt_block(self):
        self.assertNotIn("Долг партнёрам", await self.render(None))
