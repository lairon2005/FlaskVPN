"""
ManagerService на настоящей БД: права, приватность клиента, деньги, идемпотентность,
временные ключи, инкассация. Мокаются только панель и Telegram.
"""
import dataclasses
import datetime
import unittest
from types import SimpleNamespace

from sqlalchemy import func, select

from manager_env import GIB, build_env
from remnawave.client import RemnawaveAPIError


class EnvTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await build_env()
        self.svc = self.env.service
        self.E = self.env.module.ManagerError

    async def asyncTearDown(self):
        await self.env.engine.dispose()

    async def manager(self, **rights):
        return await self.env.make_manager(**rights)

    async def count_offline_clients(self):
        from db import User
        async with self.env.session_maker() as session:
            return (await session.execute(
                select(func.count()).select_from(User).where(User.origin == "offline")
            )).scalar_one()

    async def cash_issue(self, manager, nonce="n1", **kwargs):
        kwargs.setdefault("product", "tariff")
        kwargs.setdefault("tariff_id", 2)
        return await self.svc.issue(manager.id, method="cash", idempotency_nonce=nonce, **kwargs)


# =============================================================================
# Права менеджера
# =============================================================================

class PermissionTests(EnvTestCase):
    async def test_unknown_and_invited_are_refused(self):
        manager, _ = await self.svc.invite("Новый", admin_id=1)
        with self.assertRaises(self.E) as ctx:
            await self.svc.require_active(manager.id)
        self.assertEqual(ctx.exception.code, "not_manager")
        with self.assertRaises(self.E):
            await self.svc.require_active(9999)

    async def test_blocked_manager_is_stopped_immediately(self):
        manager = await self.manager(can_accept_cash=True)
        await self.cash_issue(manager)
        await self.svc.set_status(manager.id, "blocked", admin_id=1)

        with self.assertRaises(self.E) as ctx:
            await self.cash_issue(manager, nonce="n2")
        self.assertEqual(ctx.exception.code, "blocked")

    async def test_unblocked_manager_works_again(self):
        manager = await self.manager(can_accept_cash=True)
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        await self.svc.set_status(manager.id, "active", admin_id=1)
        result = await self.cash_issue(manager)
        self.assertEqual(result.status, "completed")

    async def test_deleted_manager_is_gone_but_journal_stays(self):
        manager = await self.manager(can_accept_cash=True)
        await self.cash_issue(manager)
        await self.svc.set_status(manager.id, "deleted", admin_id=1)

        with self.assertRaises(self.E):
            await self.svc.require_active(manager.id)
        journal = await self.svc.admin_history(manager.id)
        self.assertEqual(len(journal), 1)

    async def test_missing_rights_refuse_each_action(self):
        manager = await self.manager(can_issue_tariff=False, can_issue_custom=False, can_issue_temp=False)
        with self.assertRaises(self.E) as ctx:
            await self.svc.quote(manager.id, product="tariff", tariff_id=2)
        self.assertEqual(ctx.exception.code, "no_right")
        with self.assertRaises(self.E):
            await self.svc.quote(manager.id, product="custom", days=10)
        with self.assertRaises(self.E):
            await self.svc.issue_temp(manager.id, "t1")

    async def test_cash_requires_explicit_right(self):
        manager = await self.manager()  # can_accept_cash по умолчанию выключено
        with self.assertRaises(self.E) as ctx:
            await self.cash_issue(manager)
        self.assertEqual(ctx.exception.code, "cash_not_allowed")
        self.assertEqual(await self.count_offline_clients(), 0)

    async def test_global_stats_need_a_separate_right(self):
        manager = await self.manager()
        with self.assertRaises(self.E):
            await self.svc.global_stats(manager.id)
        await self.env.repos.managers.update_rights(manager.id, can_view_global_stats=True)
        stats = await self.svc.global_stats(manager.id)
        self.assertEqual(stats.clients, 0)

    async def test_rights_change_applies_to_the_very_next_call(self):
        manager = await self.manager(can_accept_cash=True)
        await self.cash_issue(manager)
        await self.svc.update_rights(manager.id, admin_id=1, can_accept_cash=False)
        with self.assertRaises(self.E):
            await self.cash_issue(manager, nonce="n2")

    async def test_one_telegram_account_is_one_manager(self):
        await self.manager(telegram_id=77)
        manager2, token = await self.svc.invite("Второй", admin_id=1)
        with self.assertRaises(self.E):
            await self.svc.accept_invite(token, 77)


class InviteTests(EnvTestCase):
    async def test_invite_is_single_use(self):
        manager, token = await self.svc.invite("Иван", admin_id=1)
        await self.svc.accept_invite(token, 10)
        with self.assertRaises(self.E):
            await self.svc.accept_invite(token, 11)

    async def test_expired_invite_is_refused(self):
        manager, token = await self.svc.invite("Иван", admin_id=1)
        from sqlalchemy import update
        from db import Manager
        async with self.env.session_maker() as session:
            await session.execute(update(Manager).where(Manager.id == manager.id)
                                  .values(invite_expires_at=datetime.datetime.now() - datetime.timedelta(minutes=1)))
            await session.commit()
        with self.assertRaises(self.E):
            await self.svc.accept_invite(token, 10)

    async def test_wrong_token_is_refused(self):
        await self.svc.invite("Иван", admin_id=1)
        with self.assertRaises(self.E):
            await self.svc.accept_invite("not-a-token", 10)

    async def test_token_is_not_stored_in_clear(self):
        manager, token = await self.svc.invite("Иван", admin_id=1)
        stored = await self.env.repos.managers.get(manager.id)
        self.assertNotEqual(stored.invite_token_hash, token)
        self.assertEqual(len(stored.invite_token_hash), 64)

    async def test_reinvite_invalidates_old_link(self):
        manager, old = await self.svc.invite("Иван", admin_id=1)
        new = await self.svc.reinvite(manager.id)
        with self.assertRaises(self.E):
            await self.svc.accept_invite(old, 10)
        await self.svc.accept_invite(new, 10)


