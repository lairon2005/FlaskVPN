"""
Админка менеджеров: приглашение, логин для сайта, сброс пароля, права, лимиты,
статистика, журнал, инкассация.

Пароль админ не видит и не задаёт: логин назначает он, а пароль менеджер придумывает
сам по одноразовой ссылке из бота (после приглашения или после сброса).

Только администратор (IsAdmin на роутере) может создавать, блокировать и удалять
менеджеров, менять их права и видеть журнал всех. Удаление мягкое: журнал и чеки
остаются, доступ закрывается навсегда.
"""
import csv
import io

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from loader import config, logger
from tgbot.commands import apply_commands
from tgbot.filters.admin import IsAdmin
from tgbot.filters.manager import forget
from tgbot.handlers.manager.common import OP_TITLES, STATUS_ICONS, password_link_text, site_url
from tgbot.keyboards.inline import cancel_fsm_keyboard
from tgbot.keyboards.manager import password_link_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_dt, fmt_money, receipt_number
from tgbot.services.manager_service import ManagerError
from tgbot.states.admin_manager_states import AdminManagerFSM

admin_managers_router = Router()
admin_managers_router.message.filter(IsAdmin())
admin_managers_router.callback_query.filter(IsAdmin())

RIGHTS = {
    "can_issue_tariff": "Выдача по тарифу",
    "can_issue_custom": "Свои дни",
    "can_issue_temp": "Временные ключи",
    "can_accept_cash": "Приём наличных",
    "can_view_global_stats": "Общая статистика",
}
NUMBERS = {
    "temp_keys_per_day": ("Лимит временных ключей в день", 0, 1000),
    "cash_limit": ("Потолок наличных «к сдаче», ₽ («-» — без лимита)", 0, 10_000_000),
}
STATUS_TITLES = {"invited": "⏳ приглашён", "active": "✅ активен", "blocked": "🚫 заблокирован"}
LOG_PAGE = 10


def _bot_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=mgr_{token}"


async def _bot_username(bot: Bot) -> str:
    return config.tg_bot.tg_bot_username or (await bot.get_me()).username


def _kb(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            builder.button(text=text, callback_data=data)
    builder.adjust(*[len(r) for r in rows])
    return builder.as_markup()


# --- Список ---------------------------------------------------------------------

async def _render_list(message: Message) -> None:
    managers = await manager_service.list_managers()
    builder = InlineKeyboardBuilder()
    for m in managers:
        icon = {"active": "✅", "invited": "⏳", "blocked": "🚫"}.get(m.status, "•")
        builder.button(text=f"{icon} {m.display_name} (#{m.id})", callback_data=f"admin_mgr:{m.id}")
    builder.button(text="➕ Пригласить менеджера", callback_data="admin_mgr_new")
    builder.button(text="📜 Журнал всех менеджеров", callback_data="admin_mgr_log:0:0")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_main_menu")
    builder.adjust(1)
    await message.edit_text(
        "👔 <b>Менеджеры</b>\n\nОфлайн-продавцы: выдают ключи по тарифу, на свои дни и временные. "
        "Каждая операция пишется в журнал.\n\n" + ("" if managers else "Пока нет ни одного менеджера."),
        reply_markup=builder.as_markup(),
    )


@admin_managers_router.callback_query(F.data == "admin_managers")
async def managers_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _render_list(call.message)


# --- Приглашение -------------------------------------------------------------------

@admin_managers_router.callback_query(F.data == "admin_mgr_new")
async def invite_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminManagerFSM.new_name)
    await call.message.edit_text(
        "Введите имя менеджера (так он будет подписан в чеках):",
        reply_markup=cancel_fsm_keyboard("admin_managers"),
    )


LOGIN_HINT = "латиница, цифры, «.», «_», «-», 3–32 символа, первая — буква. Например: <code>ivan.p</code>"


