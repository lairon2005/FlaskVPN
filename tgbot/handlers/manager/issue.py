"""
Выдача ключа менеджером: кому → что → предпросмотр с ценой → способ оплаты → результат.

Всё, что выбрано по пути (клиент, продукт, тариф, дни, nonce подтверждения),
лежит в FSM-данных. Цена и права НЕ верятся FSM: на «Подтвердить» сервис заново
считает сумму (и сверяет с тем, что менеджер видел), заново проверяет права и
лимиты. Nonce подтверждения делает кнопку идемпотентной — двойной тап не выдаст
два ключа.
"""
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tgbot.handlers.manager.common import (
    current_manager, new_nonce, qr_file, quote_text, show, show_error,
)
from tgbot.handlers.manager.menu import manager_router
from tgbot.keyboards.manager import (
    cancel_keyboard, invoice_keyboard, pick_client_keyboard, preview_keyboard, receipt_keyboard,
    tariffs_keyboard, what_keyboard, who_keyboard, PAGE,
)
from tgbot.services import manager_service
from tgbot.services.manager_receipts import fmt_money
from tgbot.services.manager_service import ManagerError
from tgbot.states.manager_states import ManagerFSM
from loader import logger


def _client_label(client_code: str | None, temp_key_id: int | None = None) -> str:
    if client_code:
        return f"<code>{client_code}</code>"
    return "новый клиент (с временного ключа)" if temp_key_id else "новый клиент"


# --- Кому ---------------------------------------------------------------------

@manager_router.callback_query(F.data == "mgr:issue")
async def issue_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await show(call, "🔑 <b>Кому выдаём ключ?</b>", who_keyboard())


