"""Атомарность репозиториев менеджеров на настоящем SQL: гонки, уникальные ключи, диапазоны id."""
import asyncio
import datetime
import itertools
import unittest

import db_harness
from database.repositories.client_access import ClientAccessRepository
from database.repositories.manager import ManagerClientRepository, ManagerRepository
from database.repositories.manager_operation import ManagerOperationRepository
from database.repositories.temp_key import TempKeyRepository
from database.repositories.user import OFFLINE_ID_MAX, OFFLINE_ID_MIN, UserRepository

FUTURE = lambda: datetime.datetime.now() + datetime.timedelta(hours=1)  # noqa: E731


class RepoCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sm, self.engine = await db_harness.make_session_maker()
        self.managers = ManagerRepository(self.sm)
        self.clients = ManagerClientRepository(self.sm)
        self.ops = ManagerOperationRepository(self.sm)
        self.temps = TempKeyRepository(self.sm)
        self.access = ClientAccessRepository(self.sm)
        self.users = UserRepository(self.sm)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def active_manager(self, tg=1):
        await self.managers.create_invited("М", 1, f"h{tg}", FUTURE())
        return await self.managers.accept_invite(f"h{tg}", tg)


class OfflineClientTests(RepoCase):
    async def test_id_range_and_defaults(self):
        user = await self.users.create_offline_client(lambda: "ABCDEF", "hash1", None)
        self.assertTrue(OFFLINE_ID_MIN <= user.user_id <= OFFLINE_ID_MAX)
        self.assertLess(user.user_id, -1_000_000_000)           # не пересекается с веб-пользователями
        self.assertEqual((user.origin, user.is_active, user.vpn_username), ("offline", False, "off_abcdef"))
        self.assertIsNone(user.email)
        self.assertIsNone(user.username)

    async def test_code_collision_is_retried(self):
        await self.users.create_offline_client(lambda: "AAAAAA", "h1", None)
        codes = itertools.chain(["AAAAAA", "AAAAAA"], itertools.repeat("BBBBBB"))
        user = await self.users.create_offline_client(lambda: next(codes), "h2", None)
        self.assertEqual(user.client_code, "BBBBBB")

    async def test_set_client_code_refuses_taken_code_and_does_not_overwrite(self):
        a = await self.users.create_offline_client(lambda: "AAAAAA", "h1", None)
        tg, _ = await self.users.get_or_create(555, "Имя", "u")
        self.assertFalse(await self.users.set_client_code(tg.user_id, "AAAAAA"))
        self.assertTrue(await self.users.set_client_code(tg.user_id, "CCCCCC"))
        self.assertFalse(await self.users.set_client_code(tg.user_id, "DDDDDD"))   # уже есть — не меняем
        self.assertEqual((await self.users.get(tg.user_id)).client_code, "CCCCCC")

    async def test_cabinet_token_hash_is_unique_and_replaceable(self):
        a = await self.users.create_offline_client(lambda: "AAAAAA", "same", None)
        with self.assertRaises(RuntimeError):   # второй с тем же хэшем не создаётся
            codes = itertools.count()
            await self.users.create_offline_client(lambda: f"X{next(codes):05d}", "same", None)
        await self.users.set_cabinet_token_hash(a.user_id, "new")
        self.assertIsNone(await self.users.get_by_cabinet_token_hash("same"))
        self.assertEqual((await self.users.get_by_cabinet_token_hash("new")).user_id, a.user_id)