@admin_managers_router.message(AdminManagerFSM.new_name)
async def invite_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 64:
        await message.answer("Имя — от 2 до 64 символов. Попробуйте ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AdminManagerFSM.new_login)
    await message.answer(
        f"Теперь логин для входа на сайт ({LOGIN_HINT}).\n\n"
        "Пароль менеджер придумает сам — бот пришлёт ему ссылку сразу после принятия приглашения.",
        reply_markup=cancel_fsm_keyboard("admin_managers"),
    )


@admin_managers_router.message(AdminManagerFSM.new_login)
async def invite_login(message: Message, state: FSMContext, bot: Bot):
    name = (await state.get_data()).get("name")
    if not name:
        await state.clear()
        return
    try:
        manager, token = await manager_service.invite(name, message.from_user.id, login=message.text or "")
    except ManagerError as e:
        await message.answer(f"❌ {e.message}\n\nВведите другой логин:", reply_markup=cancel_fsm_keyboard("admin_managers"))
        return
    await state.clear()
    link = _bot_link(await _bot_username(bot), token)
    login_line = f"Логин для сайта: <code>{manager.login}</code>\n\n" if manager.login else ""
    await message.answer(
        f"✅ Менеджер <b>{manager.display_name}</b> (#{manager.id}) создан.\n{login_line}"
        "Отправьте ему эту ссылку — она одноразовая и действует 24 часа. "
        "После перехода в бота он станет менеджером и задаст себе пароль:\n\n"
        f"<code>{link}</code>\n\n"
        "По умолчанию ему разрешены выдача по тарифу, свои дни и временные ключи; "
        "приём наличных и общая статистика выключены.",
        reply_markup=_kb((("⚙️ Права и лимиты", f"admin_mgr:{manager.id}"),), (("⬅️ К менеджерам", "admin_managers"),)),
    )


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_reinvite:"))
async def reinvite(call: CallbackQuery, bot: Bot):
    manager_id = int(call.data.split(":")[1])
    try:
        token = await manager_service.reinvite(manager_id)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    link = _bot_link(await _bot_username(bot), token)
    await call.message.answer(f"🔗 Новая ссылка (старая больше не работает, действует 24 часа):\n<code>{link}</code>")


# --- Карточка -----------------------------------------------------------------------

def _web_line(manager) -> str:
    if not manager.login:
        return "логин не задан ⚠️"
    return f"логин <code>{manager.login}</code> · пароль {'задан ✅' if manager.has_password else 'не задан ⏳'}"


async def _card(manager_id: int):
    managers = {m.id: m for m in await manager_service.list_managers()}
    manager = managers.get(manager_id)
    if manager is None:
        return None, None
    stats = await manager_service.admin_stats(manager_id)
    outstanding, ops = await manager_service.cash_outstanding(manager_id)

    rights = "\n".join(f"{'✅' if getattr(manager, key) else '❌'} {title}" for key, title in RIGHTS.items())
    limit = "без лимита" if manager.cash_limit is None else fmt_money(manager.cash_limit)
    text = (
        f"👔 <b>{manager.display_name}</b> (#{manager.id})\n"
        f"Статус: {STATUS_TITLES.get(manager.status, manager.status)}"
        + (f" · Telegram <code>{manager.telegram_id}</code>" if manager.telegram_id else "") + "\n"
        f"Сайт: {_web_line(manager)}\n\n"
        f"<b>Права:</b>\n{rights}\n\n"
        f"Временных ключей в день: {manager.temp_keys_per_day}\n"
        f"Потолок наличных: {limit}\n\n"
        f"<b>Сегодня:</b> {stats.today.count} оп. · {fmt_money(stats.today.revenue)}\n"
        f"<b>Месяц:</b> {stats.month.count} оп. · {fmt_money(stats.month.revenue)}\n"
        f"<b>Клиентов:</b> {stats.clients} · временных за месяц: {stats.temp_total} "
        f"(стали подпиской: {stats.temp_converted})\n"
        f"💵 <b>К сдаче:</b> {fmt_money(outstanding)} ({ops} оп.)"
    )

    builder = InlineKeyboardBuilder()
    if manager.status in ("active", "blocked"):
        for key, title in RIGHTS.items():
            builder.button(text=f"{'✅' if getattr(manager, key) else '❌'} {title}",
                           callback_data=f"admin_mgr_tg:{manager.id}:{key}")
        builder.button(text="🔢 Лимит временных ключей", callback_data=f"admin_mgr_num:{manager.id}:temp_keys_per_day")
        builder.button(text="💵 Потолок наличных", callback_data=f"admin_mgr_num:{manager.id}:cash_limit")
        if ops:
            builder.button(text=f"💰 Принять выручку {fmt_money(outstanding)}", callback_data=f"admin_mgr_settle:{manager.id}")
        builder.button(text="🔤 Логин для сайта", callback_data=f"admin_mgr_login:{manager.id}")
        if manager.has_password:
            builder.button(text="🔑 Сбросить пароль", callback_data=f"admin_mgr_pwreset:{manager.id}")
        elif manager.login and manager.status == "active":
            builder.button(text="📨 Выслать ссылку «задать пароль»", callback_data=f"admin_mgr_pwresetok:{manager.id}")
        builder.button(text="📜 Журнал", callback_data=f"admin_mgr_log:{manager.id}:0")
        builder.button(text="📥 CSV журнала", callback_data=f"admin_mgr_csv:{manager.id}")
        if manager.status == "active":
            builder.button(text="🚫 Заблокировать", callback_data=f"admin_mgr_st:{manager.id}:blocked")
        else:
            builder.button(text="✅ Разблокировать", callback_data=f"admin_mgr_st:{manager.id}:active")
    elif manager.status == "invited":
        builder.button(text="🔤 Логин для сайта", callback_data=f"admin_mgr_login:{manager.id}")
        builder.button(text="🔗 Новая ссылка-приглашение", callback_data=f"admin_mgr_reinvite:{manager.id}")
    builder.button(text="🗑 Удалить", callback_data=f"admin_mgr_del:{manager.id}")
    builder.button(text="⬅️ К менеджерам", callback_data="admin_managers")
    builder.adjust(1)
    return text, builder.as_markup()


async def _show_card(call: CallbackQuery, manager_id: int):
    text, markup = await _card(manager_id)
    if text is None:
        await call.message.edit_text("Менеджер не найден.", reply_markup=_kb((("⬅️ К менеджерам", "admin_managers"),)))
        return
    await call.message.edit_text(text, reply_markup=markup)


@admin_managers_router.callback_query(F.data.regexp(r"^admin_mgr:\d+$"))
async def card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show_card(call, int(call.data.split(":")[1]))


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_tg:"))
async def toggle_right(call: CallbackQuery):
    _, manager_id, right = call.data.split(":")
    manager_id = int(manager_id)
    if right not in RIGHTS:
        await call.answer("Неизвестное право", show_alert=True)
        return
    current = next((m for m in await manager_service.list_managers() if m.id == manager_id), None)
    if current is None:
        await call.answer("Менеджер не найден", show_alert=True)
        return
    await manager_service.update_rights(manager_id, call.from_user.id, **{right: not getattr(current, right)})
    forget(current.telegram_id or 0)
    await call.answer("Сохранено")
    await _show_card(call, manager_id)


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_num:"))
async def number_start(call: CallbackQuery, state: FSMContext):
    _, manager_id, field = call.data.split(":")
    if field not in NUMBERS:
        await call.answer("Неизвестное поле", show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminManagerFSM.edit_number)
    await state.update_data(manager_id=int(manager_id), field=field)
    title, low, high = NUMBERS[field]
    await call.message.edit_text(
        f"✏️ <b>{title}</b>\n\nВведите число от {low} до {high}:",
        reply_markup=cancel_fsm_keyboard(f"admin_mgr:{manager_id}"),
    )


@admin_managers_router.message(AdminManagerFSM.edit_number)
async def number_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    field, manager_id = data["field"], data["manager_id"]
    title, low, high = NUMBERS[field]
    raw = (message.text or "").strip()
    if field == "cash_limit" and raw == "-":
        value = None
    else:
        if not raw.isdigit() or not (low <= int(raw) <= high):
            await message.answer(f"Нужно целое число от {low} до {high}. Попробуйте ещё раз:")
            return
        value = int(raw)
    await state.clear()
    await manager_service.update_rights(manager_id, message.from_user.id, **{field: value})
    await message.answer(f"✅ Сохранено: {title} — {'без лимита' if value is None else value}.",
                         reply_markup=_kb((("⬅️ К менеджеру", f"admin_mgr:{manager_id}"),)))


# --- Логин и пароль для сайта ----------------------------------------------------------

async def _send_password_link(bot: Bot, manager_id: int, token: str | None, intro: str) -> bool:
    """Шлёт менеджеру в бот ссылку «задать пароль». False — некому или Telegram отказал."""
    manager = next((m for m in await manager_service.list_managers() if m.id == manager_id), None)
    if token is None or manager is None or not manager.telegram_id:
        return False
    text, url = password_link_text(token)
    try:
        await bot.send_message(
            manager.telegram_id,
            f"{intro}\n\n{text}\n\nВаш логин: <code>{manager.login}</code>\nВход: {site_url('/manager/login')}",
            reply_markup=password_link_keyboard(url),
        )
    except Exception as e:
        logger.warning(f"[manager] cannot send password link to #{manager_id}: {e}")
        return False
    return True


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_login:"))
async def login_start(call: CallbackQuery, state: FSMContext):
    manager_id = int(call.data.split(":")[1])
    await call.answer()
    await state.set_state(AdminManagerFSM.edit_login)
    await state.update_data(manager_id=manager_id)
    await call.message.edit_text(
        f"🔤 <b>Логин для входа на сайт</b>\n\nВведите логин ({LOGIN_HINT}):",
        reply_markup=cancel_fsm_keyboard(f"admin_mgr:{manager_id}"),
    )


@admin_managers_router.message(AdminManagerFSM.edit_login)
async def login_apply(message: Message, state: FSMContext, bot: Bot):
    manager_id = (await state.get_data()).get("manager_id")
    try:
        login = await manager_service.set_login(manager_id, message.text or "", message.from_user.id)
    except ManagerError as e:
        await message.answer(f"❌ {e.message}\n\nВведите другой логин:",
                             reply_markup=cancel_fsm_keyboard(f"admin_mgr:{manager_id}"))
        return
    await state.clear()
    note = ""
    manager = next((m for m in await manager_service.list_managers() if m.id == manager_id), None)
    if manager and manager.status == "active":
        if manager.has_password:
            await _notify_login_changed(bot, manager, login)
        else:
            # Действующему менеджеру без пароля — сразу ссылка, иначе он так и не войдёт на сайт.
            try:
                token = await manager_service.create_password_link(manager_id)
            except ManagerError:
                token = None
            sent = await _send_password_link(bot, manager_id, token, "🌐 Для вас открыт вход в панель на сайте.")
            note = "\n\nМенеджеру отправлена ссылка, чтобы задать пароль." if sent else \
                "\n\n⚠️ Не удалось отправить менеджеру ссылку — пусть откроет «Вход на сайт» в панели."
    await message.answer(f"✅ Логин сохранён: <code>{login}</code>.{note}",
                         reply_markup=_kb((("⬅️ К менеджеру", f"admin_mgr:{manager_id}"),)))


async def _notify_login_changed(bot: Bot, manager, login: str) -> None:
    try:
        await bot.send_message(manager.telegram_id, f"🔤 Администратор изменил ваш логин для сайта: <code>{login}</code>. "
                                                    "Пароль прежний.")
    except Exception as e:
        logger.warning(f"[manager] cannot notify #{manager.id} about the new login: {e}")


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_pwreset:"))
async def password_reset_ask(call: CallbackQuery):
    manager_id = int(call.data.split(":")[1])
    await call.answer()
    await call.message.edit_text(
        "🔑 <b>Сбросить пароль?</b>\n\nТекущий пароль перестанет работать, все входы на сайте завершатся. "
        "Менеджеру придёт ссылка, чтобы задать новый.",
        reply_markup=_kb((("🔑 Да, сбросить", f"admin_mgr_pwresetok:{manager_id}"),
                          ("Отмена", f"admin_mgr:{manager_id}"))),
    )


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_pwresetok:"))
async def password_reset_apply(call: CallbackQuery, bot: Bot):
    manager_id = int(call.data.split(":")[1])
    try:
        token = await manager_service.reset_password(manager_id, call.from_user.id)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    sent = await _send_password_link(bot, manager_id, token, "🔑 Администратор сбросил ваш пароль для сайта.")
    await call.answer(
        "Готово. Менеджеру отправлена ссылка, чтобы задать пароль." if sent
        else "Пароль сброшен, но ссылку отправить не удалось — пусть откроет «Вход на сайт» в панели.",
        show_alert=True,
    )
    await _show_card(call, manager_id)


# --- Статус, удаление ----------------------------------------------------------------

@admin_managers_router.callback_query(F.data.startswith("admin_mgr_st:"))
async def set_status(call: CallbackQuery, bot: Bot):
    _, manager_id, status = call.data.split(":")
    manager_id = int(manager_id)
    if status not in ("blocked", "active"):
        return
    current = next((m for m in await manager_service.list_managers() if m.id == manager_id), None)
    await manager_service.set_status(manager_id, status, call.from_user.id)
    if current and current.telegram_id:
        forget(current.telegram_id)
        await apply_commands(bot, current.telegram_id, is_manager=(status == "active"))
    await call.answer("Заблокирован" if status == "blocked" else "Разблокирован")
    await _show_card(call, manager_id)


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_del:"))
async def delete_ask(call: CallbackQuery):
    manager_id = int(call.data.split(":")[1])
    await call.answer()
    await call.message.edit_text(
        "Удалить менеджера? Доступ закроется навсегда. Журнал операций и чеки сохранятся.\n\n"
        "Несданные наличные останутся в журнале — примите выручку заранее.",
        reply_markup=_kb((("🗑 Да, удалить", f"admin_mgr_delok:{manager_id}"),
                          ("Отмена", f"admin_mgr:{manager_id}"))),
    )


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_delok:"))
async def delete_apply(call: CallbackQuery, bot: Bot):
    manager_id = int(call.data.split(":")[1])
    current = next((m for m in await manager_service.list_managers() if m.id == manager_id), None)
    await manager_service.set_status(manager_id, "deleted", call.from_user.id)
    if current and current.telegram_id:
        forget(current.telegram_id)
        await apply_commands(bot, current.telegram_id, is_manager=False)
    await call.answer("Удалён")
    await _render_list(call.message)


# --- Инкассация --------------------------------------------------------------------------

@admin_managers_router.callback_query(F.data.startswith("admin_mgr_settle:"))
async def settle_ask(call: CallbackQuery):
    manager_id = int(call.data.split(":")[1])
    total, count = await manager_service.cash_outstanding(manager_id)
    await call.answer()
    if not count:
        await _show_card(call, manager_id)
        return
    await call.message.edit_text(
        f"💰 <b>Принять выручку</b>\n\nНаличных операций: {count}\nСумма: <b>{fmt_money(total)}</b>\n\n"
        "Подтверждайте, только когда деньги реально получены на руки.",
        reply_markup=_kb((("✅ Деньги получены", f"admin_mgr_settleok:{manager_id}"),
                          ("Отмена", f"admin_mgr:{manager_id}"))),
    )


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_settleok:"))
async def settle_apply(call: CallbackQuery):
    manager_id = int(call.data.split(":")[1])
    settlement = await manager_service.settle(manager_id, call.from_user.id)
    await call.answer(
        f"Принято {fmt_money(settlement.amount)} ({settlement.operations_count} оп.)" if settlement else "Нечего принимать",
        show_alert=True,
    )
    await _show_card(call, manager_id)


# --- Журнал -------------------------------------------------------------------------------

def _log_line(op) -> str:
    icon = STATUS_ICONS.get(op.status, "•")
    parts = [f"{icon} <b>{receipt_number(op.id)}</b> {fmt_dt(op.created_at)} · м#{op.manager_id} · "
             f"{OP_TITLES.get(op.op_type, op.op_type)}"]
    detail = []
    if op.client_code:
        detail.append(f"клиент <code>{op.client_code}</code>")
    if op.tariff_name:
        detail.append(op.tariff_name + (f" {op.days} дн." if op.days else ""))
    if op.price:
        detail.append(f"{fmt_money(op.price)} {'нал.' if op.payment_method == 'cash' else 'онлайн'}")
    if op.key_fingerprint:
        detail.append(f"ключ …{op.key_fingerprint}")
    if detail:
        parts.append("   " + " · ".join(detail))
    return "\n".join(parts)


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_log:"))
async def journal(call: CallbackQuery):
    _, manager_id, page = call.data.split(":")
    manager_id, page = int(manager_id), int(page)
    await call.answer()
    rows = await manager_service.admin_history(manager_id or None, page, LOG_PAGE + 1)
    has_next = len(rows) > LOG_PAGE
    rows = rows[:LOG_PAGE]
    nav = []
    if page > 0:
        nav.append(("◀️", f"admin_mgr_log:{manager_id}:{page - 1}"))
    if has_next:
        nav.append(("▶️", f"admin_mgr_log:{manager_id}:{page + 1}"))
    back = f"admin_mgr:{manager_id}" if manager_id else "admin_managers"
    await call.message.edit_text(
        "📜 <b>Журнал" + (f" менеджера #{manager_id}" if manager_id else " всех менеджеров") + "</b>\n\n"
        + ("\n\n".join(_log_line(r) for r in rows) or "Пусто."),
        reply_markup=_kb(*([tuple(nav)] if nav else []), (("⬅️ Назад", back),)),
    )


@admin_managers_router.callback_query(F.data.startswith("admin_mgr_csv:"))
async def csv_export(call: CallbackQuery):
    manager_id = int(call.data.split(":")[1])
    await call.answer("Собираю…")
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["чек", "менеджер_id", "дата_UTC", "тип", "статус", "клиент_код", "тариф", "дней",
                     "срок_действия_до", "цена", "оплата", "ключ", "отпечаток", "сдано"])
    page = 0
    while True:
        rows = await manager_service.admin_history(manager_id, page, 200)
        for op in rows:
            writer.writerow([
                receipt_number(op.id), op.manager_id, op.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                op.op_type, op.status, op.client_code or "", op.tariff_name or "", op.days or "",
                op.key_expires_at.strftime("%Y-%m-%d %H:%M") if op.key_expires_at else "",
                op.price or 0, op.payment_method or "", op.key_username or "", op.key_fingerprint or "",
                "да" if op.settled_at else "",
            ])
        if len(rows) < 200:
            break
        page += 1
    data = ("﻿" + buffer.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"manager_{manager_id}_journal.csv"),
        caption=f"Журнал менеджера #{manager_id}",
    )
