# tests/test_email_verification.py
"""
Коды подтверждения email (webapp/core/verification.py): в токене регистрации не должно
быть открытого кода — JWT подписан, но не зашифрован, его payload читает любой.
"""
import base64
import json
import re
import unittest
from pathlib import Path

from jose import jwt

from webapp.core.security import ALGORITHM, SECRET_KEY
from webapp.core.verification import check_code, generate_code, hash_code

ROOT = Path(__file__).resolve().parent.parent


class CodeGenerationTest(unittest.TestCase):
    def test_six_digits(self):
        for _ in range(200):
            self.assertRegex(generate_code(), r"^\d{6}$")

    def test_not_constant(self):
        self.assertGreater(len({generate_code() for _ in range(50)}), 1)


class CodeHashTest(unittest.TestCase):
    def test_correct_code_passes(self):
        h = hash_code("a@b.ru", "123456")
        self.assertTrue(check_code("a@b.ru", "123456", h))

    def test_input_whitespace_and_email_case_ignored(self):
        h = hash_code("A@B.ru", "123456")
        self.assertTrue(check_code("a@b.ru", " 123456 ", h))

    def test_wrong_code_fails(self):
        self.assertFalse(check_code("a@b.ru", "654321", hash_code("a@b.ru", "123456")))

    def test_hash_bound_to_email(self):
        h = hash_code("victim@b.ru", "123456")
        self.assertFalse(check_code("attacker@b.ru", "123456", h))

    def test_empty_hash_rejected(self):
        # старый токен с открытым кодом не содержит code_hash — не должен проходить
        self.assertFalse(check_code("a@b.ru", "123456", ""))
        self.assertFalse(check_code("a@b.ru", "123456", None))

    def test_hash_does_not_contain_code_and_needs_secret(self):
        h = hash_code("a@b.ru", "123456")
        self.assertNotIn("123456", h)
        # без ключа тот же алгоритм даёт другой хэш → офлайн-перебор 10^6 кодов невозможен
        import hashlib
        naive = hashlib.sha256(b"email-verification:a@b.ru:123456").hexdigest()
        self.assertNotEqual(h, naive)


class RegistrationTokenTest(unittest.TestCase):
    def test_payload_readable_by_client_has_no_plain_code(self):
        code = "482913"
        token = jwt.encode(
            {"type": "registration", "email": "a@b.ru", "code_hash": hash_code("a@b.ru", code)},
            SECRET_KEY, algorithm=ALGORITHM,
        )
        body = token.split(".")[1]
        payload_json = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        self.assertNotIn(code, payload_json)
        self.assertNotIn("code", json.loads(payload_json))

    def test_auth_router_never_stores_plain_code_in_token(self):
        source = (ROOT / "webapp/routers/auth.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'payload\["code"\]|"code":\s*(new_)?code', source))


if __name__ == "__main__":
    unittest.main()
