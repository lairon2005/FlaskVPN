"""Бот менеджера сквозным прогоном: приглашение, меню, выдача (наличные/QR/свои дни), временный ключ, клиенты."""
import unittest

from aiogram.dispatcher.event.bases import UNHANDLED

from bot_env import build_bot_env

MANAGER_TG = 7001


class BotCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env, self.bot = await build_bot_env()
        self.svc = self.env.service
        self.session = self.bot.session

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def manager(self, **rights):
        return await self.env.make_manager(telegram_id=MANAGER_TG, **rights)

    async def issue_cash_via_buttons(self, user=MANAGER_TG, tariff_id=2):
        await self.bot.press(user, "mgr:issue")
        await self.bot.press(user, "mgr:who:new")
        await self.bot.press(user, "mgr:what:tariff")
        await self.bot.press(user, f"mgr:tariff:{tariff_id}")
        await self.bot.press(user, "mgr:go:cash")


class InviteTests(BotCase):
    async def test_invite_link_makes_a_manager(self):
        manager, token = await self.svc.invite("Иван Петров", admin_id=1)
        await self.bot.send(MANAGER_TG, f"/start mgr_{token}")

        self.assertIn("Добро пожаловать", self.session.last_text())
        self.assertIn("Иван Петров", self.session.last_text())
        self.assertEqual((await self.svc.view_by_telegram(MANAGER_TG)).display_name, "Иван Петров")
        self.bot.handlers.commands.apply_commands.assert_awaited()

    async def test_second_person_cannot_reuse_the_link(self):
        manager, token = await self.svc.invite("Иван", admin_id=1)
        await self.bot.send(MANAGER_TG, f"/start mgr_{token}")
        self.session.clear()
        await self.bot.send(MANAGER_TG + 1, f"/start mgr_{token}")
        self.assertIn("недействительна", self.session.last_text())
        self.assertIsNone(await self.svc.view_by_telegram(MANAGER_TG + 1))

    async def test_garbage_token(self):
        await self.bot.send(MANAGER_TG, "/start mgr_нет-такого")
        self.assertIn("недействительна", self.session.last_text())


class AccessTests(BotCase):
    async def test_non_manager_gets_nothing_from_the_panel(self):
        self.assertIs(await self.bot.send(999, "/manager"), UNHANDLED)
        self.assertEqual(self.session.calls, [])
        # старая кнопка в руках не-менеджера: вежливый отказ, и ни одно действие не выполняется
        for data in ("mgr:menu", "mgr:go:cash", "mgr:clients:0", "mgr:link:ABCDEF"):
            self.session.clear()
            await self.bot.press(999, data)
            self.assertEqual(len(self.session.alerts()), 1, data)
            self.assertIn("закрыт", self.session.alerts()[0])
            self.assertEqual([c for c, _ in self.session.calls if c != "AnswerCallbackQuery"], [], data)
        self.assertEqual(await self.svc.admin_history(), [])

    async def test_blocked_manager_loses_the_panel_immediately(self):
        manager = await self.manager()
        await self.bot.send(MANAGER_TG, "/manager")
        self.assertIn("Панель менеджера", self.session.last_text())
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        self.session.clear()

        await self.bot.press(MANAGER_TG, "mgr:issue")            # даже навигация — без кэша-лазейки

        self.assertTrue(any("закрыт" in a for a in self.session.alerts()), self.session.calls)
        self.assertEqual(self.session.of("EditMessageText"), [])

    async def test_unblocked_manager_gets_the_panel_back_after_forget(self):
        manager = await self.manager()
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        await self.bot.send(MANAGER_TG, "/manager")              # отказ запомнен на 30 с
        await self.svc.set_status(manager.id, "active", admin_id=1)
        self.bot.handlers.filters.forget(MANAGER_TG)             # так делает админ-хендлер
        self.session.clear()
        await self.bot.send(MANAGER_TG, "/manager")
        self.assertIn("Панель менеджера", self.session.last_text())

    async def test_menu(self):
        await self.manager(can_accept_cash=True)
        await self.bot.send(MANAGER_TG, "/manager")
        text = self.session.last_text()
        self.assertIn("Панель менеджера", text)
        self.assertIn("К сдаче", text)

    async def test_menu_hides_cash_line_without_the_right(self):
        await self.manager()
        await self.bot.send(MANAGER_TG, "/manager")
        self.assertNotIn("К сдаче", self.session.last_text())

    async def test_global_stats_button_only_with_the_right(self):
        manager = await self.manager()
        await self.bot.send(MANAGER_TG, "/manager")
        markup = str(self.session.of("SendMessage")[-1]["reply_markup"])
        self.assertNotIn("mgr:gstats", markup)
        await self.bot.press(MANAGER_TG, "mgr:gstats")
        self.assertTrue(any("права" in a for a in self.session.alerts()) or "права" in self.session.last_text())

    async def test_web_login_link(self):
        await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:web")
        text = self.session.last_text()
        self.assertIn("https://example.com/manager/login?t=", text)
        token = text.split("?t=")[1].split()[0].strip()
        self.assertIsNotNone(await self.svc.consume_login_token(token))


