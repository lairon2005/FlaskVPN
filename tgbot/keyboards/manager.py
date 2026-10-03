"""Клавиатуры панели менеджера. Все callback_data — с префиксом `mgr:`; клиент адресуется кодом, не id."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAGE = 8


def manager_menu_keyboard(*, tariffs=(), can_custom: bool = False, can_temp: bool = True,
                          can_global_stats: bool = False) -> InlineKeyboardMarkup:
    """
    Главный экран: сверху — быстрая продажа новому клиенту (тариф одной кнопкой),
    ниже — продление своим клиентам и всё остальное.
    """
    builder = InlineKeyboardBuilder()
    rows = []
    for t in tariffs:
        builder.button(text=f"⚡ {t.name} — {t.price:.0f} ₽", callback_data=f"mgr:q:{t.id}")
    rows += [2] * (len(tariffs) // 2) + ([1] if len(tariffs) % 2 else [])
    if can_custom:
        builder.button(text="📅 Любой срок", callback_data="mgr:q:custom")
        rows.append(1)
    if can_temp:
        builder.button(text="⏱ Пробный ключ на 1 час", callback_data="mgr:temp")
        rows.append(1)
    builder.button(text="🔄 Продлить моему клиенту", callback_data="mgr:pickc:0")
    builder.button(text="🔢 Клиент по коду", callback_data="mgr:who:code")
    builder.button(text="👥 Мои клиенты", callback_data="mgr:clients:0")
    builder.button(text="📜 История", callback_data="mgr:hist:0")
    builder.button(text="📊 Статистика", callback_data="mgr:stats")
    builder.button(text="🌐 Вход на сайт", callback_data="mgr:web")
    rows += [1, 1, 2, 2]
    if can_global_stats:
        builder.button(text="📈 Общая статистика", callback_data="mgr:gstats")
        rows.append(1)
    builder.button(text="❓ Как продавать", callback_data="mgr:help")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu")
    rows += [1, 1]
    builder.adjust(*rows)
    return builder.as_markup()


def welcome_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❓ Как продавать — памятка", callback_data="mgr:help")
    builder.button(text="👔 Открыть панель", callback_data="mgr:menu")
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
        builder.button(text="📦 Тариф", callback_data="mgr:what:tariff")
    if can_custom:
        builder.button(text="📅 Любой срок", callback_data="mgr:what:custom")
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.adjust(1)
    return builder.as_markup()


def days_keyboard(back: str) -> InlineKeyboardMarkup:
    """Частые сроки одной кнопкой; любое другое число менеджер пишет сообщением."""
    builder = InlineKeyboardBuilder()
    for days in (7, 14, 45, 60, 90, 180):
        builder.button(text=f"{days} дн.", callback_data=f"mgr:days:{days}")
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.button(text="❌ Отмена", callback_data="mgr:menu")
    builder.adjust(3, 3, 2)
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


def preview_keyboard(*, can_cash: bool, back: str = "mgr:what_back",
                     label: str | None = None, can_label: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    rows = []
    builder.button(text="📱 Оплата по QR — клиент платит сам", callback_data="mgr:go:online")
    rows.append(1)
    if can_cash:
        builder.button(text="💵 Принял наличные", callback_data="mgr:go:cash")
        rows.append(1)
    if can_label:
        text = f"📝 Пометка: {label[:24]}" + ("…" if len(label) > 24 else "") if label else "📝 Добавить пометку о клиенте"
        builder.button(text=text, callback_data="mgr:label")
        rows.append(1)
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.button(text="❌ Отмена", callback_data="mgr:menu")
    rows.append(2)
    builder.adjust(*rows)
    return builder.as_markup()


def label_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Без пометки", callback_data="mgr:label:clear")
    builder.button(text="⬅️ Назад к оплате", callback_data="mgr:label:back")
    builder.adjust(1)
    return builder.as_markup()


def invoice_keyboard(operation_id: int) -> InlineKeyboardMarkup:
    """Под QR оплаты. Статус обновляется сам — кнопка «проверить» не нужна."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить счёт", callback_data=f"mgr:cancel:{operation_id}")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def receipt_keyboard(*, has_key: bool, client_code: str | None = None) -> InlineKeyboardMarkup:
    """Под QR установки (или под чеком): что делать дальше."""
    builder = InlineKeyboardBuilder()
    if has_key:
        builder.button(text="📲 Инструкция и приложения", callback_data="instruction_info")
    if client_code:
        builder.button(text="👤 Карточка клиента", callback_data=f"mgr:c:{client_code}")
    builder.button(text="⚡ Новая продажа", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выдать пробный ключ", callback_data="mgr:temp_go")
    builder.button(text="📋 Мои пробные ключи", callback_data="mgr:temps")
    builder.button(text="⬅️ Назад", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_result_keyboard(key_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оформить подписку на этот ключ", callback_data=f"mgr:conv:{key_id}")
    builder.button(text="📲 Инструкция и приложения", callback_data="instruction_info")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_list_keyboard(keys) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key in keys:
        builder.button(text=f"💳 Подписка на ключ до {key['until']}", callback_data=f"mgr:conv:{key['id']}")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def temp_expiring_keyboard(key_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оформить подписку на этот ключ", callback_data=f"mgr:conv:{key_id}")
    builder.adjust(1)
    return builder.as_markup()


def guide_keyboard(sections, current: str | None = None) -> InlineKeyboardMarkup:
    """Оглавление памятки: каждый раздел — кнопкой."""
    builder = InlineKeyboardBuilder()
    for section in sections:
        if section.key != current:
            builder.button(text=section.title, callback_data=f"mgr:help:{section.key}")
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
    builder.row(InlineKeyboardButton(text="🔢 Добавить клиента по коду", callback_data="mgr:who:code"))
    builder.row(InlineKeyboardButton(text="⬅️ Панель менеджера", callback_data="mgr:menu"))
    return builder.as_markup()


def client_card_keyboard(client_code: str, *, can_issue: bool, has_key: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_key:
        builder.button(text="📲 Показать QR для установки", callback_data=f"mgr:link:{client_code}")
    if can_issue:
        builder.button(text="💳 Продлить", callback_data=f"mgr:pick:{client_code}")
    builder.button(text="🔕 Выключить автопродление", callback_data=f"mgr:noauto:{client_code}")
    builder.button(text="🔗 Новая ссылка на кабинет", callback_data=f"mgr:cab:{client_code}")
    builder.button(text="📝 Изменить пометку", callback_data=f"mgr:lbl:{client_code}")
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


def web_access_keyboard(site_url: str, *, has_password: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Открыть сайт", url=site_url)
    builder.button(text="🔑 Сменить пароль" if has_password else "🔑 Задать пароль", callback_data="mgr:pwd")
    if has_password:
        builder.button(text="🚪 Завершить все входы на сайте", callback_data="mgr:endsess")
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def password_link_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Задать пароль", url=url)
    builder.button(text="⬅️ Панель менеджера", callback_data="mgr:menu")
    builder.adjust(1)
    return builder.as_markup()


def web_login_notice_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚪 Это не я — завершить все входы", callback_data="mgr:endsess")
    builder.button(text="🔑 Сменить пароль", callback_data="mgr:pwd")
    builder.adjust(1)
    return builder.as_markup()
