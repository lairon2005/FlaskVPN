from aiogram.fsm.state import State, StatesGroup


class PartnerFSM(StatesGroup):
    """Партнёр: реквизиты СБП (телефон → банк) и сумма вывода."""
    enter_phone = State()
    enter_bank = State()
    enter_amount = State()


class AdminPartnerFSM(StatesGroup):
    """Админка партнёров: кого сделать партнёром, персональная ставка, общие настройки."""
    new_partner = State()
    edit_percent = State()
    edit_setting = State()
