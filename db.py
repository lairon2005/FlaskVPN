# db.py (ФИНАЛЬНАЯ ВЕРСИЯ НА SQLAlchemy)

import datetime
from sqlalchemy import (
    create_engine, BigInteger, String, DateTime, Boolean, ForeignKey,
    Integer, Float, select, func, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import load_config

# --- 1. Настройка ---
config = load_config()
db_config = config.dataBase
DSN = f"postgresql+asyncpg://{db_config.user}:{db_config.password}@{db_config.host}:{db_config.port}/{db_config.db_name}"
SYNC_DSN = f"postgresql://{db_config.user}:{db_config.password}@{db_config.host}:{db_config.port}/{db_config.db_name}"

# Создаем асинхронный "движок" и фабрику сессий
async_engine = create_async_engine(DSN)
async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

# --- 2. Базовая модель ---
class Base(DeclarativeBase):
    pass

# --- 3. Определяем все ваши модели на новом синтаксисе ---
class User(Base):
    __tablename__ = 'users'
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    full_name: Mapped[str] = mapped_column(String)
    reg_date: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    subscription_end_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    vpn_username: Mapped[str] = mapped_column(String, unique=True, nullable=True)
    remnawave_uuid: Mapped[str] = mapped_column(String(36), unique=True, nullable=True)
    
    # --- НОВОЕ ПОЛЕ ДЛЯ ОТСЛЕЖИВАНИЯ ТРИАЛА ---
    has_received_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='SET NULL'), nullable=True)
    referral_bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    is_first_payment_made: Mapped[bool] = mapped_column(Boolean, default=False)
    support_topic_id: Mapped[int] = mapped_column(Integer, nullable=True)
    
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    
    reset_code: Mapped[str] = mapped_column(String(10), nullable=True)
    reset_code_expire: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    reset_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')

    verification_code: Mapped[str] = mapped_column(String(10), nullable=True)
    verification_code_expire: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')

    # Активен ли аккаунт для рассылок. False = пользователь заблокировал бота
    # (ставится при TelegramForbiddenError в джобах рассылок), такие юзеры
    # пропускаются, чтобы не долбить Telegram и не засорять логи. Возврат в
    # активные — в get_or_create при следующем обращении пользователя к боту.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true', nullable=False)

    # Сколько дополнительных устройств (сверх базового лимита тарифа) оплачено.
    # Слоты живут по дате подписки: действуют до subscription_end_date и
    # обнуляются джобом, когда подписка истекла (см. sync_device_limits в
    # tgbot/services/scheduler.py). В панель уходит абсолютное значение
    # hwidDeviceLimit = базовый лимит + extra_devices — иначе персональный
    # лимит навсегда отвязывается от глобального fallbackDeviceLimit.
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)

    # Вводный тариф (1 ₽ за неделю) уже оплачен — повторно не продаётся.
    # «В intro-периоде» = intro_used AND NOT is_first_payment_made: 1 ₽ первой
    # оплатой не считается, флаг первой оплаты ставит только списание полной
    # цены при переходе на тариф продления (см. tgbot/services/intro_offer.py).
    intro_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)

    # Квота трафика текущей подписки, ГБ/мес (0 = безлимит, NULL = ещё не
    # записана — тогда берём из панели). И докупленные сверху ГБ: они живут
    # до subscription_end_date, как слоты устройств, и сгорают джобом
    # sync_traffic_limits. В панель пишется сумма: квота + extra_traffic_gb.
    traffic_quota_gb: Mapped[int] = mapped_column(Integer, nullable=True)
    extra_traffic_gb: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)

    # Откуда клиент: 'bot' — Telegram, 'web' — кабинет с email, 'offline' — создан
    # менеджером при офлайн-продаже (без Telegram и email, user_id отрицательный).
    origin: Mapped[str] = mapped_column(String(8), default='bot', server_default='bot', nullable=False)
    # Короткий код клиента (6 символов base32 без 0/O/1/I): менеджеру вместо
    # Telegram ID, клиенту — чтобы назвать его при обращении. Только у офлайн-клиентов
    # и у тех, кто сам запросил «код для менеджера».
    client_code: Mapped[str] = mapped_column(String(8), unique=True, nullable=True)
    # Хэш токена личного кабинета офлайн-клиента (ссылка из чека /c/<token>).
    # Сам токен нигде не хранится — только sha256.
    cabinet_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=True)
    acquired_by_manager_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('managers.id', ondelete='SET NULL'), nullable=True
    )
    # Пришёл по ссылке пользователя, который В ТОТ МОМЕНТ был активным партнёром
    # (денежная рефералка, tgbot/services/partner_service.py). Только с оплат таких
    # друзей партнёру капают проценты: приглашённые до включения партнёрки не считаются.
    partner_referred: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)

