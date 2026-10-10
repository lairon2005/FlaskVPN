"""Главное меню панели менеджера, история, статистика, вход на сайт (логин, пароль, сеансы)."""
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from tgbot.filters.manager import IsManager, forget
from tgbot.handlers.manager.common import (
    current_manager, format_brief, password_link_text, show, show_error, site_url,
)
from tgbot.keyboards.manager import (
    back_to_manager_menu, cancel_keyboard, fee_keyboard, guide_keyboard, history_keyboard, manager_menu_keyboard, password_link_keyboard,
    web_access_keyboard,
)
from tgbot.services import manager_service
from tgbot.services.manager_guide import GUIDE, section as guide_section
from tgbot.services.manager_receipts import fmt_money
from tgbot.services.manager_service import SERVICE_FEE_MAX, ManagerError
from tgbot.states.manager_states import ManagerFSM

manager_router = Router(name="manager")
manager_router.message.filter(F.chat.type == ChatType.PRIVATE, IsManager())
manager_router.callback_query.filter(IsManager())


@manager_router.error(ExceptionTypeFilter(ManagerError))
async def manager_error_handler(event: ErrorEvent):
    """
    Отказ сервиса, не пойманный хендлером (чаще всего — менеджера заблокировали, а кэш
    фильтра ещё помнит его). Показываем текст отказа и сбрасываем кэш: следующий апдейт
    этого человека уже не попадёт в панель.
    """
    error: ManagerError = event.exception
    update = event.update
    user = (update.callback_query or update.message).from_user if (update.callback_query or update.message) else None
    if user is not None:
        forget(user.id)
    if update.callback_query:
        await update.callback_query.answer(error.message, show_alert=True)
    elif update.message:
        await update.message.answer(f"❌ {error.message}")
    return True

HISTORY_PAGE = 8

_TYPE_NAMES = {"issue_tariff": "по тарифу", "issue_custom": "любой срок",
               "issue_temp": "пробные", "convert_temp": "из пробных"}
_METHOD_NAMES = {"cash": "наличные", "online": "онлайн", "free": "бесплатно"}


def _sales_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "продажа"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "продажи"
    return "продаж"


async def show_menu(event: Message | CallbackQuery, state: FSMContext):
    """Главный экран: итоги дня и быстрая продажа — тариф одной кнопкой."""
    await state.clear()
    manager = await current_manager(event)
    try:
        stats = await manager_service.stats(manager.id)
    except ManagerError as e:
        await show_error(event, e)
        return
    tariffs = await manager_service.list_tariffs() if manager.can_issue_tariff else []

    today = stats.today
    lines = [
        f"👔 <b>Панель менеджера</b> · {manager.display_name}",
        f"📅 Сегодня: <b>{today.count}</b> {_sales_word(today.count)} на {fmt_money(today.revenue)}",
    ]
    if today.fees:
        lines.append(f"💼 Ваш заработок сегодня: <b>{fmt_money(today.fees)}</b>")
    if manager.can_accept_cash:
        lines.append(f"💵 К сдаче: <b>{fmt_money(stats.cash_outstanding)}</b>")
    if stats.fees_outstanding:
        lines.append(f"💸 К выплате вам: <b>{fmt_money(stats.fees_outstanding)}</b>")
    if tariffs or manager.can_issue_custom:
        lines.append("\n⚡ <b>Новый клиент</b> — нажмите тариф, дальше выберете оплату.")
    lines.append("🔄 <b>Постоянный клиент</b> — «Продлить моему клиенту».")
    await show(event, "\n".join(lines), manager_menu_keyboard(
        tariffs=tariffs, can_custom=manager.can_issue_custom, can_temp=manager.can_issue_temp,
        can_global_stats=manager.can_view_global_stats, service_fee=manager.service_fee,
    ))


@manager_router.message(Command("manager"))
async def manager_command(message: Message, state: FSMContext):
    await show_menu(message, state)


@manager_router.callback_query(F.data == "mgr:menu")
async def manager_menu_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await show_menu(call, state)


