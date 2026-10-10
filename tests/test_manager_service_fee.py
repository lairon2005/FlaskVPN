"""
Услуга менеджера (подключение и настройка) и чек картинкой.

Деньги: услуга входит в сумму продажи отдельной строкой, автопродление её не списывает;
наличные — услуга остаётся у менеджера и не входит в «к сдаче»; по QR через ЮKassa идёт
только подписка, услугу клиент отдаёт менеджеру наличными (в нашем чеке она есть, в счёте
и фискальном чеке ЮKassa — нет). «К выплате менеджеру» копится только по старым онлайн-продажам,
где услуга шла через ЮKassa; возврат её снимает.
Чек: каждому адресату — картинка и текст одним сообщением, в группе нет QR и пометки.
"""
import importlib.util
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin  # noqa: F401  до patch.dict: иначе он выгрузит плагины Pillow

from manager_env import build_env

ROOT = Path(__file__).resolve().parents[1]


class FeeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await build_env()
        self.svc = self.env.service
        self.E = self.env.module.ManagerError

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def manager(self, fee=250, **rights):
        return await self.env.make_manager(service_fee=fee, **rights)

    async def sell(self, manager, method="cash", nonce="n1", **kwargs):
        kwargs.setdefault("product", "tariff")
        kwargs.setdefault("tariff_id", 2)
        return await self.svc.issue(manager.id, method=method, idempotency_nonce=nonce, **kwargs)

    async def pay(self, n=1, amount=149.0, card=None):
        """Оплата QR-счёта: по QR приходит только подписка (149 ₽), услуга — наличными менеджеру."""
        await self.env.payments.process_successful_payment(f"yk-{n}", amount, payment_method=card)
        await self.svc.on_payment_succeeded(f"yk-{n}")


class SettingTests(FeeCase):
    async def test_new_manager_sells_with_250_by_default(self):
        manager = await self.env.make_manager(service_fee=None)
        self.assertEqual((await self.svc.view(manager.id)).service_fee, self.env.module.SERVICE_FEE_DEFAULT)
        self.assertEqual(self.env.module.SERVICE_FEE_DEFAULT, 250)
        quote = await self.svc.quote(manager.id, product="tariff", tariff_id=2)
        self.assertEqual((quote.service_fee, quote.total), (250.0, 399.0))

    async def test_manager_sets_his_own_price(self):
        manager = await self.manager()
        view = await self.svc.set_service_fee(manager.id, 300)
        self.assertEqual(view.service_fee, 300)
        quote = await self.svc.quote(manager.id, product="tariff", tariff_id=2)
        self.assertEqual(quote.total, 449.0)

    async def test_bounds(self):
        manager = await self.manager()
        for bad in (-1, 1001, 99.5, "250", True):
            with self.assertRaises(self.E) as ctx:
                await self.svc.set_service_fee(manager.id, bad)
            self.assertEqual(ctx.exception.code, "bad_fee", bad)
        self.assertEqual((await self.svc.set_service_fee(manager.id, 0)).service_fee, 0)
        self.assertEqual((await self.svc.set_service_fee(manager.id, 1000)).service_fee, 1000)

    async def test_admin_can_lock_the_price(self):
        manager = await self.manager()
        await self.svc.admin_set_service_fee(manager.id, admin_id=1, fee=200, locked=True)
        with self.assertRaises(self.E) as ctx:
            await self.svc.set_service_fee(manager.id, 500)
        self.assertEqual(ctx.exception.code, "fee_locked")
        view = await self.svc.view(manager.id)
        self.assertEqual((view.service_fee, view.service_fee_locked), (200, True))
        await self.svc.admin_set_service_fee(manager.id, admin_id=1, locked=False)
        self.assertEqual((await self.svc.set_service_fee(manager.id, 500)).service_fee, 500)

    async def test_price_change_does_not_log_the_manager_out(self):
        manager = await self.manager()
        before = await self.svc.session_version(manager.id)
        await self.svc.set_service_fee(manager.id, 300)
        await self.svc.admin_set_service_fee(manager.id, admin_id=1, fee=200, locked=True)
        self.assertEqual(await self.svc.session_version(manager.id), before)

    async def test_price_change_between_preview_and_confirm_is_caught(self):
        manager = await self.manager(can_accept_cash=True)
        quote = await self.svc.quote(manager.id, product="tariff", tariff_id=2)
        await self.svc.set_service_fee(manager.id, 300)
        with self.assertRaises(self.E) as ctx:
            await self.sell(manager, expected_total=quote.total)
        self.assertEqual(ctx.exception.code, "price_changed")