@manager_router.callback_query(F.data == "mgr:who:new")
async def who_new(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(client_code=None, temp_key_id=None)
    await _show_what(call, state)


@manager_router.callback_query(F.data == "mgr:who:code")
async def who_code(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(ManagerFSM.enter_code)
    await show(
        call,
        "🔢 <b>Код от клиента</b>\n\n"
        "Попросите клиента открыть бота → профиль → «👔 Код для менеджера» и продиктовать "
        "6 символов. Код одноразовый и живёт 15 минут.",
        cancel_keyboard("mgr:issue"),
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
        await message.answer(f"❌ {e.message}", reply_markup=cancel_keyboard("mgr:issue"))
        return
    await state.update_data(client_code=client_code, temp_key_id=None)
    await state.set_state(None)
    await message.answer(f"✅ Клиент <code>{client_code}</code> добавлен.")
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
        await show(call, "У вас пока нет клиентов. Выберите «Новый клиент» или добавьте по коду.", who_keyboard())
        return
    await show(call, "👥 <b>Выберите клиента</b>", pick_client_keyboard(rows, page, total))


@manager_router.callback_query(F.data.startswith("mgr:pick:"))
async def pick_client(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(client_code=call.data.split(":", 2)[2], temp_key_id=None)
    await _show_what(call, state)


# --- Что ----------------------------------------------------------------------

async def _show_what(event, state: FSMContext):
    manager = await current_manager(event)
    data = await state.get_data()
    back = "mgr:menu" if data.get("temp_key_id") else "mgr:issue"
    await show(
        event,
        f"📦 <b>Что выдаём?</b>\n\n👤 Клиент: {_client_label(data.get('client_code'), data.get('temp_key_id'))}",
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
    await state.set_state(ManagerFSM.enter_days)
    settings = await manager_service.custom_settings()
    await show(
        call,
        f"📅 <b>Свои дни</b>\n\nВведите количество дней (от 1 до {settings.max_days}).\n"
        "Цену посчитает система — вы увидите её до подтверждения.",
        cancel_keyboard("mgr:what_back"),
    )


@manager_router.callback_query(F.data.startswith("mgr:tariff:"))
async def tariff_chosen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(product="tariff", tariff_id=int(call.data.rsplit(":", 1)[1]), days=None)
    await _show_preview(call, state)


@manager_router.message(ManagerFSM.enter_days)
async def days_entered(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно целое число дней. Например: 45", reply_markup=cancel_keyboard("mgr:what_back"))
        return
    await state.set_state(None)
    await state.update_data(product="custom", tariff_id=None, days=int(raw))
    await _show_preview(message, state)


# --- Предпросмотр ---------------------------------------------------------------

async def _show_preview(event, state: FSMContext):
    manager = await current_manager(event)
    data = await state.get_data()
    try:
        quote = await manager_service.quote(
            manager.id, product=data["product"], client_code=data.get("client_code"),
            tariff_id=data.get("tariff_id"), days=data.get("days"),
        )
    except ManagerError as e:
        await show_error(event, e, cancel_keyboard("mgr:what_back"))
        return

    await state.update_data(nonce=new_nonce(), total=quote.total)
    await show(
        event,
        quote_text(quote, client_label=_client_label(data.get("client_code"), data.get("temp_key_id"))),
        preview_keyboard(can_cash=manager.can_accept_cash),
    )


# --- Подтверждение ----------------------------------------------------------------

@manager_router.callback_query(F.data.startswith("mgr:go:"))
async def confirm_issue(call: CallbackQuery, state: FSMContext):
    method = call.data.rsplit(":", 1)[1]
    data = await state.get_data()
    if not data.get("nonce") or not data.get("product"):
        await call.answer("Сессия устарела — начните выдачу заново.", show_alert=True)
        await state.clear()
        return
    await call.answer("⏳ Оформляю…")
    manager = await current_manager(call)

    try:
        result = await manager_service.issue(
            manager.id, product=data["product"], method=method, idempotency_nonce=data["nonce"],
            client_code=data.get("client_code"), tariff_id=data.get("tariff_id"), days=data.get("days"),
            expected_total=data.get("total"), temp_key_id=data.get("temp_key_id"),
        )
    except ManagerError as e:
        if e.code == "price_changed":
            await show(call, f"❌ {e.message}")
            await _show_preview(call, state)
        else:
            await show_error(call, e, cancel_keyboard("mgr:what_back"))
        return

    await state.clear()
    if result.replayed:
        await show(call, f"ℹ️ Эта операция уже оформлена (чек № {result.operation_id}). Проверьте историю.")
        return

    if result.status == "pending_payment":
        await _show_invoice(call, result)
    else:
        await _show_done(call, result)


async def _show_done(call: CallbackQuery, result):
    text = result.receipt_text or "✅ Ключ выдан."
    if result.cabinet_url:
        text += (
            f"\n\n🔗 <b>Кабинет клиента</b> (покажите один раз — потом ссылку не восстановить):\n"
            f"<code>{result.cabinet_url}</code>\nВ кабинете клиент продлевает подписку и управляет автопродлением."
        )
    await show(call, text, receipt_keyboard(has_key=bool(result.subscription_url)))
    if result.subscription_url:
        await call.message.answer_photo(
            qr_file(result.subscription_url, "key.png"),
            caption="📲 QR для установки — клиент сканирует его в приложении",
        )


async def _show_invoice(call: CallbackQuery, result):
    caption = (
        f"💳 <b>Счёт на {fmt_money(result.price)}</b> · чек № {result.operation_id}\n\n"
        "Клиент сканирует QR камерой или открывает ссылку и платит картой/через СБП. "
        "Ключ выдастся сам, как только придёт оплата — вам придёт сообщение.\n\n"
        f"<code>{result.payment_url}</code>"
    )
    if result.cabinet_url:
        caption += f"\n\n🔗 Кабинет клиента (покажите один раз):\n<code>{result.cabinet_url}</code>"
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer_photo(
        qr_file(result.payment_url, "pay.png"), caption=caption, reply_markup=invoice_keyboard(result.operation_id),
    )


@manager_router.callback_query(F.data.startswith("mgr:chk:"))
async def check_payment(call: CallbackQuery):
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
        await call.answer("✅ Оплата получена — ключ выдан, чек отправлен отдельным сообщением.", show_alert=True)
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
    if ok:
        await call.answer("Счёт отменён. Не оплачивайте старую ссылку.", show_alert=True)
        try:
            await call.message.edit_caption(caption="🚫 Счёт отменён.")
        except Exception:
            logger.debug("Could not edit invoice caption", exc_info=True)
    else:
        await call.answer("Счёт уже оплачен или отменён.", show_alert=True)