class IssueFlowTests(BotCase):
    async def test_cash_issue_by_tariff(self):
        manager = await self.manager(can_accept_cash=True)
        await self.bot.press(MANAGER_TG, "mgr:issue")
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        self.assertIn("Месяц", str(self.session.of("EditMessageText")[-1]["reply_markup"]))
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        preview = self.session.last_text()
        self.assertIn("149", preview)
        self.assertIn("500 ГБ", preview)
        self.assertIn("автоматически", preview)               # согласие на автопродление видно до оплаты

        self.session.clear()
        await self.bot.press(MANAGER_TG, "mgr:go:cash")

        history = await self.svc.history(manager.id)
        self.assertEqual((history[0].status, history[0].price, history[0].payment_method), ("completed", 149.0, "cash"))
        done = self.session.texts()[0]
        self.assertIn("Чек № M-000001", done)
        self.assertIn("SECRET", done)                         # ссылка менеджеру
        self.assertIn("/c/", done)                            # ссылка кабинета клиента
        self.assertEqual(len(self.session.of("SendPhoto")), 1)  # QR для установки
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 149.0)

    async def test_double_tap_on_confirm_issues_one_key(self):
        manager = await self.manager(can_accept_cash=True)
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        await self.bot.press(MANAGER_TG, "mgr:go:cash")
        await self.bot.press(MANAGER_TG, "mgr:go:cash")          # второй тап по устаревшей кнопке
        self.assertEqual(len(await self.svc.history(manager.id)), 1)
        self.assertTrue(any("устарела" in a for a in self.session.alerts()))

    async def test_cash_button_is_absent_and_refused_without_the_right(self):
        manager = await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        self.assertNotIn("mgr:go:cash", str(self.session.of("EditMessageText")[-1]["reply_markup"]))
        self.session.clear()
        await self.bot.press(MANAGER_TG, "mgr:go:cash")           # подделка callback
        self.assertTrue(any("наличных" in t for t in self.session.texts()))
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 0)

    async def test_online_issue_shows_qr_and_check_buttons(self):
        manager = await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        self.session.clear()
        await self.bot.press(MANAGER_TG, "mgr:go:online")

        photo = self.session.of("SendPhoto")[-1]
        self.assertIn("pay.example", photo["caption"])
        buttons = str(photo["reply_markup"])
        self.assertIn("mgr:chk:", buttons)
        self.assertIn("mgr:cancel:", buttons)

        op_id = (await self.svc.history(manager.id))[0].id
        self.session.clear()
        await self.bot.press(MANAGER_TG, f"mgr:chk:{op_id}")
        self.assertTrue(any("ещё не пришла" in a for a in self.session.alerts()))

        await self.env.payments.process_successful_payment("yk-1", 149.0)
        self.session.clear()
        await self.bot.press(MANAGER_TG, f"mgr:chk:{op_id}")
        self.assertTrue(any("Оплата получена" in a for a in self.session.alerts()))

    async def test_online_invoice_can_be_cancelled(self):
        manager = await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        await self.bot.press(MANAGER_TG, "mgr:go:online")
        op_id = (await self.svc.history(manager.id))[0].id
        self.session.clear()
        await self.bot.press(MANAGER_TG, f"mgr:cancel:{op_id}")
        self.assertTrue(any("отменён" in a for a in self.session.alerts()))
        self.assertEqual((await self.svc.history(manager.id))[0].status, "cancelled")

    async def test_custom_days_flow(self):
        manager = await self.manager(can_accept_cash=True)
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:custom")
        self.assertIn("Свои дни", self.session.last_text())
        await self.bot.send(MANAGER_TG, "45")
        preview = self.session.last_text()
        self.assertIn("279", preview)
        self.assertIn("45", preview)
        await self.bot.press(MANAGER_TG, "mgr:go:cash")
        op = (await self.svc.history(manager.id))[0]
        self.assertEqual((op.op_type, op.days, op.price), ("issue_custom", 45, 279.0))

    async def test_custom_days_rejects_garbage_and_out_of_range(self):
        await self.manager(can_accept_cash=True)
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:custom")
        for bad in ("abc", "-5", "1.5"):
            self.session.clear()
            await self.bot.send(MANAGER_TG, bad)
            self.assertIn("целое число", self.session.last_text(), bad)
        self.session.clear()
        await self.bot.send(MANAGER_TG, "9999")
        self.assertIn("❌", self.session.last_text())

    async def test_custom_days_preview_suggests_a_cheaper_tariff(self):
        await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:custom")
        await self.bot.send(MANAGER_TG, "25")
        self.assertIn("Выгоднее стандартный тариф", self.session.last_text())

    async def test_client_by_access_code(self):
        manager = await self.manager()
        user = await self.env.make_telegram_client(555)
        code = await self.svc.create_access_code(user.user_id)
        await self.bot.press(MANAGER_TG, "mgr:who:code")
        self.session.clear()
        await self.bot.send(MANAGER_TG, code)

        self.assertTrue(any("добавлен" in t for t in self.session.texts()))
        self.assertTrue(self.session.of("DeleteMessage"))        # код из переписки удалён
        self.assertNotIn("555", " ".join(self.session.texts()))   # Telegram ID не показан
        self.assertEqual((await self.svc.list_clients(manager.id))[1], 1)

    async def test_wrong_access_code(self):
        await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:who:code")
        self.session.clear()
        await self.bot.send(MANAGER_TG, "ABCDEF")
        self.assertIn("не найден", self.session.last_text())

    async def test_issue_to_existing_client_from_list(self):
        manager = await self.manager(can_accept_cash=True)
        await self.issue_cash_via_buttons()
        code = (await self.svc.history(manager.id))[0].client_code

        await self.bot.press(MANAGER_TG, "mgr:issue")
        await self.bot.press(MANAGER_TG, "mgr:pickc:0")
        self.assertIn(f"mgr:pick:{code}", str(self.session.of("EditMessageText")[-1]["reply_markup"]))
        await self.bot.press(MANAGER_TG, f"mgr:pick:{code}")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        await self.bot.press(MANAGER_TG, "mgr:go:cash")

        history = await self.svc.history(manager.id)
        self.assertEqual(len(history), 2)
        self.assertEqual({h.client_code for h in history}, {code})


