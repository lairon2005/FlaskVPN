# webapp/core/mail.py
"""Отправка писем с кодами.

Основной путь — HTTP API Resend (порт 443: не упирается в закрытые на VPS
25/465/587). Включается, если задан RESEND_API_KEY. Без него работает SMTP
(`MAIL_*`) как запасной вариант.
"""
import os
import asyncio
import logging
import httpx
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import EmailStr

logger = logging.getLogger(__name__)

MAIL_TIMEOUT = 15  # секунд на отправку письма
RESEND_API_URL = "https://api.resend.com/emails"

MAIL_FROM = os.getenv("MAIL_FROM", "noreply@flaskvpn.ru")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "FlaskVPN")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")


class MailSendError(Exception):
    """Ошибка отправки email — содержит безопасное сообщение для пользователя."""
    pass


def _smtp_config() -> ConnectionConfig:
    port = int(os.getenv("MAIL_PORT", 587))
    use_ssl = port == 465  # 465 = SSL/TLS; 587/25 = STARTTLS
    return ConnectionConfig(
        MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
        MAIL_FROM=MAIL_FROM,
        MAIL_FROM_NAME=MAIL_FROM_NAME,
        MAIL_PORT=port,
        MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
        MAIL_STARTTLS=not use_ssl,
        MAIL_SSL_TLS=use_ssl,
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=True,
    )


async def _send_via_resend(subject: str, recipients: list[str], html: str):
    async with httpx.AsyncClient(timeout=MAIL_TIMEOUT) as client:
        response = await client.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": f"{MAIL_FROM_NAME} <{MAIL_FROM}>",
                "to": recipients,
                "subject": subject,
                "html": html,
            },
        )
    if response.status_code >= 400:
        # Тело ответа Resend — описание ошибки (домен не подтверждён, неверный ключ), без секретов
        raise RuntimeError(f"Resend HTTP {response.status_code}: {response.text[:300]}")


async def _send_via_smtp(subject: str, recipients: list[str], html: str):
    message = MessageSchema(
        subject=subject,
        recipients=recipients,
        body=html,
        subtype=MessageType.html
    )
    await asyncio.wait_for(FastMail(_smtp_config()).send_message(message), timeout=MAIL_TIMEOUT)


async def _send_mail(subject: str, recipients: list[str], html: str):
    """Отправляет email с таймаутом; наружу — только безопасные сообщения."""
    send = _send_via_resend if RESEND_API_KEY else _send_via_smtp
    try:
        await send(subject, recipients, html)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        logger.error(f"Mail send timeout ({MAIL_TIMEOUT}s) for {recipients}")
        raise MailSendError("Сервер почты не отвечает. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Mail send failed for {recipients}: {e}")
        raise MailSendError("Не удалось отправить письмо. Попробуйте позже.")


def _brand_code_email(heading: str, lead: str, code: str, footnote: str) -> str:
    """HTML письма в оформлении бренд-бука FLASK.

    Вёрстка на таблицах и инлайн-стилях: почтовые клиенты режут <style> и
    веб-шрифты, поэтому цвета бренда прописаны значениями, а шрифты — системные.
    """
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="background-color:#F7F1E8;margin:0;padding:32px 12px;">
      <tr><td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="max-width:480px;background-color:#FFFCF6;border:1px solid #D5DFF4;border-radius:20px;">
          <tr><td style="padding:28px 28px 8px;">
            <div style="font-family:'Trebuchet MS',Verdana,Arial,sans-serif;font-size:22px;font-weight:bold;
                        color:#2457C5;letter-spacing:-0.5px;">Flask<span style="color:#FF7627;">VPN</span></div>
            <div style="font-family:Consolas,'Courier New',monospace;font-size:10px;letter-spacing:2px;
                        text-transform:uppercase;color:#8D97AE;padding-top:4px;">Freedom connects people</div>
          </td></tr>
          <tr><td style="padding:18px 28px 0;font-family:Arial,Helvetica,sans-serif;color:#17203A;">
            <h2 style="margin:0 0 8px;font-size:19px;color:#17203A;">{heading}</h2>
            <p style="margin:0;font-size:15px;line-height:1.6;color:#5C6A88;">{lead}</p>
          </td></tr>
          <tr><td align="center" style="padding:20px 28px 6px;">
            <div style="display:inline-block;background-color:#FFFFFF;border:2px solid #2457C5;border-radius:14px;
                        padding:14px 26px;font-family:Consolas,'Courier New',monospace;font-size:30px;
                        font-weight:bold;letter-spacing:8px;color:#2457C5;">{code}</div>
          </td></tr>
          <tr><td style="padding:10px 28px 28px;font-family:Arial,Helvetica,sans-serif;">
            <p style="margin:0;font-size:12px;line-height:1.6;color:#8D97AE;">{footnote}</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
    """


async def send_reset_code(email: EmailStr, code: str):
    """Отправляет код восстановления на почту"""
    html = _brand_code_email(
        "Сброс пароля",
        "Вы запросили сброс пароля. Введите этот код на странице восстановления:",
        code,
        "Если это были не вы, просто проигнорируйте письмо.",
    )
    await _send_mail("Сброс пароля | FlaskVPN", [email], html)


async def send_verification_email(email: EmailStr, code: str):
    """Отправляет код подтверждения email при регистрации"""
    html = _brand_code_email(
        "Подтверждение email",
        "Для подтверждения вашего email введите код:",
        code,
        "Код действителен 15 минут. Если вы не регистрировались, просто проигнорируйте это письмо.",
    )
    await _send_mail("Подтверждение Email | FlaskVPN", [email], html)