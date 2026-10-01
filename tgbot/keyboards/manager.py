"""Клавиатуры панели менеджера. Все callback_data — с префиксом `mgr:`; клиент адресуется кодом, не id."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAGE = 8


def manager_menu_keyboard(*, can_global_stats: bool = False, can_temp: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Выдать ключ", callback_data="mgr:issue")
    if can_temp:
        builder.button(text="⏱ Временный ключ", callback_data="mgr:temp")
    builder.button(text="👥 Мои клиенты", callback_data="mgr:clients:0")
    builder.button(text="📜 История", callback_data="mgr:hist:0")
    builder.button(text="📊 Моя статистика", callback_data="mgr:stats")
    builder.button(text="🌐 Панель на сайте", callback_data="mgr:web")
    if can_global_stats:
        builder.button(text="📈 Общая статистика", callback_data="mgr:gstats")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def back_to_manager_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    return builder.as_markup()


def cancel_keyboard(back: str = "mgr:menu") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.button(text="❌ Отмена", callback_data="mgr:menu")
    builder.adjust(2)
    return builder.as_markup()


def who_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Новый клиент", callback_data="mgr:who:new")
    builder.button(text="🔢 По коду от клиента", callback_data="mgr:who:code")
    builder.button(text="👥 Из моих клиентов", callback_data="mgr:pickc:0")
    builder.button(text="⬅️ Назад", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def pick_client_keyboard(rows, page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        mark = "🟢" if row.is_active else "⚪️"
        label = f" · {row.label}" if row.label else ""
        builder.button(text=f"{mark} {row.client_code}{label}", callback_data=f"mgr:pick:{row.client_code}")
    builder.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"mgr:pickc:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"mgr:pickc:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="mgr:issue"))
    return builder.as_markup()


def what_keyboard(*, can_tariff: bool, can_custom: bool, back: str = "mgr:issue") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_tariff:
        builder.button(text="📦 По тарифу", callback_data="mgr:what:tariff")
    if can_custom:
        builder.button(text="📅 Свои дни", callback_data="mgr:what:custom")
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.adjust(1)
    return builder.as_markup()


def tariffs_keyboard(tariffs) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for t in tariffs:
        quota = "∞" if t.quota_gb == 0 else f"{t.quota_gb} ГБ"
        builder.button(
            text=f"{t.name} — {t.price:.0f} ₽ · {t.days} дн. · {quota}",
            callback_data=f"mgr:tariff:{t.id}",
        )
    builder.button(text="⬅️ Назад", callback_data="mgr:what_back")
    builder.adjust(1)
    return builder.as_markup()


def preview_keyboard(*, can_cash: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📱 Оплата по QR (онлайн)", callback_data="mgr:go:online")
    if can_cash:
        builder.button(text="💵 Принял наличные", callback_data="mgr:go:cash")
    builder.button(text="⬅️ Назад", callback_data="mgr:what_back")
    builder.button(text="❌ Отмена", callback_data="mgr:menu")
    builder.adjust(1, 1, 2) if can_cash else builder.adjust(1, 2)
    return builder.as_markup()


def invoice_keyboard(operation_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Проверить оплату", callback_data=f"mgr:chk:{operation_id}")
    builder.button(text="❌ Отменить счёт", callback_data=f"mgr:cancel:{operation_id}")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def receipt_keyboard(*, has_key: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_key:
        builder.button(text="📲 Инструкция по установке", callback_data="instruction_info")
    builder.button(text="🔑 Выдать ещё", callback_data="mgr:issue")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выдать временный ключ", callback_data="mgr:temp_go")
    builder.button(text="📋 Мои активные ключи", callback_data="mgr:temps")
    builder.button(text="⬅️ Назад", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_result_keyboard(key_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оформить подписку этому клиенту", callback_data=f"mgr:conv:{key_id}")
    builder.button(text="📲 Инструкция по установке", callback_data="instruction_info")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_list_keyboard(keys) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key in keys:
        builder.button(text=f"💳 Оформить на ключ …{key['fingerprint']}", callback_data=f"mgr:conv:{key['id']}")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def clients_keyboard(rows, page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        mark = "🟢" if row.is_active else "⚪️"
        label = f" · {row.label}" if row.label else ""
        builder.button(text=f"{mark} {row.client_code}{label}", callback_data=f"mgr:c:{row.client_code}")
    builder.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"mgr:clients:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"mgr:clients:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="➕ Добавить по коду", callback_data="mgr:who:code"))
    builder.row(InlineKeyboardButton(text="⬅️ Панель менеджера", callback_data="mgr:menu"))
    return builder.as_markup()


def client_card_keyboard(client_code: str, *, can_issue: bool, has_key: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_key:
        builder.button(text="🔑 Ссылка для установки", callback_data=f"mgr:link:{client_code}")
    if can_issue:
        builder.button(text="💳 Продлить / выдать", callback_data=f"mgr:pick:{client_code}")
    builder.button(text="🔕 Выключить автопродление", callback_data=f"mgr:noauto:{client_code}")
    builder.button(text="🔗 Новая ссылка кабинета", callback_data=f"mgr:cab:{client_code}")
    builder.button(text="⬅️ К клиентам", callback_data="mgr:clients:0")
    builder.adjust(1)
    return builder.as_markup()


def history_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"mgr:hist:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"mgr:hist:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ Панель менеджера", callback_data="mgr:menu"))
    return builder.as_markup()


def client_access_notice_keyboard() -> InlineKeyboardMarkup:
    """Клиенту: отозвать доступ менеджера. Хендлер — в tgbot/handlers/user/manager_code.py."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Закрыть доступ менеджеру", callback_data="mgr_client_revoke")
    return builder.as_markup()
