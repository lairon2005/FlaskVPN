"""
Отправка в Telegram всего, что связано с операциями менеджеров.

Отдельный модуль, чтобы ManagerService оставался чистой бизнес-логикой: сервис
отдаёт готовый чек (текст и картинку), а как и куда он попадёт (группа, личка
менеджера, личка клиента, кнопки) решает нотификатор.

Чек — ВСЕГДА одно сообщение: картинка чека, а текст чека — подписью к ней. Не
нарисовалась картинка — уходит один текст. Подпись в Telegram ограничена 1024
символами: если чек длиннее, из подписи сначала уходит ссылка установки (она же
QR на картинке), и только потом текст обрезается.

Все методы бросают исключения Telegram наружу — вызывающий (ManagerService._publish)
сам решает, что чек — отчётность и его сбой не должен отменять выдачу.
"""
import re
from html import escape, unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto

from loader import logger
from tgbot.keyboards.manager import (
    client_access_notice_keyboard, receipt_keyboard, temp_expiring_keyboard, web_login_notice_keyboard,
)

CAPTION_LIMIT = 1024
_TAG = re.compile(r"<[^>]+>")
_INSTALL_LINK = re.compile(r"\n🔗 Ссылка для установки:\n<code>[^<]*</code>")


def _visible_len(html: str) -> int:
    """Сколько символов Telegram насчитает в подписи: без тегов и с раскрытыми сущностями."""
    return len(unescape(_TAG.sub("", html)))


def fit_caption(html: str) -> str:
    """Текст чека, который влезает в подпись к фото."""
    if _visible_len(html) <= CAPTION_LIMIT:
        return html
    html = _INSTALL_LINK.sub("", html)
    if _visible_len(html) <= CAPTION_LIMIT:
        return html
    plain = unescape(_TAG.sub("", html))
    return escape(plain[:CAPTION_LIMIT - 1] + "…")


def _photo(image: bytes, op_id: int | None = None) -> BufferedInputFile:
    name = f"check-M-{op_id:06d}.png" if op_id else "check.png"
    return BufferedInputFile(image, filename=name)


class ManagerNotifier:
    def __init__(self, bot: Bot, config):
        self._bot = bot
        self._config = config

    async def send_receipt(self, chat_id: int, text: str, image: bytes | None, *, op_id: int | None = None,
                           reply_markup=None, message_thread_id: int | None = None):
        """Чек одним сообщением: фото с подписью-текстом (или только текст, если картинки нет)."""
        if image:
            return await self._bot.send_photo(
                chat_id, _photo(image, op_id), caption=fit_caption(text), reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            )
        return await self._bot.send_message(
            chat_id, text, reply_markup=reply_markup, message_thread_id=message_thread_id,
            disable_web_page_preview=True,
        )

    async def post_group_receipt(self, op_id: int, text: str, existing: tuple[int, int] | None,
                                 image: bytes | None = None) -> tuple[int, int] | None:
        """
        Групповой чек. Если для операции чек уже опубликован — правим то же
        сообщение (статус «ждём оплату» → «выдан» → «ключ удалён»): и картинку, и подпись.
        Старый текстовый чек (до картинок) фото не станет — тогда публикуем новый.
        """
        chat_id, topic_id = self._config.tg_bot.manager_receipts_target
        if existing and existing[1]:
            try:
                if image:
                    await self._bot.edit_message_media(
                        chat_id=existing[0], message_id=existing[1],
                        media=InputMediaPhoto(media=_photo(image, op_id), caption=fit_caption(text)),
                    )
                else:
                    await self._bot.edit_message_text(chat_id=existing[0], message_id=existing[1], text=text)
                return existing
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    return existing
                logger.warning(f"[manager] cannot edit group receipt of op {op_id}: {e}; posting a new one")
        message = await self.send_receipt(chat_id, text, image, op_id=op_id, message_thread_id=topic_id)
        return message.chat.id, message.message_id

    async def notify_manager(self, telegram_id: int, text: str, *, image: bytes | None = None,
                             has_key: bool = False, client_code: str | None = None) -> None:
        """Чек менеджеру: картинка (с QR установки, если ключ выдан) и текст — одним сообщением."""
        await self.send_receipt(
            telegram_id, text, image, reply_markup=receipt_keyboard(has_key=has_key, client_code=client_code),
        )

    async def notify_client(self, user_id: int, text: str, image: bytes | None = None) -> None:
        await self.send_receipt(user_id, text, image)

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

    async def update_invoice(self, chat_id: int, message_id: int, text: str) -> None:
        """Живой статус счёта: правим подпись к QR оплаты и убираем кнопки — они больше не нужны."""
        try:
            await self._bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise

    async def notify_temp_expiring(self, telegram_id: int, key_id: int, *, minutes_left: int, until: str) -> None:
        await self._bot.send_message(
            telegram_id,
            f"⏱ <b>Пробный ключ закончится через {minutes_left} мин</b> (в {until} МСК)\n\n"
            "Самое время предложить клиенту подписку. Оформите её на этот же ключ — "
            "переустанавливать ничего не придётся.",
            reply_markup=temp_expiring_keyboard(key_id),
        )
