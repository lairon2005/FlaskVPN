"""Коды клиентов, токены и отпечатки: формат, нормализация ввода, отсутствие секретов в хэше."""
import importlib.util
import re
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "manager_security.py"
_spec = importlib.util.spec_from_file_location("manager_security_under_test", _path)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


class ClientCodeTests(unittest.TestCase):
    def test_format_and_alphabet(self):
        for _ in range(300):
            code = ms.generate_client_code()
            self.assertEqual(len(code), 6)
            self.assertTrue(set(code) <= set(ms.CODE_ALPHABET))
            self.assertFalse(set(code) & set("01OI"))

    def test_codes_are_not_constant(self):
        self.assertGreater(len({ms.generate_client_code() for _ in range(50)}), 40)

    def test_normalize_accepts_sloppy_input(self):
        self.assertEqual(ms.normalize_client_code(" k7f-3q2 "), "K7F3Q2")
        self.assertEqual(ms.normalize_client_code("k7f3q2"), "K7F3Q2")

    def test_normalize_rejects_garbage(self):
        for bad in ("", None, "K7F3Q", "K7F3Q22", "K7F3Q0", "K7F3QO", "K7F3Q1", "K7F3QI", "привет!", "@user12"):
            self.assertIsNone(ms.normalize_client_code(bad), bad)

    def test_telegram_id_and_email_are_not_codes(self):
        self.assertIsNone(ms.normalize_client_code("123456"))
        self.assertIsNone(ms.normalize_client_code("a@b.c"))


class TokenTests(unittest.TestCase):
    def test_token_is_long_and_unique(self):
        tokens = {ms.new_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)
        self.assertTrue(all(len(t) >= 40 for t in tokens))

    def test_hash_is_sha256_hex_and_not_the_token(self):
        token = ms.new_token()
        digest = ms.hash_token(token)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn(token, digest)
        self.assertEqual(digest, ms.hash_token(token))

    def test_tokens_equal_is_exact(self):
        self.assertTrue(ms.tokens_equal("abc", "abc"))
        self.assertFalse(ms.tokens_equal("abc", "abd"))
        self.assertFalse(ms.tokens_equal("", "abc"))


class FingerprintTests(unittest.TestCase):
    def test_short_and_stable(self):
        fp = ms.key_fingerprint("https://sub.example/abc")
        self.assertRegex(fp, r"^[0-9a-f]{4}$")
        self.assertEqual(fp, ms.key_fingerprint("https://sub.example/abc"))

    def test_temp_username_is_valid_for_panel(self):
        for _ in range(50):
            name = ms.temp_username()
            self.assertRegex(name, r"^tmp_[a-z0-9]{8}$")
            self.assertTrue(3 <= len(name) <= 36)


if __name__ == "__main__":
    unittest.main()
