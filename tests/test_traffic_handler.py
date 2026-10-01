"""Докупка трафика в боте (tgbot/handlers/user/traffic.py): экран, счёт, висящий счёт, отказы."""
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_broadcast_promo import FakeCallbackQuery, _fsm_state, _load_module


def quote(**overrides):
    values = dict(
        ok=True, error=None, packs=2, added_gb=200, price=98.0, price_per_pack=49, remaining_days=20,
        current_limit_gb=500, total_limit_gb=700, available=8,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def load_traffic_handler():
    loader = types.ModuleType("loader")
    loader.logger = Mock()
    loader.config = SimpleNamespace(yookassa=SimpleNamespace(shop_id="x", secret_key="y"))

    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    payment = types.ModuleType("tgbot.services.payment")
    payment.create_payment = Mock(return_value=("https://pay.example/1", "yk-1"))
    payment.get_payment_url = Mock(return_value="https://pay.example/old")
    services.payment = payment
    services.payment_service = AsyncMock()
    services.payment_service.get_pending_payment.return_value = None
    services.traffic_service = AsyncMock()

    keyboards = types.ModuleType("tgbot.keyboards")
    keyboards.__path__ = []
    inline = types.ModuleType("tgbot.keyboards.inline")
    inline.back_to_main_menu_keyboard = Mock(return_value="main_kb")
    inline.traffic_invoice_keyboard = Mock(return_value="invoice_kb")
    inline.traffic_purchase_keyboard = Mock(return_value="purchase_kb")

    database = types.ModuleType("database")
    database.user_repo = AsyncMock()
    database.user_repo.get.return_value = SimpleNamespace(email="a@b.c")

    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    module = _load_module(
        "traffic_handler_under_test", "tgbot/handlers/user/traffic.py",
        {"loader": loader, "tgbot": tgbot, "tgbot.services": services, "tgbot.services.payment": payment,
         "tgbot.keyboards": keyboards, "tgbot.keyboards.inline": inline, "database": database},
    )
    module.CallbackQuery = FakeCallbackQuery
    return module, SimpleNamespace(
        traffic_service=services.traffic_service, payment_service=services.payment_service,
        payment=payment, inline=inline, user_repo=database.user_repo,
    )


def bot():
    return SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bot")))


class TrafficHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_shows_current_new_limit_and_price(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote()
        call = FakeCallbackQuery("buy_traffic")
        call.answer = AsyncMock()

        await module.buy_traffic_handler(call)

        text = call.message.edit_text.await_args.args[0]
        self.assertIn("500 ГБ/мес", text)
        self.assertIn("700 ГБ/мес", text)
        self.assertIn("98 ₽", text)
        self.assertIn("20 дн.", text)
        deps.inline.traffic_purchase_keyboard.assert_called_once_with(2, 8, 700)

    async def test_refusal_text_is_shown_as_is(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote(ok=False, error="Докупить трафик можно только при активной подписке.")
        call = FakeCallbackQuery("buy_traffic")
        call.answer = AsyncMock()
        await module.buy_traffic_handler(call)
        self.assertIn("только при активной подписке", call.message.edit_text.await_args.args[0])

    async def test_quantity_button_recomputes_on_the_server(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote(packs=3)
        call = FakeCallbackQuery("traffic_qty:3")
        call.answer = AsyncMock()
        await module.change_traffic_quantity(call)
        deps.traffic_service.quote.assert_awaited_once_with(123, 3)

    async def test_bad_quantity_falls_back_to_one(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote()
        call = FakeCallbackQuery("traffic_qty:abc")
        call.answer = AsyncMock()
        await module.change_traffic_quantity(call)
        deps.traffic_service.quote.assert_awaited_once_with(123, 1)

    async def test_pay_creates_a_traffic_invoice_without_tariff(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote()
        call = FakeCallbackQuery("traffic_pay:sbp:2")
        call.answer = AsyncMock()
        call.message.message_id = 9

        await module.pay_traffic_handler(call, _fsm_state(), bot())

        kwargs = deps.payment.create_payment.call_args.kwargs
        self.assertEqual(kwargs["amount"], 98.0)
        self.assertEqual(kwargs["payment_method_type"], "sbp")
        self.assertEqual(kwargs["metadata"]["kind"], "traffic")
        self.assertEqual(kwargs["items"], [{"description": kwargs["description"], "quantity": 2, "amount": 49.0}])
        record = deps.payment_service.create_payment_record.await_args.kwargs
        self.assertEqual((record["kind"], record["tariff_id"], record["extra_traffic_gb"], record["final_amount"]),
                         ("traffic", None, 200, 98.0))

    async def test_price_is_recomputed_at_pay_time_not_taken_from_the_button(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote(price=147.0, packs=2, price_per_pack=73.5)
        call = FakeCallbackQuery("traffic_pay:card:2")
        call.answer = AsyncMock()
        call.message.message_id = 9
        await module.pay_traffic_handler(call, _fsm_state(), bot())
        self.assertEqual(deps.payment.create_payment.call_args.kwargs["amount"], 147.0)

    async def test_unavailable_quote_creates_no_invoice(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote(ok=False, error="Вы уже докупили максимальный объём трафика.")
        call = FakeCallbackQuery("traffic_pay:card:2")
        call.answer = AsyncMock()
        await module.pay_traffic_handler(call, _fsm_state(), bot())
        deps.payment.create_payment.assert_not_called()
        deps.payment_service.create_payment_record.assert_not_awaited()

    async def test_pending_invoice_blocks_a_second_one_and_offers_cancel(self):
        module, deps = load_traffic_handler()
        deps.payment_service.get_pending_payment.return_value = SimpleNamespace(
            kind="traffic", extra_traffic_gb=100, extra_devices=0, final_amount=49.0, yookassa_payment_id="yk-0")
        call = FakeCallbackQuery("traffic_pay:card:1")
        call.answer = AsyncMock()

        await module.pay_traffic_handler(call, _fsm_state(), bot())

        deps.payment.create_payment.assert_not_called()
        self.assertIn("неоплаченный счёт", call.message.edit_text.await_args.args[0])
        deps.inline.traffic_invoice_keyboard.assert_called_with("https://pay.example/old", 1)

    async def test_gateway_failure_is_reported_and_nothing_is_recorded(self):
        module, deps = load_traffic_handler()
        deps.traffic_service.quote.return_value = quote()
        deps.payment.create_payment.side_effect = RuntimeError("gateway down")
        call = FakeCallbackQuery("traffic_pay:card:2")
        call.answer = AsyncMock()
        await module.pay_traffic_handler(call, _fsm_state(), bot())
        self.assertIn("Не удалось создать счёт", call.message.edit_text.await_args.args[0])
        deps.payment_service.create_payment_record.assert_not_awaited()

    async def test_cancel_invoice_returns_to_the_screen_with_the_same_quantity(self):
        module, deps = load_traffic_handler()
        deps.payment_service.cancel_pending_payment.return_value = SimpleNamespace()
        deps.traffic_service.quote.return_value = quote(packs=3)
        call = FakeCallbackQuery("traffic_cancel_invoice:3")
        call.answer = AsyncMock()
        await module.cancel_traffic_invoice(call)
        deps.traffic_service.quote.assert_awaited_once_with(123, 3)
        self.assertIn("отменён", call.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
