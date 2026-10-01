"""
Токены, коды и отпечатки менеджерской подсистемы — чистые функции на stdlib.

Правило: в БД лежит только sha256 секрета. Сам токен существует в момент
выдачи (ссылка в сообщении) и больше нигде не хранится, так что утечка дампа
БД не даёт ни входа менеджера, ни доступа к кабинету клиента.
"""
import hashlib
import hmac
import secrets

# 32 символа без путаницы 0/O, 1/I: код диктуют голосом и вбивают руками.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

# Токены ссылок: 32 байта энтропии в url-safe виде.
TOKEN_BYTES = 32

# Символы, которые легко спутать при вводе кода вручную → к тому, что есть в алфавите.
_CONFUSABLE = str.maketrans({"0": "O", "1": "I", "L": "I", "l": "I"})


def generate_client_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_client_code(raw: str) -> str | None:
    """
    Приводит введённый руками код к каноническому виду или None, если это не код.

    Регистр и пробелы/дефисы не важны. Алфавит не содержит 0/O и 1/I, поэтому
    «O» и «0» ввода — оба означают букву, которой в коде нет: их считаем
    опечаткой и отклоняем, а не угадываем.
    """
    code = "".join(ch for ch in (raw or "").upper() if ch not in " -_")
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def key_fingerprint(subscription_url_or_username: str) -> str:
    """
    Короткий отпечаток ключа для журнала и группового чека: по нему можно узнать
    ключ, но нельзя им воспользоваться. 4 hex-символа sha256 — достаточно, чтобы
    различать операции одного менеджера, и недостаточно, чтобы что-то восстановить.
    """
    return hashlib.sha256((subscription_url_or_username or "").encode("utf-8")).hexdigest()[:4]


def temp_username() -> str:
    """Имя временного пользователя в панели: tmp_ + 8 случайных символов (a-z0-9)."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "tmp_" + "".join(secrets.choice(alphabet) for _ in range(8))