class ClientsAndHistoryTests(BotCase):
    async def test_clients_list_card_and_link(self):
        manager = await self.manager(can_accept_cash=True)
        await self.issue_cash_via_buttons()
        code = (await self.svc.history(manager.id))[0].client_code

        await self.bot.press(MANAGER_TG, "mgr:clients:0")
        self.assertIn(code, str(self.session.of("EditMessageText")[-1]["reply_markup"]))
        await self.bot.press(MANAGER_TG, f"mgr:c:{code}")
        card = self.session.last_text()
        self.assertIn(code, card)
        self.assertIn("Android", card)                          # платформа устройства
        self.assertNotIn("SECRET-HWID", card)

        self.session.clear()
        await self.bot.press(MANAGER_TG, f"mgr:link:{code}")
        self.assertIn("SECRET", " ".join(self.session.texts()))
        self.assertEqual(len(self.session.of("SendPhoto")), 1)
        self.assertIn("key_view", [h.op_type for h in await self.svc.history(manager.id)])

    async def test_foreign_client_card_is_refused(self):
        a = await self.env.make_manager(telegram_id=1, can_accept_cash=True)
        await self.manager()
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n")
        self.session.clear()
        await self.bot.press(MANAGER_TG, f"mgr:c:{result.client_code}")
        self.assertIn("Клиент не найден", self.session.last_text())
        await self.bot.press(MANAGER_TG, f"mgr:link:{result.client_code}")
        self.assertNotIn("SECRET", " ".join(self.session.texts()))

    async def test_history_and_stats_screens(self):
        manager = await self.manager(can_accept_cash=True)
        await self.issue_cash_via_buttons()
        await self.bot.press(MANAGER_TG, "mgr:hist:0")
        self.assertIn("Выдача по тарифу", self.session.last_text())
        await self.bot.press(MANAGER_TG, "mgr:stats")
        self.assertIn("149", self.session.last_text())

    async def test_disable_autorenew_and_cabinet_reset(self):
        manager = await self.manager(can_accept_cash=True)
        await self.issue_cash_via_buttons()
        code = (await self.svc.history(manager.id))[0].client_code
        self.env.cards.get_card.return_value = type("C", (), {"auto_renew_enabled": True})()
        self.session.clear()
        await self.bot.press(MANAGER_TG, f"mgr:noauto:{code}")
        self.assertTrue(self.session.alerts())
        await self.bot.press(MANAGER_TG, f"mgr:cab:{code}")
        self.assertIn("/c/", " ".join(self.session.texts()))


class TempKeyFlowTests(BotCase):
    async def test_temp_key_issue_and_convert(self):
        manager = await self.manager(can_accept_cash=True)
        await self.bot.press(MANAGER_TG, "mgr:temp")
        self.assertIn("60 мин", self.session.last_text())
        self.session.clear()
        await self.bot.press(MANAGER_TG, "mgr:temp_go")

        self.assertIn("Временный ключ выдан", self.session.texts()[0])
        self.assertEqual(len(self.session.of("SendPhoto")), 1)
        self.assertEqual(len(self.env.panel.users), 1)
        keys = await self.svc.list_temp_keys(manager.id)
        self.assertEqual(len(keys), 1)

        await self.bot.press(MANAGER_TG, f"mgr:conv:{keys[0]['id']}")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        await self.bot.press(MANAGER_TG, "mgr:go:cash")

        self.assertEqual(len(self.env.panel.users), 1)          # тот же ключ в панели
        op = (await self.svc.history(manager.id))[0]
        self.assertEqual((op.op_type, op.status), ("convert_temp", "completed"))

    async def test_temp_key_double_tap(self):
        manager = await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:temp")
        await self.bot.press(MANAGER_TG, "mgr:temp_go")
        await self.bot.press(MANAGER_TG, "mgr:temp_go")
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_temp_limit_message(self):
        await self.manager(temp_keys_per_day=1)
        for _ in range(2):
            await self.bot.press(MANAGER_TG, "mgr:temp")
            await self.bot.press(MANAGER_TG, "mgr:temp_go")
        self.assertIn("лимит", self.session.last_text())
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_temp_list_screen(self):
        await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:temp")
        await self.bot.press(MANAGER_TG, "mgr:temp_go")
        await self.bot.press(MANAGER_TG, "mgr:temps")
        self.assertIn("Активные временные ключи", self.session.last_text())

    async def test_temp_button_refused_without_the_right(self):
        await self.manager(can_issue_temp=False)
        await self.bot.press(MANAGER_TG, "mgr:temp")
        await self.bot.press(MANAGER_TG, "mgr:temp_go")
        self.assertEqual(len(self.env.panel.users), 0)


