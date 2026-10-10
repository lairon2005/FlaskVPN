# webapp/core/origin.py
"""Проверка источника POST-запроса — вторая линия защиты от CSRF поверх SameSite-cookie."""
from urllib.parse import urlparse

from fastapi import Request


def origin_allowed(request: Request) -> bool:
    """Origin (или Referer) запроса должен совпадать с хостом сайта. Нет заголовка — отказ."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    host = urlparse(source).netloc
    return bool(host) and host == request.headers.get("host", "")
