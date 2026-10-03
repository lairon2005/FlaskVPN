from aiogram.fsm.state import State, StatesGroup


class AdminManagerFSM(StatesGroup):
    """Диалоги админки менеджеров: имя и логин нового менеджера, логин существующего, числовые лимиты."""
    new_name = State()
    new_login = State()
    edit_login = State()
    edit_number = State()


class PricingSettingsFSM(StatesGroup):
    """Ввод значения настройки цен «своих дней», временных ключей и доступа."""
    edit_value = State()
