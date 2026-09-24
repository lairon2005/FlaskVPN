# tgbot/handlers/user/payment_methods.py

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from tgbot.keyboards.inline import (
    payment_method_keyboard,
    pm_delete_confirm_keyboard,
    renew_tariff_select_keyboard,
    back_to_main_menu_keyboard,
)
from database import tariff_repo, user_repo
from tgbot.services import payment_method_service
from tgbot.services.pricing import effective_price
from tgbot.services.intro_offer import is_in_intro, conversion_price

payment_methods_router = Router()


async def _edit_or_resend(call: CallbackQuery, text: str, reply_markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await call.message.delete()
        await call.message.answer(text, reply_markup=reply_markup)


async def _show_card(call: CallbackQuery) -> None:
    """Общий хелпер: показывает актуальный экран управления картой."""
    user_id = call.from_user.id
    card = await payment_method_service.get_card(user_id)

    if card is None:
        text = (
            "💳 <b>Моя карта</b>\n\n"
            "У вас пока нет сохранённой карты.\n\n"
            "Карта сохранится автоматически при следующей оплате, "
            "и вы сможете управлять автопродлением здесь."
        )
        reply_markup = back_to_main_menu_keyboard()
    else:
        if card.card_type == "SBP":
            card_line = "СБП"
        else:
            card_line = f"{card.card_type or 'Карта'} •••• {card.card_last4 or '——'}"

        renew_tariff = await tariff_repo.get_by_id(card.renew_tariff_id) if card.renew_tariff_id else None
        in_intro = is_in_intro(await user_repo.get(user_id))
        if renew_tariff:
            # Автопродление — по определению непрерывное продление, показываем лоялти-цену.
            # После пробной недели — обычную цену: её обещали в согласии.
            if in_intro:
                renew_price = conversion_price(renew_tariff)
            else:
                renew_price = effective_price(renew_tariff, user_has_active_sub=True)
            renew_tariff_line = f"{renew_tariff.name} — {renew_price} RUB"
        else:
            renew_tariff_line = "не выбран"

        renew_status = "✅ включено" if card.auto_renew_enabled else "❌ выключено"
        text = (
            f"💳 <b>Моя карта</b>\n\n"
            f"Карта: <b>{card_line}</b>\n"
            f"Автопродление: {renew_status}\n"
            f"Тариф продления: <b>{renew_tariff_line}</b>\n\n"
            + (
                "Списание — в день окончания пробной недели, напомним за сутки."
                if in_intro else
                "Подписка будет автоматически продлена за 3 дня до окончания."
            )
        )
        reply_markup = payment_method_keyboard(card.auto_renew_enabled)

    await _edit_or_resend(call, text, reply_markup)


@payment_methods_router.callback_query(F.data == "manage_card")
async def manage_card_handler(call: CallbackQuery) -> None:
    """Открывает экран управления сохранённой картой."""
    await call.answer()
    await _show_card(call)


@payment_methods_router.callback_query(F.data == "pm_toggle_renew")
async def pm_toggle_renew_handler(call: CallbackQuery) -> None:
    """Переключает автопродление и перерисовывает экран."""
    new_state = await payment_method_service.toggle_auto_renew(call.from_user.id)
    status_text = "Автопродление включено" if new_state else "Автопродление выключено"
    await call.answer(status_text)
    await _show_card(call)


@payment_methods_router.callback_query(F.data == "pm_change_tariff")
async def pm_change_tariff_handler(call: CallbackQuery) -> None:
    """Показывает список тарифов для выбора тарифа автопродления."""
    await call.answer()

    card = await payment_method_service.get_card(call.from_user.id)
    if card is None:
        await _show_card(call)
        return

    active_tariffs = await tariff_repo.get_active()
    tariffs_list = list(active_tariffs) if active_tariffs else []
    if not tariffs_list:
        await call.answer("Сейчас нет доступных тарифов.", show_alert=True)
        return

    text = (
        "🔁 <b>Тариф продления</b>\n\n"
        "Выберите тариф, на который подписка будет продлеваться автоматически.\n"
        "По умолчанию — тот, что вы купили последним."
    )
    await _edit_or_resend(
        call, text, renew_tariff_select_keyboard(tariffs_list, card.renew_tariff_id)
    )


@payment_methods_router.callback_query(F.data.startswith("pm_set_tariff_"))
async def pm_set_tariff_handler(call: CallbackQuery) -> None:
    """Сохраняет выбранный тариф продления и возвращает к экрану карты."""
    tariff_id = int(call.data.split("_")[3])

    tariff = await tariff_repo.get_by_id(tariff_id)
    if not tariff or not tariff.is_active or tariff.is_intro:
        await call.answer("Тариф недоступен.", show_alert=True)
        return

    updated = await payment_method_service.set_renew_tariff(call.from_user.id, tariff_id)
    await call.answer("Тариф продления обновлён" if updated else "Карта не найдена")
    await _show_card(call)


@payment_methods_router.callback_query(F.data == "pm_delete")
async def pm_delete_handler(call: CallbackQuery) -> None:
    """Запрашивает подтверждение удаления карты."""
    await call.answer()
    text = (
        "⚠️ <b>Удалить карту?</b>\n\n"
        "Автопродление будет отключено, для новых оплат карту нужно будет привязать заново."
    )
    try:
        await call.message.edit_text(text, reply_markup=pm_delete_confirm_keyboard())
    except TelegramBadRequest:
        await call.message.delete()
        await call.message.answer(text, reply_markup=pm_delete_confirm_keyboard())


@payment_methods_router.callback_query(F.data == "pm_delete_confirm")
async def pm_delete_confirm_handler(call: CallbackQuery) -> None:
    """Удаляет карту после подтверждения."""
    await payment_method_service.delete_card(call.from_user.id)
    text = "✅ Карта удалена. Автопродление отключено."
    await call.answer("Карта удалена")
    try:
        await call.message.edit_text(text, reply_markup=back_to_main_menu_keyboard())
    except TelegramBadRequest:
        await call.message.delete()
        await call.message.answer(text, reply_markup=back_to_main_menu_keyboard())
