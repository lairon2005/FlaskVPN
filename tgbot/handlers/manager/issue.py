"""
Выдача ключа менеджером.

Два входа:
  * быстрая продажа — тариф или «Любой срок» прямо с главного экрана: это всегда новый клиент;
  * мастер — кому (мой клиент / по коду / с пробного ключа) → что → предпросмотр.
Дальше общий путь: предпросмотр с ценой (и пометкой о новом клиенте) → способ оплаты → результат.

Всё, что выбрано по пути (клиент, продукт, тариф, дни, пометка, nonce подтверждения),
лежит в FSM-данных. Цена и права НЕ верятся FSM: на «Подтвердить» сервис заново
считает сумму (и сверяет с тем, что менеджер видел), заново проверяет права и
лимиты. Nonce подтверждения делает кнопку идемпотентной — двойной тап не выдаст
два ключа.
"""
from html import escape

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tgbot.handlers.manager.common import (
    current_manager, new_nonce, qr_file, quote_text, send_receipt, show, show_error,
)
from tgbot.handlers.manager.menu import manager_router, show_menu
from tgbot.keyboards.manager import (
    cancel_keyboard, days_keyboard, invoice_keyboard, label_keyboard, pick_client_keyboard,
    preview_keyboard, receipt_keyboard, tariffs_keyboard, what_keyboard, who_keyboard, PAGE,
)
from tgbot.services import manager_service
from tgbot.services.manager_guide import CABINET_HINT
from tgbot.services.manager_receipts import fmt_money, receipt_number
from tgbot.services.manager_service import LABEL_MAX, ManagerError
from tgbot.states.manager_states import ManagerFSM
from loader import logger


def _client_label(data: dict) -> str:
    client_code = data.get("client_code")
    if client_code:
        return f"<code>{client_code}</code>"
    who = "новый клиент (с пробного ключа)" if data.get("temp_key_id") else "новый клиент"
    label = data.get("label")
    return who + (f" · «{escape(label)}»" if label else "")


def _back(data: dict) -> str:
    """Куда ведёт «Назад» с экрана выбора срока и предпросмотра: быстрая продажа — на главный экран."""
    return "mgr:menu" if data.get("quick") else "mgr:what_back"


# --- Быстрая продажа --------------------------------------------------------------

@manager_router.callback_query(F.data.startswith("mgr:q:"))
async def quick_sale(call: CallbackQuery, state: FSMContext):
    """Кнопка тарифа на главном экране: новый клиент без лишних вопросов."""
    await call.answer()
    await state.clear()
    await state.update_data(client_code=None, temp_key_id=None, quick=True)
    target = call.data.split(":", 2)[2]
    if target == "custom":
        await _ask_days(call, state)
        return
    await state.update_data(product="tariff", tariff_id=int(target), days=None)
    await _show_preview(call, state)


# --- Кому ---------------------------------------------------------------------

@manager_router.callback_query(F.data == "mgr:issue")
async def issue_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await show(call, "🔑 <b>Кому оформляем подписку?</b>", who_keyboard())


