"""
Менеджеры офлайн-продаж: права, доступ к клиентам, выдача ключей, чеки, инкассация.

Главный принцип — ВСЕ проверки здесь. Хендлеры бота и роуты сайта тонкие и не
обходят сервис, а сервис каждый раз перечитывает менеджера из БД: блокировка
действует мгновенно, без ожидания, пока истечёт чья-то сессия или кэш.

Приватность клиента: менеджер получает только DTO с белым списком полей
(`ClientCard`, `ClientRow`, `OperationBrief`) — ORM-объекты `User`/`Payment`
наружу не уходят. Telegram ID, email, платёжные данные и полная история в эти
DTO не попадают физически. Клиент адресуется кодом (`client_code`), а не id:
у Telegram-клиента id — это его Telegram ID.

Деньги идут тем же конвейером, что и обычные платежи: наличная продажа — это
Payment(source='cash') с немедленной обработкой process_successful_payment,
QR — платёж ЮKassa с manager_id в записи. Поэтому extend/слоты/трафик/флаг
первой оплаты/бонус рефереру работают одинаково во всех каналах.
"""
import asyncio
import datetime
from dataclasses import dataclass, field

from loader import logger
from remnawave.client import RemnawaveAPIError
from tgbot.services.custom_pricing import (
    CustomDaysError, CustomPriceSettings, TariffRef, compute_custom_price,
    DEFAULT_DAY_PRICE, DEFAULT_MAX_DAYS, DEFAULT_MIN_PRICE, DEFAULT_SHORT_PREMIUM,
    SETTING_DAY_PRICE, SETTING_MAX_DAYS, SETTING_MIN_PRICE, SETTING_SHORT_PREMIUM,
)
from tgbot.services.device_pricing import build_checkout, days_left, receipt_items
from tgbot.services.manager_receipts import (
    KIND_CUSTOM, KIND_TARIFF, KIND_TEMP, ReceiptData, fmt_dt, fmt_money, fmt_time,
    format_client_receipt, format_group_receipt, format_manager_receipt,
)
from tgbot.services.manager_security import (
    generate_client_code, hash_password, hash_token, key_fingerprint, new_token,
    normalize_client_code, normalize_login, password_problem, temp_username, verify_password,
)
from tgbot.services.pricing import effective_price
from tgbot.services.receipt_image import render_receipt
from tgbot.services.traffic_pricing import (
    GIB, gb_to_packs, packs_cost_for_tariff, packs_to_gb, tariff_quota_gb,
)

# --- настройки (app_settings) ---------------------------------------------
SETTING_TEMP_MINUTES = "temp_key_minutes"
SETTING_TEMP_TRAFFIC = "temp_key_traffic_gb"
SETTING_TEMP_DEVICES = "temp_key_devices"
SETTING_ACCESS_GRACE = "manager_access_grace_days"

DEFAULT_TEMP_MINUTES = 60
DEFAULT_TEMP_TRAFFIC_GB = 5
DEFAULT_TEMP_DEVICES = 1
DEFAULT_ACCESS_GRACE_DAYS = 14

# Услуга менеджера (подключение и настройка): входит в каждую его продажу отдельной позицией,
# автопродление её не включает. Цену задаёт сам менеджер в этих границах.
SERVICE_FEE_DEFAULT = 250
SERVICE_FEE_MAX = 1000

# Как называется выдача на произвольный срок в чеках, журнале и на экранах.
CUSTOM_PRODUCT_NAME = "Любой срок"
# За сколько минут до конца пробного ключа напомнить менеджеру предложить подписку.
TEMP_REMIND_MINUTES = 10

# --- ограничения -----------------------------------------------------------
INVITE_TTL_HOURS = 24
# Ссылка «задать пароль» из бота.
PASSWORD_LINK_TTL_MINUTES = 30
# Подбор пароля: столько неверных попыток подряд — и вход по логину закрыт на LOGIN_LOCK_MINUTES.
MAX_FAILED_LOGINS = 5
LOGIN_LOCK_MINUTES = 15
ACCESS_CODE_TTL_MINUTES = 15
CODE_ATTEMPTS_PER_HOUR = 5
TEMP_DELETE_MAX_ATTEMPTS = 5
# Не оплатили за столько минут после истечения временного ключа — отвязываем и удаляем.
CONVERT_GRACE_MINUTES = 30

MESSAGES = {
    "not_manager": "У вас нет доступа к панели менеджера.",
    "blocked": "Доступ менеджера закрыт. Обратитесь к администратору.",
    "no_right": "У вас нет права на это действие. Обратитесь к администратору.",
    "no_access": "Нет доступа к этому клиенту. Попросите клиента сгенерировать код доступа.",
    "client_not_found": "Клиент не найден.",
    "bad_code": "Код не найден или устарел. Попросите клиента сгенерировать новый.",
    "code_rate_limited": "Слишком много неверных кодов. Подождите час или обратитесь к администратору.",
    "bad_tariff": "Этот тариф недоступен для продажи.",
    "bad_days": "Некорректное количество дней.",
    "bad_extras": "Столько доп. устройств или трафика добавить нельзя.",
    "bad_method": "Этот способ оплаты недоступен.",
    "cash_not_allowed": "Приём наличных вам не разрешён. Используйте оплату по QR.",
    "cash_limit": "Превышен лимит наличных «к сдаче». Сдайте выручку администратору.",
    "temp_limit": "Дневной лимит временных ключей исчерпан.",
    "price_changed": "Цена изменилась. Проверьте новую сумму и подтвердите ещё раз.",
    "bad_fee": f"Цена услуги — целое число рублей от 0 до {SERVICE_FEE_MAX}.",
    "fee_locked": "Цену вашей услуги зафиксировал администратор — изменить её может только он.",
    "pending_payment": "У клиента уже есть неоплаченный счёт. Отмените его или дождитесь оплаты.",
    "temp_not_found": "Временный ключ не найден или уже недоступен.",
    "invalid_invite": "Ссылка-приглашение недействительна или устарела.",
    "bad_login": "Логин: 3–32 символа, латиница, цифры, «.», «_», «-», начинается с буквы.",
    "login_taken": "Этот логин уже занят другим менеджером.",
    "bad_password": "Пароль не подходит.",
    "bad_credentials": "Неверный логин или пароль.",
    "login_locked": "Слишком много неверных попыток. Вход временно закрыт — попробуйте через 15 минут.",
    "invalid_password_link": "Ссылка недействительна или устарела. Получите новую в боте: «Панель менеджера» → «Вход на сайт».",
    "panel": "VPN-панель временно недоступна. Повторите через несколько секунд.",
    "payment_failed": "Не удалось провести платёж. Ключ не выдан.",
    "generic": "Не удалось выполнить операцию. Обратитесь к администратору.",
}