class RealNotifierTests(BotCase):
    """С настоящим ManagerNotifier: что и куда реально уходит через Bot API."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import importlib.util, sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from bot_env import _load, _pkg

        root = Path(__file__).resolve().parents[1]
        config = SimpleNamespace(tg_bot=SimpleNamespace(manager_receipts_target=(-100500, 77)))
        stubs = {
            "loader": SimpleNamespace(logger=Mock(), config=config),
            "tgbot": _pkg("tgbot"), "tgbot.services": _pkg("tgbot.services"), "tgbot.keyboards": _pkg("tgbot.keyboards"),
            "tgbot.keyboards.manager": _load("tgbot.keyboards.manager", root / "tgbot/keyboards/manager.py"),
            "tgbot.services.qr_generator": _load("tgbot.services.qr_generator", root / "tgbot/services/qr_generator.py"),
        }
        with patch.dict(sys.modules, stubs):
            module = _load("tgbot.services.manager_notifier", root / "tgbot/services/manager_notifier.py")
        self.svc.notifier = module.ManagerNotifier(self.bot.bot, config)

    async def test_group_receipt_goes_to_the_topic_without_secrets(self):
        await self.manager(can_accept_cash=True)
        await self.issue_cash_via_buttons()

        group = [d for d in self.session.of("SendMessage") if d["chat_id"] == -100500]
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0]["message_thread_id"], 77)
        for secret in ("SECRET", "http", "7001"):
            self.assertNotIn(secret, group[0]["text"])
        self.assertIn("M-000001", group[0]["text"])

    async def test_temp_key_receipt_is_edited_in_place_when_the_key_dies(self):
        manager = await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:temp")
        await self.bot.press(MANAGER_TG, "mgr:temp_go")
        group_messages = [d for d in self.session.of("SendMessage") if d["chat_id"] == -100500]
        self.assertEqual(len(group_messages), 1)
        self.assertIn("Временный ключ", group_messages[0]["text"])

        from sqlalchemy import update
        from db import TempKey
        import datetime
        async with self.env.session_maker() as session:
            await session.execute(update(TempKey).values(expires_at=datetime.datetime.now() - datetime.timedelta(minutes=1)))
            await session.commit()
        self.session.clear()

        await self.svc.expire_temp_keys()

        edits = self.session.of("EditMessageText")
        self.assertEqual(len(edits), 1)
        self.assertIn("🗑 Ключ удалён", edits[0]["text"])
        self.assertEqual(edits[0]["chat_id"], -100500)
        # менеджеру — отдельное уведомление, но без ссылки удалённого ключа
        to_manager = [d for d in self.session.of("SendMessage") if d["chat_id"] == MANAGER_TG]
        self.assertEqual(len(to_manager), 1)
        self.assertNotIn("SECRET", to_manager[0]["text"])

    async def test_online_payment_notifies_manager_with_link_and_qr(self):
        manager = await self.manager()
        await self.bot.press(MANAGER_TG, "mgr:who:new")
        await self.bot.press(MANAGER_TG, "mgr:what:tariff")
        await self.bot.press(MANAGER_TG, "mgr:tariff:2")
        await self.bot.press(MANAGER_TG, "mgr:go:online")
        self.session.clear()

        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")

        to_manager = [d for d in self.session.of("SendMessage") if d["chat_id"] == MANAGER_TG]
        self.assertEqual(len(to_manager), 1)
        self.assertIn("SECRET", to_manager[0]["text"])
        self.assertEqual(len([d for d in self.session.of("SendPhoto") if d["chat_id"] == MANAGER_TG]), 1)
        group_edit = [d for d in self.session.of("EditMessageText") if d["chat_id"] == -100500]
        self.assertEqual(len(group_edit), 1)
        self.assertIn("Ключ выдан", group_edit[0]["text"])
        self.assertNotIn("SECRET", group_edit[0]["text"])

    async def test_receipt_failure_does_not_break_the_issue(self):
        await self.manager(can_accept_cash=True)

        async def boom(*a, **k):
            raise RuntimeError("telegram is down")
        self.svc.notifier.post_group_receipt = boom
        await self.issue_cash_via_buttons()
        history = await self.svc.history(1)
        self.assertEqual(history[0].status, "completed")        # ключ выдан, деньги учтены


if __name__ == "__main__":
    unittest.main()
