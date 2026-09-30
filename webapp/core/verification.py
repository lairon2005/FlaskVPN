# webapp/core/verification.py
"""Коды подтверждения email: генерация и проверка.

Токен регистрации уходит браузеру (скрытое поле формы), а JWT только подписан,
не зашифрован — payload читается base64-декодированием. Поэтому в токене лежит
не сам код, а его HMAC с серверным ключом: без SECRET_KEY по хэшу код не
подобрать (даже перебором 10^6 вариантов), а привязка к email не даёт
подсунуть токен с чужим кодом.
"""
import hashlib
import hmac
import secrets

from webapp.core.security import SECRET_KEY

CODE_LENGTH = 6


def generate_code() -> str:
    """Шестизначный код из криптостойкого генератора (random.choices предсказуем)."""
    return ''.join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


def hash_code(email: str, code: str) -> str:
    message = f"email-verification:{email.strip().lower()}:{code.strip()}"
    return hmac.new(SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def check_code(email: str, code: str, code_hash: str) -> bool:
    """Сравнение за постоянное время; пустой хэш (старый токен с открытым кодом) не проходит."""
    if not code_hash:
        return False
    return hmac.compare_digest(hash_code(email, code), code_hash)