class Tariff(Base):
    __tablename__ = 'tariffs'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Float)
    duration_days: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Тарифы 2.0: квота трафика, лоялти-цена, подсветка ---
    data_limit_gb: Mapped[int] = mapped_column(Integer, nullable=True)      # NULL или 0 → безлимит, сброс раз в месяц
    loyalty_price: Mapped[float] = mapped_column(Float, nullable=True)     # "цена навсегда" при непрерывном продлении
    is_highlighted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # метка "⭐ выбор большинства"

    # Цена в Telegram Stars (XTR), docs/tma-roadmap.md фаза 3.2. NULL → оплата
    # Stars для этого тарифа недоступна (кнопка "⭐" не показывается в tariffs.html).
    price_stars: Mapped[int] = mapped_column(Integer, nullable=True)

    # Вводный тариф: продаётся один раз тем, кто ещё не платил, только с
    # сохранением карты; по истечении срока карта автоматически списывается
    # за renew_tariff_id. Без renew_tariff_id пользователям не показывается.
    is_intro: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    renew_tariff_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('tariffs.id', ondelete='SET NULL'), nullable=True
    )

class PromoCode(Base):
    __tablename__ = 'promo_codes'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    expire_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses_left: Mapped[int] = mapped_column(Integer, default=1)

class UsedPromoCode(Base):
    __tablename__ = 'used_promo_codes'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id'))
    promo_code_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('promo_codes.id'))
    used_date: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)

    # Бэкстоп на уровне БД против TOCTOU-гонки validate()/use(): без него два
    # конкурентных апдейта могут оба пройти проверку has_user_used() до того, как
    # первый закоммитит свою строку, и погасить один и тот же промокод дважды
    # (см. security review 2026-07-26). PromoCodeRepository.try_claim() полагается
    # на этот индекс через INSERT ... ON CONFLICT DO NOTHING.
    __table_args__ = (
        UniqueConstraint('user_id', 'promo_code_id', name='uq_used_promo_user_code'),
    )

class Payment(Base):
    __tablename__ = 'payments'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    yookassa_payment_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id'))
    tariff_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('tariffs.id'), nullable=True)
    original_amount: Mapped[float] = mapped_column(Float)
    final_amount: Mapped[float] = mapped_column(Float)
    promo_code: Mapped[str] = mapped_column(String, nullable=True)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default='pending')  # pending / succeeded / failed / refunded / cancelled
    source: Mapped[str] = mapped_column(String, default='bot')  # bot / web / tma / auto / stars / manager (QR) / cash

    # 'subscription' — покупка/продление тарифа (в т.ч. вместе со слотами устройств
    # и пакетами трафика), 'custom' — «свои дни» офлайн-продажи (tariff_id = NULL,
    # срок в days), 'devices' / 'traffic' — докупка слотов или ГБ в середине
    # оплаченного периода (tariff_id = NULL, подписка не продлевается).
    # Вебхуку нужно различать: в этих случаях трогать expireAt нельзя.
    kind: Mapped[str] = mapped_column(String(16), default='subscription', server_default='subscription', nullable=False)

    # Сколько слотов устройств оплачено этим платежом. Для kind='subscription'
    # это подтверждение уже имеющихся слотов на новый срок, для kind='devices' —
    # сколько слотов добавить к User.extra_devices.
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)

    # Сколько ГБ/мес докупленного трафика оплачено этим платежом. Для
    # kind='subscription' — абсолютное значение на новый срок (как extra_devices),
    # для kind='traffic' — сколько ГБ добавить к User.extra_traffic_gb.
    extra_traffic_gb: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)

    # Офлайн-продажа: кто из менеджеров провёл платёж (NULL — обычная оплата) и,
    # для kind='custom' («свои дни»), на сколько дней — у такого платежа нет тарифа.
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('managers.id', ondelete='SET NULL'), nullable=True)
    days: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    # Telegram Stars (XTR), docs/tma-roadmap.md фаза 3.2. Для source='stars' —
    # "сырой" telegram_payment_charge_id из Message.successful_payment, нужен
    # verbatim для рефандов через Bot API refundStarPayment(user_id, charge_id).
    # NULL для всех остальных источников (YooKassa-платежей).
    telegram_payment_charge_id: Mapped[str] = mapped_column(String, unique=True, nullable=True)


