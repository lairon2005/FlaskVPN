from aiogram.fsm.state import State, StatesGroup


class AdminManagerFSM(StatesGroup):
    """Диалоги админки менеджеров: имя нового менеджера и числовые лимиты."""
    new_name = State()
    edit_number = State()


class PricingSettingsFSM(StatesGroup):
    """Ввод значения настройки цен «своих дней», временных ключей и доступа."""
    edit_value = State()
