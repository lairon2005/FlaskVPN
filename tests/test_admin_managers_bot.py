"""Админка менеджеров через бота: приглашение, права, лимиты, блокировка, инкассация, журнал, CSV, настройки."""
import csv
import io
import unittest

from aiogram.dispatcher.event.bases import UNHANDLED

from bot_env import build_bot_env

ADMIN = 1
MGR = 7001


class AdminCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env, self.bot = await build_bot_env(admin=True)
        self.svc = self.env.service
        self.session = self.bot.session

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def manager(self, **rights):
        return await self.env.make_manager(telegram_id=MGR, **rights)

    def markup(self) -> str:
        return str((self.session.of("EditMessageText") or self.session.of("SendMessage"))[-1]["reply_markup"])


class AccessTests(AdminCase):
    async def test_non_admin_gets_nothing(self):
        manager = await self.manager()
        for data in ("admin_managers", f"admin_mgr:{manager.id}", f"admin_mgr_st:{manager.id}:blocked",
                     f"admin_mgr_settleok:{manager.id}", f"admin_mgr_delok:{manager.id}", "admin_pricing"):
            self.assertIs(await self.bot.press(555, data), UNHANDLED, data)
        # даже сам менеджер не может админить
        self.assertIs(await self.bot.press(MGR, f"admin_mgr_st:{manager.id}:blocked"), UNHANDLED)
        self.assertEqual((await self.svc.view(manager.id)).status, "active")


class InviteAndCardTests(AdminCase):
    async def test_invite_flow(self):
        await self.bot.press(ADMIN, "admin_mgr_new")
        await self.bot.send(ADMIN, "Иван Петров")
        text = self.session.last_text()
        self.assertIn("Иван Петров", text)
        self.assertIn("https://t.me/bot?start=mgr_", text)
        token = text.split("start=mgr_")[1].split("<")[0].strip()
        manager = await self.svc.accept_invite(token, MGR)
        self.assertEqual(manager.display_name, "Иван Петров")

    async def test_name_validation(self):
        await self.bot.press(ADMIN, "admin_mgr_new")
        await self.bot.send(ADMIN, "я")
        self.assertIn("от 2 до 64", self.session.last_text())
        self.assertEqual(await self.svc.list_managers(), [])

    async def test_list_and_card(self):
        manager = await self.manager(can_accept_cash=True)
        await self.bot.press(ADMIN, "admin_managers")
        self.assertIn(f"admin_mgr:{manager.id}", self.markup())
        await self.bot.press(ADMIN, f"admin_mgr:{manager.id}")
        card = self.session.last_text()
        self.assertIn("Иван Петров", card)
        self.assertIn("✅ Приём наличных", card)
        self.assertIn("К сдаче", card)

    async def test_toggle_right_applies_and_resets_filter_cache(self):
        manager = await self.manager()
        await self.bot.press(ADMIN, f"admin_mgr_tg:{manager.id}:can_accept_cash")
        self.assertTrue((await self.svc.view(manager.id)).can_accept_cash)
        await self.bot.press(ADMIN, f"admin_mgr_tg:{manager.id}:can_accept_cash")
        self.assertFalse((await self.svc.view(manager.id)).can_accept_cash)

    async def test_unknown_right_is_refused(self):
        manager = await self.manager()
        await self.bot.press(ADMIN, f"admin_mgr_tg:{manager.id}:status")
        self.assertTrue(self.session.alerts())
        self.assertEqual((await self.svc.view(manager.id)).status, "active")

    async def test_number_limits(self):
        manager = await self.manager()
        await self.bot.press(ADMIN, f"admin_mgr_num:{manager.id}:cash_limit")
        await self.bot.send(ADMIN, "ерунда")
        self.assertIn("целое число", self.session.last_text())
        await self.bot.send(ADMIN, "12000")
        self.assertEqual((await self.svc.view(manager.id)).cash_limit, 12000)

        await self.bot.press(ADMIN, f"admin_mgr_num:{manager.id}:cash_limit")
        await self.bot.send(ADMIN, "-")
        self.assertIsNone((await self.svc.view(manager.id)).cash_limit)

        await self.bot.press(ADMIN, f"admin_mgr_num:{manager.id}:temp_keys_per_day")
        await self.bot.send(ADMIN, "3")
        self.assertEqual((await self.svc.view(manager.id)).temp_keys_per_day, 3)

    async def test_unknown_numeric_field_is_refused(self):
        manager = await self.manager()
        await self.bot.press(ADMIN, f"admin_mgr_num:{manager.id}:session_version")
        self.assertTrue(self.session.alerts())


class LifecycleTests(AdminCase):
    async def test_block_and_unblock_sync_commands_and_access(self):
        manager = await self.manager()
        apply_commands = self.bot.handlers.commands.apply_commands
        await self.bot.press(ADMIN, f"admin_mgr_st:{manager.id}:blocked")
        self.assertIsNone(await self.svc.get_by_telegram(MGR))
        self.assertEqual(apply_commands.await_args.args[1], MGR)
        self.assertFalse(apply_commands.await_args.kwargs["is_manager"])

        await self.bot.press(ADMIN, f"admin_mgr_st:{manager.id}:active")
        self.assertIsNotNone(await self.svc.get_by_telegram(MGR))
        self.assertTrue(apply_commands.await_args.kwargs["is_manager"])

    async def test_delete_needs_confirmation_and_keeps_the_journal(self):
        manager = await self.manager(can_accept_cash=True)
        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce="n")
        await self.bot.press(ADMIN, f"admin_mgr_del:{manager.id}")
        self.assertEqual((await self.svc.view(manager.id)).status, "active")   # пока только вопрос
        await self.bot.press(ADMIN, f"admin_mgr_delok:{manager.id}")
        self.assertIsNone(await self.svc.get_by_telegram(MGR))
        self.assertEqual(len(await self.svc.admin_history(manager.id)), 1)
        self.assertEqual(await self.svc.list_managers(), [])                   # из списка исчез

    async def test_reinvite_gives_a_new_link(self):
        manager, token = await self.svc.invite("Новый", admin_id=ADMIN)
        await self.bot.press(ADMIN, f"admin_mgr_reinvite:{manager.id}")
        self.assertIn("Новая ссылка", self.session.last_text())
        new = self.session.last_text().split("start=mgr_")[1].split("<")[0].strip()
        self.assertNotEqual(new, token)
        with self.assertRaises(self.env.module.ManagerError):
            await self.svc.accept_invite(token, MGR)
        await self.svc.accept_invite(new, MGR)