class UserPaymentMethod(Base):
    """Сохранённый метод оплаты YooKassa для автопродления (одна карта на пользователя)."""
    __tablename__ = 'user_payment_methods'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'), unique=True)
    yookassa_payment_method_id: Mapped[str] = mapped_column(String, unique=True)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=True)   # NULL для СБП
    card_type: Mapped[str] = mapped_column(String, nullable=True)      # 'MasterCard'/'Visa'/'МИР'/'SBP'
    auto_renew_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    renew_tariff_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('tariffs.id'), nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    # Время последней попытки автосписания. Переход с вводного тарифа идёт
    # почасовым джобом — без этой отметки отказ карты повторялся бы каждый час.
    last_attempt_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)


class LifecycleMessage(Base):
    """
    Единый трекер «касаний» для всех lifecycle-серий (напоминания о продлении,
    win-back для ушедших плативших, дрип активации неплативших).

    UNIQUE(user_id, series, step) не даёт отправить одно и то же касание дважды.
    `step` для повторяющихся событий (например, продление подписки каждый месяц)
    кодируется вместе с "циклом" (датой-якорем), чтобы серия могла повториться
    заново в следующем цикле — см. `_cycle_key()` в tgbot/services/scheduler.py.
    Промо-касания (несущие скидку/подарок, из-за которых есть риск бана бота при
    слишком частой рассылке) дополнительно помечаются префиксом "promo:" в `step`.
    """
    __tablename__ = 'lifecycle_messages'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    series: Mapped[str] = mapped_column(String(32))    # 'renewal' | 'winback' | 'activation'
    step: Mapped[str] = mapped_column(String(64))       # напр. 'd-3_20260716', 'promo:wave2_20260701'
    sent_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)

    __table_args__ = (
        UniqueConstraint('user_id', 'series', 'step', name='uq_lifecycle_user_series_step'),
    )


class Channel(Base):
    __tablename__ = 'channels'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String)
    invite_link: Mapped[str] = mapped_column(String)


class AppSetting(Base):
    """
    Изменяемые из админки настройки сервиса (key → value строкой).

    Зачем не .env: цену доп. устройства и лимиты админ меняет заметно чаще, чем
    выкатывается релиз, а правка .env требует refresh.sh и пересоздания
    контейнеров. Значения читаются через SettingsRepository с кэшем в памяти
    (ключей единицы, запрос на каждый показ тарифов не нужен).

    Известные ключи — в tgbot/services/device_pricing.py.
    """
    __tablename__ = 'app_settings'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now
    )


# =============================================================================
# --- Менеджеры (офлайн-продажи) ---
# =============================================================================

class Manager(Base):
    """
    Менеджер — человек, который офлайн продаёт и устанавливает VPN.

    Роль живёт отдельно от User: менеджер входит в бота и на сайт под своим
    Telegram ID, но видит только свою панель. Удаление мягкое (status='deleted'):
    журнал и чеки не должны пропадать вместе с человеком.
    """
    __tablename__ = 'managers'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # NULL, пока приглашение не принято
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(64))
    # invited | active | blocked | deleted
    status: Mapped[str] = mapped_column(String(16), default='invited', server_default='invited', nullable=False)

    can_issue_tariff: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true', nullable=False)
    can_issue_custom: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true', nullable=False)
    can_issue_temp: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true', nullable=False)
    can_accept_cash: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    can_view_global_stats: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    temp_keys_per_day: Mapped[int] = mapped_column(Integer, default=5, server_default='5', nullable=False)
    # Потолок «к сдаче» по наличным, ₽. NULL — без ограничения.
    cash_limit: Mapped[int] = mapped_column(Integer, default=5000, server_default='5000', nullable=True)
    # Цена услуги менеджера (подключение и настройка), ₽: входит в каждую его продажу
    # отдельной позицией. Задаёт сам менеджер; если админ зафиксировал — менять нельзя.
    service_fee: Mapped[int] = mapped_column(Integer, default=250, server_default='250', nullable=False)
    service_fee_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)

    # Растёт при блокировке/смене прав — веб-сессии с прежним значением становятся недействительными.
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1', nullable=False)
    # В БД только sha256 токенов — сами токены существуют лишь в ссылке.
    invite_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=True)
    invite_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    # Одноразовая ссылка из бота «задать пароль» (после приглашения, по кнопке, после сброса админом).
    login_token_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    login_token_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    # Вход на сайт: логин задаёт админ, пароль — сам менеджер (по ссылке из бота). Хэш argon2.
    login: Mapped[str] = mapped_column(String(32), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    password_changed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    # Подбор пароля: после MAX_FAILED_LOGINS неверных попыток вход закрыт до locked_until.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)
    locked_until: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=True)


