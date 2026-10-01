from aiogram.fsm.state import State, StatesGroup


class ManagerFSM(StatesGroup):
    """Ввод текста в панели менеджера: код клиента или число дней. Остальное — кнопки."""
    enter_code = State()
    enter_days = State()
