from aiogram.fsm.state import State, StatesGroup


class ManagerFSM(StatesGroup):
    """Ввод текста в панели менеджера: код клиента, число дней, пометка о клиенте, цена услуги. Остальное — кнопки."""
    enter_code = State()
    enter_days = State()
    enter_label = State()       # пометка нового клиента на экране оплаты
    edit_label = State()        # пометка существующего клиента из карточки
    edit_fee = State()          # цена своей услуги
