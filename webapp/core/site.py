# webapp/core/site.py
"""Общий контекст сайта: ссылки, канонический адрес, юридические реквизиты.

Регистрируется как Jinja-global `site` в webapp/main.py, поэтому шаблонам
не нужно тянуть config и не нужно хардкодить домен или ссылку на бота.

Реквизиты берутся из .env — выдуманных значений в шаблонах нет. Пока
переменные не заполнены, юридические страницы честно показывают прочерк,
а не правдоподобную выдумку.
"""
from dataclasses import dataclass, field

from environs import Env


@dataclass(frozen=True)
class SiteInfo:
    domain: str
    base_url: str
    bot_url: str | None
    support_url: str | None
    # --- Реквизиты для оферты, политики и футера ---
    legal_name: str | None          # «ИП Иванов Иван Иванович» / «Самозанятый …»
    legal_inn: str | None
    legal_ogrnip: str | None
    legal_address: str | None
    legal_email: str | None

    @property
    def has_legal(self) -> bool:
        """Заполнены ли реквизиты. Пока нет — в футере висит предупреждение
        только для владельца, а не правдоподобная заглушка для покупателя."""
        return bool(self.legal_name and self.legal_inn and self.legal_email)


def load_site_info() -> SiteInfo:
    env = Env()
    env.read_env()

    domain = env.str("DOMAIN", default="localhost").strip()
    scheme = "http" if domain.startswith("localhost") else "https"

    bot_username = env.str("TG_BOT_USERNAME", default=None)
    bot_url = f"https://t.me/{bot_username}" if bot_username else None

    # Поддержка ведётся в том же боте, отдельного аккаунта нет — если ссылки
    # на бота нет, кнопка поддержки в футере просто не рисуется.
    support_url = f"{bot_url}?start=support" if bot_url else None

    return SiteInfo(
        domain=domain,
        base_url=f"{scheme}://{domain}",
        bot_url=bot_url,
        support_url=support_url,
        legal_name=env.str("LEGAL_NAME", default=None),
        legal_inn=env.str("LEGAL_INN", default=None),
        legal_ogrnip=env.str("LEGAL_OGRNIP", default=None),
        legal_address=env.str("LEGAL_ADDRESS", default=None),
        legal_email=env.str("LEGAL_EMAIL", default=None),
    )
