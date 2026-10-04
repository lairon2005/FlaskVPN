"""Клавиатуры партнёрской (денежной) рефералки: экран партнёра в боте и заявки на вывод у админов."""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tgbot.services.partner_rules import fmt_rub

REJECT_REASONS = {
    "req": "Неверные реквизиты",
    "fraud": "Подозрение на накрутку",
    "other": "Другая причина — напишем в поддержке",
}


def partner_menu_keyboard(ov) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if ov.pending_withdrawal is None:
        builder.button(text="💸 Вывести на карту (СБП)", callback_data="pt:wd")
    builder.button(text="💳 Оплатить подписку с баланса", callback_data="pt:pay")
    builder.button(text="📜 История начислений", callback_data="pt:hist")
    builder.button(text="🏦 Реквизиты СБП", callback_data="pt:req")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def back_to_partner_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К балансу", callback_data="pt:menu")
    return builder.as_markup()


def withdraw_amount_keyboard(balance_kop: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Вывести всё — {fmt_rub(balance_kop)}", callback_data=f"pt:wd_sum:{balance_kop}")
    builder.button(text="⬅️ Отмена", callback_data="pt:menu")
    builder.adjust(1)
    return builder.as_markup()


def withdraw_confirm_keyboard(amount_kop: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить заявку", callback_data=f"pt:wd_ok:{amount_kop}")
    builder.button(text="⬅️ Отмена", callback_data="pt:menu")
    builder.adjust(1)
    return builder.as_markup()


def balance_tariffs_keyboard(offers) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for offer in offers:
        mark = "" if offer.affordable else "🔒 "
        builder.button(text=f"{mark}{offer.tariff.name} — {fmt_rub(offer.total_kop)}",
                       callback_data=f"pt:pay_t:{offer.tariff.id}")
    builder.button(text="⬅️ К балансу", callback_data="pt:menu")
    builder.adjust(1)
    return builder.as_markup()


def balance_pay_confirm_keyboard(tariff_id: int, nonce: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Оплатить с баланса", callback_data=f"pt:pay_ok:{tariff_id}:{nonce}")
    builder.button(text="⬅️ Отмена", callback_data="pt:pay")
    builder.adjust(1)
    return builder.as_markup()


def requisites_keyboard(has_requisites: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить" if has_requisites else "➕ Указать", callback_data="pt:req_edit")
    builder.button(text="⬅️ К балансу", callback_data="pt:menu")
    builder.adjust(1)
    return builder.as_markup()


def cancel_partner_fsm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="pt:menu")
    return builder.as_markup()


# --- админы: заявка на вывод -----------------------------------------------------

def withdrawal_admin_keyboard(withdrawal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выплачено", callback_data=f"pw_paid:{withdrawal_id}")
    builder.button(text="❌ Отклонить", callback_data=f"pw_rej:{withdrawal_id}")
    builder.adjust(2)
    return builder.as_markup()


def withdrawal_paid_confirm_keyboard(withdrawal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, деньги переведены", callback_data=f"pw_paid_ok:{withdrawal_id}")
    builder.button(text="⬅️ Назад", callback_data=f"pw_back:{withdrawal_id}")
    builder.adjust(1)
    return builder.as_markup()


def withdrawal_reject_keyboard(withdrawal_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, title in REJECT_REASONS.items():
        builder.button(text=title, callback_data=f"pw_rr:{withdrawal_id}:{code}")
    builder.button(text="⬅️ Назад", callback_data=f"pw_back:{withdrawal_id}")
    builder.adjust(1)
    return builder.as_markup()