class CashTests(FeeCase):
    async def test_fee_is_in_the_sale_but_not_in_cash_to_hand_over(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.sell(manager)

        self.assertEqual(result.price, 399.0)
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual((op.price, op.service_fee), (399.0, 250.0))
        payment = await self.env.repos.payments.get_by_yookassa_id(op.payment_id)
        self.assertEqual(payment.final_amount, 399.0)               # клиент заплатил всё
        self.assertEqual(await self.svc.cash_outstanding(manager.id), (149.0, 1))   # сдать — только тариф
        self.assertEqual((await self.svc.fees_outstanding(manager.id))[0], 0)      # наличную услугу не выплачивают

        settlement = await self.svc.settle(manager.id, admin_id=9)
        self.assertEqual(settlement.amount, 149.0)

    async def test_cash_limit_counts_only_the_shop_money(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=149)
        await self.sell(manager)                                    # 399 на руках, но сдать 149 — в лимите
        with self.assertRaises(self.E) as ctx:
            await self.sell(manager, nonce="n2")
        self.assertEqual(ctx.exception.code, "cash_limit")

    async def test_receipts_show_the_breakdown(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.sell(manager)
        for text in (result.receipt_text, self.env.notifier.group[-1][1]):
            self.assertIn("Подписка: 149 ₽", text)
            self.assertIn("Подключение и настройка VPN: 250 ₽", text)
            self.assertIn("Итого: 399 ₽", text)

    async def test_temp_key_has_no_fee(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual((op.price, op.service_fee), (0, 0))

    async def test_custom_days_and_conversion_carry_the_fee(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.sell(manager, product="custom", tariff_id=None, days=45)
        self.assertEqual(result.price, 279.0 + 250)
        temp = await self.svc.issue_temp(manager.id, "t1")
        converted = await self.sell(manager, nonce="n2", temp_key_id=temp.key_id)
        self.assertEqual(converted.price, 399.0)


class OnlineTests(FeeCase):
    async def test_qr_bills_only_the_subscription(self):
        manager = await self.manager()
        result = await self.sell(manager, method="online")
        kwargs = self.env.created_payments[0]
        self.assertEqual(kwargs["amount"], 149.0)                  # услуга в счёт ЮKassa не входит
        items = kwargs["items"]
        self.assertEqual(sum(i["amount"] * i["quantity"] for i in items), 149.0)
        self.assertNotIn("Подключение и настройка VPN", [i["description"] for i in items])
        self.assertIn("Месяц", items[0]["description"])
        # менеджеру — счёт на подписку и напоминание взять услугу наличными
        self.assertEqual((result.price, result.fee_cash), (149.0, 250.0))
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual((op.price, op.service_fee, op.fee_in_cash), (399.0, 250.0, True))
        payment = await self.env.repos.payments.get_by_yookassa_id(op.payment_id)
        self.assertEqual((payment.original_amount, payment.final_amount), (149.0, 149.0))
        self.assertIn("Услуга менеджера при продлении не списывается",
                      (await self.svc.quote(manager.id, product="tariff", tariff_id=2)).renew_text)

    async def test_no_fee_no_cash_part(self):
        manager = await self.manager(fee=0)
        result = await self.sell(manager, method="online")
        self.assertEqual(len(self.env.created_payments[0]["items"]), 1)
        self.assertEqual((result.price, result.fee_cash), (149.0, 0.0))

    async def test_payment_of_the_subscription_completes_the_sale(self):
        manager = await self.manager()
        result = await self.sell(manager, method="online")
        await self.pay()
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.status, "completed")
        payment = await self.env.repos.payments.get_by_yookassa_id(op.payment_id)
        self.assertEqual(payment.status, "succeeded")

    async def test_autorenew_charges_only_the_tariff(self):
        manager = await self.manager()
        result = await self.sell(manager, method="online")
        await self.pay(card=SimpleNamespace(id="pm-1", saved=True))
        op = await self.env.repos.ops.get(result.operation_id)
        # карта сохранена с тарифом продления — автосписание считает цену по нему, без услуги
        renew_tariff_id = self.env.cards.save_from_yookassa.await_args.kwargs["renew_tariff_id"]
        tariff = await self.env.repos.tariffs.get_by_id(renew_tariff_id)
        self.assertEqual(tariff.price, 149)
        self.assertEqual(op.service_fee, 250.0)

    async def test_cash_fee_of_qr_sale_is_not_owed_to_the_manager(self):
        manager = await self.manager()
        await self.sell(manager, method="online")
        await self.pay()
        # услугу менеджер уже взял наличными — выплачивать нечего, в «к сдаче» её тоже нет
        self.assertEqual(await self.svc.fees_outstanding(manager.id), (0.0, 0))
        self.assertEqual(await self.svc.cash_outstanding(manager.id), (0.0, 0))
        self.assertIsNone(await self.svc.payout_fees(manager.id, admin_id=9))
        stats = await self.svc.stats(manager.id)
        self.assertEqual((stats.today.revenue, stats.today.fees, stats.fees_outstanding), (399.0, 250.0, 0.0))

    async def test_old_qr_fees_still_wait_for_payout(self):
        """Продажи до перехода: услуга пришла магазину через ЮKassa (fee_in_cash=false) — её выплачивают."""
        manager = await self.manager()
        result = await self.sell(manager, method="online")
        await self.env.repos.ops.update(result.operation_id, fee_in_cash=False)
        self.assertEqual(await self.svc.fees_outstanding(manager.id), (0.0, 0))   # не оплачено — не долг
        await self.pay()
        self.assertEqual(await self.svc.fees_outstanding(manager.id), (250.0, 1))

        payout = await self.svc.payout_fees(manager.id, admin_id=9)
        self.assertEqual((payout.amount, payout.operations_count), (250.0, 1))
        self.assertEqual(await self.svc.fees_outstanding(manager.id), (0.0, 0))
        self.assertIsNone(await self.svc.payout_fees(manager.id, admin_id=9))
        self.assertIn("выплатил вам 250 ₽", self.env.notifier.manager_texts[-1][1])

    async def test_receipt_shows_the_fee_paid_in_cash(self):
        manager = await self.manager()
        result = await self.sell(manager, method="online")
        await self.pay()
        text = self.env.notifier.group[-1][1]
        self.assertIn("Подписка: 149 ₽", text)
        self.assertIn("Подключение VPN (наличными): 250 ₽", text)
        self.assertIn("Итого: 399 ₽", text)
        self.assertIn("услуга — наличными менеджеру", text)
        image = await self.svc.receipt_image(manager.id, result.operation_id)
        self.assertTrue(image.startswith(b"\x89PNG"))

    async def test_refund_takes_the_fee_out(self):
        manager = await self.manager()
        result = await self.sell(manager, method="online")
        await self.pay()
        await self.env.payments.process_refund("yk-1")
        await self.svc.on_payment_refunded("yk-1")

        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.status, "refunded")
        self.assertEqual(await self.svc.fees_outstanding(manager.id), (0.0, 0))
        self.assertEqual((await self.svc.stats(manager.id)).today.fees, 0)
        self.assertIn("возвращена", self.env.notifier.group[-1][1])
        await self.svc.on_payment_refunded("yk-1")                 # повтор вебхука — без второго чека
        self.assertEqual(sum(1 for op_id, _ in self.env.notifier.group if op_id == result.operation_id), 3)


class DeliveryTests(FeeCase):
    """Чек каждому адресату — картинка и текст одним сообщением."""

    async def test_every_receipt_carries_an_image(self):
        manager = await self.manager()
        client = await self.env.make_telegram_client(555)
        await self.svc.add_client_by_code(manager.id, await self.svc.create_access_code(client.user_id))
        await self.sell(manager, method="online", client_code=client.client_code or
                        (await self.env.repos.users.get(555)).client_code)
        await self.pay()

        notifier = self.env.notifier
        self.assertTrue(all(image and image.startswith(b"\x89PNG") for _, image in notifier.group_images))
        self.assertTrue(notifier.manager_msgs[-1]["image"].startswith(b"\x89PNG"))
        self.assertTrue(notifier.manager_msgs[-1]["has_key"])
        self.assertEqual(notifier.client_images[-1][0], 555)
        self.assertTrue(notifier.client_images[-1][1].startswith(b"\x89PNG"))
        self.assertIn("Подключение VPN (наличными): 250 ₽", notifier.client_msgs[-1][1])

    async def test_cash_result_carries_the_image_for_the_handler(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.sell(manager)
        self.assertTrue(result.receipt_image.startswith(b"\x89PNG"))
        temp = await self.svc.issue_temp(manager.id, "t1")
        self.assertTrue(temp.receipt_image.startswith(b"\x89PNG"))

    async def test_broken_renderer_still_delivers_the_text(self):
        manager = await self.manager(can_accept_cash=True)
        with patch.object(self.env.module, "render_receipt", side_effect=RuntimeError("no fonts")):
            result = await self.sell(manager)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.receipt_image)
        self.assertIn("Итого", self.env.notifier.group[-1][1])

    async def test_site_receipt_is_only_for_its_manager(self):
        a = await self.env.make_manager(telegram_id=1, can_accept_cash=True)
        b = await self.env.make_manager(telegram_id=2)
        result = await self.sell(a)
        self.assertTrue((await self.svc.receipt_image(a.id, result.operation_id)).startswith(b"\x89PNG"))
        self.assertIsNone(await self.svc.receipt_image(b.id, result.operation_id))
        self.assertIsNone(await self.svc.receipt_image(a.id, 9999))

    async def test_client_sees_only_his_receipts(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.sell(manager)
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertTrue((await self.svc.client_receipt_image(op.client_user_id, op.id)).startswith(b"\x89PNG"))
        self.assertIsNone(await self.svc.client_receipt_image(op.client_user_id + 1, op.id))
        receipts = await self.svc.client_receipts(op.client_user_id)
        self.assertEqual([r.id for r in receipts], [op.id])


# =============================================================================
# Картинка и подпись — чистые функции
# =============================================================================

def _pure(name):
    spec = importlib.util.spec_from_file_location(f"tgbot.services.{name}", ROOT / "tgbot" / "services" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mr = _pure("manager_receipts")
with patch.dict(sys.modules, {"tgbot.services.manager_receipts": mr}):
    ri = _pure("receipt_image")


def _data(**overrides):
    base = dict(
        op_id=123, created_at=datetime(2026, 10, 1, 11, 32), manager_id=7, manager_name="Иван Петров",
        kind="tariff", status="completed", client_code="K7F3Q2", client_is_new=True, client_label="Анна, кофейня",
        product_title="Месяц", days=30, traffic_gb=500, devices_limit=5, expires_at=datetime(2026, 10, 31, 11, 32),
        price=399.0, service_fee=250.0, payment_method="online", autorenew=True,
        key_username="off_k7f3q2", key_fingerprint="3f9a", subscription_url="https://sub.example/SECRET",
    )
    base.update(overrides)
    return mr.ReceiptData(**base)


class ImageTests(unittest.TestCase):
    def test_every_status_and_audience_renders(self):
        for status in ("completed", "pending_payment", "cancelled", "failed", "refunded"):
            for audience in ri.AUDIENCES:
                png = ri.render_receipt(_data(status=status), audience)
                self.assertTrue(png.startswith(b"\x89PNG"), (status, audience))
        for kind in ("custom", "temp"):
            self.assertTrue(ri.render_receipt(_data(kind=kind, custom_note="45 дн. по формуле → 279 ₽")))

    def test_group_image_has_no_qr_and_no_label(self):
        # Ни ссылки подписки (QR), ни пометки менеджера в групповой картинке нет: она не зависит от них.
        with_secrets = ri.render_receipt(_data(), "group")
        without = ri.render_receipt(_data(subscription_url=None, client_label=None), "group")
        self.assertEqual(with_secrets, without)
        self.assertNotEqual(ri.render_receipt(_data(), "manager"),
                            ri.render_receipt(_data(subscription_url=None), "manager"))

    def test_no_qr_for_a_dead_key(self):
        dead = _data(kind="temp", temp_deleted_at=datetime(2026, 10, 1, 12, 32))
        self.assertEqual(ri.render_receipt(dead, "manager"), ri.render_receipt(_data(
            kind="temp", temp_deleted_at=datetime(2026, 10, 1, 12, 32), subscription_url=None), "manager"))

    def test_long_values_wrap(self):
        png = ri.render_receipt(_data(client_label="Очень длинная пометка " * 5, manager_name="Константин Константинопольский"))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_unknown_audience(self):
        with self.assertRaises(ValueError):
            ri.render_receipt(_data(), "everyone")


class CaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bot_env import _load, _pkg
        stubs = {
            "loader": SimpleNamespace(logger=Mock(), config=None),
            "tgbot": _pkg("tgbot"), "tgbot.services": _pkg("tgbot.services"), "tgbot.keyboards": _pkg("tgbot.keyboards"),
            "tgbot.keyboards.manager": _load("tgbot.keyboards.manager", ROOT / "tgbot/keyboards/manager.py"),
        }
        with patch.dict(sys.modules, stubs):
            cls.notifier = _load("tgbot.services.manager_notifier", ROOT / "tgbot/services/manager_notifier.py")

    def test_short_receipt_is_kept_as_is(self):
        text = mr.format_manager_receipt(_data())
        self.assertEqual(self.notifier.fit_caption(text), text)

    def test_long_receipt_drops_the_link_first(self):
        text = mr.format_manager_receipt(_data(custom_note="x" * 700, kind="custom"))
        fitted = self.notifier.fit_caption(text)
        self.assertNotIn("SECRET", fitted)                  # ссылка ушла — она есть QR на картинке
        self.assertIn("Итого", fitted)
        self.assertLessEqual(len(re.sub(r"<[^>]+>", "", fitted)), 1024)

    def test_huge_text_is_cut(self):
        fitted = self.notifier.fit_caption("<b>" + "я" * 3000 + "</b>")
        self.assertLessEqual(len(fitted), 1024)
        self.assertTrue(fitted.endswith("…"))


if __name__ == "__main__":
    unittest.main()


# =============================================================================
# Сайт, бот менеджера, админка
# =============================================================================

from test_manager_web import BASE, WebCase  # noqa: E402


class WebFeeTests(WebCase):
    async def test_account_sets_the_fee(self):
        manager = await self.env.make_manager(service_fee=None)
        async with self.client() as client:
            await self.login(client, manager)
            page = await client.get("/manager/account")
            self.assertIn('value="250"', page.text)
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

            response = await client.post("/manager/account/fee", data={"csrf": csrf, "service_fee": "300"},
                                         headers={"origin": BASE})
            self.assertEqual(response.status_code, 303)
            self.assertEqual((await self.svc.view(manager.id)).service_fee, 300)

            bad = await client.post("/manager/account/fee", data={"csrf": csrf, "service_fee": "5000"},
                                    headers={"origin": BASE})
            self.assertEqual(bad.status_code, 400)
            self.assertIn("от 0 до 1000", bad.text)
            forged = await client.post("/manager/account/fee", data={"csrf": "x", "service_fee": "1"},
                                       headers={"origin": BASE})
            self.assertEqual(forged.status_code, 403)
            self.assertEqual((await self.svc.view(manager.id)).service_fee, 300)

    async def test_locked_fee_has_no_form(self):
        manager = await self.env.make_manager(service_fee=None)
        await self.svc.admin_set_service_fee(manager.id, admin_id=1, locked=True)
        async with self.client() as client:
            await self.login(client, manager)
            page = await client.get("/manager/account")
            self.assertNotIn('action="/manager/account/fee"', page.text)
            self.assertIn("зафиксировал администратор", page.text)

    async def test_quote_shows_the_fee_and_receipt_png_is_private(self):
        a = await self.env.make_manager(telegram_id=1, service_fee=250, can_accept_cash=True)
        b = await self.env.make_manager(telegram_id=2, service_fee=250)
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n1")
        async with self.client() as client:
            csrf = await self.login(client, a)
            quote = await client.post("/manager/api/quote", json={"product": "tariff", "tariff_id": 2},
                                      headers=self.api(csrf))
            self.assertEqual((quote.json()["service_fee"], quote.json()["total"]), (250.0, 399.0))
            png = await client.get(f"/manager/receipt/{result.operation_id}.png")
            self.assertEqual(png.status_code, 200)
            self.assertEqual(png.headers["content-type"], "image/png")
            self.assertTrue(png.content.startswith(b"\x89PNG"))
            download = await client.get(f"/manager/receipt/{result.operation_id}.png?download=1")
            self.assertIn("attachment", download.headers["content-disposition"])
            history = await client.get("/manager/history")
            self.assertIn(f"/manager/receipt/{result.operation_id}.png", history.text)
        async with self.client() as client:
            await self.login(client, b)
            self.assertEqual((await client.get(f"/manager/receipt/{result.operation_id}.png")).status_code, 404)
        async with self.client() as client:
            self.assertEqual((await client.get(f"/manager/receipt/{result.operation_id}.png")).status_code, 401)


from bot_env import build_bot_env  # noqa: E402

MGR = 7001
ADMIN = 1


class BotFeeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env, self.bot = await build_bot_env(admin=True)
        self.svc = self.env.service
        self.session = self.bot.session

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def test_manager_changes_his_fee_in_the_bot(self):
        manager = await self.env.make_manager(telegram_id=MGR, service_fee=None)
        await self.bot.send(MGR, "/manager")
        self.assertIn("mgr:fee", str(self.session.of("SendMessage")[-1]["reply_markup"]))
        await self.bot.press(MGR, "mgr:fee")
        self.assertIn("250 ₽", self.session.last_text())
        await self.bot.press(MGR, "mgr:fee:edit")
        await self.bot.send(MGR, "много")
        self.assertIn("от 0 до 1000", self.session.last_text())
        await self.bot.send(MGR, "300")
        self.assertIn("сохранена", self.session.last_text())
        self.assertEqual((await self.svc.view(manager.id)).service_fee, 300)

    async def test_locked_fee_cannot_be_edited(self):
        manager = await self.env.make_manager(telegram_id=MGR, service_fee=None)
        await self.svc.admin_set_service_fee(manager.id, admin_id=1, locked=True)
        await self.bot.press(MGR, "mgr:fee")
        self.assertNotIn("mgr:fee:edit", str(self.session.of("EditMessageText")[-1]["reply_markup"]))
        await self.bot.press(MGR, "mgr:fee:edit")
        self.assertTrue(any("зафиксировал" in a for a in self.session.alerts()))

    async def test_preview_shows_the_fee(self):
        await self.env.make_manager(telegram_id=MGR, service_fee=None)
        await self.bot.press(MGR, "mgr:q:2")
        preview = self.session.last_text()
        self.assertIn("ваша услуга", preview)
        self.assertIn("К оплате: 399 ₽", preview)

    async def test_admin_sets_locks_and_pays_out(self):
        manager = await self.env.make_manager(telegram_id=MGR, service_fee=None)
        await self.bot.press(ADMIN, f"admin_mgr:{manager.id}")
        self.assertIn("Услуга: 250 ₽", self.session.last_text())
        await self.bot.press(ADMIN, f"admin_mgr_num:{manager.id}:service_fee")
        await self.bot.send(ADMIN, "200")
        await self.bot.press(ADMIN, f"admin_mgr_feelock:{manager.id}")
        view = await self.svc.view(manager.id)
        self.assertEqual((view.service_fee, view.service_fee_locked), (200, True))

        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="o1")
        # продажа до перехода на «услугу наличными»: услуга шла через ЮKassa и ждёт выплаты
        await self.env.repos.ops.update(result.operation_id, fee_in_cash=False)
        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")
        await self.bot.press(ADMIN, f"admin_mgr:{manager.id}")
        self.assertIn(f"admin_mgr_payout:{manager.id}", str(self.session.of("EditMessageText")[-1]["reply_markup"]))
        await self.bot.press(ADMIN, f"admin_mgr_payout:{manager.id}")
        self.assertIn("200 ₽", self.session.last_text())
        self.assertEqual((await self.svc.fees_outstanding(manager.id))[0], 200.0)   # вопрос — не выплата
        await self.bot.press(ADMIN, f"admin_mgr_payoutok:{manager.id}")
        self.assertEqual(await self.svc.fees_outstanding(manager.id), (0.0, 0))
        self.assertTrue(any("Выплачено" in a for a in self.session.alerts()))

    async def test_manager_cannot_pay_himself(self):
        manager = await self.env.make_manager(telegram_id=MGR)
        from aiogram.dispatcher.event.bases import UNHANDLED
        for data in (f"admin_mgr_payoutok:{manager.id}", f"admin_mgr_feelock:{manager.id}"):
            self.assertIs(await self.bot.press(MGR, data), UNHANDLED, data)