class WebLoginTests(EnvTestCase):
    PASSWORD = "Секрет-2026"

    async def code_of(self, coro):
        with self.assertRaises(self.E) as ctx:
            await coro
        return ctx.exception.code

    async def test_invite_sets_a_normalized_login(self):
        manager, _ = await self.svc.invite("Иван", admin_id=1, login="  Ivan.P ")
        self.assertEqual(manager.login, "ivan.p")
        self.assertFalse(manager.has_password)

    async def test_bad_and_taken_logins_are_refused_before_creating(self):
        await self.svc.invite("Иван", admin_id=1, login="ivan")
        for raw, code in (("IVAN", "login_taken"), ("ив", "bad_login"), ("иван", "bad_login"),
                          ("1abc", "bad_login"), ("a b", "bad_login"), ("x" * 33, "bad_login")):
            self.assertEqual(await self.code_of(self.svc.invite("Другой", admin_id=1, login=raw)), code, raw)
        self.assertEqual(len(await self.svc.list_managers()), 1)

    async def test_admin_changes_login(self):
        a = await self.manager(login="ivan", telegram_id=1)
        b = await self.manager(login="petr", telegram_id=2)
        self.assertEqual(await self.code_of(self.svc.set_login(b.id, "ivan", admin_id=1)), "login_taken")
        self.assertEqual(await self.svc.set_login(a.id, "Ivan", admin_id=1), "ivan")  # свой логин — не «занят»
        self.assertEqual(await self.svc.set_login(b.id, "petr.n", admin_id=1), "petr.n")

    async def test_password_link_needs_a_login(self):
        manager = await self.manager()
        self.assertEqual(await self.code_of(self.svc.create_password_link(manager.id)), "bad_login")

    async def test_password_link_lifecycle(self):
        manager = await self.manager(login="ivan")
        token = await self.svc.create_password_link(manager.id)
        for _ in range(3):  # показ формы ссылку не гасит
            self.assertEqual((await self.svc.password_link_owner(token)).id, manager.id)
        # плохой пароль ссылку не сжигает
        self.assertEqual(await self.code_of(self.svc.set_password_by_link(token, "short")), "bad_password")
        self.assertEqual(await self.code_of(self.svc.set_password_by_link(token, "ivan12345")), "bad_password")
        view = await self.svc.set_password_by_link(token, self.PASSWORD)
        self.assertTrue(view.has_password)
        self.assertEqual(await self.code_of(self.svc.set_password_by_link(token, self.PASSWORD)), "invalid_password_link")
        self.assertIsNone(await self.svc.password_link_owner(token))
        self.assertIsNone(await self.svc.password_link_owner(""))

    async def test_password_is_stored_as_argon2_hash(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        stored = await self.env.repos.managers.get(manager.id)
        self.assertTrue(stored.password_hash.startswith("$argon2"))
        self.assertNotIn(self.PASSWORD, stored.password_hash)
        self.assertIsNone(stored.login_token_hash)

    async def test_new_link_replaces_the_old_one(self):
        manager = await self.manager(login="ivan")
        old = await self.svc.create_password_link(manager.id)
        new = await self.svc.create_password_link(manager.id)
        self.assertIsNone(await self.svc.password_link_owner(old))
        self.assertIsNotNone(await self.svc.password_link_owner(new))

    async def test_setting_a_password_ends_web_sessions(self):
        manager = await self.manager(login="ivan")
        before = await self.svc.session_version(manager.id)
        await self.svc.set_password_by_link(await self.svc.create_password_link(manager.id), self.PASSWORD)
        self.assertGreater(await self.svc.session_version(manager.id), before)
        self.assertTrue(any("Пароль" in text for _, text in self.env.notifier.manager_texts))

    async def test_authenticate(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        self.assertEqual((await self.svc.authenticate(" IVAN ", self.PASSWORD)).id, manager.id)
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", "wrong-pass")), "bad_credentials")
        self.assertEqual(await self.code_of(self.svc.authenticate("nobody", self.PASSWORD)), "bad_credentials")
        self.assertEqual(await self.code_of(self.svc.authenticate("", "")), "bad_credentials")

    async def test_manager_without_password_cannot_log_in(self):
        await self.manager(login="ivan")
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", "")), "bad_credentials")
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", "anything-1")), "bad_credentials")

    async def test_lockout_after_five_failures(self):
        await self.manager(login="ivan", password=self.PASSWORD)
        for _ in range(4):
            self.assertEqual(await self.code_of(self.svc.authenticate("ivan", "wrong-pass")), "bad_credentials")
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", "wrong-pass")), "login_locked")
        # даже верный пароль не пускает, пока действует блокировка
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", self.PASSWORD)), "login_locked")
        self.assertTrue(any("неверных попыток" in text for _, text in self.env.notifier.manager_texts))

    async def test_lock_expires_and_success_resets_the_counter(self):
        import datetime
        from sqlalchemy import update
        from db import Manager
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        for _ in range(5):
            with self.assertRaises(self.E):
                await self.svc.authenticate("ivan", "wrong-pass")
        async with self.env.session_maker() as session:
            await session.execute(update(Manager).where(Manager.id == manager.id).values(
                locked_until=datetime.datetime.now() - datetime.timedelta(seconds=1)))
            await session.commit()
        self.assertEqual((await self.svc.authenticate("ivan", self.PASSWORD)).id, manager.id)
        stored = await self.env.repos.managers.get(manager.id)
        self.assertEqual((stored.failed_logins, stored.locked_until), (0, None))

    async def test_blocked_status_is_revealed_only_with_the_right_password(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", "wrong-pass")), "bad_credentials")
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", self.PASSWORD)), "blocked")

    async def test_deleting_frees_the_login(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        await self.svc.set_status(manager.id, "deleted", admin_id=1)
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", self.PASSWORD)), "bad_credentials")
        again, _ = await self.svc.invite("Новый Иван", admin_id=1, login="ivan")
        self.assertEqual(again.login, "ivan")

    async def test_change_password(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        before = await self.svc.session_version(manager.id)
        self.assertEqual(await self.code_of(self.svc.change_password(manager.id, "wrong-pass", "Новый-пароль-1")), "bad_password")
        self.assertEqual(await self.code_of(self.svc.change_password(manager.id, self.PASSWORD, self.PASSWORD)), "bad_password")
        self.assertEqual(await self.code_of(self.svc.change_password(manager.id, self.PASSWORD, "123")), "bad_password")
        version = await self.svc.change_password(manager.id, self.PASSWORD, "Новый-пароль-1")
        self.assertGreater(version, before)
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", self.PASSWORD)), "bad_credentials")
        await self.svc.authenticate("ivan", "Новый-пароль-1")

    async def test_admin_reset(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        before = await self.svc.session_version(manager.id)
        token = await self.svc.reset_password(manager.id, admin_id=1)
        self.assertGreater(await self.svc.session_version(manager.id), before)
        self.assertFalse((await self.svc.view(manager.id)).has_password)
        self.assertEqual(await self.code_of(self.svc.authenticate("ivan", self.PASSWORD)), "bad_credentials")
        await self.svc.set_password_by_link(token, "Другой-пароль-2")
        await self.svc.authenticate("ivan", "Другой-пароль-2")

    async def test_reset_of_blocked_manager_gives_no_link(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        self.assertIsNone(await self.svc.reset_password(manager.id, admin_id=1))

    async def test_blocked_manager_cannot_get_a_password_link(self):
        manager = await self.manager(login="ivan")
        token = await self.svc.create_password_link(manager.id)
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        self.assertIsNone(await self.svc.password_link_owner(token))
        with self.assertRaises(self.E):
            await self.svc.create_password_link(manager.id)

    async def test_end_web_sessions(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        before = await self.svc.session_version(manager.id)
        await self.svc.end_web_sessions(manager.id)
        self.assertGreater(await self.svc.session_version(manager.id), before)
        await self.svc.authenticate("ivan", self.PASSWORD)  # пароль остался

    async def test_blocking_bumps_session_version(self):
        manager = await self.manager()
        before = (await self.env.repos.managers.get(manager.id)).session_version
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        after = (await self.env.repos.managers.get(manager.id)).session_version
        self.assertGreater(after, before)

    async def test_web_login_notification(self):
        manager = await self.manager(login="ivan", password=self.PASSWORD)
        view = await self.svc.authenticate("ivan", self.PASSWORD)
        await self.svc.notify_web_login(view, "1.2.3.4", "Chrome · Windows")
        self.assertEqual(self.env.notifier.web_logins[-1]["to"], 1001)
        self.assertEqual(self.env.notifier.web_logins[-1]["ip"], "1.2.3.4")


# =============================================================================
# Приватность клиента (ТЗ п.7)
# =============================================================================

class PrivacyTests(EnvTestCase):
    ALLOWED_CARD_FIELDS = {
        "client_code", "label", "subscription_active", "subscription_end", "days_left", "devices_used",
        "devices_limit", "device_platforms", "traffic_used_gb", "traffic_limit_gb", "extra_devices",
        "extra_traffic_gb", "has_key", "access_until", "operations",
    }
    FORBIDDEN = {"user_id", "telegram_id", "username", "email", "full_name", "password_hash", "hwid", "ip",
                 "payments", "referrer_id", "support_topic_id", "subscription_url", "card", "yookassa"}

    async def _telegram_client_with_access(self, manager):
        user = await self.env.make_telegram_client(555, is_first_payment_made=True)
        code = await self.svc.create_access_code(user.user_id)
        client_code = await self.svc.add_client_by_code(manager.id, code)
        return user, client_code

    async def test_client_card_has_exactly_the_whitelisted_fields(self):
        names = {f.name for f in dataclasses.fields(self.env.module.ClientCard)}
        self.assertEqual(names, self.ALLOWED_CARD_FIELDS)
        self.assertFalse(names & self.FORBIDDEN)

    async def test_operation_brief_has_no_client_identity_beyond_the_code(self):
        names = {f.name for f in dataclasses.fields(self.env.module.OperationBrief)}
        self.assertFalse(names & self.FORBIDDEN)
        self.assertIn("client_code", names)

    async def test_card_of_telegram_client_hides_telegram_identity(self):
        manager = await self.manager()
        user, client_code = await self._telegram_client_with_access(manager)
        card = await self.svc.get_client_card(manager.id, client_code)

        flattened = repr(card)
        for secret in (str(user.user_id), "real_username", "Настоящее Имя", "SECRET-HWID", "1.2.3.4"):
            self.assertNotIn(secret, flattened)
        self.assertEqual(card.client_code, client_code)

    async def test_card_shows_device_platforms_but_not_hwid_or_ip(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)
        card = await self.svc.get_client_card(manager.id, result.client_code)
        self.assertEqual(card.device_platforms, ("Android",))
        self.assertNotIn("SECRET-HWID", repr(card))

    async def test_client_list_row_has_no_telegram_data(self):
        manager = await self.manager()
        user, client_code = await self._telegram_client_with_access(manager)
        rows, total = await self.svc.list_clients(manager.id)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].client_code, client_code)
        self.assertNotIn(str(user.user_id), repr(rows))
        self.assertNotIn("real_username", repr(rows))

    async def test_install_link_view_is_logged(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)
        url = await self.svc.get_install_link(manager.id, result.client_code)
        self.assertIn("SECRET", url)
        history = await self.svc.history(manager.id)
        self.assertIn("key_view", [h.op_type for h in history])

    async def test_history_does_not_expose_other_managers_operations(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        await self.cash_issue(a, nonce="a1")
        await self.cash_issue(b, nonce="b1")
        self.assertEqual(len(await self.svc.history(a.id)), 1)
        self.assertEqual(len(await self.svc.history(b.id)), 1)


class ClientAccessTests(EnvTestCase):
    async def test_other_managers_client_is_invisible(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        result = await self.cash_issue(a)

        with self.assertRaises(self.E) as ctx:
            await self.svc.get_client_card(b.id, result.client_code)
        self.assertEqual(ctx.exception.code, "client_not_found")
        with self.assertRaises(self.E):
            await self.svc.get_install_link(b.id, result.client_code)
        with self.assertRaises(self.E):
            await self.svc.quote(b.id, product="tariff", tariff_id=2, client_code=result.client_code)

    async def test_unknown_code_looks_the_same_as_foreign_code(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(telegram_id=2)
        result = await self.cash_issue(a)
        with self.assertRaises(self.E) as foreign:
            await self.svc.get_client_card(b.id, result.client_code)
        with self.assertRaises(self.E) as unknown:
            await self.svc.get_client_card(b.id, "ZZZZZZ")
        self.assertEqual(foreign.exception.code, unknown.exception.code)
        self.assertEqual(foreign.exception.message, unknown.exception.message)

    async def test_access_code_is_one_time(self):
        a = await self.manager(telegram_id=1)
        b = await self.manager(telegram_id=2)
        user = await self.env.make_telegram_client(555)
        code = await self.svc.create_access_code(user.user_id)

        await self.svc.add_client_by_code(a.id, code)
        with self.assertRaises(self.E) as ctx:
            await self.svc.add_client_by_code(b.id, code)
        self.assertEqual(ctx.exception.code, "bad_code")

    async def test_expired_access_code_is_refused(self):
        a = await self.manager()
        user = await self.env.make_telegram_client(555)
        code = await self.svc.create_access_code(user.user_id)
        from sqlalchemy import update
        from db import ClientAccessCode
        async with self.env.session_maker() as session:
            await session.execute(update(ClientAccessCode).values(
                expires_at=datetime.datetime.now() - datetime.timedelta(seconds=1)))
            await session.commit()
        with self.assertRaises(self.E):
            await self.svc.add_client_by_code(a.id, code)

    async def test_new_code_kills_the_previous_one(self):
        a = await self.manager()
        user = await self.env.make_telegram_client(555)
        old = await self.svc.create_access_code(user.user_id)
        new = await self.svc.create_access_code(user.user_id)
        with self.assertRaises(self.E):
            await self.svc.add_client_by_code(a.id, old)
        await self.svc.add_client_by_code(a.id, new)

    async def test_wrong_codes_are_rate_limited(self):
        a = await self.manager()
        for _ in range(5):
            with self.assertRaises(self.E) as ctx:
                await self.svc.add_client_by_code(a.id, "ABCDEF")
            self.assertEqual(ctx.exception.code, "bad_code")
        with self.assertRaises(self.E) as ctx:
            await self.svc.add_client_by_code(a.id, "ABCDEF")
        self.assertEqual(ctx.exception.code, "code_rate_limited")

    async def test_rate_limit_also_blocks_a_correct_code(self):
        a = await self.manager()
        user = await self.env.make_telegram_client(555)
        code = await self.svc.create_access_code(user.user_id)
        for _ in range(5):
            with self.assertRaises(self.E):
                await self.svc.add_client_by_code(a.id, "ABCDEF")
        with self.assertRaises(self.E) as ctx:
            await self.svc.add_client_by_code(a.id, code)
        self.assertEqual(ctx.exception.code, "code_rate_limited")

    async def test_malformed_input_counts_as_an_attempt(self):
        a = await self.manager()
        for junk in ("123456", "a@b.c", "", "привет"):
            with self.assertRaises(self.E):
                await self.svc.add_client_by_code(a.id, junk)
        self.assertEqual(await self.env.repos.access.count_failed(
            a.id, datetime.datetime.now() - datetime.timedelta(hours=1)), 4)

    async def test_client_is_notified_and_can_revoke(self):
        a = await self.manager()
        user = await self.env.make_telegram_client(555)
        code = await self.svc.create_access_code(user.user_id)
        client_code = await self.svc.add_client_by_code(a.id, code)
        self.assertEqual(self.env.notifier.client_access, [(555, "Иван Петров")])

        self.assertEqual(await self.svc.revoke_access_by_client(555), 1)
        with self.assertRaises(self.E):
            await self.svc.get_client_card(a.id, client_code)

    async def test_expired_access_hides_the_client(self):
        a = await self.manager()
        user = await self.env.make_telegram_client(555)
        client_code = await self.svc.add_client_by_code(a.id, await self.svc.create_access_code(user.user_id))
        from sqlalchemy import update
        from db import ManagerClient
        async with self.env.session_maker() as session:
            await session.execute(update(ManagerClient).values(
                access_until=datetime.datetime.now() - datetime.timedelta(seconds=1)))
            await session.commit()
        with self.assertRaises(self.E):
            await self.svc.get_client_card(a.id, client_code)

    async def test_access_grant_is_journaled(self):
        a = await self.manager()
        user = await self.env.make_telegram_client(555)
        await self.svc.add_client_by_code(a.id, await self.svc.create_access_code(user.user_id))
        self.assertIn("access_grant", [h.op_type for h in await self.svc.history(a.id)])


# =============================================================================
# Выдача за наличные
# =============================================================================

class CashIssueTests(EnvTestCase):
    async def test_new_client_tariff_cash(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.price, 149.0)
        self.assertIn("SECRET", result.subscription_url)
        self.assertIsNotNone(result.cabinet_url)
        self.assertTrue(result.cabinet_url.startswith("https://example.com/c/"))

        user = await self.env.repos.users.get_by_client_code(result.client_code)
        self.assertEqual(user.origin, "offline")
        self.assertLess(user.user_id, -1_000_000_000)
        self.assertFalse(user.is_active)  # в Telegram не писать
        self.assertTrue(user.is_first_payment_made)
        self.assertEqual(user.acquired_by_manager_id, manager.id)
        self.assertGreater(user.subscription_end_date, datetime.datetime.now() + datetime.timedelta(days=29))
        # 500 ГБ — базовая квота тарифа без своего лимита
        self.assertEqual(self.env.subscription.calls[-1][1:], (30, 500))

    async def test_payment_and_journal_agree(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)

        op = await self.env.repos.ops.get(result.operation_id)
        payment = await self.env.repos.payments.get_by_yookassa_id(op.payment_id)
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.source, "cash")
        self.assertEqual(payment.manager_id, manager.id)
        self.assertEqual(payment.final_amount, op.price)
        self.assertEqual(op.status, "completed")

    async def test_journal_has_every_field_the_spec_asks_for(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)
        op = await self.env.repos.ops.get(result.operation_id)

        self.assertEqual(op.manager_id, manager.id)
        self.assertIsNotNone(op.created_at)
        self.assertEqual(op.op_type, "issue_tariff")
        self.assertEqual(op.client_code, result.client_code)
        self.assertIsNotNone(op.client_user_id)
        self.assertEqual(op.tariff_name, "Месяц")
        self.assertEqual(op.days, 30)
        self.assertIsNotNone(op.key_expires_at)
        self.assertEqual(op.price, 149.0)
        self.assertTrue(op.key_username.startswith("off_"))
        self.assertEqual(len(op.key_fingerprint), 4)

    async def test_cash_goes_to_outstanding_and_group_receipt_is_posted(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)

        total, count = await self.svc.cash_outstanding(manager.id)
        self.assertEqual((total, count), (149.0, 1))
        op_id, group_text = self.env.notifier.group[-1]
        self.assertEqual(op_id, result.operation_id)
        self.assertNotIn("SECRET", group_text)       # ссылки в группе нет
        self.assertNotIn("http", group_text)
        self.assertIn("M-000001", group_text)
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertIsNotNone(op.receipt_message_id)  # чтобы можно было дописать «ключ удалён»

    async def test_manager_gets_the_link_in_his_own_receipt(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)
        self.assertIn("SECRET", result.receipt_text)

    async def test_idempotency_double_tap_issues_one_key(self):
        manager = await self.manager(can_accept_cash=True)
        first = await self.cash_issue(manager, nonce="same")
        second = await self.cash_issue(manager, nonce="same")

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.operation_id, first.operation_id)
        self.assertEqual(await self.count_offline_clients(), 1)
        self.assertEqual(len(self.env.subscription.calls), 1)
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 149.0)

    async def test_same_nonce_from_another_manager_is_not_a_replay(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        ra = await self.cash_issue(a, nonce="same")
        rb = await self.cash_issue(b, nonce="same")
        self.assertNotEqual(ra.operation_id, rb.operation_id)

    async def test_custom_days_price_and_payment_kind(self):
        manager = await self.manager(can_accept_cash=True)
        quote = await self.svc.quote(manager.id, product="custom", days=45)
        result = await self.svc.issue(manager.id, product="custom", days=45, method="cash",
                                      idempotency_nonce="c1", expected_total=quote.total)

        self.assertEqual(result.price, 279.0)
        self.assertEqual(self.env.subscription.calls[-1][1:], (45, 500))
        op = await self.env.repos.ops.get(result.operation_id)
        payment = await self.env.repos.payments.get_by_yookassa_id(op.payment_id)
        self.assertEqual((payment.kind, payment.days, payment.tariff_id), ("custom", 45, None))
        self.assertEqual(op.op_type, "issue_custom")
        self.assertEqual(op.tariff_name, "Свои дни")

    async def test_custom_days_bounds(self):
        manager = await self.manager(can_accept_cash=True)
        for bad in (0, -3, 366, None, 1.5):
            with self.assertRaises(self.E) as ctx:
                await self.svc.quote(manager.id, product="custom", days=bad)
            self.assertEqual(ctx.exception.code, "bad_days", bad)

    async def test_quote_shows_cheaper_standard_tariff(self):
        manager = await self.manager()
        quote = await self.svc.quote(manager.id, product="custom", days=25)
        self.assertIsNotNone(quote.hint)
        self.assertEqual(quote.hint.name, "Месяц")

    async def test_price_changed_between_preview_and_confirm(self):
        manager = await self.manager(can_accept_cash=True)
        quote = await self.svc.quote(manager.id, product="tariff", tariff_id=2)
        await self.env.repos.settings.set("custom_day_price", 9)  # на тариф не влияет
        await self.env.repos.tariffs.update_field(2, "price", 199)

        with self.assertRaises(self.E) as ctx:
            await self.cash_issue(manager, expected_total=quote.total)
        self.assertEqual(ctx.exception.code, "price_changed")
        self.assertEqual(await self.count_offline_clients(), 0)

    async def test_hidden_or_intro_tariff_is_not_sold(self):
        manager = await self.manager(can_accept_cash=True)
        await self.env.repos.tariffs.update_field(2, "is_active", False)
        with self.assertRaises(self.E) as ctx:
            await self.cash_issue(manager, tariff_id=2)
        self.assertEqual(ctx.exception.code, "bad_tariff")
        await self.env.repos.tariffs.update_field(1, "is_intro", True)
        with self.assertRaises(self.E):
            await self.cash_issue(manager, nonce="n2", tariff_id=1)
        with self.assertRaises(self.E):
            await self.cash_issue(manager, nonce="n3", tariff_id=999)

    async def test_validation_errors_do_not_leave_orphan_clients(self):
        manager = await self.manager(can_accept_cash=True)
        for kwargs in (dict(tariff_id=999), dict(product="custom", days=0)):
            with self.assertRaises(self.E):
                await self.cash_issue(manager, nonce=str(kwargs), **kwargs)
        self.assertEqual(await self.count_offline_clients(), 0)

    async def test_failed_operation_is_journaled(self):
        manager = await self.manager(can_accept_cash=True)
        with self.assertRaises(self.E):
            await self.cash_issue(manager, tariff_id=999)
        history = await self.svc.history(manager.id)
        self.assertEqual([(h.status) for h in history], ["failed"])

    async def test_cash_limit(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=200)
        await self.cash_issue(manager, nonce="n1")           # 149 из 200
        with self.assertRaises(self.E) as ctx:
            await self.cash_issue(manager, nonce="n2")       # +149 > 200
        self.assertEqual(ctx.exception.code, "cash_limit")

    async def test_settlement_frees_the_limit(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=200)
        await self.cash_issue(manager, nonce="n1")
        await self.svc.settle(manager.id, admin_id=1)
        result = await self.cash_issue(manager, nonce="n2")
        self.assertEqual(result.status, "completed")

    async def test_unlimited_cash_when_limit_is_null(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=None)
        for i in range(4):
            await self.cash_issue(manager, nonce=f"n{i}")
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 4 * 149.0)

    async def test_existing_client_extras_are_carried_into_the_price(self):
        manager = await self.manager(can_accept_cash=True)
        first = await self.cash_issue(manager, nonce="n1")
        from sqlalchemy import update
        from db import User
        async with self.env.session_maker() as session:
            await session.execute(update(User).where(User.client_code == first.client_code)
                                  .values(extra_devices=2, extra_traffic_gb=200))
            await session.commit()

        quote = await self.svc.quote(manager.id, product="tariff", tariff_id=2, client_code=first.client_code)
        # 149 тариф + 2 слота × 49 × 1 мес + 2 пакета × 49 × 1 мес
        self.assertEqual(quote.total, 149 + 98 + 98)
        self.assertEqual((quote.slots, quote.packs), (2, 2))

        result = await self.cash_issue(manager, nonce="n2", client_code=first.client_code)
        self.assertEqual(result.price, 345.0)
        # квота 500 + 200 докупленных уходит в панель одним числом
        self.assertEqual(self.env.subscription.calls[-1][2], 700)

    async def test_renewing_an_existing_client_keeps_the_same_panel_user(self):
        manager = await self.manager(can_accept_cash=True)
        first = await self.cash_issue(manager, nonce="n1")
        second = await self.cash_issue(manager, nonce="n2", client_code=first.client_code)
        self.assertEqual(second.client_code, first.client_code)
        self.assertEqual(await self.count_offline_clients(), 1)
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_unlimited_quota_tariff_has_no_traffic_extras(self):
        env = await build_env(tariffs=[dict(name="Безлимит", price=499, duration_days=30, data_limit_gb=0)])
        try:
            manager = await env.make_manager(can_accept_cash=True)
            result = await env.service.issue(manager.id, product="tariff", tariff_id=1, method="cash",
                                             idempotency_nonce="u1")
            self.assertEqual(env.subscription.calls[-1][2], 0)
            self.assertEqual(result.price, 499.0)
        finally:
            await env.engine.dispose()

    async def test_panel_outage_does_not_leave_a_pending_payment(self):
        manager = await self.manager(can_accept_cash=True)

        async def boom(*a, **k):
            raise RuntimeError("panel down")
        self.env.subscription.extend = boom

        with self.assertRaises(self.E):
            await self.cash_issue(manager)
        self.assertIsNone(await self.env.repos.payments.get_user_pending(
            (await self.env.repos.users.get_by_client_code(
                (await self.svc.history(manager.id))[0].client_code)).user_id))
        op = (await self.svc.history(manager.id))[0]
        self.assertEqual(op.status, "failed")
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 0)  # неудача в «к сдаче» не попадает


# =============================================================================
# Онлайн-оплата по QR
# =============================================================================

class OnlineIssueTests(EnvTestCase):
    async def online_issue(self, manager, nonce="o1", **kwargs):
        kwargs.setdefault("product", "tariff")
        kwargs.setdefault("tariff_id", 2)
        return await self.svc.issue(manager.id, method="online", idempotency_nonce=nonce, **kwargs)

    async def test_issue_creates_pending_payment_and_gives_no_key_yet(self):
        manager = await self.manager()
        result = await self.online_issue(manager)

        self.assertEqual(result.status, "pending_payment")
        self.assertTrue(result.payment_url.startswith("https://pay.example/"))
        self.assertIsNone(result.subscription_url)
        self.assertEqual(self.env.subscription.calls, [])   # пока не оплатили — ничего не выдано
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 0)

        kwargs = self.env.created_payments[0]
        self.assertTrue(kwargs["save_payment_method"])       # автопродление по умолчанию включено
        self.assertEqual(kwargs["amount"], 149.0)
        self.assertEqual(kwargs["metadata"]["manager_id"], str(manager.id))
        payment = await self.env.repos.payments.get_by_yookassa_id("yk-1")
        self.assertEqual((payment.source, payment.manager_id, payment.status), ("manager", manager.id, "pending"))

    async def test_autorenew_is_off_when_the_shop_cannot_save_cards(self):
        env = await build_env(save_card=False)
        try:
            manager = await env.make_manager()
            await env.service.issue(manager.id, product="tariff", tariff_id=2, method="online",
                                    idempotency_nonce="o1")
            self.assertFalse(env.created_payments[0]["save_payment_method"])
        finally:
            await env.engine.dispose()

    async def test_quote_carries_the_consent_text(self):
        manager = await self.manager()
        quote = await self.svc.quote(manager.id, product="tariff", tariff_id=2)
        self.assertIn("автоматически", quote.renew_text)
        self.assertIn("149", quote.renew_text)

    async def test_custom_days_consent_names_the_renew_tariff(self):
        manager = await self.manager()
        quote = await self.svc.quote(manager.id, product="custom", days=10)
        self.assertIn("149", quote.renew_text)  # тариф, ближайший к 30 дням

    async def test_webhook_completes_the_operation_and_posts_receipts(self):
        manager = await self.manager()
        result = await self.online_issue(manager)
        # вебхук: платёж обработан конвейером, затем хук менеджера
        processed = await self.env.payments.process_successful_payment("yk-1", 149.0)
        self.assertIsNotNone(processed)
        await self.svc.on_payment_succeeded("yk-1")

        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.status, "completed")
        self.assertIsNotNone(op.key_username)
        self.assertTrue(any("SECRET" in m["text"] for m in self.env.notifier.manager_msgs))
        self.assertFalse(any("SECRET" in text for _, text in self.env.notifier.group))
        self.assertIn("(новый)", self.env.notifier.group[-1][1])

    async def test_webhook_hook_is_idempotent(self):
        manager = await self.manager()
        await self.online_issue(manager)
        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")
        count = len(self.env.notifier.manager_msgs)
        await self.svc.on_payment_succeeded("yk-1")
        self.assertEqual(len(self.env.notifier.manager_msgs), count)

    async def test_second_invoice_for_the_same_client_is_blocked(self):
        manager = await self.manager()
        first = await self.online_issue(manager)
        # завершим первую, чтобы у клиента появился код, и выставим второй счёт
        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")
        await self.online_issue(manager, nonce="o2", client_code=first.client_code)
        with self.assertRaises(self.E) as ctx:
            await self.online_issue(manager, nonce="o3", client_code=first.client_code)
        self.assertEqual(ctx.exception.code, "pending_payment")

    async def test_cancelled_invoice_cancels_the_operation(self):
        manager = await self.manager()
        result = await self.online_issue(manager)
        await self.env.repos.payments.update_status("yk-1", "cancelled")

        self.assertEqual(await self.svc.sync_pending_operations(), 1)
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.status, "cancelled")
        self.assertIn("Отменено", self.env.notifier.group[-1][1])

    async def test_sync_completes_paid_operation_if_webhook_was_lost(self):
        manager = await self.manager()
        result = await self.online_issue(manager)
        await self.env.payments.process_successful_payment("yk-1", 149.0)   # деньги пришли, хук не вызвали

        self.assertEqual(await self.svc.sync_pending_operations(), 1)
        self.assertEqual((await self.env.repos.ops.get(result.operation_id)).status, "completed")

    async def test_manager_can_cancel_his_own_pending_invoice_only(self):
        a = await self.manager(telegram_id=1)
        b = await self.manager(telegram_id=2)
        result = await self.online_issue(a)
        self.assertFalse(await self.svc.cancel_pending(b.id, result.operation_id))
        self.assertTrue(await self.svc.cancel_pending(a.id, result.operation_id))
        self.assertEqual((await self.env.repos.ops.get(result.operation_id)).status, "cancelled")

    async def test_payment_gateway_failure_is_a_clean_error(self):
        manager = await self.manager()

        def broken(**kwargs):
            raise RuntimeError("gateway down")
        self.svc._create_payment = broken
        with self.assertRaises(self.E) as ctx:
            await self.online_issue(manager)
        self.assertEqual(ctx.exception.code, "payment_failed")

    async def test_online_custom_days_saves_card_for_a_regular_tariff(self):
        manager = await self.manager()
        await self.online_issue(manager, product="custom", tariff_id=None, days=20)
        await self.env.payments.process_successful_payment(
            "yk-1", self.env.created_payments[0]["amount"], payment_method=SimpleNamespace(saved=True))
        renew_id = self.env.cards.save_from_yookassa.await_args.kwargs["renew_tariff_id"]
        self.assertEqual(renew_id, 2)  # «Месяц» — ближайший к 30 дням


# =============================================================================
# Временные ключи
# =============================================================================

class TempKeyTests(EnvTestCase):
    async def test_issue_creates_a_limited_one_hour_key(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")

        self.assertIn("SECRET", result.subscription_url)
        user = next(iter(self.env.panel.users.values()))
        self.assertTrue(user["username"].startswith("tmp_"))
        self.assertEqual(user["trafficLimitBytes"], 5 * GIB)
        self.assertEqual(user["trafficLimitStrategy"], "NO_RESET")
        self.assertEqual(user["hwidDeviceLimit"], 1)
        self.assertIn(f"manager #{manager.id}", user["description"])
        lifetime = result.expires_at - datetime.datetime.now()
        self.assertTrue(datetime.timedelta(minutes=59) < lifetime <= datetime.timedelta(minutes=60))

    async def test_journal_and_group_receipt_for_temp_key(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual((op.op_type, op.price, op.payment_method), ("issue_temp", 0, "free"))
        self.assertIsNotNone(op.key_expires_at)
        _, group_text = self.env.notifier.group[-1]
        self.assertIn("Временный ключ", group_text)
        self.assertNotIn("SECRET", group_text)

    async def test_lifetime_follows_settings(self):
        env = await build_env(settings={"temp_key_minutes": 15, "temp_key_traffic_gb": 2})
        try:
            manager = await env.make_manager()
            result = await env.service.issue_temp(manager.id, "t1")
            user = next(iter(env.panel.users.values()))
            self.assertEqual(user["trafficLimitBytes"], 2 * GIB)
            self.assertLessEqual(result.expires_at - datetime.datetime.now(), datetime.timedelta(minutes=15))
        finally:
            await env.engine.dispose()

    async def test_daily_limit(self):
        manager = await self.manager(temp_keys_per_day=2)
        await self.svc.issue_temp(manager.id, "t1")
        await self.svc.issue_temp(manager.id, "t2")
        with self.assertRaises(self.E) as ctx:
            await self.svc.issue_temp(manager.id, "t3")
        self.assertEqual(ctx.exception.code, "temp_limit")
        self.assertEqual(len(self.env.panel.users), 2)

    async def test_idempotent_double_tap(self):
        manager = await self.manager()
        first = await self.svc.issue_temp(manager.id, "same")
        second = await self.svc.issue_temp(manager.id, "same")
        self.assertEqual(first.key_id, second.key_id)
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_panel_failure_is_journaled_and_does_not_count_against_the_limit(self):
        manager = await self.manager(temp_keys_per_day=1)
        self.env.panel.fail_create = True
        with self.assertRaises(self.E) as ctx:
            await self.svc.issue_temp(manager.id, "t1")
        self.assertEqual(ctx.exception.code, "panel")
        self.env.panel.fail_create = False
        result = await self.svc.issue_temp(manager.id, "t2")   # лимит 1, но первая попытка не удалась
        self.assertTrue(result.subscription_url)

    async def test_half_created_panel_user_is_rolled_back(self):
        manager = await self.manager()
        original = self.env.panel.update_user

        async def broken_update(*a, **k):
            raise RuntimeError("patch failed")
        self.env.panel.update_user = broken_update
        with self.assertRaises(self.E):
            await self.svc.issue_temp(manager.id, "t1")
        self.assertEqual(self.env.panel.users, {})      # оставшийся без лимита устройств ключ не живёт
        self.env.panel.update_user = original

    async def test_expired_key_is_deleted_and_receipt_updated(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")
        await self._age(result.key_id)

        deleted, failed = await self.svc.expire_temp_keys()

        self.assertEqual((deleted, failed), (1, 0))
        self.assertEqual(self.env.panel.users, {})
        key = await self.env.repos.temps.get(result.key_id)
        self.assertEqual(key.status, "deleted")
        self.assertIn("🗑 Ключ удалён", self.env.notifier.group[-1][1])
        # правим то же сообщение в группе, а не шлём новое
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.receipt_message_id, 1001)

    async def test_live_key_is_not_touched(self):
        manager = await self.manager()
        await self.svc.issue_temp(manager.id, "t1")
        self.assertEqual(await self.svc.expire_temp_keys(), (0, 0))
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_already_deleted_in_panel_counts_as_success(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")
        self.env.panel.users.clear()
        await self._age(result.key_id)
        self.assertEqual(await self.svc.expire_temp_keys(), (1, 0))

    async def test_network_failure_is_retried_next_run(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")
        await self._age(result.key_id)
        self.env.panel.fail_delete_with = RuntimeError("timeout")

        self.assertEqual(await self.svc.expire_temp_keys(), (0, 1))
        self.assertEqual((await self.env.repos.temps.get(result.key_id)).status, "active")

        self.env.panel.fail_delete_with = None
        self.assertEqual(await self.svc.expire_temp_keys(), (1, 0))

    async def test_server_error_from_panel_is_retried_not_swallowed(self):
        manager = await self.manager()
        result = await self.svc.issue_temp(manager.id, "t1")
        await self._age(result.key_id)
        self.env.panel.fail_delete_with = RemnawaveAPIError(500, "boom")
        self.assertEqual(await self.svc.expire_temp_keys(), (0, 1))
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_list_shows_only_own_live_keys(self):
        a = await self.manager(telegram_id=1)
        b = await self.manager(telegram_id=2)
        await self.svc.issue_temp(a.id, "t1")
        await self.svc.issue_temp(b.id, "t2")
        self.assertEqual(len(await self.svc.list_temp_keys(a.id)), 1)

    async def _age(self, key_id):
        from sqlalchemy import update
        from db import TempKey
        async with self.env.session_maker() as session:
            await session.execute(update(TempKey).where(TempKey.id == key_id).values(
                expires_at=datetime.datetime.now() - datetime.timedelta(minutes=1)))
            await session.commit()


class TempConversionTests(EnvTestCase):
    async def test_cash_conversion_keeps_the_same_panel_key(self):
        manager = await self.manager(can_accept_cash=True)
        temp = await self.svc.issue_temp(manager.id, "t1")
        temp_username = next(iter(self.env.panel.users))

        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash",
                                      idempotency_nonce="c1", temp_key_id=temp.key_id)

        self.assertEqual(result.status, "completed")
        self.assertEqual(list(self.env.panel.users), [temp_username])     # новый пользователь не создан
        self.assertEqual(result.subscription_url, temp.subscription_url)  # клиенту не нужна переустановка
        user = await self.env.repos.users.get_by_client_code(result.client_code)
        self.assertEqual(user.vpn_username, temp_username)
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "converted")
        self.assertEqual(self.env.subscription.calls[-1][1:], (30, 500))
        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.op_type, "convert_temp")

    async def test_converted_key_is_never_deleted_by_the_job(self):
        manager = await self.manager(can_accept_cash=True)
        temp = await self.svc.issue_temp(manager.id, "t1")
        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash",
                             idempotency_nonce="c1", temp_key_id=temp.key_id)
        from sqlalchemy import update
        from db import TempKey
        async with self.env.session_maker() as session:
            await session.execute(update(TempKey).values(
                expires_at=datetime.datetime.now() - datetime.timedelta(hours=2)))
            await session.commit()
        self.assertEqual(await self.svc.expire_temp_keys(), (0, 0))
        self.assertEqual(len(self.env.panel.users), 1)

    async def test_conversion_grants_the_manager_access_to_the_new_client(self):
        manager = await self.manager(can_accept_cash=True)
        temp = await self.svc.issue_temp(manager.id, "t1")
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash",
                                      idempotency_nonce="c1", temp_key_id=temp.key_id)
        card = await self.svc.get_client_card(manager.id, result.client_code)
        self.assertTrue(card.subscription_active)

    async def test_cannot_convert_another_managers_key(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        temp = await self.svc.issue_temp(a.id, "t1")
        with self.assertRaises(self.E) as ctx:
            await self.svc.issue(b.id, product="tariff", tariff_id=2, method="cash",
                                 idempotency_nonce="c1", temp_key_id=temp.key_id)
        self.assertEqual(ctx.exception.code, "temp_not_found")
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "active")

    async def test_failed_conversion_returns_the_key_to_active(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=10)
        temp = await self.svc.issue_temp(manager.id, "t1")
        with self.assertRaises(self.E):
            await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash",
                                 idempotency_nonce="c1", temp_key_id=temp.key_id)
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "active")

    async def test_failed_conversion_can_be_retried(self):
        """После сбоя клиент-«сирота» не должен держать имя ключа в панели — иначе повтор упрётся в UNIQUE."""
        manager = await self.manager(can_accept_cash=True)
        temp = await self.svc.issue_temp(manager.id, "t1")
        real_extend = self.env.subscription.extend

        async def broken(*a, **k):
            raise RuntimeError("panel down")
        self.env.subscription.extend = broken
        with self.assertRaises(self.E):
            await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash",
                                 idempotency_nonce="c1", temp_key_id=temp.key_id)
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "active")

        self.env.subscription.extend = real_extend
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="cash",
                                      idempotency_nonce="c2", temp_key_id=temp.key_id)

        self.assertEqual(result.status, "completed")
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "converted")
        user = await self.env.repos.users.get_by_client_code(result.client_code)
        self.assertEqual(user.vpn_username, temp.subscription_url.split("/")[-1].removesuffix("-SECRET"))

    async def test_invalid_conversion_input_does_not_create_a_client(self):
        manager = await self.manager(can_accept_cash=True)
        temp = await self.svc.issue_temp(manager.id, "t1")
        with self.assertRaises(self.E):
            await self.svc.issue(manager.id, product="tariff", tariff_id=999, method="cash",
                                 idempotency_nonce="c1", temp_key_id=temp.key_id)
        self.assertEqual(await self.count_offline_clients(), 0)
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "active")

    async def test_failed_attempt_by_a_stranger_cannot_revert_a_running_conversion(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        temp = await self.svc.issue_temp(a.id, "t1")
        await self.env.repos.temps.transition(temp.key_id, "active", "converting")
        with self.assertRaises(self.E):
            await self.svc.issue(b.id, product="tariff", tariff_id=2, method="cash",
                                 idempotency_nonce="c1", temp_key_id=temp.key_id)
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "converting")

    async def test_online_conversion_waits_for_payment(self):
        manager = await self.manager()
        temp = await self.svc.issue_temp(manager.id, "t1")
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online",
                                      idempotency_nonce="c1", temp_key_id=temp.key_id)
        self.assertEqual(result.status, "pending_payment")
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "converting")

        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "converted")

    async def test_cancelled_online_conversion_releases_the_key(self):
        manager = await self.manager()
        temp = await self.svc.issue_temp(manager.id, "t1")
        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online",
                             idempotency_nonce="c1", temp_key_id=temp.key_id)
        await self.env.repos.payments.update_status("yk-1", "cancelled")
        await self.svc.sync_pending_operations()
        self.assertEqual((await self.env.repos.temps.get(temp.key_id)).status, "active")