class ManagerClient(Base):
    """
    Рабочий доступ менеджера к клиенту: без записи здесь менеджер клиента не видит.

    Доступ выдаётся при создании клиента, конвертации временного ключа или по
    одноразовому коду клиента, и ограничен по сроку (access_until).
    """
    __tablename__ = 'manager_clients'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('managers.id', ondelete='CASCADE'))
    client_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    # Заметка менеджера о клиенте — видна ему и админу.
    label: Mapped[str] = mapped_column(String(64), nullable=True)
    granted_via: Mapped[str] = mapped_column(String(16))  # created | code | temp_convert
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    access_until: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('manager_id', 'client_user_id', name='uq_manager_client'),
    )


class ManagerOperation(Base):
    """
    Журнал действий менеджера. Только INSERT и смена статуса — записи не удаляются.
    Номер чека — id (формат M-000123).
    """
    __tablename__ = 'manager_operations'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('managers.id'), index=True)
    # issue_tariff | issue_custom | issue_temp | convert_temp | access_grant | key_view
    # | autorenew_off | cabinet_link_reset
    op_type: Mapped[str] = mapped_column(String(24))
    # processing | pending_payment | completed | failed | cancelled | refunded
    status: Mapped[str] = mapped_column(String(16), default='processing', server_default='processing')
    client_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True, index=True)
    client_code: Mapped[str] = mapped_column(String(8), nullable=True)   # снимок на момент операции
    tariff_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    tariff_name: Mapped[str] = mapped_column(String(128), nullable=True)  # снимок
    days: Mapped[int] = mapped_column(Integer, nullable=True)
    traffic_gb: Mapped[int] = mapped_column(Integer, nullable=True)       # лимит трафика на момент выдачи
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    price: Mapped[float] = mapped_column(Float, default=0, server_default='0')
    # Сколько из price — услуга менеджера (снимок на момент продажи). Наличные: остаётся
    # у менеджера, в «к сдаче» не входит. Онлайн: клиент отдаёт её менеджеру наличными
    # (fee_in_cash) — в счёт ЮKassa она не входит. Старые онлайн-продажи (fee_in_cash=false)
    # брали услугу через ЮKassa — она к выплате менеджеру.
    service_fee: Mapped[float] = mapped_column(Float, default=0, server_default='0', nullable=False)
    fee_in_cash: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    fee_paid_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    fee_payout_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price_details: Mapped[dict] = mapped_column(JSONB, nullable=True)     # из чего сложилась цена
    payment_method: Mapped[str] = mapped_column(String(8), nullable=True)  # cash | online | free
    payment_id: Mapped[str] = mapped_column(String, nullable=True)        # yookassa_payment_id
    key_username: Mapped[str] = mapped_column(String(64), nullable=True)
    key_fingerprint: Mapped[str] = mapped_column(String(16), nullable=True)
    key_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    settled_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    settlement_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=True)
    error_code: Mapped[str] = mapped_column(String(32), nullable=True)
    receipt_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    receipt_message_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    # Сообщение со счётом (QR оплаты) в боте у менеджера: после оплаты/отмены правится само.
    invoice_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    invoice_message_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now, index=True)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)


class TempKey(Base):
    """Временный ключ менеджера: живёт ограниченное время, затем удаляется из панели."""
    __tablename__ = 'temp_keys'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('managers.id'), index=True)
    operation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('manager_operations.id'))
    client_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    rw_username: Mapped[str] = mapped_column(String(64), unique=True)
    rw_uuid: Mapped[str] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    # active | converting | converted | deleted
    status: Mapped[str] = mapped_column(String(12), default='active', server_default='active')
    deleted_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    delete_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    # Когда менеджеру напомнили «ключ скоро закончится — предложите подписку» (один раз).
    reminded_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)


class ManagerSettlement(Base):
    """Инкассация: админ принял у менеджера выручку наличными."""
    __tablename__ = 'manager_settlements'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('managers.id'), index=True)
    amount: Mapped[float] = mapped_column(Float)
    operations_count: Mapped[int] = mapped_column(Integer)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)


