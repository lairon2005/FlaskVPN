# webapp/core/mail.py
import os
import asyncio
import logging
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import EmailStr

logger = logging.getLogger(__name__)

MAIL_TIMEOUT = 15  # секунд на отправку письма

# Читаем настройки прямо из ENV для простоты
_port = int(os.getenv("MAIL_PORT", 587))
# Порт 465 = SSL/TLS; 587/25 = STARTTLS
_use_ssl = _port == 465
conf = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
    MAIL_FROM=os.getenv("MAIL_FROM", "noreply@flaskvpn.ru"),
    MAIL_FROM_NAME=os.getenv("MAIL_FROM_NAME", "FlaskVPN"),
    MAIL_PORT=_port,
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_STARTTLS=not _use_ssl,
    MAIL_SSL_TLS=_use_ssl,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True
)


async def _send_mail(subject: str, recipients: list[str], html: str):
    """Отправляет email с таймаутом."""
    message = MessageSchema(
        subject=subject,
        recipients=recipients,
        body=html,
        subtype=MessageType.html
    )
    fm = FastMail(conf)
    try:
        await asyncio.wait_for(fm.send_message(message), timeout=MAIL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Mail send timeout ({MAIL_TIMEOUT}s) for {recipients}")
        raise MailSendError("Сервер почты не отвечает. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Mail send failed for {recipients}: {e}")
        raise MailSendError("Не удалось отправить письмо. Попробуйте позже.")


class MailSendError(Exception):
    """Ошибка отправки email — содержит безопасное сообщение для пользователя."""
    pass


async def send_reset_code(email: EmailStr, code: str):
    """Отправляет код восстановления на почту"""
    html = f"""
    <div style="background-color: #121212; color: #ffffff; padding: 20px; font-family: Arial;">
        <h2 style="color: #06b6d4;">FlaskVPN</h2>
        <p>Вы запросили сброс пароля.</p>
        <p>Ваш код подтверждения:</p>
        <h1 style="background: #2c2c2c; padding: 10px; display: inline-block; border-radius: 8px; border: 1px solid #06b6d4;">{code}</h1>
        <p style="color: #888; font-size: 12px; margin-top: 20px;">Если это были не вы, просто проигнорируйте письмо.</p>
    </div>
    """
    await _send_mail("Сброс пароля | FlaskVPN", [email], html)


async def send_verification_email(email: EmailStr, code: str):
    """Отправляет код подтверждения email при регистрации"""
    html = f"""
    <div style="background-color: #121212; color: #ffffff; padding: 20px; font-family: Arial;">
        <h2 style="color: #06b6d4;">FlaskVPN</h2>
        <p>Для подтверждения вашего email введите код:</p>
        <h1 style="background: #2c2c2c; padding: 10px; display: inline-block; border-radius: 8px; border: 1px solid #06b6d4;">{code}</h1>
        <p style="color: #888; font-size: 12px; margin-top: 20px;">Код действителен 15 минут. Если вы не регистрировались, просто проигнорируйте это письмо.</p>
    </div>
    """
    await _send_mail("Подтверждение Email | FlaskVPN", [email], html)