# =============================================================================
# Инкассация и статистика
# =============================================================================

class SettlementAndStatsTests(EnvTestCase):
    async def test_settle_sums_unsettled_cash_and_resets(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=None)
        await self.cash_issue(manager, nonce="n1")
        await self.cash_issue(manager, nonce="n2", tariff_id=3)

        settlement = await self.svc.settle(manager.id, admin_id=9)

        self.assertEqual((settlement.amount, settlement.operations_count), (149.0 + 399.0, 2))
        self.assertEqual(await self.svc.cash_outstanding(manager.id), (0.0, 0))
        self.assertIsNone(await self.svc.settle(manager.id, admin_id=9))   # второй раз нечего

    async def test_settle_is_per_manager(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        await self.cash_issue(a, nonce="a1")
        await self.cash_issue(b, nonce="b1")
        await self.svc.settle(a.id, admin_id=9)
        self.assertEqual((await self.svc.cash_outstanding(b.id))[0], 149.0)

    async def test_online_payments_are_not_cash_to_hand_over(self):
        manager = await self.manager(can_accept_cash=True)
        await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="o1")
        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")
        self.assertEqual((await self.svc.cash_outstanding(manager.id))[0], 0)

    async def test_stats(self):
        manager = await self.manager(can_accept_cash=True, cash_limit=None)
        await self.cash_issue(manager, nonce="n1")
        await self.cash_issue(manager, nonce="n2", product="custom", tariff_id=None, days=45)
        await self.svc.issue_temp(manager.id, "t1")

        stats = await self.svc.stats(manager.id)

        self.assertEqual(stats.today.count, 3)
        self.assertEqual(stats.today.revenue, 149.0 + 279.0)
        self.assertEqual(stats.today.by_type["issue_custom"], 1)
        self.assertEqual(stats.cash_outstanding, 428.0)
        self.assertEqual(stats.clients, 2)
        self.assertEqual((stats.temp_total, stats.temp_converted), (1, 0))

    async def test_stats_are_per_manager(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        await self.cash_issue(a, nonce="a1")
        self.assertEqual((await self.svc.stats(b.id)).today.count, 0)

    async def test_failed_operations_are_not_revenue(self):
        manager = await self.manager(can_accept_cash=True)
        with self.assertRaises(self.E):
            await self.cash_issue(manager, tariff_id=999)
        self.assertEqual((await self.svc.stats(manager.id)).today.revenue, 0)

    async def test_admin_sees_full_journal(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(can_accept_cash=True, telegram_id=2)
        await self.cash_issue(a, nonce="a1")
        await self.cash_issue(b, nonce="b1")
        self.assertEqual(len(await self.svc.admin_history()), 2)
        self.assertEqual(len(await self.svc.admin_history(a.id)), 1)

    async def test_autorenew_off_is_journaled(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)
        self.assertTrue(await self.svc.disable_autorenew(manager.id, result.client_code))
        self.assertIn("autorenew_off", [h.op_type for h in await self.svc.history(manager.id)])


class LatePaymentAndCabinetTests(EnvTestCase):
    async def test_payment_after_timeout_still_completes_the_operation(self):
        """Счёт закрылся по таймауту, клиент оплатил по старой ссылке: подписка выдана — журнал это показывает."""
        manager = await self.manager()
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="o1")
        await self.env.repos.payments.update_status("yk-1", "cancelled")
        await self.svc.sync_pending_operations()
        self.assertEqual((await self.env.repos.ops.get(result.operation_id)).status, "cancelled")

        await self.env.payments.process_successful_payment("yk-1", 149.0)
        await self.svc.on_payment_succeeded("yk-1")

        op = await self.env.repos.ops.get(result.operation_id)
        self.assertEqual(op.status, "completed")
        self.assertIsNotNone(op.key_username)

    async def test_cabinet_link_reset_kills_the_old_one(self):
        manager = await self.manager(can_accept_cash=True)
        result = await self.cash_issue(manager)
        old_hash = (await self.env.repos.users.get_by_client_code(result.client_code)).cabinet_token_hash

        url = await self.svc.reset_cabinet_link(manager.id, result.client_code)

        self.assertTrue(url.startswith("https://example.com/c/"))
        user = await self.env.repos.users.get_by_client_code(result.client_code)
        self.assertNotEqual(user.cabinet_token_hash, old_hash)
        self.assertIn("cabinet_link_reset", [h.op_type for h in await self.svc.history(manager.id)])

    async def test_cabinet_link_reset_is_for_offline_clients_only(self):
        manager = await self.manager()
        user = await self.env.make_telegram_client(555)
        code = await self.svc.add_client_by_code(manager.id, await self.svc.create_access_code(user.user_id))
        with self.assertRaises(self.E):
            await self.svc.reset_cabinet_link(manager.id, code)

    async def test_cabinet_link_reset_needs_access(self):
        a = await self.manager(can_accept_cash=True, telegram_id=1)
        b = await self.manager(telegram_id=2)
        result = await self.cash_issue(a)
        with self.assertRaises(self.E):
            await self.svc.reset_cabinet_link(b.id, result.client_code)


class DigestAndCheckTests(EnvTestCase):
    async def _expire_soon(self, client_code, days=2):
        from sqlalchemy import update
        from db import User
        async with self.env.session_maker() as session:
            await session.execute(update(User).where(User.client_code == client_code).values(
                subscription_end_date=datetime.datetime.now() + datetime.timedelta(days=days)))
            await session.commit()

    async def test_digest_lists_expiring_clients_without_autorenew(self):
        manager = await self.manager(can_accept_cash=True, telegram_id=4242)
        result = await self.cash_issue(manager)
        await self._expire_soon(result.client_code)

        digest = await self.svc.renewal_digest(days=3)

        self.assertEqual(len(digest), 1)
        telegram_id, lines = digest[0]
        self.assertEqual(telegram_id, 4242)
        self.assertIn(result.client_code, lines[0])

    async def test_digest_skips_clients_with_autorenew_and_far_expiry(self):
        manager = await self.manager(can_accept_cash=True, telegram_id=4242)
        near = await self.cash_issue(manager, nonce="a")
        far = await self.cash_issue(manager, nonce="b")
        await self._expire_soon(near.client_code)
        await self._expire_soon(far.client_code, days=20)
        self.env.cards.get_card.return_value = SimpleNamespace(auto_renew_enabled=True)

        self.assertEqual(await self.svc.renewal_digest(days=3), [])

    async def test_digest_is_not_sent_to_blocked_managers(self):
        manager = await self.manager(can_accept_cash=True, telegram_id=4242)
        result = await self.cash_issue(manager)
        await self._expire_soon(result.client_code)
        await self.svc.set_status(manager.id, "blocked", admin_id=1)
        self.assertEqual(await self.svc.renewal_digest(days=3), [])

    async def test_check_operation_completes_paid_invoice(self):
        manager = await self.manager()
        result = await self.svc.issue(manager.id, product="tariff", tariff_id=2, method="online",
                                      idempotency_nonce="o1")
        self.assertEqual((await self.svc.check_operation(manager.id, result.operation_id)).status, "pending_payment")
        await self.env.payments.process_successful_payment("yk-1", 149.0)
        self.assertEqual((await self.svc.check_operation(manager.id, result.operation_id)).status, "completed")

    async def test_check_operation_of_another_manager_is_hidden(self):
        a = await self.manager(telegram_id=1)
        b = await self.manager(telegram_id=2)
        result = await self.svc.issue(a.id, product="tariff", tariff_id=2, method="online", idempotency_nonce="o1")
        self.assertIsNone(await self.svc.check_operation(b.id, result.operation_id))


class TariffListTests(EnvTestCase):
    async def test_manager_sees_active_regular_tariffs(self):
        manager = await self.manager()
        await self.env.repos.tariffs.update_field(1, "is_intro", True)
        await self.env.repos.tariffs.update_field(3, "is_active", False)
        names = [t.name for t in await self.svc.list_tariffs()]
        self.assertEqual(names, ["Месяц"])

    async def test_quota_is_shown(self):
        env = await build_env(tariffs=[dict(name="Безлимит", price=499, duration_days=30, data_limit_gb=0),
                                       dict(name="Лимит", price=99, duration_days=30, data_limit_gb=100),
                                       dict(name="База", price=149, duration_days=30, data_limit_gb=None)])
        try:
            quotas = {t.name: t.quota_gb for t in await env.service.list_tariffs()}
            self.assertEqual(quotas, {"Безлимит": 0, "Лимит": 100, "База": 500})
        finally:
            await env.engine.dispose()


if __name__ == "__main__":
    unittest.main()
