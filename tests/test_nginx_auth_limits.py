import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "etc/nginx/templates/default.conf.template").read_text()


def _server_blocks() -> list[str]:
    """Разбивает шаблон на top-level блоки server { ... } по балансу скобок."""
    blocks, depth, start = [], 0, None
    for i, ch in enumerate(TEMPLATE):
        if ch == "{":
            if depth == 0:
                start = TEMPLATE.rfind("server", 0, i)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(TEMPLATE[start:i + 1])
                start = None
    return [b for b in blocks if b.lstrip().startswith("server")]


class AuthRateLimitTests(unittest.TestCase):
    def test_zones_declared_before_first_server(self) -> None:
        first_server = TEMPLATE.index("server {")
        for zone in ("auth_code", "auth_mail"):
            decl = TEMPLATE.index(f"zone={zone}:")
            self.assertLess(decl, first_server)
        self.assertIn("limit_req_status 429;", TEMPLATE)

    def test_code_endpoints_limited_on_both_web_hosts(self) -> None:
        # основной хост (*.$DOMAIN) и app.$DOMAIN проксируют один flask_site —
        # лимит должен стоять на обоих, иначе его обходят через второй хост
        hosts = [b for b in _server_blocks() if "proxy_pass http://flask_site:8000" in b]
        self.assertEqual(len(hosts), 2)
        for block in hosts:
            for path, zone in (
                ("location = /verify-email", "auth_code"),
                ("location = /reset-password", "auth_code"),
            ):
                start = block.index(path)
                body = block[start:block.index("\n    }", start)]
                self.assertIn(f"limit_req zone={zone}", body, path)
                self.assertIn("proxy_pass http://flask_site:8000;", body, path)
            mail = re.search(r"location ~ \^/\((register\|resend-code\|forgot-password)\)\$ \{(.*?)\n    \}", block, re.S)
            self.assertIsNotNone(mail)
            self.assertIn("limit_req zone=auth_mail", mail.group(2))

    def test_all_zones_used(self) -> None:
        for zone in ("auth_code", "auth_mail"):
            self.assertIn(f"limit_req zone={zone}", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
