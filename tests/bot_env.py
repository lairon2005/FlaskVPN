# tests/bot_env.py
"""
Сквозные тесты бота без Telegram: настоящие роутеры aiogram, настоящий Dispatcher
(FSM, фильтры, middleware-порядок), настоящий ManagerService на SQLite — а вместо
сети «записывающая» сессия Bot API, которая запоминает каждый запрос и отвечает
правдоподобно.

Так проверяются те ~600 строк хендлеров, которые иначе ловили бы ошибки только на
живом боте: опечатка в callback_data, забытый await, неверное имя переменной.
"""
import datetime
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.methods import (
    AnswerCallbackQuery, DeleteMessage, EditMessageCaption, EditMessageText, SendDocument,
    SendMessage, SendPhoto, SetMyCommands,
)
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from manager_env import build_env
from real_traffic_pricing import _load, real_custom_pricing

ROOT = Path(__file__).resolve().parents[1]
BOT_ID = 4242


class RecordingSession(BaseSession):
    """Bot API без сети: каждый вызов записывается и получает правдоподобный ответ."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self._message_id = 5000

    async def close(self):
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - не нужен
        yield b""

    async def make_request(self, bot, method, timeout=None):
        data = method.model_dump()
        self.calls.append((type(method).__name__, data))
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
            self._message_id += 1
            return self._message(method.chat_id, getattr(method, "text", None) or getattr(method, "caption", None)).as_(bot)
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            return self._message(method.chat_id or 0, getattr(method, "text", None) or getattr(method, "caption", None),
                                 message_id=method.message_id or self._message_id).as_(bot)
        if isinstance(method, (DeleteMessage, AnswerCallbackQuery, SetMyCommands)):
            return True
        return True

    def _message(self, chat_id, text, message_id=None):
        return Message(
            message_id=message_id or self._message_id, date=datetime.datetime.now(),
            chat=Chat(id=int(chat_id), type=ChatType.PRIVATE if int(chat_id) > 0 else ChatType.SUPERGROUP),
            from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text=text,
        )

    # --- удобные выборки ---
    def of(self, name: str):
        return [data for call, data in self.calls if call == name]

    def texts(self) -> list[str]:
        out = []
        for call, data in self.calls:
            if call in ("SendMessage", "EditMessageText"):
                out.append(data.get("text") or "")
            elif call in ("SendPhoto", "EditMessageCaption"):
                out.append(data.get("caption") or "")
        return out

    def alerts(self) -> list[str]:
        return [d.get("text") or "" for d in self.of("AnswerCallbackQuery") if d.get("text")]

    def last_text(self) -> str:
        return self.texts()[-1] if self.texts() else ""

    def clear(self):
        self.calls.clear()


def _pkg(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def load_manager_handlers(env, admin: bool = False):
    """Пакет tgbot.handlers.manager (и, по желанию, админские модули), привязанный к сервису тестового окружения."""
    config = SimpleNamespace(
        webhook=SimpleNamespace(domain="example.com", use_webhook=False),
        tg_bot=SimpleNamespace(admin_ids=[1], tg_bot_username="bot", manager_receipts_target=(-100500, 77)),
    )
    services = _pkg("tgbot.services")
    services.manager_service = env.service

    def real(name, relpath):
        return _load(name, ROOT / relpath)

    stubs = {
        "loader": SimpleNamespace(logger=Mock(), config=config),
        "tgbot": _pkg("tgbot"), "tgbot.services": services,
        "tgbot.services.manager_service": env.module,
        "tgbot.services.manager_receipts": real("tgbot.services.manager_receipts", "tgbot/services/manager_receipts.py"),
        "tgbot.services.qr_generator": real("tgbot.services.qr_generator", "tgbot/services/qr_generator.py"),
        "tgbot.keyboards": _pkg("tgbot.keyboards"),
        "tgbot.keyboards.manager": real("tgbot.keyboards.manager", "tgbot/keyboards/manager.py"),
        "tgbot.states": _pkg("tgbot.states"),
        "tgbot.states.manager_states": real("tgbot.states.manager_states", "tgbot/states/manager_states.py"),
        "tgbot.handlers": _pkg("tgbot.handlers"),
        "utils": _pkg("utils"),
        "utils.telegram_ui": real("utils.telegram_ui", "utils/telegram_ui.py"),
    }
    stubs["tgbot.filters"] = _pkg("tgbot.filters")
    real_custom = real_custom_pricing()
    with patch.dict(sys.modules, stubs):
        stubs["tgbot.filters.manager"] = real("tgbot.filters.manager", "tgbot/filters/manager.py")
        sys.modules["tgbot.filters.manager"] = stubs["tgbot.filters.manager"]
        commands = real("tgbot.commands", "tgbot/commands.py")
        commands.apply_commands = AsyncMock()
        sys.modules["tgbot.commands"] = commands
        stubs["tgbot.commands"] = commands

        pkg_dir = ROOT / "tgbot" / "handlers" / "manager"
        spec = importlib.util.spec_from_file_location(
            "tgbot.handlers.manager", pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)])
        package = importlib.util.module_from_spec(spec)
        sys.modules["tgbot.handlers.manager"] = package
        spec.loader.exec_module(package)
        modules = {name: mod for name, mod in sys.modules.items() if name.startswith("tgbot.handlers.manager")}

        admin_modules = {}
        if admin:
            from aiogram.types import InlineKeyboardMarkup
            from aiogram.utils.keyboard import InlineKeyboardBuilder

            def cancel_fsm_keyboard(back):
                builder = InlineKeyboardBuilder()
                builder.button(text="❌ Отмена", callback_data=back)
                return builder.as_markup()

            inline = types.ModuleType("tgbot.keyboards.inline")
            inline.cancel_fsm_keyboard = cancel_fsm_keyboard
            sys.modules["tgbot.keyboards.inline"] = inline
            database = types.ModuleType("database")
            database.settings_repo = env.repos.settings
            database.tariff_repo = env.repos.tariffs
            sys.modules["database"] = database
            sys.modules["tgbot.filters.admin"] = real("tgbot.filters.admin", "tgbot/filters/admin.py")
            sys.modules["tgbot.services.custom_pricing"] = real_custom
            sys.modules["tgbot.handlers.admin"] = _pkg("tgbot.handlers.admin")
            sys.modules["tgbot.states.admin_manager_states"] = real(
                "tgbot.states.admin_manager_states", "tgbot/states/admin_manager_states.py")
            for name in ("managers", "pricing_settings"):
                full = f"tgbot.handlers.admin.{name}"
                module = real(full, f"tgbot/handlers/admin/{name}.py")
                sys.modules[full] = module
                admin_modules[name] = module

    # Роутеры (aiogram запрещает подключать один роутер дважды) — свежие для каждого теста: пакет загружен заново.
    return SimpleNamespace(package=package, modules=modules, filters=stubs["tgbot.filters.manager"],
                           commands=commands, admin=admin_modules)


class BotHarness:
    """Dispatcher + бот с записывающей сессией + фабрики апдейтов."""

    def __init__(self, env, handlers):
        self.env = env
        self.handlers = handlers
        self.session = RecordingSession()
        self.bot = Bot("12345:TEST", session=self.session)
        self.dp = Dispatcher()
        self.dp.include_routers(
            handlers.package.manager_invite_router, handlers.package.manager_router, handlers.package.manager_denied_router,
        )
        if handlers.admin:
            self.dp.include_routers(
                handlers.admin["managers"].admin_managers_router, handlers.admin["pricing_settings"].admin_pricing_router,
            )
        self._update_id = 0
        self._message_id = 100

    async def send(self, user_id: int, text: str):
        self._update_id += 1
        self._message_id += 1
        entities = None
        if text.startswith("/"):
            length = len(text.split()[0])
            entities = [MessageEntity(type="bot_command", offset=0, length=length)]
        message = Message(
            message_id=self._message_id, date=datetime.datetime.now(), text=text, entities=entities,
            chat=Chat(id=user_id, type=ChatType.PRIVATE), from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        )
        return await self.dp.feed_update(self.bot, Update(update_id=self._update_id, message=message))

    async def press(self, user_id: int, data: str):
        self._update_id += 1
        self._message_id += 1
        carrier = Message(
            message_id=self._message_id, date=datetime.datetime.now(), text="экран",
            chat=Chat(id=user_id, type=ChatType.PRIVATE), from_user=User(id=BOT_ID, is_bot=True, first_name="bot"),
        )
        call = CallbackQuery(id=str(self._update_id), from_user=User(id=user_id, is_bot=False, first_name="Тест"),
                             chat_instance="ci", data=data, message=carrier)
        return await self.dp.feed_update(self.bot, Update(update_id=self._update_id, callback_query=call))


async def build_bot_env(admin: bool = False, **kwargs):
    env = await build_env(**kwargs)
    handlers = load_manager_handlers(env, admin=admin)
    harness = BotHarness(env, handlers)
    return env, harness