@manager_router.callback_query(F.data == "mgr:who:new")
async def who_new(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(client_code=None, temp_key_id=None, quick=False)
    await _show_what(call, state)


@manager_router.callback_query(F.data == "mgr:who:code")
async def who_code(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(ManagerFSM.enter_code)
    await show(
        call,
        "🔢 <b>Клиент по коду</b>\n\n"
        "Так вы получите доступ к клиенту, который купил сам в боте или у другого менеджера.\n\n"
        "Попросите клиента открыть бота → профиль → «👔 Код для менеджера» и продиктовать "
        "6 символов. Отправьте их сюда сообщением. Код одноразовый и действует 15 минут.",
        cancel_keyboard("mgr:menu"),
    )


@manager_router.message(ManagerFSM.enter_code)
async def code_entered(message: Message, state: FSMContext):
    manager = await current_manager(message)
    try:
        await message.delete()  # код — как пароль: не оставляем в переписке
    except Exception:
        pass
    try:
        client_code = await manager_service.add_client_by_code(manager.id, message.text or "")
    except ManagerError as e:
        await message.answer(f"❌ {e.message}", reply_markup=cancel_keyboard("mgr:menu"))
        return
    await state.update_data(client_code=client_code, temp_key_id=None, quick=False)
    await state.set_state(None)
    await message.answer(f"✅ Клиент <code>{client_code}</code> добавлен в «Мои клиенты».")
    await _show_what(message, state)


@manager_router.callback_query(F.data.startswith("mgr:pickc:"))
async def pick_client_list(call: CallbackQuery):
    await call.answer()
    manager = await current_manager(call)
    page = int(call.data.rsplit(":", 1)[1])
    try:
        rows, total = await manager_service.list_clients(manager.id, page, PAGE)
    except ManagerError as e:
        await show_error(call, e)
        return
    if not rows and page == 0:
        await show(call, "У вас пока нет клиентов. Продайте подписку с главного экрана или добавьте клиента по коду.",
                   who_keyboard())
        return
    await show(call, "🔄 <b>Кому продлеваем?</b>\n\n🟢 подписка активна · ⚪️ закончилась",
               pick_client_keyboard(rows, page, total))


@manager_router.callback_query(F.data.startswith("mgr:pick:"))
async def pick_client(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(client_code=call.data.split(":", 2)[2], temp_key_id=None, quick=False)
    await _show_what(call, state)


# --- Что ----------------------------------------------------------------------

async def _show_what(event, state: FSMContext):
    manager = await current_manager(event)
    data = await state.get_data()
    back = "mgr:menu" if data.get("temp_key_id") else "mgr:issue"
    await show(
        event,
        f"📦 <b>Что оформляем?</b>\n\n👤 Клиент: {_client_label(data)}",
        what_keyboard(can_tariff=manager.can_issue_tariff, can_custom=manager.can_issue_custom, back=back),
    )


@manager_router.callback_query(F.data == "mgr:what_back")
async def what_back(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(None)
    await _show_what(call, state)


@manager_router.callback_query(F.data == "mgr:what:tariff")
async def what_tariff(call: CallbackQuery):
    await call.answer()
    tariffs = await manager_service.list_tariffs()
    if not tariffs:
        await show(call, "Нет доступных тарифов. Обратитесь к администратору.")
        return
    await show(call, "📦 <b>Выберите тариф</b>", tariffs_keyboard(tariffs))


@manager_router.callback_query(F.data == "mgr:what:custom")
async def what_custom(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _ask_days(call, state)


async def _ask_days(event, state: FSMContext):
    await state.set_state(ManagerFSM.enter_days)
    settings = await manager_service.custom_settings()
    data = await state.get_data()
    await show(
        event,
        "📅 <b>Подписка на любой срок</b>\n\n"
        f"Выберите срок кнопкой или напишите число дней (от 1 до {settings.max_days}).\n"
        "Цену посчитает система — вы увидите её до оплаты.",
        days_keyboard(_back(data)),
    )


@manager_router.callback_query(F.data.startswith("mgr:tariff:"))
async def tariff_chosen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(product="tariff", tariff_id=int(call.data.rsplit(":", 1)[1]), days=None)
    await _show_preview(call, state)


@manager_router.callback_query(F.data.startswith("mgr:days:"))
async def days_chosen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(None)
    await state.update_data(product="custom", tariff_id=None, days=int(call.data.rsplit(":", 1)[1]))
    await _show_preview(call, state)


@manager_router.message(ManagerFSM.enter_days)
async def days_entered(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        data = await state.get_data()
        await message.answer("Нужно целое число дней. Например: 45", reply_markup=days_keyboard(_back(data)))
        return
    await state.set_state(None)
    await state.update_data(product="custom", tariff_id=None, days=int(raw))
    await _show_preview(message, state)


# --- Пометка о новом клиенте ---------------------------------------------------------

@manager_router.callback_query(F.data == "mgr:label")
async def label_ask(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(ManagerFSM.enter_label)
    await show(
        call,
        "📝 <b>Пометка о клиенте</b>\n\n"
        "Напишите, как вы запомните клиента: например «Анна, кофейня на Ленина». "
        f"До {LABEL_MAX} символов.\n\n"
        "Пометку видите только вы и администратор — клиенту и в общий чат она не уходит.",
        label_keyboard(),
    )


@manager_router.message(ManagerFSM.enter_label)
async def label_entered(message: Message, state: FSMContext):
    label = " ".join((message.text or "").split())[:LABEL_MAX]
    await state.set_state(None)
    await state.update_data(label=label or None)
    await _show_preview(message, state)


@manager_router.callback_query(F.data.in_({"mgr:label:clear", "mgr:label:back"}))
async def label_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(None)
    if call.data.endswith("clear"):
        await state.update_data(label=None)
    await _show_preview(call, state)


# --- Предпросмотр ---------------------------------------------------------------

async def _show_preview(event, state: FSMContext):
    manager = await current_manager(event)
    data = await state.get_data()
    if not data.get("product"):
        await show_menu(event, state)
        return
    try:
        quote = await manager_service.quote(
            manager.id, product=data["product"], client_code=data.get("client_code"),
            tariff_id=data.get("tariff_id"), days=data.get("days"),
        )
    except ManagerError as e:
        await show_error(event, e, cancel_keyboard(_back(data)))
        return

    await state.update_data(nonce=new_nonce(), total=quote.total)
    await show(
        event,
        quote_text(quote, client_label=_client_label(data)),
        preview_keyboard(can_cash=manager.can_accept_cash, back=_back(data),
                         can_label=not data.get("client_code"), label=data.get("label")),
    )


# --- Подтверждение ----------------------------------------------------------------

@manager_router.callback_query(F.data.startswith("mgr:go:"))
async def confirm_issue(call: CallbackQuery, state: FSMContext):
    method = call.data.rsplit(":", 1)[1]
    data = await state.get_data()
    if not data.get("nonce") or not data.get("product"):
        await call.answer("Сессия устарела — начните продажу заново.", show_alert=True)
        await state.clear()
        return
    await call.answer("⏳ Оформляю…")
    manager = await current_manager(call)

    try:
        result = await manager_service.issue(
            manager.id, product=data["product"], method=method, idempotency_nonce=data["nonce"],
            client_code=data.get("client_code"), tariff_id=data.get("tariff_id"), days=data.get("days"),
            expected_total=data.get("total"), temp_key_id=data.get("temp_key_id"), label=data.get("label"),
        )
    except ManagerError as e:
        if e.code == "price_changed":
            await show(call, f"❌ {e.message}")
            await _show_preview(call, state)
        else:
            await show_error(call, e, cancel_keyboard(_back(data)))
        return

    await state.clear()
    if result.replayed:
        await show(call, f"ℹ️ Эта продажа уже оформлена (чек № {receipt_number(result.operation_id)}). "
                         "Проверьте «📜 История».")
        return

    if result.status == "pending_payment":
        await _show_invoice(call, result)
    else:
        await _show_done(call, result)


async def _show_done(call: CallbackQuery, result):
    """
    Готово: (новому клиенту) QR личного кабинета → чек одним сообщением: картинка с QR
    установки и шагами для клиента + текст чека. Чек — последним: его менеджер разворачивает
    к клиенту, и под ним — что дальше.
    """
    text = result.receipt_text or "✅ Ключ выдан."
    if result.cabinet_url:
        await call.message.answer_photo(
            qr_file(result.cabinet_url, "cabinet.png"),
            caption=f"🔗 <b>Личный кабинет клиента</b>\n\n{CABINET_HINT}\n\n<code>{result.cabinet_url}</code>",
        )
    await send_receipt(
        call, result.operation_id, text, result.receipt_image,
        receipt_keyboard(has_key=bool(result.subscription_url), client_code=result.client_code),
    )


async def _show_invoice(call: CallbackQuery, result):
    caption = (
        f"💳 <b>Счёт на {fmt_money(result.price)}</b> · чек № {receipt_number(result.operation_id)}\n\n"
        + (f"💵 <b>Возьмите с клиента наличными вашу услугу — {fmt_money(result.fee_cash)}.</b> "
           "В счёт по QR она не входит.\n\n" if result.fee_cash else "")
        + "📱 Разверните телефон к клиенту: он наводит камеру на QR и платит картой или через СБП.\n\n"
        "⏳ <b>Ждём оплату.</b> Это сообщение обновится само, а чек с QR для установки придёт сразу после оплаты.\n\n"
        f"<code>{result.payment_url}</code>"
    )
    if result.cabinet_url:
        caption += f"\n\n🔗 Личный кабинет клиента (покажите после оплаты, показывается один раз):\n<code>{result.cabinet_url}</code>"
    try:
        await call.message.delete()
    except Exception:
        pass
    sent = await call.message.answer_photo(
        qr_file(result.payment_url, "pay.png"), caption=caption, reply_markup=invoice_keyboard(result.operation_id),
    )
    manager = await current_manager(call)
    try:
        await manager_service.remember_invoice_message(manager.id, result.operation_id, sent.chat.id, sent.message_id)
    except Exception:
        logger.warning("Could not remember invoice message", exc_info=True)


@manager_router.callback_query(F.data.startswith("mgr:chk:"))
async def check_payment(call: CallbackQuery):
    """Кнопка из старых сообщений со счётом (теперь статус обновляется сам)."""
    manager = await current_manager(call)
    op_id = int(call.data.rsplit(":", 1)[1])
    try:
        brief = await manager_service.check_operation(manager.id, op_id)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    if brief is None:
        await call.answer("Операция не найдена.", show_alert=True)
    elif brief.status == "completed":
        await call.answer("✅ Оплата получена — ключ выдан, чек с QR для установки пришёл отдельным сообщением.", show_alert=True)
    elif brief.status == "pending_payment":
        await call.answer("⏳ Оплата ещё не пришла.", show_alert=False)
    else:
        await call.answer("Счёт отменён или не оплачен.", show_alert=True)


@manager_router.callback_query(F.data.startswith("mgr:cancel:"))
async def cancel_invoice(call: CallbackQuery):
    manager = await current_manager(call)
    op_id = int(call.data.rsplit(":", 1)[1])
    try:
        ok = await manager_service.cancel_pending(manager.id, op_id)
    except ManagerError as e:
        await call.answer(e.message, show_alert=True)
        return
    # Подпись счёта сервис уже поправил сам (живой статус) — здесь только всплывашка.
    if ok:
        await call.answer("Счёт отменён. Не оплачивайте старую ссылку.", show_alert=True)
    else:
        await call.answer("Счёт уже оплачен или отменён.", show_alert=True)
