"""
Экран партнёра (денежная рефералка): баланс, вывод по СБП, оплата подписки с баланса,
история начислений, реквизиты. Callback-префикс `pt:`.

Партнёру «👥 Пригласить друга» и /referral открывают этот экран вместо обычной
рефералки (см. start.show_referral_info). Отключённый партнёр сюда попадает кнопкой
«💰 Партнёрский баланс» — новых начислений нет, но баланс вывести/потратить можно.

Суммы и права из callback_data не верятся: сервис перепроверяет баланс на каждом шаге.
"""
import secrets
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from loader import logger
from tgbot.keyboards.partner import (
    back_to_partner_keyboard, balance_pay_confirm_keyboard, balance_tariffs_keyboard,
    cancel_partner_fsm_keyboard, partner_menu_keyboard, requisites_keyboard,
    withdraw_amount_keyboard, withdraw_confirm_keyboard,
)
from tgbot.services import partner_service, user_service
from tgbot.services.partner_rules import fmt_rub, normalize_phone, parse_rub
from tgbot.services.partner_service import PartnerError
from tgbot.services.referral_service import REFERRAL_TRIAL_DAYS
from tgbot.services.utils import decline_word
from tgbot.states.partner_states import PartnerFSM

partner_router = Router()

LEDGER_TITLES = {
    "accrual": "💰 с оплаты друга",
    "reversal": "↩️ возврат оплаты друга",
    "withdrawal": "💸 вывод",
    "withdrawal_return": "↩️ вывод отклонён",
    "spend": "💳 оплата подписки",
    "spend_return": "↩️ оплата не прошла",
}
ACCRUAL_STATUS = {"hold": " · в холде", "cancelled": " · отменено", "reversed": " · списано"}


def _days(n: int) -> str:
    return f"{n} {decline_word(n, ('день', 'дня', 'дней'))}"


async def _show(event: Message | CallbackQuery, text: str, reply_markup) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
            return
        except TelegramBadRequest:
            pass
        await event.message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
    else:
        await event.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)


async def _referral_link(bot: Bot, user_id: int) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=ref{user_id}"


async def show_partner_menu(event: Message | CallbackQuery, bot: Bot) -> bool:
    """Экран партнёра. False — пользователь не партнёр (покажут обычную рефералку)."""
    user_id = event.from_user.id
    ov = await partner_service.overview(user_id)
    if ov is None:
        return False

    lines = ["🤝 <b>Партнёрская программа</b>", ""]
    if ov.active:
        link = await _referral_link(bot, user_id)
        lines += [
            f"С каждой оплаты приглашённого друга — <b>{ov.percent}%</b> вам на баланс.",
            f"Другу: {_days(REFERRAL_TRIAL_DAYS)} бесплатно.",
            "",
            f"Ваша ссылка: <code>{link}</code>",
        ]
    else:
        lines.append("Партнёрская программа отключена: новые начисления не поступают. "
                     "Накопленный баланс можно вывести или оплатить им подписку.")
    lines += ["", f"💰 Баланс: <b>{fmt_rub(ov.balance_kop)}</b>"]
    if ov.hold_kop:
        release = f", ближайшее зачисление {ov.next_release:%d.%m}" if ov.next_release else ""
        lines.append(f"⏳ В холде: {fmt_rub(ov.hold_kop)}{release}")
    lines += [
        f"👥 Приглашено: {ov.invited} · оплатили: {ov.paid_friends}",
        f"📈 Заработано: {fmt_rub(ov.earned_kop)} · выведено: {fmt_rub(ov.withdrawn_kop)}",
    ]
    if ov.pending_withdrawal is not None:
        w = ov.pending_withdrawal
        lines += ["", f"🕓 Заявка на вывод #{w.id} на {fmt_rub(w.amount_kop)} — на рассмотрении."]
    lines += [
        "",
        f"<i>Начисление становится доступным через {_days(ov.settings.hold_days)} после оплаты друга. "
        f"Вывод — от {fmt_rub(ov.settings.min_withdrawal_kop)} по СБП.</i>",
    ]
    if ov.active:
        share = (f"Пользуюсь стабильным VPN: 5 стран, без ограничения скорости. "
                 f"По моей ссылке {_days(REFERRAL_TRIAL_DAYS)} бесплатно → {link}")
        lines += ["", "📤 <b>Текст для пересылки другу</b> (нажмите, чтобы скопировать):", f"<code>{share}</code>"]

    await _show(event, "\n".join(lines), partner_menu_keyboard(ov))
    return True