@manager_router.callback_query(F.data.startswith("mgr:hist:"))
async def history_handler(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    page = int(call.data.rsplit(":", 1)[1])
    try:
        rows = await manager_service.history(manager.id, page, HISTORY_PAGE + 1)
    except ManagerError as e:
        await show_error(call, e)
        return
    has_next = len(rows) > HISTORY_PAGE
    rows = rows[:HISTORY_PAGE]
    text = "📜 <b>Мои операции</b>\n\n" + ("\n\n".join(format_brief(r) for r in rows) or "Пока пусто.")
    await show(call, text, history_keyboard(page, has_next))


def _stats_block(title: str, period) -> str:
    if not period.count:
        return f"<b>{title}:</b> —"
    kinds = ", ".join(f"{_TYPE_NAMES.get(k, k)} {v}" for k, v in period.by_type.items())
    methods = ", ".join(f"{_METHOD_NAMES.get(k, k)} {fmt_money(v)}" for k, v in period.by_method.items() if v)
    fees = f"\n   💼 ваш заработок: {fmt_money(period.fees)}" if period.fees else ""
    return (f"<b>{title}:</b> {period.count} оп. · {fmt_money(period.revenue)}\n   {kinds}"
            + (f"\n   {methods}" if methods else "") + fees)


def _stats_text(title: str, stats, *, with_clients: bool = True) -> str:
    lines = [f"📊 <b>{title}</b>\n", _stats_block("Сегодня", stats.today),
             _stats_block("Неделя", stats.week), _stats_block("Месяц", stats.month), ""]
    if with_clients:
        lines.append(f"👥 Клиентов: {stats.clients}")
        lines.append(f"💵 К сдаче: {fmt_money(stats.cash_outstanding)} ({stats.cash_operations} оп.)")
        lines.append(f"💸 К выплате вам: {fmt_money(stats.fees_outstanding)} ({stats.fees_operations} оп.)")
    if stats.temp_total:
        lines.append(f"⏱ Пробных ключей за месяц: {stats.temp_total}, стали подпиской: {stats.temp_converted}")
    return "\n".join(lines)


@manager_router.callback_query(F.data == "mgr:stats")
async def stats_handler(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    try:
        stats = await manager_service.stats(manager.id)
    except ManagerError as e:
        await show_error(call, e)
        return
    await show(call, _stats_text("Моя статистика", stats), back_to_manager_menu())


@manager_router.callback_query(F.data == "mgr:gstats")
async def global_stats_handler(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    try:
        stats = await manager_service.global_stats(manager.id)
    except ManagerError as e:
        await show_error(call, e)
        return
    await show(call, _stats_text("Общая статистика (все менеджеры)", stats, with_clients=False), back_to_manager_menu())


# --- Цена своей услуги -------------------------------------------------------------

def fee_text(manager, fees_due: float) -> str:
    """Экран «Моя услуга»: одинаковый смысл в боте и на сайте."""
    lines = [
        "💼 <b>Ваша услуга — подключение и настройка VPN</b>\n",
        f"Цена: <b>{fmt_money(manager.service_fee)}</b>",
        "",
        "Добавляется к каждой вашей продаже (тариф, любой срок, подписка на пробный ключ) "
        "отдельной строкой — клиент видит её в чеке. Пробный ключ бесплатный, без услуги.",
        "♻️ Автопродление списывает только стоимость подписки — без услуги.",
        "💵 Наличные: услуга остаётся у вас, в «к сдаче» не входит.",
        "💳 Оплата по QR: клиент платит онлайн только подписку, а услугу отдаёт вам наличными.",
    ]
    if fees_due:
        # Старые продажи, где услуга шла через ЮKassa, — их выплачивает администратор.
        lines.append(f"💸 За прошлые продажи по QR вам к выплате: {fmt_money(fees_due)}.")
    if manager.service_fee_locked:
        lines.append("\n🔒 Цену зафиксировал администратор — изменить её может только он.")
    return "\n".join(lines)


@manager_router.callback_query(F.data == "mgr:fee")
async def fee_handler(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(None)
    manager = await current_manager(call)
    fees_due, _ = await manager_service.fees_outstanding(manager.id)
    await show(call, fee_text(manager, fees_due), fee_keyboard(locked=manager.service_fee_locked))


@manager_router.callback_query(F.data == "mgr:fee:edit")
async def fee_edit(call: CallbackQuery, state: FSMContext):
    manager = await current_manager(call)
    if manager.service_fee_locked:
        await call.answer(ManagerError("fee_locked").message, show_alert=True)
        return
    await call.answer()
    await state.set_state(ManagerFSM.edit_fee)
    await show(call, f"✏️ Напишите новую цену услуги в рублях — целое число от 0 до {SERVICE_FEE_MAX}.\n\n"
                     "0 — продавать без услуги.", cancel_keyboard("mgr:fee"))


@manager_router.message(ManagerFSM.edit_fee)
async def fee_entered(message: Message, state: FSMContext):
    manager = await current_manager(message)
    raw = (message.text or "").strip().replace("₽", "").strip()
    try:
        if not raw.isdigit():
            raise ManagerError("bad_fee")
        manager = await manager_service.set_service_fee(manager.id, int(raw))
    except ManagerError as e:
        await message.answer(f"❌ {e.message}", reply_markup=cancel_keyboard("mgr:fee"))
        if e.code == "fee_locked":
            await state.set_state(None)
        return
    await state.set_state(None)
    fees_due, _ = await manager_service.fees_outstanding(manager.id)
    await message.answer("✅ Цена услуги сохранена. Новые продажи — уже по ней.\n\n" + fee_text(manager, fees_due),
                         reply_markup=fee_keyboard(locked=manager.service_fee_locked))


@manager_router.callback_query(F.data == "mgr:web")
async def web_access_handler(call: CallbackQuery):
    """Адрес сайта и логин. Входа по ссылке нет — только логин и пароль."""
    await call.answer()
    manager = await current_manager(call)
    if not manager.login:
        await show(call, "🌐 <b>Вход на сайт</b>\n\nЛогин для сайта ещё не задан. Обратитесь к администратору.",
                   back_to_manager_menu())
        return
    password = "задан ✅" if manager.has_password else "не задан — нажмите «Задать пароль»"
    await show(
        call,
        "🌐 <b>Вход в панель на сайте</b>\n\n"
        f"Адрес: {site_url('/manager/login')}\n"
        f"Логин: <code>{manager.login}</code>\n"
        f"Пароль: {password}\n\n"
        "О каждом входе на сайт придёт уведомление сюда.",
        web_access_keyboard(site_url("/manager/login"), has_password=manager.has_password),
    )


@manager_router.callback_query(F.data == "mgr:pwd")
async def password_link_handler(call: CallbackQuery):
    manager = await current_manager(call)
    try:
        token = await manager_service.create_password_link(manager.id)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    text, url = password_link_text(token)
    # Новым сообщением: уведомление о входе, из которого жмут кнопку, должно остаться в истории.
    await call.message.answer(text, reply_markup=password_link_keyboard(url))


@manager_router.callback_query(F.data == "mgr:endsess")
async def end_sessions_handler(call: CallbackQuery):
    manager = await current_manager(call)
    await manager_service.end_web_sessions(manager.id)
    await call.answer("Все входы на сайте завершены. Если пароль мог узнать кто-то ещё — смените его.",
                      show_alert=True)


# --- Памятка «Как продавать» ---------------------------------------------------------

@manager_router.callback_query(F.data == "mgr:help")
async def guide_handler(call: CallbackQuery):
    await call.answer()
    await show(
        call,
        "❓ <b>Как продавать</b>\n\nКороткая памятка: как оформить подписку за минуту, что отвечать клиентам "
        "и что делать, если что-то не работает. Выберите раздел:",
        guide_keyboard(GUIDE),
    )


@manager_router.callback_query(F.data.startswith("mgr:help:"))
async def guide_section_handler(call: CallbackQuery):
    await call.answer()
    section = guide_section(call.data.split(":", 2)[2])
    if section is None:
        await show(call, "Раздел не найден.", guide_keyboard(GUIDE))
        return
    body = "\n\n".join(escape(line) for line in section.lines)
    await show(call, f"<b>{escape(section.title)}</b>\n\n{body}", guide_keyboard(GUIDE, current=section.key))
