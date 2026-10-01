# tests/db_harness.py
"""
Реальная БД для тестов репозиториев: SQLite в памяти вместо PostgreSQL.

Тесты проекта изолированы от .env и сети, но атомарные операции (UPDATE ...
RETURNING, уникальные ключи, идемпотентность) мокать бессмысленно — проверять
надо настоящий SQL. SQLite понимает всё, что используют репозитории менеджеров;
два PG-специфичных типа подменяем компиляторами на время тестов.

`db.py` на импорте вызывает load_config(), поэтому до импорта подставляем
заглушки переменных окружения — настоящий .env не нужен и не читается.
"""
import atexit
import os
import tempfile

for key, value in {
    "BOT_TOKEN": "0:test", "ADMINS": "1", "SUPPORT_CHAT_ID": "1", "TRANSACTION_LOG_TOPIC_ID": "1",
    "DB_HOST": "localhost", "DB_PORT": "5432", "DB_USER": "u", "DB_PASSWORD": "p", "DB_NAME": "n",
    "SERVER_URL": "/x", "DOMAIN": "localhost", "USE_WEBHOOK": "False",
    "YOOKASSA_SHOP_ID": "1", "YOOKASSA_SECRET_KEY": "k",
    "REMNAWAVE_API_URL": "http://localhost", "REMNAWAVE_API_TOKEN": "t",
}.items():
    os.environ.setdefault(key, value)

from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    # В SQLite только INTEGER PRIMARY KEY — алиас rowid и умеет автоинкремент.
    return "INTEGER"


_TMP_FILES: list[str] = []


@atexit.register
def _cleanup() -> None:
    for path in _TMP_FILES:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


async def make_session_maker():
    """
    Свежая пустая БД со всеми таблицами проекта. Возвращает (session_maker, engine).

    Файловая, а не «в памяти»: в памяти SQLite отдаёт ОДНО общее соединение, и откат
    одной сессии откатывает чужие незавершённые транзакции — гонки (два конкурентных
    INSERT с одним ключом) вели бы себя не так, как в PostgreSQL. С файлом у каждой
    сессии своё соединение, а конкурентные записи SQLite сериализует блокировкой.
    """
    import db  # noqa: E402  (после заглушек окружения)

    handle, path = tempfile.mkstemp(suffix=".sqlite3", prefix="flaskvpn-test-")
    os.close(handle)
    _TMP_FILES.append(path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30})
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine
