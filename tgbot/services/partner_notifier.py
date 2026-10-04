"""
Доставка в Telegram всего, что связано с партнёрами: начисления партнёру, заявки
на вывод в топик админов (с кнопками решения) и итог по заявке.

Методы бросают исключения Telegram наружу — PartnerService._notify ловит их сам:
деньги уже посчитаны, сбой доставки их не откатывает.
"""
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from tgbot.keyboards.partner import REJECT_REASONS, withdrawal_admin_keyboard
from tgbot.services.partner_rules import fmt_rub

STATUS_TITLES = {"pending": "⏳ ждёт выплаты", "paid": "✅ выплачено", "rejected": "❌ отклонено"}


def _user_label(user) -> str:
    if user is None:
        return "—"
    name = escape(user.full_name or "без имени")
    username = f" @{escape(user.username)}" if user.username else ""
    return f"{name}{username} (ID <code>{user.user_id}</code>)"


def withdrawal_text(withdrawal, user) -> str:
    """Карточка заявки — одна и та же в топике выплат и в админке."""
    lines = [
        f"💸 <b>Заявка на вывод #{withdrawal.id}</b> — {STATUS_TITLES.get(withdrawal.status, withdrawal.status)}",
        "",
        f"Партнёр: {_user_label(user)}",
        f"Сумма: <b>{fmt_rub(withdrawal.amount_kop)}</b>",
        f"СБП: <code>{escape(withdrawal.sbp_phone)}</code>, {escape(withdrawal.sbp_bank)}",
        f"Создана: {withdrawal.created_at:%d.%m.%Y %H:%M}",
    ]
    if withdrawal.status != "pending" and withdrawal.processed_at:
        lines.append(f"Решение: {withdrawal.processed_at:%d.%m.%Y %H:%M}, админ {withdrawal.admin_id}")
    if withdrawal.status == "rejected" and withdrawal.reject_reason:
        lines.append(f"Причина: {escape(withdrawal.reject_reason)}")
    if withdrawal.status == "pending":
        lines += ["", "Переведите деньги по СБП и нажмите «Выплачено». При отказе сумма вернётся на баланс партнёра."]
    return "\n".join(lines)


def reject_reason_title(code: str) -> str:
    return REJECT_REASONS.get(code, code)


class PartnerNotifier:
    def __init__(self, bot: Bot, config):
        self._bot = bot
        self._config = config

    async def accrual(self, partner_id: int, entry) -> None:
        when = (
            "Уже на балансе."
            if entry.status == "available"
            else f"Станет доступно к выводу {entry.available_at:%d.%m.%Y}."
        )
        await self._bot.send_message(
            partner_id,
            f"💰 Ваш друг оплатил подписку на {fmt_rub(entry.base_amount_kop)} — "
            f"вам начислено <b>{fmt_rub(entry.amount_kop)}</b> ({entry.percent}%).\n{when}",
        )

    async def reversal(self, partner_id: int, outcome: str, amount_kop: int, entry) -> None:
        if outcome == "cancelled":
            text = (f"↩️ Друг вернул оплату — начисление {fmt_rub(entry.amount_kop)} отменено "
                    "(оно ещё не поступило на баланс).")
        else:
            text = f"↩️ Друг вернул оплату — с баланса списано {fmt_rub(amount_kop)}."
        await self._bot.send_message(partner_id, text)

    async def released(self, partner_id: int, amount_kop: int, balance_kop: int) -> None:
        await self._bot.send_message(
            partner_id,
            f"✅ {fmt_rub(amount_kop)} стали доступны к выводу. Баланс: <b>{fmt_rub(balance_kop)}</b>.\n"
            "Открыть: «👥 Пригласить друга» в меню или /referral.",
        )

    async def withdrawal_created(self, withdrawal, user) -> tuple[int, int] | None:
        """Заявка в топик выплат. Возвращает (chat_id, message_id), чтобы потом поправить сообщение."""
        chat_id, topic_id = self._config.tg_bot.partner_payout_target
        message = await self._bot.send_message(
            chat_id, withdrawal_text(withdrawal, user), message_thread_id=topic_id,
            reply_markup=withdrawal_admin_keyboard(withdrawal.id),
        )
        return chat_id, message.message_id

    async def withdrawal_resolved(self, withdrawal, user) -> None:
        if withdrawal.notify_chat_id and withdrawal.notify_message_id:
            try:
                await self._bot.edit_message_text(
                    withdrawal_text(withdrawal, user), chat_id=withdrawal.notify_chat_id,
                    message_id=withdrawal.notify_message_id, reply_markup=None,
                )
            except TelegramBadRequest:
                pass   # сообщение удалили или уже поправили тем же текстом
        if withdrawal.status == "paid":
            text = (f"✅ Выплата <b>{fmt_rub(withdrawal.amount_kop)}</b> по заявке #{withdrawal.id} отправлена "
                    f"по СБП на {escape(withdrawal.sbp_phone)} ({escape(withdrawal.sbp_bank)}).")
        else:
            reason = f"\nПричина: {escape(withdrawal.reject_reason)}" if withdrawal.reject_reason else ""
            text = (f"❌ Заявка на вывод #{withdrawal.id} отклонена, {fmt_rub(withdrawal.amount_kop)} "
                    f"вернулись на баланс.{reason}")
        await self._bot.send_message(withdrawal.partner_user_id, text)

    async def balance_payment(self, user, tariff, amount_kop: int) -> None:
        """Оплата подписки с баланса — в лог транзакций (реальных денег магазину не пришло)."""
        chat_id = self._config.tg_bot.support_chat_id
        topic_id = self._config.tg_bot.transaction_log_topic_id
        await self._bot.send_message(
            chat_id,
            f"💳 <b>Оплата с партнёрского баланса</b>\n"
            f"Партнёр: {_user_label(user)}\nТариф: {escape(tariff.name)}\nСумма: {fmt_rub(amount_kop)}",
            message_thread_id=topic_id,
        )
