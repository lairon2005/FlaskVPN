"""
Контракт панели Remnawave 3.x.

Панель FlaskVPN (panel.flaskvpn.ru) работает на backend 3.4.4, где поля
запросов и ответов переименованы относительно 2.8.x:

    PATCH /api/users            uuid     → id
    POST  /api/hwid/devices/*   userUuid → userId
    объект пользователя         uuid     → id

Идентификатор пользователя вся остальная кодовая база (БД, сервисы,
хендлеры) знает как uuid, поэтому клиент обязан отдавать его наверх под
старым именем. Тесты фиксируют оба конца этого перевода: что уходит в
панель и что возвращается в сервисы.
"""
import unittest
from unittest.mock import AsyncMock

from remnawave.client import RemnawaveClient


class RemnawaveV3RequestContractTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> RemnawaveClient:
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_update_user_sends_id_not_uuid(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"id": "u-1"}})

        await client.update_user("u-1", expire_at="2027-01-01T00:00:00.000Z")

        payload = client._request.await_args.kwargs["json"]
        self.assertEqual(payload["id"], "u-1")
        self.assertNotIn("uuid", payload)

    async def test_delete_device_sends_user_id(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"devices": []}})

        await client.delete_user_device("u-1", "hwid-1")

        payload = client._request.await_args.kwargs["json"]
        self.assertEqual(payload, {"userId": "u-1", "hwid": "hwid-1"})

    async def test_delete_all_devices_sends_user_id(self):
        client = self.make_client()
        client._request = AsyncMock(return_value=None)

        await client.delete_all_user_devices("u-1")

        payload = client._request.await_args.kwargs["json"]
        self.assertEqual(payload, {"userId": "u-1"})

    async def test_numeric_id_is_sent_back_as_number(self):
        """
        Поля id/userId в 3.x объявлены числом: строка даёт HTTP 400.
        Бот хранит идентификатор строкой, поэтому клиент переводит обратно.
        """
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"id": 7}})

        await client.update_user("7", expire_at="2027-01-01T00:00:00.000Z")
        self.assertEqual(client._request.await_args.kwargs["json"]["id"], 7)

        client._request = AsyncMock(return_value={"response": {"devices": []}})
        await client.delete_user_device("7", "hwid-1")
        self.assertEqual(client._request.await_args.kwargs["json"]["userId"], 7)

        client._request = AsyncMock(return_value=None)
        await client.delete_all_user_devices("7")
        self.assertEqual(client._request.await_args.kwargs["json"]["userId"], 7)

    async def test_uuid_identifier_is_not_converted(self):
        """Панель 2.8.x адресуется UUID — его трогать нельзя."""
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"uuid": "abc-123"}})

        await client.update_user("abc-123", expire_at="2027-01-01T00:00:00.000Z")

        self.assertEqual(client._request.await_args.kwargs["json"]["id"], "abc-123")


class RemnawaveV3ResponseContractTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> RemnawaveClient:
        client = RemnawaveClient("https://panel.example", "token", squad_uuid="sq-1")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_create_user_exposes_id_as_uuid(self):
        client = self.make_client()
        client._request = AsyncMock(
            return_value={"response": {"id": "u-1", "username": "user_1"}}
        )

        user = await client.create_user(username="user_1")

        self.assertEqual(user["uuid"], "u-1")

    async def test_get_user_by_username_exposes_id_as_uuid(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"id": "u-2"}})

        user = await client.get_user_by_username("user_2")

        self.assertEqual(user["uuid"], "u-2")

    async def test_revoke_exposes_id_as_uuid(self):
        client = self.make_client()
        client._request = AsyncMock(
            return_value={"response": {"id": "u-3", "subscriptionUrl": "https://s/3"}}
        )

        user = await client.revoke_user_subscription("u-3")

        self.assertEqual(user["uuid"], "u-3")
        self.assertEqual(user["subscriptionUrl"], "https://s/3")

    async def test_get_all_users_exposes_id_as_uuid(self):
        client = self.make_client()
        client._request = AsyncMock(
            return_value={"response": {"users": [{"id": "u-4"}], "total": 1}}
        )

        users = await client.get_all_users()

        self.assertEqual(users[0]["uuid"], "u-4")

    async def test_existing_uuid_is_not_overwritten(self):
        """Панель 2.8.x отдаёт uuid — перевод не должен её ломать."""
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"uuid": "old-style"}})

        user = await client.get_user_by_username("user_5")

        self.assertEqual(user["uuid"], "old-style")

    async def test_numeric_id_becomes_string_uuid(self):
        """
        В 3.x id — целое число, а бот пишет его в колонку String(36):
        asyncpg на int в varchar падает, поэтому переводим в строку здесь.
        """
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"id": 42}})

        user = await client.get_user_by_username("user_42")

        self.assertEqual(user["uuid"], "42")
        self.assertIsInstance(user["uuid"], str)


if __name__ == "__main__":
    unittest.main()