class RaceTests(RepoCase):
    async def test_invite_has_a_single_winner(self):
        await self.managers.create_invited("М", 1, "tok", FUTURE())
        results = await asyncio.gather(*(self.managers.accept_invite("tok", 100 + i) for i in range(5)))
        self.assertEqual(len([r for r in results if r]), 1)

    async def test_login_token_has_a_single_winner(self):
        manager = await self.active_manager()
        await self.managers.set_login_token(manager.id, "lt", FUTURE())
        results = await asyncio.gather(*(self.managers.consume_login_token("lt") for _ in range(5)))
        self.assertEqual(len([r for r in results if r]), 1)

    async def test_expired_login_token_is_refused(self):
        manager = await self.active_manager()
        await self.managers.set_login_token(manager.id, "lt", datetime.datetime.now() - datetime.timedelta(seconds=1))
        self.assertIsNone(await self.managers.consume_login_token("lt"))

    async def test_access_code_has_a_single_winner(self):
        user = await self.users.create_offline_client(lambda: "AAAAAA", "h", None)
        await self.access.create_code(user.user_id, "code-hash", FUTURE())
        results = await asyncio.gather(*(self.access.consume("code-hash", m) for m in range(1, 6)))
        self.assertEqual(len([r for r in results if r is not None]), 1)

    async def test_idempotent_create_has_a_single_creator(self):
        manager = await self.active_manager()
        results = await asyncio.gather(*(
            self.ops.create_idempotent("k1", manager_id=manager.id, op_type="issue_tariff") for _ in range(8)
        ))
        created = [r for r in results if r[1]]
        self.assertEqual(len(created), 1)
        self.assertEqual(len({op.id for op, _ in results}), 1)

    async def test_temp_key_conversion_has_a_single_winner(self):
        manager = await self.active_manager()
        op, _ = await self.ops.create_idempotent("k", manager_id=manager.id, op_type="issue_temp")
        key = await self.temps.create(manager.id, op.id, "tmp_x", "u1", FUTURE())
        results = await asyncio.gather(*(self.temps.transition(key.id, "active", "converting") for _ in range(6)))
        self.assertEqual(results.count(True), 1)


class OperationLogTests(RepoCase):
    async def test_journal_snapshots_cannot_be_rewritten(self):
        manager = await self.active_manager()
        op, _ = await self.ops.create_idempotent("k", manager_id=manager.id, op_type="issue_tariff")
        with self.assertRaises(ValueError):
            await self.ops.update(op.id, manager_id=999)
        with self.assertRaises(ValueError):
            await self.ops.update(op.id, idempotency_key="other")
        with self.assertRaises(ValueError):
            await self.ops.update(op.id, created_at=datetime.datetime(2000, 1, 1))

    async def test_settle_takes_exactly_the_unsettled_cash(self):
        manager = await self.active_manager()
        for i, (price, method, status) in enumerate([(100, "cash", "completed"), (200, "cash", "completed"),
                                                     (300, "online", "completed"), (400, "cash", "failed"),
                                                     (500, "cash", "pending_payment")]):
            op, _ = await self.ops.create_idempotent(f"k{i}", manager_id=manager.id, op_type="issue_tariff",
                                                     price=price, payment_method=method, status=status)
        settlement = await self.ops.settle(manager.id, admin_id=9)
        self.assertEqual((settlement.amount, settlement.operations_count), (300.0, 2))
        self.assertEqual(await self.ops.cash_outstanding(manager.id), (0.0, 0))
        self.assertIsNone(await self.ops.settle(manager.id, admin_id=9))

    async def test_daily_count_ignores_failed_and_other_types(self):
        manager = await self.active_manager()
        await self.ops.create_idempotent("a", manager_id=manager.id, op_type="issue_temp", status="completed")
        await self.ops.create_idempotent("b", manager_id=manager.id, op_type="issue_temp", status="failed")
        await self.ops.create_idempotent("c", manager_id=manager.id, op_type="issue_tariff", status="completed")
        self.assertEqual(await self.ops.count_today(manager.id, "issue_temp"), 1)


class AccessTests(RepoCase):
    async def test_revoked_access_revives_on_regrant(self):
        manager = await self.active_manager()
        user = await self.users.create_offline_client(lambda: "AAAAAA", "h", None)
        await self.clients.grant(manager.id, user.user_id, "created", FUTURE())
        await self.clients.revoke(user.user_id)
        self.assertIsNone(await self.clients.get_active(manager.id, user.user_id))
        await self.clients.grant(manager.id, user.user_id, "code", FUTURE())
        self.assertIsNotNone(await self.clients.get_active(manager.id, user.user_id))

    async def test_extend_never_shortens(self):
        manager = await self.active_manager()
        user = await self.users.create_offline_client(lambda: "AAAAAA", "h", None)
        far = datetime.datetime.now() + datetime.timedelta(days=30)
        await self.clients.grant(manager.id, user.user_id, "created", far)
        await self.clients.extend(manager.id, user.user_id, datetime.datetime.now() + datetime.timedelta(days=1))
        link = await self.clients.get_active(manager.id, user.user_id)
        self.assertGreater(link.access_until, datetime.datetime.now() + datetime.timedelta(days=29))

    async def test_rights_update_rejects_unknown_fields(self):
        manager = await self.active_manager()
        with self.assertRaises(ValueError):
            await self.managers.update_rights(manager.id, status="active")
        with self.assertRaises(ValueError):
            await self.managers.update_rights(manager.id, telegram_id=1)


if __name__ == "__main__":
    unittest.main()
