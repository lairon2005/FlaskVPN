"""
Отправка в Telegram всего, что связано с операциями менеджеров.

Отдельный модуль, чтобы ManagerService оставался чистой бизнес-логикой: сервис
отдаёт готовый текст чека, а как и куда он попадёт (группа, личка менеджера,
личка клиента, QR-картинка, кнопки) решает нотификатор.

Все методы бросают исключения Telegram наружу — вызывающий (ManagerService._publish)
сам решает, что чек — отчётность и его сбой не должен отменять выдачу.
"""
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

from loader import logger
from tgbot.keyboards.manager import client_access_notice_keyboard, receipt_keyboard, web_login_notice_keyboard
from tgbot.services.qr_generator import create_qr_code


class ManagerNotifier:
    def __init__(self, bot: Bot, config):
        self._bot = bot
        self._config = config

    async def post_group_receipt(self, op_id: int, text: str,
                                 existing: tuple[int, int] | None) -> tuple[int, int] | None:
        """
        Групповой чек. Если для операции чек уже опубликован — правим то же
        сообщение (статус «ждём оплату» → «выдан» → «ключ удалён»), а не плодим новые.
        """
        chat_id, topic_id = self._config.tg_bot.manager_receipts_target
        if existing and existing[1]:
            try:
                await self._bot.edit_message_text(
                    chat_id=existing[0], message_id=existing[1], text=text,
                )
                return existing
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    return existing
                logger.warning(f"[manager] cannot edit group receipt of op {op_id}: {e}; posting a new one")
        message = await self._bot.send_message(chat_id=chat_id, message_thread_id=topic_id, text=text)
        return message.chat.id, message.message_id

    async def notify_manager(self, telegram_id: int, text: str, *, subscription_url: str | None = None,
                             operation_id: int | None = None) -> None:
        """Чек менеджеру. Для выданного ключа — ещё и QR для установки, чтобы клиент сразу сканировал."""
        await self._bot.send_message(
            telegram_id, text, reply_markup=receipt_keyboard(has_key=bool(subscription_url)),
            disable_web_page_preview=True,
        )
        if subscription_url:
            qr = create_qr_code(subscription_url)
            await self._bot.send_photo(
                telegram_id, BufferedInputFile(qr.getvalue(), filename="key.png"),
                caption="📲 QR для установки — клиент сканирует его в приложении",
            )

    async def notify_client(self, user_id: int, text: str) -> None:
        await self._bot.send_message(user_id, text)

    async def notify_client_access(self, user_id: int, manager_name: str) -> None:
        await self._bot.send_message(
            user_id,
            f"👔 Менеджер <b>{manager_name}</b> получил доступ к вашей подписке "
            "для установки и настройки VPN.\n\n"
            "Он видит срок подписки, трафик и число устройств — но не платёжные данные "
            "и не переписку. Доступ закроется сам; закрыть сейчас можно кнопкой ниже.",
            reply_markup=client_access_notice_keyboard(),
        )

    async def notify_manager_text(self, telegram_id: int, text: str) -> None:
        await self._bot.send_message(telegram_id, text)

    async def notify_web_login(self, telegram_id: int, *, ip: str | None, device: str | None, when: str) -> None:
        """О каждом входе в панель на сайте: если входил не он — одной кнопкой закрывает все сеансы."""
        lines = ["🔐 <b>Вход в панель менеджера на сайте</b>", f"🕒 {when} МСК"]
        if ip:
            lines.append(f"🌐 IP: <code>{escape(ip)}</code>")
        if device:
            lines.append(f"💻 {escape(device)}")
        lines.append("\nЕсли это были не вы — завершите все входы и смените пароль.")
        await self._bot.send_message(telegram_id, "\n".join(lines), reply_markup=web_login_notice_keyboard())
