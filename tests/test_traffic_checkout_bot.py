"""
Чекаут бота с пакетами трафика: клавиатуры и хендлеры.

Количество устройств и пакетов едет в callback_data (а не в FSM) — значит, формат
кнопок и обратная совместимость со СТАРЫМИ сообщениями (без `_<packs>`) обязаны держаться.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_broadcast_promo import _callback_query, _fsm_state, load_payment_handlers_module
from test_ui_mode import buttons, load_inline_module


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_inline_module()

    def targets(self, markup):
        return {text: target for text, _kind, target in buttons(markup)}

    def test_checkout_step_has_two_steppers_and_pay_button(self):
        markup = self.kb.tariff_traffic_keyboard(tariff_id=5, slots=1, packs=2, max_slots=5, max_packs=10,
                                                 base_limit=5, pack_gb=100)
        found = [t for _, _, t in buttons(markup)]
        # устройства
        self.assertIn("tslots_5_0_2", found)
        self.assertIn("tslots_5_2_2", found)
        # трафик
        self.assertIn("tslots_5_1_1", found)
        self.assertIn("tslots_5_1_3", found)
        # оплата несёт оба количества
        self.assertIn("tpay_5_1_2", found)
        labels = [text for text, _, _ in buttons(markup)]
        self.assertIn("📱 6 устройств", labels)
        self.assertIn("📊 +200 ГБ", labels)

    def test_steppers_stop_at_the_edges(self):
        low = [t for _, _, t in buttons(self.kb.tariff_traffic_keyboard(5, 0, 0, 5, 10, 5, 100))]
        self.assertNotIn("tslots_5_-1_0", low)
        self.assertNotIn("tslots_5_0_-1", low)
        high = [t for _, _, t in buttons(self.kb.tariff_traffic_keyboard(5, 5, 10, 5, 10, 5, 100))]
        self.assertNotIn("tslots_5_6_10", high)
        self.assertNotIn("tslots_5_5_11", high)

    def test_unlimited_tariff_hides_the_traffic_stepper(self):
        markup = self.kb.tariff_traffic_keyboard(5, 0, 0, 5, max_packs=0, base_limit=5, pack_gb=100)
        labels = [text for text, _, _ in buttons(markup)]
        self.assertFalse(any("ГБ" in label for label in labels))

    def test_payment_method_buttons_carry_packs_and_keep_old_format_compatible(self):
        markup = self.kb.payment_method_choice_keyboard(5, slots=2, packs=3)
        found = [t for _, _, t in buttons(markup)]
        self.assertIn("paymethod_card_5_2_3", found)
        self.assertIn("paymethod_sbp_5_2_3", found)
        # без packs (интро-тариф, старые вызовы) — нулевые пакеты, но формат единый
        self.assertIn("paymethod_card_5_0_0", [t for _, _, t in buttons(self.kb.payment_method_choice_keyboard(5))])

    def test_every_callback_fits_telegram_limit(self):
        markups = [
            self.kb.tariff_traffic_keyboard(999999, 5, 10, 5, 10, 5, 100),
            self.kb.payment_method_choice_keyboard(999999, 5, packs=10),
            self.kb.traffic_purchase_keyboard(10, 10, 1500),
            self.kb.traffic_invoice_keyboard("https://pay.example/x", 10),
        ]
        for markup in markups:
            for _text, kind, target in buttons(markup):
                if kind == "callback":
                    self.assertLessEqual(len(target.encode()), 64, target)

    def test_profile_and_admin_menus_have_the_new_entries(self):
        profile = [t for _, _, t in buttons(self.kb.profile_keyboard("https://sub"))]
        self.assertIn("buy_traffic", profile)
        self.assertIn("mgr_client_code", profile)
        admin = [t for _, _, t in buttons(self.kb.admin_main_menu_keyboard())]
        for expected in ("admin_traffic_settings", "admin_managers", "admin_pricing"):
            self.assertIn(expected, admin)

    def test_manager_button_only_for_managers(self):
        self.assertNotIn("mgr:menu", [t for _, _, t in buttons(self.kb.main_menu_keyboard())])
        self.assertIn("mgr:menu", [t for _, _, t in buttons(self.kb.main_menu_keyboard(is_manager=True))])

    def test_traffic_screen_keyboard(self):
        markup = self.kb.traffic_purchase_keyboard(packs=2, max_packs=4, total_gb=700)
        found = [t for _, _, t in buttons(markup)]
        for expected in ("traffic_qty:1", "traffic_qty:3", "traffic_pay:card:2", "traffic_pay:sbp:2"):
            self.assertIn(expected, found)
        edge = [t for _, _, t in buttons(self.kb.traffic_purchase_keyboard(packs=1, max_packs=1, total_gb=600))]
        self.assertNotIn("traffic_qty:0", edge)
        self.assertNotIn("traffic_qty:2", edge)


class CheckoutHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Хендлеры чекаута с настоящими настройками цен (стабы остальных сервисов — из test_broadcast_promo)."""

    def _setup(self, **tariff_fields):
        module, deps = load_payment_handlers_module()
        fields = dict(id=5, name="Месяц", price=149, duration_days=30, is_active=True, is_intro=False,
                      data_limit_gb=None, loyalty_price=None, renew_tariff_id=None)
        fields.update(tariff_fields)
        tariff = SimpleNamespace(**fields)
        deps.tariff_repo.get_by_id.return_value = tariff
        deps.tariff_repo.get_by_id_map.return_value = {5: tariff}
        deps.user_repo.get.return_value = SimpleNamespace(
            user_id=123, extra_devices=0, extra_traffic_gb=0, subscription_end_date=None,
            is_first_payment_made=False, intro_used=False,
        )
        deps.payment_service = module.payment_service
        deps.payment_service.get_pending_payment.return_value = None
        deps.payment_service.hold_promo = AsyncMock(return_value=None)
        module.payment.create_payment.return_value = ("https://pay.example/1", "yk-1")
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bot")))
        return module, deps, bot

    async def test_stepper_screen_shows_totals_and_traffic_line(self):
        module, deps, _ = self._setup()
        call = _callback_query("tslots_5_1_2")
        await module.change_tariff_slots_handler(call, _fsm_state())

        text = call.message.edit_text.await_args.args[0]
        self.assertIn("700 ГБ/мес", text)               # 500 + 200 докупленных
        self.assertIn("247.00", text)                    # 100 (цена тарифа в стабе) + 49 (слот) + 98 (2 пакета)

    async def test_old_stepper_callback_without_packs_still_works(self):
        module, _, _ = self._setup()
        call = _callback_query("tslots_5_1")             # кнопка из сообщения до появления трафика
        await module.change_tariff_slots_handler(call, _fsm_state())
        text = call.message.edit_text.await_args.args[0]
        self.assertIn("Трафик:", text)
        self.assertNotIn("доп.)", text.split("Устройств:")[0])   # без пакетов строка трафика без «+ N доп.»
        self.assertIn("149.00", text)                    # 100 + 49

    async def test_packs_are_capped_by_the_server_limit(self):
        module, _, _ = self._setup()
        call = _callback_query("tslots_5_0_999")         # подделанная кнопка
        await module.change_tariff_slots_handler(call, _fsm_state())
        text = call.message.edit_text.await_args.args[0]
        self.assertIn("1500 ГБ/мес", text)               # 500 + 10 пакетов × 100

    async def test_unlimited_tariff_ignores_packs(self):
        module, _, _ = self._setup(data_limit_gb=0)
        call = _callback_query("tslots_5_0_4")
        await module.change_tariff_slots_handler(call, _fsm_state())
        text = call.message.edit_text.await_args.args[0]
        self.assertIn("Безлимит", text)
        self.assertNotIn("Итого", text)                  # доплат нет — итог не нужен

    async def test_payment_with_packs_charges_and_records_them(self):
        module, deps, bot = self._setup()
        call = _callback_query("paymethod_card_5_1_2")
        call.message.edit_text.return_value = SimpleNamespace(message_id=9)
        await module.select_payment_method_handler(call, _fsm_state(), bot)

        kwargs = module.payment.create_payment.call_args.kwargs
        self.assertEqual(kwargs["amount"], 247.0)
        self.assertEqual(kwargs["metadata"]["traffic_gb"], "200")
        items = kwargs["items"]
        self.assertEqual(len(items), 3)                  # тариф, устройства, трафик — отдельными строками чека
        self.assertAlmostEqual(sum(i["quantity"] * i["amount"] for i in items), 247.0)
        record = deps.payment_service.create_payment_record.await_args.kwargs
        self.assertEqual((record["extra_devices"], record["extra_traffic_gb"], record["final_amount"]), (1, 200, 247.0))

    async def test_old_payment_callback_means_no_packs(self):
        module, deps, bot = self._setup()
        call = _callback_query("paymethod_sbp_5_1")      # до появления трафика
        call.message.edit_text.return_value = SimpleNamespace(message_id=9)
        await module.select_payment_method_handler(call, _fsm_state(), bot)
        self.assertEqual(module.payment.create_payment.call_args.kwargs["amount"], 149.0)
        self.assertEqual(deps.payment_service.create_payment_record.await_args.kwargs["extra_traffic_gb"], 0)

    async def test_packs_on_unlimited_tariff_are_not_charged(self):
        module, deps, bot = self._setup(data_limit_gb=0)
        call = _callback_query("paymethod_card_5_0_5")
        call.message.edit_text.return_value = SimpleNamespace(message_id=9)
        await module.select_payment_method_handler(call, _fsm_state(), bot)
        self.assertEqual(module.payment.create_payment.call_args.kwargs["amount"], 100.0)
        self.assertEqual(deps.payment_service.create_payment_record.await_args.kwargs["extra_traffic_gb"], 0)

    async def test_renewal_default_keeps_already_paid_packs(self):
        """При продлении степпер стартует с уже оплаченных пакетов — иначе продление молча сняло бы их."""
        module, deps, _ = self._setup()
        deps.user_repo.get.return_value = SimpleNamespace(
            user_id=123, extra_devices=0, extra_traffic_gb=300, subscription_end_date=None,
            is_first_payment_made=True, intro_used=False,
        )
        call = _callback_query("select_tariff_5")
        await module.select_tariff_handler(call, _fsm_state(), SimpleNamespace())
        self.assertIn("800 ГБ/мес", call.message.edit_text.await_args.args[0])   # 500 + 300


if __name__ == "__main__":
    unittest.main()