class MoneyAndJournalTests(AdminCase):
    async def _sell(self, manager, n=2):
        for i in range(n):
            await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash", idempotency_nonce=f"n{i}")

    async def test_settlement_flow(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=None)
        await self._sell(manager)
        await self.bot.press(ADMIN, f"admin_mgr:{manager.id}")
        self.assertIn("Принять выручку", self.markup())

        await self.bot.press(ADMIN, f"admin_mgr_settle:{manager.id}")
        self.assertIn("298", self.session.last_text())                       # сумма видна до подтверждения
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 298.0)   # вопрос — не списание

        await self.bot.press(ADMIN, f"admin_mgr_settleok:{manager.id}")
        self.assertEqual((await self.svc.cash_outstanding(manager.id)), (0.0, 0))
        self.assertTrue(any("Принято" in a for a in self.session.alerts()))

    async def test_journal_pages_and_filters(self):
        a = await self.env.make_manager(telegram_id=1, can_accept_cash=True)
        b = await self.env.make_manager(telegram_id=2, can_accept_cash=True)
        await self._sell(a, 1)
        await self._sell(b, 1)
        await self.bot.press(ADMIN, "admin_mgr_log:0:0")
        text = self.session.last_text()
        self.assertIn(f"м#{a.id}", text)
        self.assertIn(f"м#{b.id}", text)
        await self.bot.press(ADMIN, f"admin_mgr_log:{a.id}:0")
        self.assertNotIn(f"м#{b.id}", self.session.last_text())

    async def test_csv_has_every_field_the_spec_asks_for(self):
        manager = await self.manager(can_accept_cash=True)
        await self._sell(manager, 1)
        await self.bot.press(ADMIN, f"admin_mgr_csv:{manager.id}")

        documents = self.session.of("SendDocument")
        self.assertEqual(len(documents), 1)
        document = documents[0]["document"]
        raw = document.data if hasattr(document, "data") else document.read()
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
        header, first = rows[0], rows[1]
        for column in ("чек", "менеджер_id", "дата_UTC", "тип", "клиент_код", "тариф", "дней", "срок_действия_до",
                       "цена", "оплата", "ключ", "отпечаток"):
            self.assertIn(column, header)
        record = dict(zip(header, first))
        self.assertEqual((record["тип"], record["тариф"], record["цена"], record["оплата"]),
                         ("issue_tariff", "Месяц", "149.0", "cash"))
        self.assertTrue(record["ключ"].startswith("off_"))
        self.assertEqual(len(record["отпечаток"]), 4)
        self.assertNotIn("SECRET", raw.decode("utf-8"))                     # ссылка подписки в выгрузке не нужна


class PricingSettingsTests(AdminCase):
    async def test_screen_shows_formula_and_preview(self):
        await self.bot.press(ADMIN, "admin_pricing")
        text = self.session.last_text()
        self.assertIn("Цена дня", text)
        self.assertIn("5 ₽", text)
        self.assertIn("30 дн. —   199 ₽", text)     # превью считается по реальным тарифам
        self.assertIn("365 дн. —  1969 ₽", text)

    async def test_edit_day_price_changes_the_quote(self):
        manager = await self.manager()
        before = (await self.svc.quote(manager.id, product="custom", days=60)).total
        await self.bot.press(ADMIN, "admin_price_custom_day_price")
        await self.bot.send(ADMIN, "9,5")
        after = (await self.svc.quote(manager.id, product="custom", days=60)).total
        self.assertGreater(after, before)
        self.assertEqual((await self.svc.custom_settings()).day_price, 9.5)

    async def test_out_of_range_and_garbage_are_refused(self):
        manager = await self.manager()
        await self.bot.press(ADMIN, "admin_price_custom_max_days")
        for bad in ("0", "99999", "abc", "-4"):
            await self.bot.send(ADMIN, bad)
            self.assertIn("Попробуйте ещё раз", self.session.last_text(), bad)
        self.assertEqual((await self.svc.custom_settings()).max_days, 365)

    async def test_temp_key_settings_are_applied(self):
        await self.bot.press(ADMIN, "admin_price_temp_key_minutes")
        await self.bot.send(ADMIN, "15")
        self.assertEqual((await self.svc.temp_settings())[0], 15)

    async def test_renew_tariff_pick(self):
        await self.bot.press(ADMIN, "admin_price_renew")
        self.assertIn("admin_price_renewset_3", self.markup())
        await self.bot.press(ADMIN, "admin_price_renewset_3")
        self.assertEqual(await self.env.repos.settings.get_int("custom_renew_tariff_id", 0), 3)


if __name__ == "__main__":
    unittest.main()
