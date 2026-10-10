"""
Админка партнёров (денежная рефералка): кого сделать партнёром, персональная ставка,
отключение, общие настройки (процент, холд, минимум вывода) и заявки на вывод.

Заявки приходят в топик выплат (PARTNER_PAYOUT_TOPIC_ID) с кнопками — те же
callback'и `pw_*` работают и из топика, и из админки: IsAdmin проверяет нажавшего.
Причина отказа выбирается кнопкой, а не текстом: топики чата поддержки слушает
support_router, и свободный ввод там ушёл бы клиенту.
"""
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import settings_repo
from loader import logger
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import cancel_fsm_keyboard
from tgbot.keyboards.partner import (
    withdrawal_admin_keyboard, withdrawal_paid_confirm_keyboard, withdrawal_reject_keyboard,
)
from tgbot.services import partner_service, user_service
from tgbot.services.partner_notifier import reject_reason_title, withdrawal_text
from tgbot.services.partner_rules import (
    SETTING_HOLD_DAYS, SETTING_MIN_WITHDRAWAL, SETTING_PERCENT, fmt_rub,
)
from tgbot.services.partner_service import PartnerError
from tgbot.states.partner_states import AdminPartnerFSM

admin_partners_router = Router()
admin_partners_router.message.filter(IsAdmin())
admin_partners_router.callback_query.filter(IsAdmin())

SETTINGS = {
    SETTING_PERCENT: ("Общая ставка", "% с каждой оплаты друга", 1, 100),
    SETTING_HOLD_DAYS: ("Холд", "дней от оплаты друга до доступности к выводу", 0, 90),
    SETTING_MIN_WITHDRAWAL: ("Минимум вывода", "₽", 1, 1_000_000),
}


def _kb(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, data in rows:
        builder.button(text=text, callback_data=data)
    builder.adjust(1)
    return builder.as_markup()


async def _edit(message: Message, text: str, reply_markup) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)


def _name(user) -> str:
    label = escape(user.full_name or "без имени")
    return f"{label} @{escape(user.username)}" if user.username else label


# --- список -------------------------------------------------------------------------

async def _render_list(message: Message) -> None:
    partners = await partner_service.list_partners()
    pending = await partner_service.list_pending_withdrawals()
    builder = InlineKeyboardBuilder()
    for partner, user in partners:
        icon = "✅" if partner.status == "active" else "⏸"
        builder.button(text=f"{icon} {user.full_name or user.user_id} · {fmt_rub(partner.balance_kop)}",
                       callback_data=f"admin_pt:{partner.user_id}")
    builder.button(text="➕ Сделать партнёром", callback_data="admin_pt_new")
    builder.button(text=f"💸 Заявки на вывод ({len(pending)})", callback_data="admin_pt_wds")
    builder.button(text="⚙️ Настройки", callback_data="admin_pt_settings")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_main_menu")
    builder.adjust(1)
    settings = await partner_service.settings()
    await _edit(
        message,
        "🤝 <b>Партнёры</b>\n\n"
        f"Денежная рефералка вместо дней: с каждой рублёвой оплаты приглашённого друга — "
        f"<b>{settings.percent}%</b> на баланс партнёра, холд {settings.hold_days} дн., "
        f"вывод от {settings.min_withdrawal} ₽ по СБП.\n\n"
        + ("" if partners else "Пока нет ни одного партнёра."),
        builder.as_markup(),
    )


@admin_partners_router.callback_query(F.data == "admin_partners")
async def partners_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _render_list(call.message)


@admin_partners_router.callback_query(F.data == "admin_pt_new")
async def partner_new(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminPartnerFSM.new_partner)
    await _edit(call.message,
                "Кого сделать партнёром? Пришлите Telegram ID, @username или email.\n\n"
                "Пользователь должен быть в базе (бот или сайт); без Telegram уведомления ему не придут. Друзья, приглашённые им раньше, "
                "в зачёт не пойдут — только новые.",
                cancel_fsm_keyboard("admin_partners"))