class ManagerPayout(Base):
    """Выплата менеджеру его услуг из онлайн-продаж (деньги пришли магазину через ЮKassa)."""
    __tablename__ = 'manager_payouts'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('managers.id'), index=True)
    amount: Mapped[float] = mapped_column(Float)
    operations_count: Mapped[int] = mapped_column(Integer)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)


class ClientAccessCode(Base):
    """
    Одноразовый код, которым клиент даёт менеджеру доступ к своей подписке.

    Хранится только sha256 кода; неверные вводы менеджера считаются отдельно
    (строки с code_hash = NULL и used_by_manager_id — журнал попыток) для лимита перебора.
    """
    __tablename__ = 'client_access_codes'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'), nullable=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    used_by_manager_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)


class Partner(Base):
    """
    Партнёр — пользователь с денежной рефералкой вместо дней: с каждой рублёвой оплаты
    приглашённого друга ему начисляется процент на баланс. Назначает только админ.

    Деньги — в копейках (целые), чтобы проценты от любой суммы не копили ошибку округления.
    balance_kop — доступно к выводу/оплате; начисления в холде лежат в partner_ledger.
    """
    __tablename__ = 'partners'
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'), primary_key=True)
    # active — начисления идут; disabled — новых начислений нет, баланс можно вывести/потратить.
    status: Mapped[str] = mapped_column(String(12), default='active', server_default='active', nullable=False)
    # Персональная ставка, %. NULL — общая из app_settings (partner_percent).
    percent: Mapped[int] = mapped_column(Integer, nullable=True)
    balance_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0', nullable=False)
    # Реквизиты для вывода по СБП: вводятся один раз, меняются из меню партнёра.
    sbp_phone: Mapped[str] = mapped_column(String(20), nullable=True)
    sbp_bank: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=True)
    disabled_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)


class PartnerLedger(Base):
    """
    Журнал движения денег партнёра. Записи не удаляются, меняется только статус начислений.

    kind / amount_kop (знак — влияние на баланс):
      accrual            +  процент с оплаты друга; status hold → available (или cancelled при возврате в холде)
      reversal           −  возврат оплаты друга после холда (не больше, чем есть на балансе)
      withdrawal         −  заявка на вывод (деньги заморожены до решения админа)
      withdrawal_return  +  заявка отклонена
      spend              −  оплата своей подписки с баланса
      spend_return       +  оплата с баланса не прошла
    UNIQUE(kind, payment_id) — идемпотентность: одна оплата не начисляется и не списывается дважды.
    """
    __tablename__ = 'partner_ledger'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('partners.user_id', ondelete='CASCADE'), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    amount_kop: Mapped[int] = mapped_column(BigInteger)
    # accrual: hold | available | cancelled | reversed; остальные — done
    status: Mapped[str] = mapped_column(String(12), default='done', server_default='done', nullable=False)
    friend_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    payment_id: Mapped[str] = mapped_column(String, nullable=True)        # yookassa_payment_id
    base_amount_kop: Mapped[int] = mapped_column(BigInteger, nullable=True)  # от какой суммы считали процент
    percent: Mapped[int] = mapped_column(Integer, nullable=True)
    withdrawal_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    available_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)

    __table_args__ = (
        UniqueConstraint('kind', 'payment_id', name='uq_partner_ledger_kind_payment'),
    )


class PartnerWithdrawal(Base):
    """Заявка партнёра на вывод по СБП. Сумма списана с баланса при создании, при отказе возвращается."""
    __tablename__ = 'partner_withdrawals'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    partner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('partners.user_id', ondelete='CASCADE'), index=True
    )
    amount_kop: Mapped[int] = mapped_column(BigInteger)
    sbp_phone: Mapped[str] = mapped_column(String(20))   # снимок реквизитов на момент заявки
    sbp_bank: Mapped[str] = mapped_column(String(64))
    # pending | paid | rejected
    status: Mapped[str] = mapped_column(String(12), default='pending', server_default='pending', nullable=False)
    reject_reason: Mapped[str] = mapped_column(String(128), nullable=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    # Сообщение с заявкой в топике выплат — правится после решения.
    notify_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    notify_message_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    processed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)


# --- 4. Функция для создания таблиц ---
def setup_database_sync():
    """Создает таблицы синхронно при старте бота."""
    engine = create_engine(SYNC_DSN)
    Base.metadata.create_all(engine)
    print("INFO: Database tables created or already exist via SQLAlchemy.")