@partner_router.callback_query(F.data == "pt:menu")
async def partner_menu(call: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await call.answer()
    if not await show_partner_menu(call, bot):
        await _show(call, "Партнёрская программа для вас не подключена.", back_to_partner_keyboard())


# --- реквизиты ------------------------------------------------------------------

@partner_router.callback_query(F.data == "pt:req")
async def requisites_show(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    ov = await partner_service.overview(call.from_user.id)
    if ov is None:
        return
    if ov.has_requisites:
        text = (f"🏦 <b>Реквизиты для вывода (СБП)</b>\n\nТелефон: <code>{escape(ov.sbp_phone)}</code>\n"
                f"Банк: {escape(ov.sbp_bank)}")
    else:
        text = "🏦 <b>Реквизиты для вывода (СБП)</b>\n\nПока не указаны."
    await _show(call, text, requisites_keyboard(ov.has_requisites))


async def _ask_phone(event: Message | CallbackQuery, state: FSMContext, then: str) -> None:
    await state.set_state(PartnerFSM.enter_phone)
    await state.update_data(then=then)
    await _show(event, "📱 Введите номер телефона, привязанный к СБП (например, +7 999 123-45-67):",
                cancel_partner_fsm_keyboard())


@partner_router.callback_query(F.data == "pt:req_edit")
async def requisites_edit(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _ask_phone(call, state, then="req")


@partner_router.message(PartnerFSM.enter_phone)
async def requisites_phone(message: Message, state: FSMContext):
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer(PartnerError("bad_phone").message, reply_markup=cancel_partner_fsm_keyboard())
        return
    await state.update_data(phone=phone)
    await state.set_state(PartnerFSM.enter_bank)
    await message.answer(f"Телефон: <code>{phone}</code>\n\n🏦 Теперь название банка (например, Т-Банк, Сбербанк):",
                         reply_markup=cancel_partner_fsm_keyboard())


@partner_router.message(PartnerFSM.enter_bank)
async def requisites_bank(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        phone, bank = await partner_service.set_requisites(message.from_user.id, data.get("phone", ""),
                                                           message.text or "")
    except PartnerError as e:
        await message.answer(e.message, reply_markup=cancel_partner_fsm_keyboard())
        return
    await message.answer(f"✅ Реквизиты сохранены: <code>{escape(phone)}</code>, {escape(bank)}.")
    if data.get("then") == "wd":
        await _ask_amount(message, state)
        return
    await state.clear()
    await message.answer("Готово.", reply_markup=back_to_partner_keyboard())


# --- вывод ----------------------------------------------------------------------------

async def _ask_amount(event: Message | CallbackQuery, state: FSMContext) -> None:
    ov = await partner_service.overview(event.from_user.id)
    if ov is None:
        await state.clear()
        return
    await state.set_state(PartnerFSM.enter_amount)
    await state.update_data(then=None)
    await _show(
        event,
        f"💸 <b>Вывод по СБП</b>\n\nДоступно: <b>{fmt_rub(ov.balance_kop)}</b>, минимум — "
        f"{fmt_rub(ov.settings.min_withdrawal_kop)}.\nНа: <code>{escape(ov.sbp_phone)}</code>, "
        f"{escape(ov.sbp_bank)}\n\nВведите сумму или нажмите кнопку:",
        withdraw_amount_keyboard(ov.balance_kop),
    )


@partner_router.callback_query(F.data == "pt:wd")
async def withdraw_start(call: CallbackQuery, state: FSMContext):
    ov = await partner_service.overview(call.from_user.id)
    if ov is None:
        await call.answer()
        return
    if ov.pending_withdrawal is not None:
        await call.answer(PartnerError("pending_exists").message, show_alert=True)
        return
    if ov.balance_kop < ov.settings.min_withdrawal_kop:
        await call.answer(f"Вывод — от {fmt_rub(ov.settings.min_withdrawal_kop)}. "
                          f"Сейчас на балансе {fmt_rub(ov.balance_kop)}.", show_alert=True)
        return
    await call.answer()
    if not ov.has_requisites:
        await _ask_phone(call, state, then="wd")
        return
    await _ask_amount(call, state)


async def _confirm_withdraw(event: Message | CallbackQuery, state: FSMContext, amount_kop: int) -> None:
    ov = await partner_service.overview(event.from_user.id)
    if ov is None:
        await state.clear()
        return
    problem = None
    if amount_kop < ov.settings.min_withdrawal_kop:
        problem = f"Минимальная сумма вывода — {fmt_rub(ov.settings.min_withdrawal_kop)}."
    elif amount_kop > ov.balance_kop:
        problem = f"На балансе только {fmt_rub(ov.balance_kop)}."
    if problem:
        if isinstance(event, CallbackQuery):
            await event.answer(problem, show_alert=True)
        else:
            await event.answer(problem + " Введите другую сумму:", reply_markup=withdraw_amount_keyboard(ov.balance_kop))
        return
    await state.clear()
    await _show(
        event,
        f"Отправить заявку на вывод <b>{fmt_rub(amount_kop)}</b>?\n\n"
        f"СБП: <code>{escape(ov.sbp_phone)}</code>, {escape(ov.sbp_bank)}\n\n"
        "Сумма сразу спишется с баланса. Администратор переведёт деньги вручную; "
        "если заявку отклонят — деньги вернутся на баланс.",
        withdraw_confirm_keyboard(amount_kop),
    )


@partner_router.message(PartnerFSM.enter_amount)
async def withdraw_amount_text(message: Message, state: FSMContext):
    amount_kop = parse_rub(message.text or "")
    if amount_kop is None:
        await message.answer("Нужна сумма числом, например 1500. Попробуйте ещё раз:",
                             reply_markup=cancel_partner_fsm_keyboard())
        return
    await _confirm_withdraw(message, state, amount_kop)


@partner_router.callback_query(F.data.startswith("pt:wd_sum:"))
async def withdraw_amount_button(call: CallbackQuery, state: FSMContext):
    await _confirm_withdraw(call, state, int(call.data.rsplit(":", 1)[1]))


@partner_router.callback_query(F.data.startswith("pt:wd_ok:"))
async def withdraw_confirm(call: CallbackQuery, bot: Bot):
    amount_kop = int(call.data.rsplit(":", 1)[1])
    try:
        withdrawal = await partner_service.request_withdrawal(call.from_user.id, amount_kop)
    except PartnerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    await _show(
        call,
        f"✅ Заявка #{withdrawal.id} на {fmt_rub(withdrawal.amount_kop)} отправлена. "
        "Мы пришлём сообщение, когда деньги будут переведены.",
        back_to_partner_keyboard(),
    )


# --- оплата подписки с баланса -----------------------------------------------------

@partner_router.callback_query(F.data == "pt:pay")
async def balance_pay_list(call: CallbackQuery):
    try:
        offers = await partner_service.balance_offers(call.from_user.id)
    except PartnerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    ov = await partner_service.overview(call.from_user.id)
    if not offers:
        await _show(call, "Сейчас нет тарифов для оплаты.", back_to_partner_keyboard())
        return
    await _show(
        call,
        f"💳 <b>Оплата подписки с баланса</b>\n\nБаланс: <b>{fmt_rub(ov.balance_kop)}</b>\n\n"
        "Оплачивается только целиком. В сумму входят уже подключённые доп. устройства и трафик — "
        "как при обычном продлении. 🔒 — на балансе пока не хватает.",
        balance_tariffs_keyboard(offers),
    )


@partner_router.callback_query(F.data.startswith("pt:pay_t:"))
async def balance_pay_pick(call: CallbackQuery):
    tariff_id = int(call.data.rsplit(":", 1)[1])
    try:
        offers = await partner_service.balance_offers(call.from_user.id)
    except PartnerError as e:
        await call.answer(e.message, show_alert=True)
        return
    offer = next((o for o in offers if o.tariff.id == tariff_id), None)
    if offer is None:
        await call.answer(PartnerError("tariff_unavailable").message, show_alert=True)
        return
    ov = await partner_service.overview(call.from_user.id)
    if not offer.affordable:
        await call.answer(f"Не хватает {fmt_rub(offer.total_kop - ov.balance_kop)}.", show_alert=True)
        return
    await call.answer()
    q = offer.quote
    parts = [f"Тариф «{escape(offer.tariff.name)}»: {q.tariff_amount:.2f} ₽"]
    if q.slots_amount:
        parts.append(f"Доп. устройства ({q.slots}): {q.slots_amount:.2f} ₽")
    if q.traffic_amount:
        parts.append(f"Доп. трафик ({q.traffic_gb} ГБ/мес): {q.traffic_amount:.2f} ₽")
    await _show(
        call,
        "💳 <b>Подтвердите оплату с баланса</b>\n\n" + "\n".join(parts) +
        f"\n\nИтого: <b>{fmt_rub(offer.total_kop)}</b> (баланс {fmt_rub(ov.balance_kop)} → "
        f"{fmt_rub(ov.balance_kop - offer.total_kop)})\n"
        f"Подписка продлится на {_days(offer.tariff.duration_days)}.",
        balance_pay_confirm_keyboard(tariff_id, secrets.token_hex(6)),
    )


@partner_router.callback_query(F.data.startswith("pt:pay_ok:"))
async def balance_pay_confirm(call: CallbackQuery):
    _, _, tariff_id, nonce = call.data.split(":", 3)
    try:
        result, replayed = await partner_service.pay_with_balance(call.from_user.id, int(tariff_id), nonce)
    except PartnerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    user = await user_service.get_user(call.from_user.id)
    until = user.subscription_end_date if user else None
    until_text = f" до <b>{until:%d.%m.%Y}</b>" if until else ""
    head = "Эта оплата уже проведена." if replayed else "✅ Оплачено с баланса."
    logger.info(f"[partner] {call.from_user.id}: оплата с баланса, tariff={tariff_id}, replayed={replayed}")
    await _show(call, f"{head} Подписка активна{until_text}.", back_to_partner_keyboard())


# --- история ---------------------------------------------------------------------------

@partner_router.callback_query(F.data == "pt:hist")
async def history(call: CallbackQuery):
    await call.answer()
    entries = await partner_service.history(call.from_user.id, limit=15)
    if not entries:
        await _show(call, "📜 Пока нет ни одного движения по балансу.", back_to_partner_keyboard())
        return
    lines = ["📜 <b>Последние операции</b>", ""]
    for e in entries:
        created = e.created_at or datetime.now()
        title = LEDGER_TITLES.get(e.kind, e.kind)
        status = ACCRUAL_STATUS.get(e.status, "") if e.kind == "accrual" else ""
        amount = f"+{fmt_rub(e.amount_kop)}" if e.amount_kop > 0 else fmt_rub(e.amount_kop)
        lines.append(f"{created:%d.%m} {title}: <b>{amount}</b>{status}")
    await _show(call, "\n".join(lines), back_to_partner_keyboard())