@admin_partners_router.message(AdminPartnerFSM.new_partner)
async def partner_new_apply(message: Message, state: FSMContext):
    try:
        partner, user = await partner_service.add_partner(message.text or "", message.from_user.id)
    except PartnerError as e:
        await message.answer(f"❌ {e.message}\n\nПопробуйте ещё раз:", reply_markup=cancel_fsm_keyboard("admin_partners"))
        return
    await state.clear()
    try:
        settings = await partner_service.settings()
        await message.bot.send_message(
            user.user_id,
            "🤝 Вам подключена <b>партнёрская программа</b>!\n\n"
            f"Теперь с каждой оплаты друзей, пришедших по вашей ссылке, вам начисляется "
            f"<b>{partner.percent or settings.percent}%</b> на баланс. Его можно вывести по СБП "
            "или оплатить им свою подписку.\n\nОткрыть: «👥 Пригласить друга» в меню или /referral.",
        )
    except Exception as e:
        logger.warning(f"[partner] не удалось уведомить нового партнёра {user.user_id}: {e}")
    menu = await message.answer("Загружаю…")
    await _render_card(menu, user.user_id)


# --- карточка ---------------------------------------------------------------------------

async def _render_card(message: Message, user_id: int) -> None:
    ov = await partner_service.overview(user_id)
    user = await user_service.get_user(user_id)
    if ov is None or user is None:
        await _edit(message, "Партнёр не найден.", _kb([("⬅️ К партнёрам", "admin_partners")]))
        return
    status = "✅ активен" if ov.active else "⏸ отключён (новых начислений нет)"
    percent = f"{ov.percent}% (персональная)" if ov.personal_percent else f"{ov.percent}% (общая)"
    requisites = (f"<code>{escape(ov.sbp_phone)}</code>, {escape(ov.sbp_bank)}"
                  if ov.has_requisites else "не указаны")
    pending = (f"\n🕓 Заявка #{ov.pending_withdrawal.id} на {fmt_rub(ov.pending_withdrawal.amount_kop)}"
               if ov.pending_withdrawal else "")
    text = (
        f"🤝 <b>{_name(user)}</b> (ID <code>{user_id}</code>)\n\n"
        f"Статус: {status}\nСтавка: {percent}\n\n"
        f"💰 Баланс: <b>{fmt_rub(ov.balance_kop)}</b>\n⏳ В холде: {fmt_rub(ov.hold_kop)}\n"
        f"📈 Заработано: {fmt_rub(ov.earned_kop)}\n💸 Выведено: {fmt_rub(ov.withdrawn_kop)}\n"
        f"👥 Приглашено: {ov.invited} · оплатили: {ov.paid_friends}\n"
        f"🏦 СБП: {requisites}{pending}"
    )
    rows = [("⏸ Отключить", f"admin_pt_off:{user_id}") if ov.active else ("▶️ Включить", f"admin_pt_on:{user_id}"),
            ("✏️ Персональная ставка", f"admin_pt_pct:{user_id}")]
    if ov.personal_percent:
        rows.append(("↩️ Сбросить на общую", f"admin_pt_pctx:{user_id}"))
    if ov.pending_withdrawal:
        rows.append(("💸 Открыть заявку", f"pw_back:{ov.pending_withdrawal.id}"))
    rows.append(("⬅️ К партнёрам", "admin_partners"))
    await _edit(message, text, _kb(rows))


@admin_partners_router.callback_query(F.data.startswith("admin_pt:"))
async def partner_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _render_card(call.message, int(call.data.split(":", 1)[1]))


