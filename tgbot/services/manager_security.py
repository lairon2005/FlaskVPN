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


# --- Логин и пароль для входа на сайт -------------------------------------------

LOGIN_MIN, LOGIN_MAX = 3, 32
PASSWORD_MIN, PASSWORD_MAX = 8, 128
_LOGIN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def normalize_login(raw: str | None) -> str | None:
    """
    Логин в каноническом виде (нижний регистр, без пробелов по краям) или None.

    Только латиница, цифры, «.», «_», «-» и первой — буква: логин диктуют и
    вводят на телефоне, кириллица и похожие символы (a/а) дали бы два «одинаковых» логина.
    """
    login = (raw or "").strip().lower()
    if not LOGIN_MIN <= len(login) <= LOGIN_MAX:
        return None
    if not login[0].isascii() or not login[0].isalpha() or any(ch not in _LOGIN_CHARS for ch in login):
        return None
    return login


def password_problem(password: str | None, login: str | None = None) -> str | None:
    """Почему пароль не годится (текст для человека) или None, если годится."""
    password = password or ""
    if len(password) < PASSWORD_MIN:
        return f"Пароль должен быть не короче {PASSWORD_MIN} символов."
    if len(password) > PASSWORD_MAX:
        return f"Пароль должен быть не длиннее {PASSWORD_MAX} символов."
    if password.strip() != password:
        return "Пароль не должен начинаться или заканчиваться пробелом."
    if len(set(password)) < 4:
        return "Пароль слишком простой: используйте разные символы."
    if login and login.lower() in password.lower():
        return "Пароль не должен содержать логин."
    return None


_pwd_context = None


def _context():
    # Ленивая инициализация: passlib/argon2 нужны только при работе с паролями,
    # и модуль остаётся импортируемым в тестах, которым пароли не нужны.
    global _pwd_context
    if _pwd_context is None:
        from passlib.context import CryptContext
        _pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
    return _pwd_context


def hash_password(password: str) -> str:
    return _context().hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Проверка пароля. Без хэша — всё равно тратим время на argon2, чтобы по таймингу нельзя было понять, есть ли логин."""
    if not password_hash:
        _context().hash(password or "x")
        return False
    try:
        return _context().verify(password or "", password_hash)
    except (ValueError, TypeError):
        return False
