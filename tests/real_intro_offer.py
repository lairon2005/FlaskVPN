# tests/real_intro_offer.py
"""Настоящий tgbot/services/intro_offer.py для изолированных загрузчиков.

Модуль без зависимостей (чистые правила вводного тарифа), поэтому в стабы
кладём его как есть: подделка правил сделала бы тесты платежей бессмысленными.
"""
import importlib.util
from pathlib import Path


def real_intro_offer():
    path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "intro_offer.py"
    spec = importlib.util.spec_from_file_location("tgbot.services.intro_offer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