@admin_partners_router.callback_query(F.data.startswith("admin_pt_off:"))
async def partner_disable(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    await partner_service.disable(user_id, call.from_user.id)
    await call.answer("Отключён: новых начислений не будет, баланс остаётся у партнёра.", show_alert=True)
    await _render_card(call.message, user_id)


@admin_partners_router.callback_query(F.data.startswith("admin_pt_on:"))
async def partner_enable(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    await partner_service.enable(user_id, call.from_user.id)
    await call.answer("Включён.")
    await _render_card(call.message, user_id)


@admin_partners_router.callback_query(F.data.startswith("admin_pt_pctx:"))
async def partner_percent_reset(call: CallbackQuery):
    user_id = int(call.data.split(":", 1)[1])
    await partner_service.set_percent(user_id, None)
    await call.answer("Ставка — общая.")
    await _render_card(call.message, user_id)


@admin_partners_router.callback_query(F.data.startswith("admin_pt_pct:"))
async def partner_percent_start(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":", 1)[1])
    await call.answer()
    await state.set_state(AdminPartnerFSM.edit_percent)
    await state.update_data(partner_id=user_id)
    await _edit(call.message, "Введите персональную ставку партнёра, % (от 1 до 100). "
                              "Действует для новых оплат друзей.",
                cancel_fsm_keyboard(f"admin_pt:{user_id}"))


@admin_partners_router.message(AdminPartnerFSM.edit_percent)
async def partner_percent_apply(message: Message, state: FSMContext):
    user_id = (await state.get_data()).get("partner_id")
    try:
        await partner_service.set_percent(user_id, int((message.text or "").strip()))
    except (ValueError, PartnerError):
        await message.answer(PartnerError("bad_percent").message, reply_markup=cancel_fsm_keyboard(f"admin_pt:{user_id}"))
        return
    await state.clear()
    logger.info(f"[admin] ставка партнёра {user_id} = {message.text} (админ {message.from_user.id})")
    menu = await message.answer("Загружаю…")
    await _render_card(menu, user_id)


# --- общие настройки -----------------------------------------------------------------

async def _render_settings(message: Message) -> None:
    s = await partner_service.settings()
    await _edit(
        message,
        "⚙️ <b>Настройки партнёрской программы</b>\n\n"
        f"Общая ставка: <b>{s.percent}%</b>\nХолд: <b>{s.hold_days} дн.</b>\n"
        f"Минимум вывода: <b>{s.min_withdrawal} ₽</b>\n\n"
        "<i>Ставка и холд применяются к новым оплатам друзей; уже начисленное не пересчитывается. "
        "Персональная ставка партнёра важнее общей.</i>",
        _kb([("💯 Ставка", f"admin_ptset:{SETTING_PERCENT}"), ("⏳ Холд", f"admin_ptset:{SETTING_HOLD_DAYS}"),
             ("💸 Минимум вывода", f"admin_ptset:{SETTING_MIN_WITHDRAWAL}"), ("⬅️ К партнёрам", "admin_partners")]),
    )


@admin_partners_router.callback_query(F.data == "admin_pt_settings")
async def partner_settings(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _render_settings(call.message)


@admin_partners_router.callback_query(F.data.startswith("admin_ptset:"))
async def partner_setting_start(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    if key not in SETTINGS:
        await call.answer("Неизвестная настройка", show_alert=True)
        return
    title, hint, minimum, maximum = SETTINGS[key]
    await call.answer()
    await state.set_state(AdminPartnerFSM.edit_setting)
    await state.update_data(setting_key=key)
    await _edit(call.message, f"✏️ <b>{title}</b> ({hint})\n\nВведите число от {minimum} до {maximum}:",
                cancel_fsm_keyboard("admin_pt_settings"))


@admin_partners_router.message(AdminPartnerFSM.edit_setting)
async def partner_setting_apply(message: Message, state: FSMContext):
    key = (await state.get_data()).get("setting_key")
    if key not in SETTINGS:
        await state.clear()
        return
    title, hint, minimum, maximum = SETTINGS[key]
    try:
        value = int((message.text or "").strip())
    except ValueError:
        value = None
    if value is None or not minimum <= value <= maximum:
        await message.answer(f"Нужно целое число от {minimum} до {maximum}:",
                             reply_markup=cancel_fsm_keyboard("admin_pt_settings"))
        return
    await settings_repo.set(key, value)
    await state.clear()
    logger.info(f"[admin] {key} изменён на {value} админом {message.from_user.id}")
    menu = await message.answer(f"✅ {title} — теперь {value}.")
    await _render_settings(menu)


# --- заявки на вывод -------------------------------------------------------------------

@admin_partners_router.callback_query(F.data == "admin_pt_wds")
async def withdrawals_list(call: CallbackQuery):
    await call.answer()
    pending = await partner_service.list_pending_withdrawals()
    rows = [(f"#{w.id} · {fmt_rub(w.amount_kop)} · {w.created_at:%d.%m}", f"pw_back:{w.id}") for w in pending]
    rows.append(("⬅️ К партнёрам", "admin_partners"))
    await _edit(call.message,
                "💸 <b>Заявки на вывод</b>\n\n" + ("Новых заявок нет." if not pending else "Ждут выплаты:"),
                _kb(rows))


async def _show_withdrawal(call: CallbackQuery, withdrawal_id: int, reply_markup=None) -> None:
    withdrawal = await partner_service.get_withdrawal(withdrawal_id)
    if withdrawal is None:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    user = await user_service.get_user(withdrawal.partner_user_id)
    if reply_markup is None and withdrawal.status == "pending":
        reply_markup = withdrawal_admin_keyboard(withdrawal.id)
    await _edit(call.message, withdrawal_text(withdrawal, user), reply_markup)


@admin_partners_router.callback_query(F.data.startswith("pw_back:"))
async def withdrawal_card(call: CallbackQuery):
    await call.answer()
    await _show_withdrawal(call, int(call.data.split(":", 1)[1]))


@admin_partners_router.callback_query(F.data.startswith("pw_paid:"))
async def withdrawal_paid_ask(call: CallbackQuery):
    await call.answer()
    withdrawal_id = int(call.data.split(":", 1)[1])
    await _show_withdrawal(call, withdrawal_id, withdrawal_paid_confirm_keyboard(withdrawal_id))


@admin_partners_router.callback_query(F.data.startswith("pw_rej:"))
async def withdrawal_reject_ask(call: CallbackQuery):
    await call.answer()
    withdrawal_id = int(call.data.split(":", 1)[1])
    await _show_withdrawal(call, withdrawal_id, withdrawal_reject_keyboard(withdrawal_id))


async def _resolve(call: CallbackQuery, withdrawal_id: int, *, paid: bool, reason: str | None = None) -> None:
    try:
        withdrawal = await partner_service.resolve_withdrawal(withdrawal_id, call.from_user.id, paid=paid, reason=reason)
    except PartnerError as e:
        await call.answer(e.message, show_alert=True)
        await _show_withdrawal(call, withdrawal_id)
        return
    await call.answer("Готово: партнёр уведомлён.")
    # Нажали не в топике (из админки) — поправить и это сообщение; топик правит нотификатор.
    if (call.message.chat.id, call.message.message_id) != (withdrawal.notify_chat_id, withdrawal.notify_message_id):
        await _show_withdrawal(call, withdrawal_id, _kb([("⬅️ К заявкам", "admin_pt_wds")]))


@admin_partners_router.callback_query(F.data.startswith("pw_paid_ok:"))
async def withdrawal_paid(call: CallbackQuery):
    await _resolve(call, int(call.data.split(":", 1)[1]), paid=True)


@admin_partners_router.callback_query(F.data.startswith("pw_rr:"))
async def withdrawal_reject(call: CallbackQuery):
    _, withdrawal_id, code = call.data.split(":", 2)
    await _resolve(call, int(withdrawal_id), paid=False, reason=reject_reason_title(code))