class ManagerError(Exception):
    """Ожидаемый отказ с кодом и текстом для менеджера."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or MESSAGES.get(code, MESSAGES["generic"])
        super().__init__(self.message)


# --- DTO: что менеджеру разрешено видеть ---------------------------------------

@dataclass(frozen=True)
class ManagerView:
    """Безопасный снимок менеджера: без токенов и служебных полей."""
    id: int
    telegram_id: int | None
    display_name: str
    status: str
    can_issue_tariff: bool
    can_issue_custom: bool
    can_issue_temp: bool
    can_accept_cash: bool
    can_view_global_stats: bool
    temp_keys_per_day: int
    cash_limit: int | None
    login: str | None = None
    has_password: bool = False
    service_fee: int = SERVICE_FEE_DEFAULT
    service_fee_locked: bool = False


@dataclass(frozen=True)
class TariffView:
    id: int
    name: str
    days: int
    price: float
    quota_gb: int          # 0 — безлимит


@dataclass(frozen=True)
class ClientRow:
    """Строка списка «Мои клиенты»."""
    client_code: str
    label: str | None
    subscription_end: datetime.datetime | None
    is_active: bool
    access_until: datetime.datetime | None


@dataclass(frozen=True)
class OperationBrief:
    """Строка истории: ровно то, что нужно менеджеру о своей операции."""
    id: int
    created_at: datetime.datetime
    op_type: str
    status: str
    client_code: str | None
    tariff_name: str | None
    days: int | None
    price: float
    payment_method: str | None
    key_fingerprint: str | None
    key_expires_at: datetime.datetime | None


@dataclass(frozen=True)
class ClientCard:
    """
    Карточка клиента для менеджера — белый список (ТЗ п.7).

    Здесь НЕТ: Telegram ID, username, email, платёжных реквизитов, истории
    платежей, реферальных данных, hwid/IP устройств. Тест test_manager_service
    фиксирует набор полей, чтобы новое поле не появилось «случайно».
    """
    client_code: str
    label: str | None
    subscription_active: bool
    subscription_end: datetime.datetime | None
    days_left: int
    devices_used: int | None
    devices_limit: int | None
    device_platforms: tuple[str, ...]
    traffic_used_gb: float | None
    traffic_limit_gb: int | None     # 0 — безлимит
    extra_devices: int
    extra_traffic_gb: int
    has_key: bool
    access_until: datetime.datetime | None
    operations: tuple[OperationBrief, ...] = ()


@dataclass(frozen=True)
class PeriodStats:
    count: int = 0
    revenue: float = 0.0
    fees: float = 0.0               # из revenue — услуга менеджера (его заработок)
    by_type: dict = field(default_factory=dict)
    by_method: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ManagerStats:
    today: PeriodStats
    week: PeriodStats
    month: PeriodStats
    cash_outstanding: float
    cash_operations: int
    clients: int
    temp_total: int
    temp_converted: int
    fees_outstanding: float = 0.0   # услуги из онлайн-продаж, ещё не выплаченные менеджеру
    fees_operations: int = 0


@dataclass
class IssueQuote:
    """Предпросмотр выдачи: что получит клиент и сколько это стоит."""
    product: str                    # tariff | custom
    tariff_id: int | None
    tariff_name: str
    days: int
    base_price: float
    slots: int
    slots_cost: float
    packs: int
    traffic_cost: float
    extra_traffic_gb: int
    total: float                    # к оплате: подписка + услуга менеджера
    quota_gb: int
    devices_limit: int
    breakdown: str | None = None
    hint: object | None = None      # custom_pricing.TariffHint
    renew_text: str | None = None   # текст согласия на автопродление (онлайн)
    autorenew_available: bool = False
    client_code: str | None = None
    client_is_new: bool = True
    price_details: dict = field(default_factory=dict)
    # Для степперов «устройства» и «трафик» на экране продажи.
    base_devices: int = 0
    max_slots: int = 0
    slot_price: int = 0             # ₽ за устройство в месяц
    max_packs: int = 0              # 0 — докупать нечего (безлимит)
    pack_gb: int = 0
    pack_price: int = 0             # ₽ за пакет в месяц
    service_fee: float = 0.0        # услуга менеджера внутри total

    @property
    def subscription_total(self) -> float:
        return self.total - self.service_fee


@dataclass
class IssueResult:
    operation_id: int
    status: str                     # completed | pending_payment
    price: float                    # счёт ЮKassa (по QR — без услуги) или итог проведённой продажи
    payment_url: str | None = None
    fee_cash: float = 0.0           # по QR: услуга, которую менеджер берёт с клиента наличными
    subscription_url: str | None = None
    client_code: str | None = None
    cabinet_url: str | None = None  # только для нового клиента — ссылка показывается один раз
    expires_at: datetime.datetime | None = None
    receipt_text: str | None = None
    receipt_image: bytes | None = None
    replayed: bool = False          # повторный запрос с тем же ключом — вернули прежний результат


@dataclass
class TempKeyResult:
    operation_id: int
    key_id: int
    subscription_url: str
    expires_at: datetime.datetime
    client_code: str | None = None
    receipt_text: str | None = None
    receipt_image: bytes | None = None


def _view(m) -> ManagerView:
    return ManagerView(
        id=m.id, telegram_id=m.telegram_id, display_name=m.display_name, status=m.status,
        can_issue_tariff=m.can_issue_tariff, can_issue_custom=m.can_issue_custom,
        can_issue_temp=m.can_issue_temp, can_accept_cash=m.can_accept_cash,
        can_view_global_stats=m.can_view_global_stats, temp_keys_per_day=m.temp_keys_per_day,
        cash_limit=m.cash_limit, login=m.login, has_password=bool(m.password_hash),
        service_fee=m.service_fee if m.service_fee is not None else SERVICE_FEE_DEFAULT,
        service_fee_locked=bool(m.service_fee_locked),
    )


def _brief(op) -> OperationBrief:
    return OperationBrief(
        id=op.id, created_at=op.created_at, op_type=op.op_type, status=op.status,
        client_code=op.client_code, tariff_name=op.tariff_name, days=op.days,
        price=op.price or 0.0, payment_method=op.payment_method,
        key_fingerprint=op.key_fingerprint, key_expires_at=op.key_expires_at,
    )


# Продажи и выдачи — у них есть чек.
_SALE_TYPES = frozenset({"issue_tariff", "issue_custom", "issue_temp", "convert_temp"})


def _now() -> datetime.datetime:
    return datetime.datetime.now()


LABEL_MAX = 64


def _clean_label(raw: str | None) -> str | None:
    """Пометка менеджера: одна строка, без лишних пробелов, не длиннее LABEL_MAX."""
    label = " ".join((raw or "").split())[:LABEL_MAX]
    return label or None


class ManagerService:
    def __init__(self, *, manager_repo, client_repo, op_repo, temp_repo, access_repo, user_repo,
                 tariff_repo, payment_service, subscription_service, traffic_service,
                 device_slot_service, remnawave, settings_repo, create_payment, config,
                 notifier=None):
        self._managers = manager_repo
        self._clients = client_repo
        self._ops = op_repo
        self._temps = temp_repo
        self._access = access_repo
        self._users = user_repo
        self._tariffs = tariff_repo
        self._payments = payment_service
        self._subscriptions = subscription_service
        self._traffic = traffic_service
        self._devices = device_slot_service
        self._remnawave = remnawave
        self._settings = settings_repo
        self._create_payment = create_payment
        self._config = config
        # Отправка в Telegram (чеки, уведомления). None — в тестах и скриптах.
        self.notifier = notifier

    # ==========================================================================
    # Доступ и права
    # ==========================================================================

    async def get_by_telegram(self, telegram_id: int):
        """Активный менеджер по Telegram ID или None (для фильтра бота)."""
        manager = await self._managers.get_by_telegram_id(telegram_id)
        return manager if manager and manager.status == "active" else None

    async def require_active(self, manager_id: int):
        """Менеджер из БД СЕЙЧАС. Любой статус кроме active — отказ."""
        manager = await self._managers.get(manager_id)
        if manager is None or manager.status in ("deleted", "invited"):
            raise ManagerError("not_manager")
        if manager.status == "blocked":
            raise ManagerError("blocked")
        return manager

    @staticmethod
    def _require_right(manager, right: str) -> None:
        if not getattr(manager, right, False):
            raise ManagerError("no_right")

    async def view_by_telegram(self, telegram_id: int) -> ManagerView | None:
        manager = await self.get_by_telegram(telegram_id)
        return _view(manager) if manager else None

    async def get_session_manager(self, manager_id: int, version: int) -> ManagerView | None:
        """Менеджер для веб-сессии: активен И версия сессии совпадает с БД (блокировка/смена прав сбрасывают входы)."""
        manager = await self._managers.get(manager_id)
        if manager is None or manager.status != "active" or manager.session_version != version:
            return None
        return _view(manager)

    async def session_version(self, manager_id: int) -> int | None:
        manager = await self._managers.get(manager_id)
        return manager.session_version if manager else None

    async def view(self, manager_id: int) -> ManagerView:
        return _view(await self.require_active(manager_id))

    # ==========================================================================
    # Управление менеджерами (вызывается только из админки)
    # ==========================================================================

    async def invite(self, display_name: str, admin_id: int, login: str | None = None) -> tuple[ManagerView, str]:
        """
        Создаёт менеджера в статусе «приглашён». Возвращает токен ссылки — он нигде не хранится.
        Логин для сайта задаёт админ сразу; пароль менеджер придумает сам после принятия приглашения.
        """
        normalized = None
        if login is not None:
            normalized = await self._check_login(login)
        token = new_token()
        manager = await self._managers.create_invited(
            display_name.strip() or "Менеджер", admin_id, hash_token(token),
            _now() + datetime.timedelta(hours=INVITE_TTL_HOURS),
        )
        if normalized and not await self._managers.set_login(manager.id, normalized):
            # Логин заняли между проверкой и записью — менеджер создан, логин админ задаст в карточке.
            logger.warning(f"[manager] login {normalized!r} was taken while inviting #{manager.id}")
        else:
            manager = await self._managers.get(manager.id)
        logger.info(f"[manager] admin {admin_id} invited manager #{manager.id}")
        return _view(manager), token

    async def _check_login(self, raw: str, manager_id: int | None = None) -> str:
        login = normalize_login(raw)
        if login is None:
            raise ManagerError("bad_login")
        existing = await self._managers.get_by_login(login)
        if existing is not None and existing.id != manager_id:
            raise ManagerError("login_taken")
        return login

    async def set_login(self, manager_id: int, raw: str, admin_id: int) -> str:
        """Админ задаёт или меняет логин. Сессии не сбрасываем: пароль тот же, сменилось только имя входа."""
        login = await self._check_login(raw, manager_id)
        if not await self._managers.set_login(manager_id, login):
            raise ManagerError("login_taken")
        logger.info(f"[manager] admin {admin_id} set login of #{manager_id}: {login}")
        return login

    async def reinvite(self, manager_id: int) -> str:
        token = new_token()
        ok = await self._managers.set_invite(
            manager_id, hash_token(token), _now() + datetime.timedelta(hours=INVITE_TTL_HOURS)
        )
        if not ok:
            raise ManagerError("invalid_invite", "Приглашение можно обновить только до его принятия.")
        return token

    async def accept_invite(self, token: str, telegram_id: int) -> ManagerView:
        manager = await self._managers.accept_invite(hash_token(token), telegram_id)
        if manager is None:
            raise ManagerError("invalid_invite")
        logger.info(f"[manager] #{manager.id} accepted invite, telegram={telegram_id}")
        return _view(manager)

    async def set_status(self, manager_id: int, status: str, admin_id: int) -> None:
        if status not in ("active", "blocked", "deleted"):
            raise ValueError(status)
        await self._managers.set_status(manager_id, status)
        logger.info(f"[manager] admin {admin_id} set manager #{manager_id} status={status}")

    async def update_rights(self, manager_id: int, admin_id: int, **fields) -> None:
        await self._managers.update_rights(manager_id, **fields)
        logger.info(f"[manager] admin {admin_id} changed rights of #{manager_id}: {fields}")

    async def list_managers(self) -> list[ManagerView]:
        return [_view(m) for m in await self._managers.list_all()]

    @staticmethod
    def _check_fee(fee) -> int:
        if isinstance(fee, bool) or not isinstance(fee, int) or not 0 <= fee <= SERVICE_FEE_MAX:
            raise ManagerError("bad_fee")
        return fee

    async def set_service_fee(self, manager_id: int, fee: int) -> ManagerView:
        """Менеджер сам задаёт цену своей услуги — если админ её не зафиксировал."""
        manager = await self.require_active(manager_id)
        if manager.service_fee_locked:
            raise ManagerError("fee_locked")
        fee = self._check_fee(fee)
        await self._managers.set_service_fee(manager.id, fee)
        logger.info(f"[manager] #{manager.id} set service fee: {fee} ₽")
        return _view(await self._managers.get(manager.id))

    async def admin_set_service_fee(self, manager_id: int, admin_id: int, *, fee: int | None = None,
                                    locked: bool | None = None) -> None:
        """Админ: задать цену услуги менеджера и/или зафиксировать её (менеджер больше не меняет)."""
        manager = await self._managers.get(manager_id)
        if manager is None or manager.status == "deleted":
            raise ManagerError("not_manager")
        value = self._check_fee(fee) if fee is not None else manager.service_fee
        await self._managers.set_service_fee(manager_id, value, locked)
        logger.info(f"[manager] admin {admin_id} set service fee of #{manager_id}: {value} ₽, locked={locked}")

    # ==========================================================================
    # Вход на сайт
    # ==========================================================================

    async def create_password_link(self, manager_id: int) -> str:
        """Одноразовая ссылка «задать пароль» (30 мин). Возвращает токен — в БД только его хэш."""
        manager = await self.require_active(manager_id)
        if not manager.login:
            raise ManagerError("bad_login", "Логин для сайта ещё не задан. Обратитесь к администратору.")
        token = new_token()
        await self._managers.set_password_token(
            manager_id, hash_token(token), _now() + datetime.timedelta(minutes=PASSWORD_LINK_TTL_MINUTES)
        )
        return token

    async def password_link_owner(self, token: str) -> ManagerView | None:
        """Чья ссылка «задать пароль» — для показа формы. Ссылку НЕ гасит (превью мессенджеров открывают GET-ом)."""
        if not token:
            return None
        manager = await self._managers.get_by_password_token(hash_token(token))
        return _view(manager) if manager else None

    async def set_password_by_link(self, token: str, password: str) -> ManagerView:
        """
        Ставит пароль по ссылке из бота. Пароль проверяется ДО погашения ссылки:
        опечатка в пароле не должна сжигать ссылку. Все прежние сессии сбрасываются.
        """
        owner = await self.password_link_owner(token)
        if owner is None:
            raise ManagerError("invalid_password_link")
        problem = password_problem(password, owner.login)
        if problem:
            raise ManagerError("bad_password", problem)
        password_hash = await asyncio.to_thread(hash_password, password)
        manager = await self._managers.set_password_by_token(hash_token(token), password_hash)
        if manager is None:
            raise ManagerError("invalid_password_link")
        logger.info(f"[manager] #{manager.id} set a web password")
        await self._notify_manager(
            manager, "🔐 Пароль для входа на сайт установлен. Все прежние входы на сайте завершены."
        )
        return _view(manager)

    async def authenticate(self, raw_login: str, password: str) -> ManagerView:
        """
        Вход по логину и паролю. Ответ на неверный логин и неверный пароль одинаковый,
        а argon2 считается в обоих случаях — по тексту и по времени не понять, есть ли такой логин.
        Статус (блокировка) раскрываем только тому, кто знает пароль.
        """
        login = normalize_login(raw_login)
        manager = await self._managers.get_by_login(login) if login else None
        if manager is not None and manager.locked_until and manager.locked_until > _now():
            raise ManagerError("login_locked")

        ok = await asyncio.to_thread(verify_password, password, manager.password_hash if manager else None)
        if manager is None or not ok:
            if manager is not None and manager.password_hash:
                locked = await self._managers.register_failed_login(
                    manager.id, MAX_FAILED_LOGINS, _now() + datetime.timedelta(minutes=LOGIN_LOCK_MINUTES)
                )
                if locked:
                    logger.warning(f"[manager] #{manager.id}: web login locked after {MAX_FAILED_LOGINS} failures")
                    await self._notify_manager(
                        manager,
                        f"⚠️ {MAX_FAILED_LOGINS} неверных попыток входа на сайт под вашим логином. "
                        f"Вход закрыт на {LOGIN_LOCK_MINUTES} минут.\n\n"
                        "Если это были не вы — смените пароль: «Панель менеджера» → «Вход на сайт».",
                    )
                    raise ManagerError("login_locked")
            raise ManagerError("bad_credentials")

        if manager.status == "blocked":
            raise ManagerError("blocked")
        if manager.status != "active":
            raise ManagerError("bad_credentials")
        if manager.failed_logins or manager.locked_until:
            await self._managers.reset_failed_logins(manager.id)
        return _view(manager)

    async def notify_web_login(self, manager: ManagerView, ip: str | None, device: str | None) -> None:
        """Уведомление в бот о каждом входе на сайт — с кнопкой «завершить все сеансы»."""
        if self.notifier is None or not manager.telegram_id:
            return
        try:
            await self.notifier.notify_web_login(manager.telegram_id, ip=ip, device=device, when=fmt_dt(_now()))
        except Exception as e:
            logger.warning(f"[manager] cannot notify #{manager.id} about web login: {e}")

    async def change_password(self, manager_id: int, old_password: str, new_password: str) -> int:
        """Смена пароля на сайте (нужен текущий). Остальные сессии сбрасываются; возвращает новую версию для текущей."""
        manager = await self.require_active(manager_id)
        if not await asyncio.to_thread(verify_password, old_password, manager.password_hash):
            raise ManagerError("bad_password", "Текущий пароль указан неверно.")
        problem = password_problem(new_password, manager.login)
        if problem:
            raise ManagerError("bad_password", problem)
        if new_password == old_password:
            raise ManagerError("bad_password", "Новый пароль совпадает с текущим.")
        version = await self._managers.set_password(manager_id, await asyncio.to_thread(hash_password, new_password))
        logger.info(f"[manager] #{manager_id} changed the web password")
        await self._notify_manager(manager, "🔐 Пароль для входа на сайт изменён. Остальные входы на сайте завершены.")
        return version

    async def reset_password(self, manager_id: int, admin_id: int) -> str | None:
        """
        Сброс админом: пароль стёрт, все веб-сессии закрыты. Активному менеджеру с логином
        возвращается токен новой ссылки «задать пароль» (её шлют ему в бот).
        """
        manager = await self._managers.get(manager_id)
        if manager is None or manager.status == "deleted":
            raise ManagerError("not_manager")
        await self._managers.set_password(manager_id, None)
        logger.info(f"[manager] admin {admin_id} reset the web password of #{manager_id}")
        if manager.status != "active" or not manager.login:
            return None
        return await self.create_password_link(manager_id)

    async def end_web_sessions(self, manager_id: int) -> None:
        """«Это был не я»: все входы на сайте закрываются, пароль остаётся."""
        await self.require_active(manager_id)
        await self._managers.bump_session(manager_id)
        logger.info(f"[manager] #{manager_id} ended all web sessions")

    async def _notify_manager(self, manager, text: str) -> None:
        if self.notifier is None or not getattr(manager, "telegram_id", None):
            return
        try:
            await self.notifier.notify_manager_text(manager.telegram_id, text)
        except Exception as e:
            logger.warning(f"[manager] cannot notify #{manager.id}: {e}")

    # ==========================================================================
    # Настройки
    # ==========================================================================

    async def temp_settings(self) -> tuple[int, int, int]:
        """(минут жизни, ГБ трафика, устройств) временного ключа."""
        return (
            max(1, await self._settings.get_int(SETTING_TEMP_MINUTES, DEFAULT_TEMP_MINUTES)),
            max(1, await self._settings.get_int(SETTING_TEMP_TRAFFIC, DEFAULT_TEMP_TRAFFIC_GB)),
            max(1, await self._settings.get_int(SETTING_TEMP_DEVICES, DEFAULT_TEMP_DEVICES)),
        )

    async def custom_settings(self) -> CustomPriceSettings:
        return CustomPriceSettings(
            day_price=await self._settings.get_float(SETTING_DAY_PRICE, DEFAULT_DAY_PRICE),
            short_premium=await self._settings.get_float(SETTING_SHORT_PREMIUM, DEFAULT_SHORT_PREMIUM),
            min_price=await self._settings.get_int(SETTING_MIN_PRICE, DEFAULT_MIN_PRICE),
            max_days=await self._settings.get_int(SETTING_MAX_DAYS, DEFAULT_MAX_DAYS),
        )

    async def _tariff_refs(self) -> list[TariffRef]:
        return [TariffRef(t.name, t.duration_days, t.price) for t in await self._tariffs.get_active()]

    async def list_tariffs(self) -> list[TariffView]:
        traffic = await self._traffic.settings()
        return [
            TariffView(t.id, t.name, t.duration_days, t.price, tariff_quota_gb(traffic, t.data_limit_gb))
            for t in await self._tariffs.get_active()
        ]

    # ==========================================================================
    # Клиенты и доступ
    # ==========================================================================

    async def _access_until(self, user) -> datetime.datetime:
        """Доступ живёт до конца подписки + запас, но не меньше суток — чтобы выдать и успеть обслужить."""
        grace = await self._settings.get_int(SETTING_ACCESS_GRACE, DEFAULT_ACCESS_GRACE_DAYS)
        floor = _now() + datetime.timedelta(days=1)
        end = user.subscription_end_date
        return max(floor, end + datetime.timedelta(days=grace)) if end else floor

    async def _ensure_client_code(self, user) -> str:
        if user.client_code:
            return user.client_code
        for _ in range(10):
            code = generate_client_code()
            if await self._users.set_client_code(user.user_id, code):
                return code
        raise ManagerError("generic")

    async def create_client(self, manager_id: int, label: str | None = None) -> tuple[str, str]:
        """
        Новый офлайн-клиент. Возвращает (client_code, токен кабинета): токен виден
        один раз — в БД остаётся только его хэш.
        """
        manager = await self.require_active(manager_id)
        token = new_token()
        user = await self._users.create_offline_client(generate_client_code, hash_token(token), manager.id)
        await self._clients.grant(manager.id, user.user_id, "created", await self._access_until(user), label)
        return user.client_code, token

    async def add_client_by_code(self, manager_id: int, raw_code: str, label: str | None = None) -> str:
        """
        Доступ к клиенту по одноразовому коду, который клиент сам сгенерировал.

        Неверные вводы считаются: после CODE_ATTEMPTS_PER_HOUR промахов за час
        менеджер заблокирован на ввод — код из 6 символов не должен подбираться.
        """
        manager = await self.require_active(manager_id)
        since = _now() - datetime.timedelta(hours=1)
        if await self._access.count_failed(manager.id, since) >= CODE_ATTEMPTS_PER_HOUR:
            raise ManagerError("code_rate_limited")

        code = normalize_client_code(raw_code)
        user_id = await self._access.consume(hash_token(code), manager.id) if code else None
        if user_id is None:
            await self._access.log_failed_attempt(manager.id)
            raise ManagerError("bad_code")

        user = await self._users.get(user_id)
        if user is None:
            raise ManagerError("client_not_found")
        client_code = await self._ensure_client_code(user)
        await self._clients.grant(manager.id, user.user_id, "code", await self._access_until(user), label)
        await self._ops.add_event(
            manager.id, "access_grant", client_user_id=user.user_id, client_code=client_code,
        )
        await self._notify_client_access(user, manager)
        return client_code

    async def _notify_client_access(self, user, manager) -> None:
        if self.notifier and user.user_id > 0:
            try:
                await self.notifier.notify_client_access(user.user_id, manager.display_name)
            except Exception:
                logger.warning("[manager] could not notify client about access", exc_info=True)

    async def _client_by_code(self, manager, raw_code: str):
        """Клиент по коду — только если у менеджера есть действующий доступ."""
        code = normalize_client_code(raw_code)
        if not code:
            raise ManagerError("client_not_found")
        user = await self._users.get_by_client_code(code)
        if user is None:
            raise ManagerError("client_not_found")
        if await self._clients.get_active(manager.id, user.user_id) is None:
            # Не различаем «нет такого» и «не ваш»: код нельзя использовать для разведки.
            raise ManagerError("client_not_found")
        return user

    async def list_clients(self, manager_id: int, page: int = 0, size: int = 8) -> tuple[list[ClientRow], int]:
        manager = await self.require_active(manager_id)
        total = await self._clients.count_for_manager(manager.id)
        rows = []
        for link, user in await self._clients.list_for_manager(manager.id, size, page * size):
            if not user.client_code:
                continue
            rows.append(ClientRow(
                client_code=user.client_code, label=link.label,
                subscription_end=user.subscription_end_date,
                is_active=days_left(user.subscription_end_date) > 0,
                access_until=link.access_until,
            ))
        return rows, total

    async def set_client_label(self, manager_id: int, client_code: str, label: str | None) -> None:
        manager = await self.require_active(manager_id)
        user = await self._client_by_code(manager, client_code)
        await self._clients.set_label(manager.id, user.user_id, label)

    async def revoke_access_by_client(self, user_id: int, manager_id: int | None = None) -> int:
        """Клиент отзывает доступ менеджера (кнопка в боте/кабинете)."""
        return await self._clients.revoke(user_id, manager_id)

    async def create_access_code(self, user_id: int) -> str:
        """Одноразовый код клиента для менеджера (живёт ACCESS_CODE_TTL_MINUTES)."""
        code = generate_client_code()
        await self._access.create_code(
            user_id, hash_token(code), _now() + datetime.timedelta(minutes=ACCESS_CODE_TTL_MINUTES)
        )
        return code

    async def get_client_card(self, manager_id: int, client_code: str) -> ClientCard:
        manager = await self.require_active(manager_id)
        user = await self._client_by_code(manager, client_code)
        link = await self._clients.get_active(manager.id, user.user_id)

        rw_user = None
        devices: list[dict] = []
        if user.vpn_username:
            try:
                rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
                if rw_user and (rw_user.get("uuid") or user.remnawave_uuid):
                    devices = await self._remnawave.get_user_devices(rw_user.get("uuid") or user.remnawave_uuid)
            except Exception:
                logger.warning("[manager] panel unavailable while building client card", exc_info=True)

        dev_settings = await self._devices.settings()
        active = days_left(user.subscription_end_date) > 0
        limit_bytes = int((rw_user or {}).get("trafficLimitBytes") or 0) if rw_user else None
        used_bytes = None
        if rw_user:
            used_bytes = int(
                (rw_user.get("userTraffic") or {}).get("usedTrafficBytes", rw_user.get("usedTrafficBytes", 0)) or 0
            )

        ops = await self._ops.list_for_client(manager.id, user.user_id, limit=5)
        return ClientCard(
            client_code=user.client_code,
            label=link.label if link else None,
            subscription_active=active,
            subscription_end=user.subscription_end_date,
            days_left=days_left(user.subscription_end_date),
            devices_used=len(devices) if rw_user else None,
            devices_limit=(dev_settings.base_limit + (user.extra_devices or 0)) if active else dev_settings.base_limit,
            device_platforms=tuple(str(d.get("platform") or "?") for d in devices),
            traffic_used_gb=round(used_bytes / GIB, 1) if used_bytes is not None else None,
            traffic_limit_gb=(limit_bytes // GIB) if limit_bytes is not None else None,
            extra_devices=user.extra_devices or 0,
            extra_traffic_gb=user.extra_traffic_gb or 0,
            has_key=bool(rw_user),
            access_until=link.access_until if link else None,
            operations=tuple(_brief(op) for op in ops),
        )

    async def get_install_link(self, manager_id: int, client_code: str) -> str:
        """Ссылка подписки клиента для установки. Каждый просмотр пишется в журнал."""
        manager = await self.require_active(manager_id)
        user = await self._client_by_code(manager, client_code)
        url = await self._subscription_url(user)
        if not url:
            raise ManagerError("client_not_found", "У клиента ещё нет ключа.")
        await self._ops.add_event(
            manager.id, "key_view", client_user_id=user.user_id, client_code=user.client_code,
            key_username=user.vpn_username, key_fingerprint=key_fingerprint(url),
        )
        return url

    async def reset_cabinet_link(self, manager_id: int, client_code: str) -> str:
        """
        Новая ссылка кабинета офлайн-клиента (старая сразу перестаёт работать).

        Нужна, когда клиент потерял ссылку из чека. Только для клиентов, которых завёл менеджер
        (у Telegram-клиентов кабинет — это бот). Показывается один раз; в журнале — факт перевыпуска.
        """
        manager = await self.require_active(manager_id)
        user = await self._client_by_code(manager, client_code)
        if user.origin != "offline":
            raise ManagerError("generic", "У этого клиента кабинет — в Telegram-боте, ссылка не нужна.")
        token = new_token()
        await self._users.set_cabinet_token_hash(user.user_id, hash_token(token))
        await self._ops.add_event(
            manager.id, "cabinet_link_reset", client_user_id=user.user_id, client_code=user.client_code,
        )
        return self._cabinet_url(token)

    async def disable_autorenew(self, manager_id: int, client_code: str) -> bool:
        """Выключает автопродление клиента по его просьбе (логируется)."""
        manager = await self.require_active(manager_id)
        user = await self._client_by_code(manager, client_code)
        if not await self._payments.disable_autorenew(user.user_id):
            return False
        await self._ops.add_event(
            manager.id, "autorenew_off", client_user_id=user.user_id, client_code=user.client_code,
        )
        return True

    # ==========================================================================
    # Цена и предпросмотр
    # ==========================================================================

    async def _subscription_url(self, user) -> str | None:
        if not user.vpn_username:
            return None
        try:
            rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
        except Exception:
            logger.warning("[manager] panel unavailable while reading subscription url", exc_info=True)
            return None
        return (rw_user or {}).get("subscriptionUrl") or None

    async def quote(self, manager_id: int, *, product: str, client_code: str | None = None,
                    tariff_id: int | None = None, days: int | None = None,
                    slots: int | None = None, packs: int | None = None) -> IssueQuote:
        """
        Предпросмотр: цена со всеми надстройками. Менеджер видит её до подтверждения.
        slots / packs — доп. устройства и пакеты трафика на новый срок; None — оставить как у клиента.
        """
        manager = await self.require_active(manager_id)
        client = await self._client_by_code(manager, client_code) if client_code else None
        return await self._build_quote(manager, client, product, tariff_id, days, slots, packs)

    async def _build_quote(self, manager, client, product: str, tariff_id, days,
                           slots: int | None = None, packs: int | None = None) -> IssueQuote:
        if product == "tariff":
            self._require_right(manager, "can_issue_tariff")
        elif product == "custom":
            self._require_right(manager, "can_issue_custom")
        else:
            raise ManagerError("generic")

        dev_settings = await self._devices.settings()
        traffic_settings = await self._traffic.settings()
        # Не выбрано — продлеваем то, что у клиента уже есть (у нового — ничего).
        if slots is None:
            slots = min((client.extra_devices or 0) if client else 0, dev_settings.max_extra)
        elif not 0 <= slots <= dev_settings.max_extra:
            raise ManagerError("bad_extras")
        extra_gb = (client.extra_traffic_gb or 0) if client else 0
        if packs is not None and not 0 <= packs <= traffic_settings.max_packs:
            raise ManagerError("bad_extras")
        has_active = bool(client and days_left(client.subscription_end_date) > 0)

        hint = None
        breakdown = None
        details: dict = {}
        if product == "tariff":
            tariff = await self._tariffs.get_by_id(tariff_id) if tariff_id else None
            if tariff is None or not tariff.is_active or tariff.is_intro:
                raise ManagerError("bad_tariff")
            duration = tariff.duration_days
            base_price = effective_price(tariff, has_active)
            quota = tariff_quota_gb(traffic_settings, tariff.data_limit_gb)
            name = tariff.name
            details = {"product": "tariff", "tariff_price": base_price}
            renew_tariff = tariff
        else:
            settings = await self.custom_settings()
            try:
                result = compute_custom_price(settings, days, await self._tariff_refs())
            except CustomDaysError as e:
                raise ManagerError("bad_days", str(e))
            duration = result.days
            base_price = float(result.price)
            quota = tariff_quota_gb(traffic_settings, None)
            name = CUSTOM_PRODUCT_NAME
            breakdown = result.breakdown(settings.day_price)
            hint = result.hint
            details = {"product": "custom", "formula_price": result.price, "reason": result.floor_reason}
            tariff = None
            renew_tariff = await self._payments.custom_renew_tariff()

        fee = float(manager.service_fee or 0)
        checkout = build_checkout(dev_settings, base_price, duration, slots)
        if quota == 0:
            packs = 0  # безлимит: докупать нечего
        elif packs is None:
            packs = min(gb_to_packs(traffic_settings, extra_gb), traffic_settings.max_packs)
        if packs:
            checkout = checkout.with_traffic(
                packs, packs_cost_for_tariff(traffic_settings, duration, packs),
                packs_to_gb(traffic_settings, packs),
            )
        details.update(slots=slots, slots_cost=checkout.slots_cost, packs=packs,
                       traffic_cost=checkout.traffic_cost, days=duration, service_fee=fee)

        autorenew = bool(self._config.yookassa.save_payment_method and renew_tariff)
        renew_text = None
        if autorenew:
            extras = " плюс выбранные доп. устройства и трафик" if (slots or packs) else ""
            renew_text = (
                f"После оплаты способ оплаты сохранится, и подписка будет продлеваться автоматически: "
                f"{renew_tariff.price:.0f} ₽{extras} каждые {renew_tariff.duration_days} дн. "
                + ("Услуга менеджера при продлении не списывается. " if fee else "")
                + "Отключить можно в личном кабинете или у менеджера."
            )

        return IssueQuote(
            product=product, tariff_id=tariff.id if tariff else None, tariff_name=name, days=duration,
            base_price=base_price, slots=slots, slots_cost=checkout.slots_cost, packs=packs,
            traffic_cost=checkout.traffic_cost, extra_traffic_gb=checkout.traffic_gb, total=checkout.total + fee,
            quota_gb=quota, devices_limit=dev_settings.base_limit + slots, breakdown=breakdown, hint=hint,
            renew_text=renew_text, autorenew_available=autorenew,
            client_code=client.client_code if client else None, client_is_new=client is None,
            price_details=details,
            base_devices=dev_settings.base_limit, max_slots=dev_settings.max_extra, slot_price=dev_settings.price,
            max_packs=traffic_settings.max_packs if quota > 0 else 0, pack_gb=traffic_settings.pack_gb,
            pack_price=traffic_settings.pack_price, service_fee=fee,
        )

    # ==========================================================================
    # Выдача по тарифу / на свои дни
    # ==========================================================================

    async def issue(self, manager_id: int, *, product: str, method: str, idempotency_nonce: str,
                    client_code: str | None = None, tariff_id: int | None = None, days: int | None = None,
                    expected_total: float | None = None, temp_key_id: int | None = None,
                    label: str | None = None, slots: int | None = None, packs: int | None = None) -> IssueResult:
        """
        Подтверждение выдачи. Идемпотентно: повтор с тем же nonce вернёт прежний
        результат, а не выдаст второй ключ.

        temp_key_id — конвертация временного ключа: клиентом становится тот, кому
        он выдан, а панельный пользователь остаётся прежним (без переустановки).
        label — пометка менеджера для НОВОГО клиента («Анна, кофейня»); существующему не меняется.
        slots / packs — доп. устройства и пакеты трафика на новый срок (None — как у клиента сейчас).
        """
        manager = await self.require_active(manager_id)
        if method not in ("cash", "online"):
            raise ManagerError("bad_method")
        if method == "cash":
            self._require_cash(manager)

        op_type = "issue_custom" if product == "custom" else "issue_tariff"
        if temp_key_id is not None:
            op_type = "convert_temp"

        key = f"{manager.id}:{idempotency_nonce}"[:64]
        existing = await self._ops.create_idempotent(
            key, manager_id=manager.id, op_type=op_type, status="processing", payment_method=method,
        )
        op, created = existing
        if not created:
            return await self._replay(op)

        try:
            if client_code is None:
                # Новый клиент (в том числе с временного ключа): сначала проверяем вход
                # (тариф, дни, цена, лимит), и только потом заводим клиента — иначе ошибка
                # валидации оставляла бы «сирот» (а у конвертации — ещё и занятое имя ключа).
                quote = await self._build_quote(manager, None, product, tariff_id, days, slots, packs)
                self._check_expected(expected_total, quote)
                if method == "cash":
                    await self._check_cash_limit(manager, quote.subscription_total)
            client, is_new, cabinet_token = await self._resolve_client(manager, client_code, temp_key_id, _clean_label(label))
            # Клиент создан — фиксируем в журнале сразу: если дальше что-то сорвётся, _fail
            # знает, кого отвязать от временного ключа.
            await self._ops.update(op.id, client_user_id=client.user_id, client_code=client.client_code)
            quote = await self._build_quote(manager, client, product, tariff_id, days, slots, packs)
            self._check_expected(expected_total, quote)
            if method == "cash":
                await self._check_cash_limit(manager, quote.subscription_total)
            details = dict(quote.price_details, new_client=is_new)
            await self._ops.update(
                op.id, client_user_id=client.user_id, client_code=client.client_code,
                tariff_id=quote.tariff_id, tariff_name=quote.tariff_name, days=quote.days,
                traffic_gb=quote.quota_gb + quote.extra_traffic_gb if quote.quota_gb else 0,
                extra_devices=quote.slots, price=quote.total, service_fee=quote.service_fee,
                price_details=details,
            )
            if method == "cash":
                return await self._issue_cash(manager, client, quote, op, cabinet_token, temp_key_id)
            return await self._issue_online(manager, client, quote, op, cabinet_token)
        except ManagerError as e:
            await self._fail(op, e.code, temp_key_id)
            raise
        except Exception:
            logger.error(f"[manager] issue failed: manager={manager.id}, op={op.id}", exc_info=True)
            await self._fail(op, "generic", temp_key_id)
            raise ManagerError("generic")

    @staticmethod
    def _check_expected(expected_total: float | None, quote: "IssueQuote") -> None:
        if expected_total is not None and abs(expected_total - quote.total) > 0.01:
            raise ManagerError("price_changed")

    def _require_cash(self, manager) -> None:
        if not manager.can_accept_cash:
            raise ManagerError("cash_not_allowed")

    async def _check_cash_limit(self, manager, amount: float) -> None:
        """amount — деньги магазина (без услуги менеджера): лимит — на то, что придётся сдать."""
        if manager.cash_limit is None:
            return
        outstanding, _ = await self._ops.cash_outstanding(manager.id)
        if outstanding + amount > manager.cash_limit + 1e-9:
            raise ManagerError("cash_limit")

    async def _fail(self, op, code: str, temp_key_id: int | None = None) -> None:
        await self._ops.update(op.id, status="failed", error_code=code, completed_at=_now())
        if temp_key_id is None:
            return
        # Возвращаем в обычное состояние только СВОЙ ключ: чужая конвертация не наша забота.
        key = await self._temps.get(temp_key_id)
        if key is None or key.manager_id != op.manager_id:
            return
        fresh = await self._ops.get(op.id)
        # Платёж прошёл — клиент уже владеет ключом, откатывать нельзя.
        paid = await self._payments.get_payment(f"cash:{op.id}")
        if paid is not None and paid.status == "succeeded":
            return
        if await self._temps.transition(key.id, "converting", "active") and fresh and fresh.client_user_id:
            # Клиент-«сирота» держал имя ключа в панели (vpn_username уникален) — отпускаем,
            # иначе повторная конвертация упёрлась бы в UNIQUE.
            await self._users.bind_panel_user(fresh.client_user_id, None, None)

    async def _resolve_client(self, manager, client_code, temp_key_id, label: str | None = None):
        """Клиент выдачи: из временного ключа, по коду или новый. Возвращает (user, is_new, cabinet_token|None)."""
        if temp_key_id is not None:
            self._require_right(manager, "can_issue_temp")
            key = await self._temps.get(temp_key_id)
            if key is None or key.manager_id != manager.id or key.status != "active":
                raise ManagerError("temp_not_found")
            if not await self._temps.transition(key.id, "active", "converting"):
                raise ManagerError("temp_not_found")
            code, token = await self.create_client(manager.id, label)
            user = await self._users.get_by_client_code(code)
            await self._users.bind_panel_user(user.user_id, key.rw_username, key.rw_uuid)
            await self._temps.transition(key.id, "converting", "converting", client_user_id=user.user_id)
            await self._clients.grant(manager.id, user.user_id, "temp_convert", await self._access_until(user), label)
            return await self._users.get(user.user_id), True, token
        if client_code:
            return await self._client_by_code(manager, client_code), False, None
        code, token = await self.create_client(manager.id, label)
        return await self._users.get_by_client_code(code), True, token

    async def _replay(self, op) -> IssueResult:
        """Тот же запрос второй раз: возвращаем то, что уже было сделано."""
        return IssueResult(
            operation_id=op.id, status="completed" if op.status == "completed" else op.status,
            price=op.price or 0.0, client_code=op.client_code, expires_at=op.key_expires_at, replayed=True,
        )

    async def _issue_cash(self, manager, client, quote: IssueQuote, op, cabinet_token, temp_key_id):
        yk_id = f"cash:{op.id}"
        kind = "custom" if quote.product == "custom" else "subscription"
        await self._payments.create_payment_record(
            yookassa_payment_id=yk_id, user_id=client.user_id, tariff_id=quote.tariff_id,
            original_amount=quote.total, final_amount=quote.total, source="cash", kind=kind,
            extra_devices=quote.slots, extra_traffic_gb=quote.extra_traffic_gb,
            manager_id=manager.id, days=quote.days if kind == "custom" else None,
        )
        await self._ops.update(op.id, payment_id=yk_id)

        processed = None
        try:
            processed = await self._payments.process_successful_payment(yk_id, quote.total)
        except Exception:
            logger.error(f"[manager] cash payment processing raised: op={op.id}", exc_info=True)
        if processed is None:
            # Исключение могло прилететь уже ПОСЛЕ продления — сверяемся со статусом платежа.
            stored = await self._payments.get_payment(yk_id)
            if stored is None or stored.status != "succeeded":
                await self._payments.mark_payment_failed(yk_id)
                raise ManagerError("payment_failed")
        if temp_key_id is not None:
            await self._temps.transition(temp_key_id, "converting", "converted")

        return await self._finalize(client, op, cabinet_token, method="cash")

    async def _issue_online(self, manager, client, quote: IssueQuote, op, cabinet_token):
        if await self._payments.has_pending_payment(client.user_id):
            raise ManagerError("pending_payment")

        dev_settings = await self._devices.settings()
        checkout = build_checkout(dev_settings, quote.base_price, quote.days, quote.slots)
        if quote.packs:
            checkout = checkout.with_traffic(quote.packs, quote.traffic_cost, quote.extra_traffic_gb)
        kind = "custom" if quote.product == "custom" else "subscription"
        description = f"Подписка VPN: {quote.tariff_name}" + (f" ({quote.days} дн.)" if kind == "custom" else "")
        # По QR через ЮKassa идёт только подписка: услугу менеджера клиент отдаёт ему наличными,
        # в счёт и фискальный чек она не входит (в нашем чеке — отдельной строкой «наличными»).
        items = receipt_items(quote.tariff_name if kind == "subscription" else f"{quote.days} дн.",
                              checkout, quote.days)
        amount = quote.subscription_total

        cfg = self._config
        if cabinet_token:
            return_url = f"https://{cfg.webhook.domain}/c/{cabinet_token}"
        elif cfg.tg_bot.tg_bot_username:
            return_url = f"https://t.me/{cfg.tg_bot.tg_bot_username}"
        else:
            return_url = f"https://{cfg.webhook.domain}/"

        try:
            payment_url, yk_id = await asyncio.to_thread(
                self._create_payment,
                amount=amount, description=description, return_url=return_url,
                user_id=client.user_id, user_email=client.email,
                shop_id=cfg.yookassa.shop_id, secret_key=cfg.yookassa.secret_key,
                save_payment_method=quote.autorenew_available,
                metadata={"manager_id": str(manager.id), "op_id": str(op.id), "kind": kind},
                items=items,
            )
        except Exception:
            logger.error(f"[manager] YooKassa payment creation failed: op={op.id}", exc_info=True)
            raise ManagerError("payment_failed")

        await self._payments.create_payment_record(
            yookassa_payment_id=yk_id, user_id=client.user_id, tariff_id=quote.tariff_id,
            original_amount=amount, final_amount=amount, source="manager", kind=kind,
            extra_devices=quote.slots, extra_traffic_gb=quote.extra_traffic_gb,
            manager_id=manager.id, days=quote.days if kind == "custom" else None,
        )
        await self._ops.update(op.id, status="pending_payment", payment_id=yk_id, payment_method="online",
                               fee_in_cash=True)
        await self._publish(op.id)
        return IssueResult(
            operation_id=op.id, status="pending_payment", price=amount, payment_url=payment_url,
            fee_cash=quote.service_fee, client_code=client.client_code, cabinet_url=self._cabinet_url(cabinet_token),
        )

    def _cabinet_url(self, token: str | None) -> str | None:
        if not token:
            return None
        return f"https://{self._config.webhook.domain}/c/{token}"

    async def _finalize(self, client, op, cabinet_token, *, method: str) -> IssueResult:
        """Операция проведена: записываем итог, рассылаем чеки, возвращаем результат менеджеру."""
        fresh = await self._users.get(client.user_id)
        url = await self._subscription_url(fresh)
        await self._ops.update(
            op.id, status="completed", completed_at=_now(), payment_method=method,
            key_username=fresh.vpn_username, key_fingerprint=key_fingerprint(url or fresh.vpn_username or ""),
            key_expires_at=fresh.subscription_end_date,
        )
        text, image = await self._publish(op.id, subscription_url=url, with_image=True)
        return IssueResult(
            operation_id=op.id, status="completed", price=(await self._ops.get(op.id)).price or 0.0,
            subscription_url=url, client_code=fresh.client_code, cabinet_url=self._cabinet_url(cabinet_token),
            expires_at=fresh.subscription_end_date, receipt_text=text, receipt_image=image,
        )

    # ==========================================================================
    # Онлайн-оплата: вебхук и сверка
    # ==========================================================================

    async def on_payment_succeeded(self, payment_id: str) -> None:
        """Вызывается вебхуком после process_successful_payment для платежа с manager_id. Идемпотентно."""
        op = await self._ops.get_by_payment_id(payment_id)
        if op is None:
            logger.warning(f"[manager] payment {payment_id} has no operation")
            return
        client = await self._users.get(op.client_user_id) if op.client_user_id else None
        if client is None:
            return
        # Из «отменена» тоже: счёт мог закрыться по таймауту, а клиент оплатил по старой ссылке —
        # платёж обработан, подписка выдана, и журнал обязан это отразить.
        if not await self._ops.transition_any(
            op.id, ("pending_payment", "cancelled"), "completed", completed_at=_now(), error_code=None,
        ):
            return  # уже завершена (повторная доставка вебхука или сверка успела раньше)

        for key in await self._temps.list_converting_for_client(client.user_id):
            await self._temps.transition(key.id, "converting", "converted")

        url = await self._subscription_url(client)
        await self._ops.update(
            op.id, key_username=client.vpn_username,
            key_fingerprint=key_fingerprint(url or client.vpn_username or ""),
            key_expires_at=client.subscription_end_date,
        )
        await self._mark_invoice(op, "✅ <b>Оплачено</b> · {price}\n\nКлюч выдан — чек с QR для установки пришёл следующим сообщением.")
        await self._publish(op.id, subscription_url=url, notify_manager=True, notify_client=True)

    async def on_payment_refunded(self, payment_id: str) -> None:
        """
        Возврат онлайн-оплаты (вебхук refund.succeeded): операция → refunded, услуга менеджера
        выпадает из «к выплате» и из заработка. Уже выплаченную услугу забирает админ вручную — пишем в лог.
        """
        op = await self._ops.get_by_payment_id(payment_id)
        if op is None or not await self._ops.transition(op.id, "completed", "refunded"):
            return
        if op.service_fee and op.fee_paid_at:
            logger.error(
                f"[manager] возврат по чеку M-{op.id:06d}: услуга менеджера #{op.manager_id} "
                f"({op.service_fee:.0f} ₽) уже выплачена — удержите её при следующей выплате"
            )
        await self._publish(op.id, notify_manager=True)

    async def remember_invoice_message(self, manager_id: int, operation_id: int, chat_id: int, message_id: int) -> None:
        """Бот показал счёт — запоминаем сообщение, чтобы обновить его, когда придёт оплата или счёт отменится."""
        op = await self._ops.get(operation_id)
        if op is None or op.manager_id != manager_id:
            return
        await self._ops.update(operation_id, invoice_chat_id=chat_id, invoice_message_id=message_id)

    async def _mark_invoice(self, op, template: str) -> None:
        """Живой статус счёта в боте: правим сообщение с QR оплаты. Сбой — не повод ронять оплату."""
        fresh = await self._ops.get(op.id)
        if self.notifier is None or not fresh or not fresh.invoice_message_id:
            return
        paid = (fresh.price or 0) - ((fresh.service_fee or 0) if fresh.fee_in_cash else 0)
        text = template.format(price=fmt_money(paid))
        try:
            await self.notifier.update_invoice(fresh.invoice_chat_id, fresh.invoice_message_id, text)
        except Exception as e:
            logger.warning(f"[manager] cannot update invoice message of op {op.id}: {e}")

    async def sync_pending_operations(self) -> int:
        """
        Сверка висящих онлайн-операций с платежами: отменённый/проваленный счёт →
        операция отменена; оплаченный, но не доведённый (вебхук упал) → доводим.
        """
        changed = 0
        for op in await self._ops.list_pending_online():
            payment = await self._payments.get_payment(op.payment_id) if op.payment_id else None
            if payment is None or payment.status in ("cancelled", "failed"):
                if await self._ops.transition(op.id, "pending_payment", "cancelled", completed_at=_now()):
                    await self._release_converting(op)
                    await self._mark_invoice(op, "🚫 <b>Счёт не оплачен и закрыт</b>\n\nЕсли клиент всё ещё хочет купить — выставьте новый.")
                    await self._publish(op.id)
                    changed += 1
            elif payment.status == "succeeded":
                await self.on_payment_succeeded(op.payment_id)
                changed += 1
        return changed

    async def _release_converting(self, op) -> None:
        """Отменили оплату конвертации — временный ключ снова обычный (доживёт свой срок)."""
        if op.op_type != "convert_temp" or not op.client_user_id:
            return
        for key in await self._temps.list_converting_for_client(op.client_user_id):
            await self._temps.transition(key.id, "converting", "active")

    async def check_operation(self, manager_id: int, operation_id: int) -> OperationBrief | None:
        """Кнопка «Проверить оплату»: доводит операцию, если деньги уже пришли, и отдаёт её состояние."""
        manager = await self.require_active(manager_id)
        op = await self._ops.get(operation_id)
        if op is None or op.manager_id != manager.id:
            return None
        if op.status == "pending_payment" and op.payment_id:
            payment = await self._payments.get_payment(op.payment_id)
            if payment is not None and payment.status == "succeeded":
                await self.on_payment_succeeded(op.payment_id)
                op = await self._ops.get(operation_id)
        return _brief(op)

    async def renewal_digest(self, days: int = 3) -> list[tuple[int, list[str]]]:
        """
        Для каждого менеджера — строки о его клиентах, у которых подписка кончается
        в ближайшие `days` дней, а автопродление не включено. [(telegram_id, [строка, ...])].
        """
        horizon = _now() + datetime.timedelta(days=days)
        result = []
        for manager in await self._managers.list_all():
            if manager.status != "active" or not manager.telegram_id:
                continue
            lines = []
            for link, user in await self._clients.list_for_manager(manager.id, limit=200):
                end = user.subscription_end_date
                if not user.client_code or not end or not (_now() < end <= horizon):
                    continue
                if await self._payments.autorenew_enabled(user.user_id):
                    continue
                label = f" · {link.label}" if link.label else ""
                lines.append(f"• <code>{user.client_code}</code>{label} — до {end.strftime('%d.%m')}")
            if lines:
                result.append((manager.telegram_id, lines))
        return result

    async def cancel_pending(self, manager_id: int, operation_id: int) -> bool:
        """Менеджер отменяет неоплаченный счёт своей операции."""
        manager = await self.require_active(manager_id)
        op = await self._ops.get(operation_id)
        if op is None or op.manager_id != manager.id or op.status != "pending_payment":
            return False
        await self._payments.cancel_pending_payment(op.client_user_id)
        if await self._ops.transition(op.id, "pending_payment", "cancelled", completed_at=_now()):
            await self._release_converting(op)
            await self._mark_invoice(op, "🚫 <b>Счёт отменён</b>\n\nНе оплачивайте старую ссылку.")
            await self._publish(op.id)
        return True

    # ==========================================================================
    # Временные ключи
    # ==========================================================================

    async def issue_temp(self, manager_id: int, idempotency_nonce: str) -> TempKeyResult:
        manager = await self.require_active(manager_id)
        self._require_right(manager, "can_issue_temp")
        if await self._ops.count_today(manager.id, "issue_temp") >= manager.temp_keys_per_day:
            raise ManagerError("temp_limit")

        key = f"{manager.id}:{idempotency_nonce}"[:64]
        op, created = await self._ops.create_idempotent(
            key, manager_id=manager.id, op_type="issue_temp", status="processing",
            payment_method="free", price=0,
        )
        if not created:
            existing = await self._temps.get_by_operation(op.id)
            if existing is None:
                raise ManagerError("generic")
            url = await self._subscription_url_by_username(existing.rw_username)
            return TempKeyResult(op.id, existing.id, url or "", existing.expires_at, op.client_code)

        minutes, gb, devices = await self.temp_settings()
        username = temp_username()
        expires_at = _now() + datetime.timedelta(minutes=minutes)
        expire_iso = (datetime.datetime.now(datetime.timezone.utc)
                      + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rw_user = None
        try:
            rw_user = await self._remnawave.create_user(
                username=username, expire_at=expire_iso, traffic_limit_bytes=gb * GIB,
                traffic_limit_strategy="NO_RESET",
                description=f"temp · manager #{manager.id} · op #{op.id}",
            )
            uuid = rw_user.get("uuid")
            await self._remnawave.update_user(uuid, hwid_device_limit=devices)
            url = (rw_user.get("subscriptionUrl")) or await self._subscription_url_by_username(username)
        except Exception:
            logger.error(f"[manager] temp key creation failed: op={op.id}", exc_info=True)
            if rw_user and rw_user.get("uuid"):
                try:
                    await self._remnawave.delete_user(rw_user["uuid"])
                except Exception:
                    logger.error("[manager] could not roll back half-created temp user", exc_info=True)
            await self._ops.update(op.id, status="failed", error_code="panel", completed_at=_now())
            raise ManagerError("panel")

        temp = await self._temps.create(manager.id, op.id, username, rw_user.get("uuid"), expires_at)
        await self._ops.update(
            op.id, status="completed", completed_at=_now(), key_username=username,
            key_fingerprint=key_fingerprint(url or username), key_expires_at=expires_at,
            traffic_gb=gb, extra_devices=0,
        )
        text, image = await self._publish(op.id, subscription_url=url, with_image=True)
        return TempKeyResult(op.id, temp.id, url or "", expires_at, None, text, image)

    async def _subscription_url_by_username(self, username: str) -> str | None:
        try:
            rw_user = await self._remnawave.get_user_by_username(username)
        except Exception:
            return None
        return (rw_user or {}).get("subscriptionUrl") or None

    async def list_temp_keys(self, manager_id: int) -> list[dict]:
        """Живые временные ключи менеджера: id, остаток времени, отпечаток."""
        manager = await self.require_active(manager_id)
        rows = []
        for key in await self._temps.list_active_for_manager(manager.id):
            rows.append({"id": key.id, "expires_at": key.expires_at, "status": key.status,
                         "fingerprint": key_fingerprint(key.rw_username)})
        return rows

    async def remind_expiring_temp_keys(self, minutes: int = TEMP_REMIND_MINUTES) -> int:
        """
        За `minutes` до конца пробного ключа — напоминание менеджеру с кнопкой «оформить подписку
        на этот ключ». По каждому ключу один раз. Возвращает число отправленных напоминаний.
        """
        if self.notifier is None:
            return 0
        now = _now()
        sent = 0
        for key in await self._temps.list_expiring(now, now + datetime.timedelta(minutes=minutes)):
            manager = await self._managers.get(key.manager_id)
            if manager is None or manager.status != "active" or not manager.telegram_id:
                continue
            if not await self._temps.mark_reminded(key.id):
                continue
            left = max(1, round((key.expires_at - now).total_seconds() / 60))
            try:
                await self.notifier.notify_temp_expiring(
                    manager.telegram_id, key.id, minutes_left=left, until=fmt_time(key.expires_at),
                )
                sent += 1
            except Exception as e:
                logger.warning(f"[manager] cannot remind about temp key #{key.id}: {e}")
        return sent

    async def expire_temp_keys(self) -> tuple[int, int]:
        """
        Удаляет из панели истёкшие временные ключи. Возвращает (удалено, ошибок).

        404 от панели — ключ уже удалён: это успех. Сетевая ошибка — повтор на
        следующем прогоне; после TEMP_DELETE_MAX_ATTEMPTS неудач зовём админов,
        потому что ключ, который обязан был умереть, продолжает работать.
        """
        deleted = failed = 0
        now = _now()
        for key in await self._temps.list_due(now):
            try:
                if key.rw_uuid:
                    await self._remnawave.delete_user(key.rw_uuid)
            except RemnawaveAPIError as e:
                if e.status != 404:
                    failed += await self._temp_delete_failed(key, e)
                    continue
            except Exception as e:
                failed += await self._temp_delete_failed(key, e)
                continue
            if await self._temps.mark_deleted(key.id):
                deleted += 1
                await self._publish(key.operation_id, temp_deleted=True, notify_manager=True)

        # Конвертация, за которую так и не заплатили: ключ мёртв, привязку к клиенту снимаем.
        for key in await self._temps.list_stuck_converting(now - datetime.timedelta(minutes=CONVERT_GRACE_MINUTES)):
            try:
                if key.rw_uuid:
                    await self._remnawave.delete_user(key.rw_uuid)
            except RemnawaveAPIError as e:
                if e.status != 404:
                    failed += await self._temp_delete_failed(key, e)
                    continue
            except Exception as e:
                failed += await self._temp_delete_failed(key, e)
                continue
            await self._temps.mark_deleted(key.id)
            deleted += 1
        return deleted, failed

    async def _temp_delete_failed(self, key, error) -> int:
        attempts = await self._temps.bump_attempts(key.id)
        logger.warning(f"[manager] temp key #{key.id} delete failed ({attempts}): {error}")
        if attempts == TEMP_DELETE_MAX_ATTEMPTS:
            logger.error(
                f"[manager] ВРЕМЕННЫЙ КЛЮЧ #{key.id} ({key.rw_username}) не удаляется из панели "
                f"после {attempts} попыток — удалите вручную"
            )
        return 1

    # ==========================================================================
    # Чеки
    # ==========================================================================

    @staticmethod
    async def _render(data: ReceiptData, audience: str) -> bytes | None:
        """PNG чека. Картинка — украшение: не нарисовалась — чек уходит текстом."""
        try:
            return await asyncio.to_thread(render_receipt, data, audience)
        except Exception:
            logger.error(f"[manager] cannot render receipt image: op={data.op_id}", exc_info=True)
            return None

    async def _publish(self, op_id: int, *, subscription_url: str | None = None,
                       temp_deleted: bool = False, notify_manager: bool = False,
                       notify_client: bool = False, with_image: bool = False):
        """
        Собирает чек операции и отдаёт notifier'у: каждому адресату — картинка и текст
        одним сообщением. Возвращает текст чека менеджеру, а с with_image — (текст, PNG):
        его показывает хендлер, когда менеджер сам ждёт результат.
        """
        op = await self._ops.get(op_id)
        if op is None:
            return (None, None) if with_image else None
        manager = await self._managers.get(op.manager_id)
        data = await self._receipt_data(op, manager, subscription_url, temp_deleted)
        manager_text = format_manager_receipt(data)
        manager_image = await self._render(data, "manager") if (with_image or notify_manager) else None
        result = (manager_text, manager_image) if with_image else manager_text
        if self.notifier is None:
            return result

        try:
            ids = await self.notifier.post_group_receipt(
                op.id, format_group_receipt(data),
                (op.receipt_chat_id, op.receipt_message_id) if op.receipt_message_id else None,
                image=await self._render(data, "group"),
            )
            if ids and ids != (op.receipt_chat_id, op.receipt_message_id):
                await self._ops.update(op.id, receipt_chat_id=ids[0], receipt_message_id=ids[1])
            if notify_manager and manager and manager.telegram_id:
                await self.notifier.notify_manager(
                    manager.telegram_id, manager_text, image=manager_image,
                    has_key=bool(subscription_url) and data.status == "completed" and not temp_deleted,
                    client_code=op.client_code if op.op_type != "issue_temp" else None,
                )
            if notify_client and op.client_user_id and op.client_user_id > 0:
                await self.notifier.notify_client(
                    op.client_user_id, format_client_receipt(data), image=await self._render(data, "client"),
                )
        except Exception:
            # Чек — отчётность, а не условие выдачи: ключ уже выдан, деньги приняты.
            logger.error(f"[manager] receipt delivery failed: op={op.id}", exc_info=True)
        return result

    async def receipt_image(self, manager_id: int, operation_id: int) -> bytes | None:
        """PNG чека своей операции — для сайта (скачать, переслать клиенту). Чужая — None."""
        manager = await self.require_active(manager_id)
        op = await self._ops.get(operation_id)
        if op is None or op.manager_id != manager.id or op.op_type not in _SALE_TYPES:
            return None
        url = None
        if op.status == "completed" and op.client_user_id:
            client = await self._users.get(op.client_user_id)
            url = await self._subscription_url(client) if client else None
        elif op.status == "completed" and op.op_type == "issue_temp" and op.key_username:
            url = await self._subscription_url_by_username(op.key_username)
        temp_deleted = False
        if op.op_type == "issue_temp":
            key = await self._temps.get_by_operation(op.id)
            temp_deleted = bool(key and key.status == "deleted")
            if temp_deleted:
                url = None
        data = await self._receipt_data(op, manager, url, temp_deleted)
        return await self._render(data, "manager")

    async def client_receipt_image(self, user_id: int, operation_id: int) -> bytes | None:
        """PNG чека покупки у менеджера — самому клиенту (кабинет). Только его операция."""
        op = await self._ops.get(operation_id)
        if op is None or op.client_user_id != user_id or op.op_type not in _SALE_TYPES - {"issue_temp"}:
            return None
        if op.status not in ("completed", "refunded"):
            return None
        manager = await self._managers.get(op.manager_id)
        client = await self._users.get(user_id)
        url = await self._subscription_url(client) if client and op.status == "completed" else None
        data = await self._receipt_data(op, manager, url, False)
        return await self._render(data, "client")

    async def client_receipts(self, user_id: int, limit: int = 5) -> list[OperationBrief]:
        """Покупки клиента у менеджеров — список чеков для его кабинета."""
        return [
            _brief(op) for op in await self._ops.list_for_client_any(user_id, limit)
            if op.op_type in _SALE_TYPES and op.status in ("completed", "refunded")
        ]

    async def _receipt_data(self, op, manager, subscription_url, temp_deleted) -> ReceiptData:
        kind = {"issue_temp": KIND_TEMP, "issue_custom": KIND_CUSTOM}.get(op.op_type, KIND_TARIFF)
        details = op.price_details or {}
        autorenew = None
        outstanding = None
        if op.payment_method == "online" and op.client_user_id:
            autorenew = await self._payments.autorenew_enabled(op.client_user_id)
        if op.payment_method == "cash":
            outstanding, _ = await self._ops.cash_outstanding(op.manager_id)
        custom_note = None
        if kind == KIND_CUSTOM and details.get("formula_price") is not None:
            custom_note = f"{op.days} дн. по формуле → {details['formula_price']} ₽"

        dev = await self._devices.settings()
        if kind == KIND_TEMP:
            _, _, devices = await self.temp_settings()
            devices_limit = devices
        else:
            devices_limit = dev.base_limit + (op.extra_devices or 0)
        label = None
        if op.client_user_id:
            link = await self._clients.get_active(op.manager_id, op.client_user_id)
            label = link.label if link else None
        return ReceiptData(
            op_id=op.id, created_at=op.created_at, manager_id=op.manager_id, client_label=label,
            manager_name=manager.display_name if manager else "—", kind=kind, status=op.status,
            client_code=op.client_code, client_is_new=bool(details.get("new_client")),
            product_title=op.tariff_name or "", days=op.days, custom_note=custom_note,
            traffic_gb=op.traffic_gb, devices_limit=devices_limit,
            expires_at=op.key_expires_at, price=op.price or 0.0, service_fee=op.service_fee or 0.0,
            fee_in_cash=bool(op.fee_in_cash), payment_method=op.payment_method,
            autorenew=autorenew, cash_outstanding=outstanding, key_username=op.key_username,
            key_fingerprint=op.key_fingerprint,
            temp_deleted_at=_now() if temp_deleted else None,
            subscription_url=subscription_url,
        )

    # ==========================================================================
    # История и статистика
    # ==========================================================================

    async def history(self, manager_id: int, page: int = 0, size: int = 10) -> list[OperationBrief]:
        manager = await self.require_active(manager_id)
        return [_brief(op) for op in await self._ops.list_for_manager(manager.id, size, page * size)]

    async def stats(self, manager_id: int) -> ManagerStats:
        manager = await self.require_active(manager_id)
        return await self._stats_for(manager.id)

    async def _stats_for(self, manager_id: int | None) -> ManagerStats:
        now = _now()
        start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = {
            "today": start_day,
            "week": start_day - datetime.timedelta(days=start_day.weekday()),
            "month": start_day.replace(day=1),
        }
        built = {}
        for name, since in periods.items():
            by_type: dict = {}
            by_method: dict = {}
            count = 0
            revenue = 0.0
            fees = 0.0
            for op_type, method, cnt, rev, fee in await self._ops.stats(manager_id, since):
                if op_type in _SALE_TYPES:
                    count += cnt
                    revenue += rev
                    fees += fee
                    by_type[op_type] = by_type.get(op_type, 0) + cnt
                    by_method[method or "free"] = by_method.get(method or "free", 0.0) + rev
            built[name] = PeriodStats(count, revenue, fees, by_type, by_method)

        cash, cash_ops = await self._ops.cash_outstanding(manager_id) if manager_id else (0.0, 0)
        fees_due, fee_ops = await self._ops.fees_outstanding(manager_id) if manager_id else (0.0, 0)
        conv = await self._temps.conversion_stats(manager_id, periods["month"])
        clients = await self._clients.count_for_manager(manager_id) if manager_id else 0
        return ManagerStats(built["today"], built["week"], built["month"], cash, cash_ops, clients,
                            conv["total"], conv["converted"], fees_due, fee_ops)

    async def global_stats(self, manager_id: int) -> ManagerStats:
        """Сводка по всем менеджерам — только с правом can_view_global_stats."""
        manager = await self.require_active(manager_id)
        self._require_right(manager, "can_view_global_stats")
        return await self._stats_for(None)

    # --- админ ---

    async def admin_stats(self, manager_id: int) -> ManagerStats:
        return await self._stats_for(manager_id)

    async def admin_history(self, manager_id: int | None = None, page: int = 0, size: int = 15):
        """Полный журнал для админа: ORM-записи как есть."""
        return await self._ops.list_all(size, page * size, manager_id)

    async def settle(self, manager_id: int, admin_id: int):
        """Инкассация: админ принял у менеджера всю несданную наличную выручку."""
        settlement = await self._ops.settle(manager_id, admin_id)
        if settlement:
            logger.info(
                f"[manager] admin {admin_id} settled manager #{manager_id}: "
                f"{settlement.amount} ₽, {settlement.operations_count} операций"
            )
        return settlement

    async def cash_outstanding(self, manager_id: int) -> tuple[float, int]:
        return await self._ops.cash_outstanding(manager_id)

    async def fees_outstanding(self, manager_id: int) -> tuple[float, int]:
        """Услуги менеджера из онлайн-продаж, ещё не выплаченные ему: (сумма, операций)."""
        return await self._ops.fees_outstanding(manager_id)

    async def payout_fees(self, manager_id: int, admin_id: int):
        """Админ выплатил менеджеру его услуги из онлайн-продаж."""
        payout = await self._ops.payout_fees(manager_id, admin_id)
        if payout:
            logger.info(
                f"[manager] admin {admin_id} paid out manager #{manager_id}: "
                f"{payout.amount} ₽, {payout.operations_count} операций"
            )
            manager = await self._managers.get(manager_id)
            await self._notify_manager(
                manager, f"💸 Администратор выплатил вам {fmt_money(payout.amount)} "
                         f"за услуги в {payout.operations_count} онлайн-продажах."
            )
        return payout
