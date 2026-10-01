"""
Админ-раздел «Свои дни и временные ключи»: формула цены, лимиты временных ключей, доступ менеджеров.

Менеджер эти настройки не видит и менять не может. Рядом с настройками — таблица-превью:
как цена «своих дней» выглядит на текущих тарифах, чтобы калибровать формулу до сохранения.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import settings_repo, tariff_repo
from loader import logger
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import cancel_fsm_keyboard
from tgbot.services import manager_service
from tgbot.services.custom_pricing import (
    SETTING_DAY_PRICE, SETTING_MAX_DAYS, SETTING_MIN_PRICE, SETTING_RENEW_TARIFF,
    SETTING_SHORT_PREMIUM, TariffRef, preview_table,
)
from tgbot.services.manager_service import (
    SETTING_ACCESS_GRACE, SETTING_TEMP_DEVICES, SETTING_TEMP_MINUTES, SETTING_TEMP_TRAFFIC,
    DEFAULT_ACCESS_GRACE_DAYS, DEFAULT_TEMP_DEVICES, DEFAULT_TEMP_MINUTES, DEFAULT_TEMP_TRAFFIC_GB,
)
from tgbot.states.admin_manager_states import PricingSettingsFSM

admin_pricing_router = Router()
admin_pricing_router.message.filter(IsAdmin())
admin_pricing_router.callback_query.filter(IsAdmin())

# ключ → (кнопка, заголовок, подсказка, минимум, максимум, дробное?, дефолт)
EDITABLE = {
    SETTING_DAY_PRICE: ("💰 Цена дня", "Цена дня", "рублей за день (база формулы)", 0.5, 1000, True, 5.0),
    SETTING_SHORT_PREMIUM: ("📈 Надбавка за короткий срок", "Коэффициент надбавки", "K в формуле d + K·√d (0 — без надбавки)", 0, 20, True, 1.5),
    SETTING_MIN_PRICE: ("🔻 Минимальный чек", "Минимальный чек", "рублей — дешевле «свои дни» не продаются", 1, 100000, False, 59),
    SETTING_MAX_DAYS: ("📅 Максимум дней", "Максимум дней", "дольше — только по тарифу", 1, 3650, False, 365),
    SETTING_TEMP_MINUTES: ("⏱ Жизнь временного ключа", "Время жизни временного ключа", "минут", 1, 1440, False, DEFAULT_TEMP_MINUTES),
    SETTING_TEMP_TRAFFIC: ("📊 Трафик временного ключа", "Трафик временного ключа", "ГБ", 1, 1000, False, DEFAULT_TEMP_TRAFFIC_GB),
    SETTING_TEMP_DEVICES: ("📱 Устройства временного ключа", "Устройств во временном ключе", "штук", 1, 20, False, DEFAULT_TEMP_DEVICES),
    SETTING_ACCESS_GRACE: ("🔒 Доступ менеджера к клиенту", "Запас доступа после конца подписки", "дней", 0, 365, False, DEFAULT_ACCESS_GRACE_DAYS),
}


async def _render(message: Message) -> None:
    s = await manager_service.custom_settings()
    refs = [TariffRef(t.name, t.duration_days, t.price) for t in await tariff_repo.get_active()]
    rows = preview_table(s, refs)
    table = "\n".join(
        f"{r.days:>4} дн. — {r.price:>5} ₽  ({r.per_day:.1f} ₽/день)" for r in rows
    )
    minutes, gb, devices = await manager_service.temp_settings()
    renew_id = await settings_repo.get_int(SETTING_RENEW_TARIFF, 0)
    renew = await tariff_repo.get_by_id(renew_id) if renew_id else None
    grace = await settings_repo.get_int(SETTING_ACCESS_GRACE, DEFAULT_ACCESS_GRACE_DAYS)

    builder = InlineKeyboardBuilder()
    for key, (button, *_rest) in EDITABLE.items():
        builder.button(text=button, callback_data=f"admin_price_{key}")
    builder.button(text="♻️ Тариф автопродления", callback_data="admin_price_renew")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_main_menu")
    builder.adjust(1)

    await message.edit_text(
        "💲 <b>Свои дни и временные ключи</b>\n\n"
        "<b>Формула:</b> цена = max(мин. чек, цена дня × (d + K·√d), цена тарифа на меньший срок, "
        "d × лучшая цена дня), округление вверх до …9\n"
        f"Цена дня <b>{s.day_price:g} ₽</b> · K <b>{s.short_premium:g}</b> · мин. чек <b>{s.min_price} ₽</b> · "
        f"максимум <b>{s.max_days}</b> дн.\n\n"
        f"<b>Превью на текущих тарифах:</b>\n<code>{table or '— нет активных тарифов —'}</code>\n\n"
        f"♻️ После «своих дней» карта продлевает: <b>{renew.name if renew else 'ближайший к 30 дням тариф'}</b>\n\n"
        f"⏱ <b>Временный ключ:</b> {minutes} мин · {gb} ГБ · {devices} устр.\n"
        f"🔒 Доступ менеджера к клиенту: до конца подписки + {grace} дн.",
        reply_markup=builder.as_markup(),
    )


@admin_pricing_router.callback_query(F.data == "admin_pricing")
async def pricing_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _render(call.message)


@admin_pricing_router.callback_query(F.data == "admin_price_renew")
async def renew_pick(call: CallbackQuery):
    await call.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="Авто (ближайший к 30 дням)", callback_data="admin_price_renewset_0")
    for t in await tariff_repo.get_active():
        builder.button(text=f"{t.name} — {t.price} ₽ / {t.duration_days} дн.", callback_data=f"admin_price_renewset_{t.id}")
    builder.button(text="⬅️ Назад", callback_data="admin_pricing")
    builder.adjust(1)
    await call.message.edit_text(
        "♻️ На какой тариф продлевается сохранённая карта после покупки «своих дней»?",
        reply_markup=builder.as_markup(),
    )


@admin_pricing_router.callback_query(F.data.startswith("admin_price_renewset_"))
async def renew_set(call: CallbackQuery):
    value = int(call.data.rsplit("_", 1)[1])
    await settings_repo.set(SETTING_RENEW_TARIFF, value)
    logger.info(f"[admin] {SETTING_RENEW_TARIFF} = {value} админом {call.from_user.id}")
    await call.answer("Сохранено")
    await _render(call.message)


@admin_pricing_router.callback_query(F.data.regexp(r"^admin_price_(?!renew)"))
async def edit_start(call: CallbackQuery, state: FSMContext):
    key = call.data.removeprefix("admin_price_")
    if key not in EDITABLE:
        await call.answer("Неизвестная настройка", show_alert=True)
        return
    await call.answer()
    _, title, hint, low, high, is_float, default = EDITABLE[key]
    current = await (settings_repo.get_float(key, default) if is_float else settings_repo.get_int(key, default))
    await state.set_state(PricingSettingsFSM.edit_value)
    await state.update_data(setting_key=key)
    await call.message.edit_text(
        f"✏️ <b>{title}</b>\n\nСейчас: <b>{current:g}</b> ({hint}).\n\nВведите новое значение от {low:g} до {high:g}:",
        reply_markup=cancel_fsm_keyboard("admin_pricing"),
    )


@admin_pricing_router.message(PricingSettingsFSM.edit_value)
async def edit_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    if key not in EDITABLE:
        await state.clear()
        return
    _, title, _hint, low, high, is_float, _default = EDITABLE[key]
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw) if is_float else int(raw)
    except ValueError:
        await message.answer(f"Нужно число от {low:g} до {high:g}. Попробуйте ещё раз:",
                             reply_markup=cancel_fsm_keyboard("admin_pricing"))
        return
    if not (low <= value <= high):
        await message.answer(f"Значение должно быть от {low:g} до {high:g}. Попробуйте ещё раз:",
                             reply_markup=cancel_fsm_keyboard("admin_pricing"))
        return
    await settings_repo.set(key, value)
    await state.clear()
    logger.info(f"[admin] {key} изменён на {value} админом {message.from_user.id}")
    await message.answer(f"✅ <b>{title}</b> — теперь <b>{value:g}</b>. Новые расчёты используют его сразу.")
    menu = await message.answer("Загружаю настройки…")
    await _render(menu)
