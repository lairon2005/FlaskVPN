"""Главное меню панели менеджера, история, статистика, вход на сайт."""
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from loader import config
from tgbot.filters.manager import IsManager, forget
from tgbot.handlers.manager.common import current_manager, format_brief, show, show_error
from tgbot.keyboards.manager import back_to_manager_menu, history_keyboard, manager_menu_keyboard
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_money
from tgbot.services.manager_service import ManagerError

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

_TYPE_NAMES = {"issue_tariff": "по тарифу", "issue_custom": "свои дни",
               "issue_temp": "временные", "convert_temp": "из временных"}
_METHOD_NAMES = {"cash": "наличные", "online": "онлайн", "free": "бесплатно"}


async def show_menu(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    manager = await current_manager(event)
    try:
        stats = await manager_service.stats(manager.id)
    except ManagerError as e:
        await show_error(event, e)
        return

    text = (
        f"👔 <b>Панель менеджера</b> — {manager.display_name}\n\n"
        f"📅 Сегодня: <b>{stats.today.count}</b> операц. · {fmt_money(stats.today.revenue)}\n"
    )
    if manager.can_accept_cash:
        text += f"💵 К сдаче: <b>{fmt_money(stats.cash_outstanding)}</b> ({stats.cash_operations} оп.)\n"
    await show(event, text, manager_menu_keyboard(
        can_global_stats=manager.can_view_global_stats, can_temp=manager.can_issue_temp,
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
    return f"<b>{title}:</b> {period.count} оп. · {fmt_money(period.revenue)}\n   {kinds}" + (f"\n   {methods}" if methods else "")


def _stats_text(title: str, stats, *, with_clients: bool = True) -> str:
    lines = [f"📊 <b>{title}</b>\n", _stats_block("Сегодня", stats.today),
             _stats_block("Неделя", stats.week), _stats_block("Месяц", stats.month), ""]
    if with_clients:
        lines.append(f"👥 Клиентов: {stats.clients}")
        lines.append(f"💵 К сдаче: {fmt_money(stats.cash_outstanding)} ({stats.cash_operations} оп.)")
    if stats.temp_total:
        lines.append(f"⏱ Временных ключей за месяц: {stats.temp_total}, стали подпиской: {stats.temp_converted}")
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


@manager_router.callback_query(F.data == "mgr:web")
async def web_login_handler(call: CallbackQuery):
    """Одноразовая ссылка входа на сайт: 5 минут и один переход."""
    manager = await current_manager(call)
    try:
        token = await manager_service.create_login_token(manager.id)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    await call.answer()
    url = f"https://{config.webhook.domain}/manager/login?t={token}"
    await show(
        call,
        "🌐 <b>Панель на сайте</b>\n\nСсылка действует 5 минут и срабатывает один раз. "
        "Не пересылайте её никому — она открывает вашу панель.\n\n" + url,
        back_to_manager_menu(),
    )
