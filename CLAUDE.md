# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

ОБЩАЙСЯ СО МНОЙ НА РУССКОМ — Speak Russian!

## Документация по модулям

| Файл | Что внутри |
|---|---|
| `tgbot/CLAUDE.md` | Хендлеры, сервисы, клавиатуры, FSM-состояния бота |
| `webapp/CLAUDE.md` | Web-dashboard: все маршруты, JWT, шаблоны, CSS/JS-паттерны, деплой |
| `docs/*.md` | Эксплуатация нод Remnawave (setup, cover-сайт, XHTTP/Hysteria2, анти-флуд, proxy-failover), `tma-roadmap.md` — план Mini App, `brand.md` — бренд-бук FLASK (палитра, шрифты, где что лежит) |

## Project Overview

**FlaskVPN** — Telegram VPN subscription bot with web dashboard and payment processing. Integrates:
- **Telegram Bot** (aiogram 3.x) — регистрация, подписки, оплата, поддержка
- **Telegram Mini App** — те же экраны внутри Telegram (`UI_MODE=tma`, роутер `webapp/routers/tma.py`)
- **Web Dashboard** (FastAPI + Jinja2) — кабинет с email-авторизацией
- **Remnawave** — внешняя панель VPN (X-Ray), управляется через `remnawave/client.py`
- **YooKassa** + **Telegram Stars** — приём платежей
- **PostgreSQL 16** — пользователи, тарифы, промокоды, платежи, настройки

## Commands

```bash
# Разовая настройка
cp env.dist .env    # ВНИМАНИЕ: env.dist неполон, см. раздел Environment Variables
openssl req -x509 -newkey rsa:2048 -nodes -keyout privkey.pem -out fullchain.pem -days 365 -subj "/CN=localhost"

# Прод: сборка образа flask_bot + пересоздание бота и сайта + reload nginx
./refresh.sh        # PostgreSQL и контейнер nginx намеренно не пересоздаются

# Dev: бот в polling (в .env USE_WEBHOOK=False)
python3 -m bot

# Dev: только веб-кабинет
uvicorn webapp.main:app --reload

# Миграции схемы (идемпотентные ALTER ... IF NOT EXISTS)
python3 fix_db.py

# Копия прод-базы в локальный Docker
./db_pull.sh root@HOST [/путь/к/проекту]

# Логи
docker logs flask_bot ; docker logs flask_site
```

### Тесты

```bash
python3 -m pytest tests/ -q                                   # весь набор (~250 тестов, ~3 с)
python3 -m pytest tests/test_device_pricing.py -q              # один файл
python3 -m pytest tests/test_device_pricing.py -k prorated -q  # по имени
python3 -m unittest tests.test_device_pricing -v                # то же через unittest
```

Конвенции тестов (важно понять до написания нового):
- Тесты написаны на **stdlib `unittest`** (`pytest` только как раннер), pytest-фикстур и `conftest.py` нет.
- Тестируемый модуль грузится **через `importlib` по пути файла**, а зависимости подменяются
  `patch.dict(sys.modules, ...)`. Причина: обычный `import tgbot.services.X` тянет
  `tgbot/services/__init__.py` → `database/__init__.py` и `loader.py` → `config.load_config()`,
  которому нужен заполненный `.env` (`BOT_TOKEN`, `REMNAWAVE_API_URL`, …). Поэтому тесты идут
  без базы, без сети и без `.env`. Образцы: `tests/test_device_pricing.py` (чистые функции),
  `tests/test_subscription_service.py` и `tests/test_stale_payment_cancel.py` (сервис со стабами).
- Линтера и CI-воркфлоу в репозитории нет (`.github/` содержит только `dependabot.yml`).

## Environment Variables (`.env`)

**`env.dist` не полон и местами расходится с `config.py`** — при заведении окружения сверяйся с `config.py`:

| Переменная | Статус | Описание |
|---|---|---|
| `ADMINS` | **обязательна**, в `env.dist` ошибочно названа `ADMIN` | Telegram ID админов через запятую (`config.py`: `env.list("ADMINS")`) |
| `SUPPORT_CHAT_ID` | **обязательна, отсутствует в `env.dist`** | ID чата поддержки (супергруппа с топиками) |
| `TRANSACTION_LOG_TOPIC_ID` | **обязательна, отсутствует в `env.dist`** | Топик для лога транзакций |
| `BOT_TOKEN` | обязательна | Токен от @BotFather |
| `USE_WEBHOOK` / `DOMAIN` / `SERVER_URL` | обязательны | `False` = polling (dev); webhook требует непустой `DOMAIN` + SSL |
| `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` | обязательны | Магазин ЮKassa |
| `YOOKASSA_SAVE_PAYMENT_METHOD` | опц., по умолч. `False` | Включать только после одобрения рекуррентов, иначе `Payment.create` падает |
| `DB_NAME/USER/PASSWORD/HOST/PORT` | обязательны | В Docker `DB_HOST=postgres` |
| `REMNAWAVE_API_URL` / `REMNAWAVE_API_TOKEN` | обязательны | Адрес и токен панели |
| `REMNAWAVE_DEFAULT_SQUAD_UUID` | опц. | Сквад для новых пользователей |
| `REMNAWAVE_ACCESS_COOKIE` | опц. | Секретная часть ссылки для eGames reverse proxy (`name=value`) |
| `REMNAWAVE_PROXY_URL` | опц. | Резервный HTTP/SOCKS5-маршрут: сначала прямой запрос, при таймауте — повтор через прокси |
| `REMNAWAVE_SUB_HOST` | опц. | Хост страницы подписки (nginx `/sub/` → 302) |
| `SECRET_KEY` | нужна webapp | Подпись JWT |
| `TG_PROXY_URL` | опц., нет в `env.dist` | Прокси для самого Bot API |
| `UI_MODE` | опц., `bot` \| `tma` | Глобальный режим интерфейса; на `DOMAIN=localhost` web_app-кнопки автоматически отключаются |
| `TG_BOT_USERNAME` / `TMA_APP_NAME` | опц. | Deep-link `t.me/<bot>/<app>` — рефералка и `return_url` ЮKassa |
| `MAIL_*` | нужны webapp | SMTP для верификации email |
| `CERT_FULLCHAIN_PATH` / `CERT_KEY_PATH` | обязательны для Docker | Абсолютные пути к сертификатам (монтируются в nginx) |

## Architecture

```
nginx (host: 80 → HTTP, 8443 → HTTPS, 127.0.0.1:8080/9443 → cover-сайт Xray-ноды)
  $DOMAIN, *.$DOMAIN
  ├── /yookassa  → flask_bot (8081)  — вебхуки YooKassa
  ├── /tma       → flask_site (8000) — экраны Mini App
  ├── /sub/*     → 302               — легаси-ссылки на REMNAWAVE_SUB_HOST
  ├── /import    → статика           — страница-редирект для старых deeplink
  └── /          → flask_site (8000) — веб-кабинет
  app.$DOMAIN
  └── /          → flask_site (8000), корень редиректит на /tma/
```

⚠️ `SERVER_URL` (он же `LOCATION` в compose) в шаблон nginx передаётся, но **нигде в нём не
используется**: отдельного location под путь Telegram-вебхука нет, запрос ушёл бы в `flask_site`.
При переводе на `USE_WEBHOOK=True` этот location нужно добавить самому
(`etc/nginx/templates/default.conf.template`, по образцу `/yookassa` → `flask_bot:8081`).

Remnawave — **внешняя** панель (`REMNAWAVE_API_URL`), в compose её нет. Marzban полностью
выпилен (контейнер, сервис, маршруты nginx). Клиент панели раздаётся хендлерам через
workflow data под именем `remnawave` (`bot.py`: `Dispatcher(..., remnawave=remnawave_client)`,
в aiohttp-приложении вебхуков — `app['remnawave']`), поэтому параметр хендлера обязан
называться именно так. Легаси-имя `marzban` осталось только там, где это факт истории:
колонка `marzban_username` в миграции `fix_db.py` и проверка в `tests/test_nginx_legacy_proxy.py`.

### Слои и точки входа

`Handler → Service → Repository → DB`. Миграция на этот слоёный вид **завершена**:
в `tgbot/handlers/` нет прямых запросов SQLAlchemy, только вызовы сервисов и репозиториев.

- **Репозитории** — `database/repositories/*.py`; синглтоны собираются в `database/__init__.py`
  (`user_repo`, `tariff_repo`, `promo_repo`, `channel_repo`, `stats_repo`, `payment_repo`,
  `payment_method_repo`, `lifecycle_repo`, `settings_repo`). Файла `database/requests.py` больше нет.
- **Сервисы** — классы в `tgbot/services/*.py`; синглтоны с уже проброшенными зависимостями
  собираются в `tgbot/services/__init__.py` (`subscription_service`, `payment_service`,
  `device_slot_service`, …). Импортируй готовый синглтон, а не создавай экземпляр в хендлере.
- **Модули-утилиты без класса**: `services/payment.py` (обёртка над SDK ЮKassa, сборка чека),
  `services/pricing.py`, `services/device_pricing.py` (чистые функции), `services/subscription.py`
  (проверка подписки на каналы), `services/utils.py`.
- `loader.py` при импорте **выполняет** `load_config()`, поднимает логгер (с редактированием
  секретов и отправкой ERROR админу) и создаёт `bot` + `remnawave_client`. Любой импорт,
  который тянет `loader`, требует валидный `.env` — отсюда приёмы в тестах.
- `webapp/routes.py` — мёртвая копия старого `auth.py`, ниоткуда не импортируется.

### Data flow

1. Telegram update → `bot.py` (polling или aiohttp-вебхук) → `tgbot/handlers/` → сервис → репозиторий → PostgreSQL.
2. Оплата: хендлер/веб создаёт платёж в ЮKassa → ЮKassa шлёт webhook на `POST /yookassa` (порт 8081) →
   `tgbot/handlers/webhook_handlers.py` → `payment_service.process_successful_payment()` →
   продление подписки/начисление слотов в БД **и** PATCH в Remnawave.
3. Все вызовы панели идут через `remnawave/client.py` (`httpx`, типизированные ошибки
   `RemnawaveAPIError` / `RemnawaveTransportError` / `RemnawaveInvalidResponseError`, failover на прокси).
4. Сквады/инбаунды определяются динамически через API — хардкода протоколов нет.

### Key files

| File | Role |
|---|---|
| `bot.py` | Точка входа: polling vs webhook, старт APScheduler, регистрация команд и middlewares |
| `loader.py` | Синглтоны: `config`, `logger`, `bot`, `remnawave_client` |
| `config.py` | Типизированные dataclass-конфиги из `.env` |
| `db.py` | Модели SQLAlchemy + `setup_database_sync()` (создание таблиц на старте) |
| `fix_db.py` | Миграции: идемпотентные `ALTER TABLE ... IF NOT EXISTS` |
| `database/repositories/` | Весь async CRUD |
| `remnawave/client.py` | `RemnawaveClient`: пользователи, устройства (hwid), сквады, ноды, статистика |
| `tgbot/handlers/` | Роутеры: `user/`, `admin/`, `support.py`, `webhook_handlers.py`; порядок сборки — `tgbot/handlers/__init__.py` |
| `tgbot/services/scheduler.py` | Все фоновые джобы + `schedule_jobs()` |
| `utils/logger.py` | `APINotificationHandler` (ERROR → админу в Telegram, с вырезанием секретов), `AiohttpNoiseFilter` |
| `webapp/main.py` | FastAPI-приложение, роутеры `auth`, `dashboard`, `payment`, `tma` |
| `scripts/` | Разовые операции: массовое обновление пользователей панели, синк `remnawave_uuid`, анти-флуд routing, cover-сайт ноды |

### Схема БД

Alembic не используется. Таблицы создаются `Base.metadata.create_all` на старте бота,
изменения существующих таблиц — руками в `fix_db.py` (и модель в `db.py`).

Модели: `User`, `Tariff`, `PromoCode`, `UsedPromoCode`, `Payment`, `UserPaymentMethod`,
`LifecycleMessage`, `Channel`, `AppSetting`.

Особенности:
- `User.user_id` — Telegram ID; пользователи веб-кабинета создаются с **отрицательными** id,
  чтобы не столкнуться с телеграмными.
- `User.is_active=False` ставится при `TelegramForbiddenError` (человек заблокировал бота) —
  такие пропускаются в рассылках и возвращаются в строй в `get_or_create`.
- `AppSetting` — правки из админки без релиза (цена слота, базовый лимит, потолок);
  читается `SettingsRepository` с кэшем 60 с.

### Платежи

- `Payment.kind`: `subscription` (покупка/продление тарифа, возможно вместе со слотами) либо
  `devices` (докупка слотов в середине периода — `tariff_id = NULL`, срок подписки не двигается).
  Вебхук обязан их различать: во втором случае трогать `expireAt` и квоту трафика нельзя.
- `Payment.source`: `bot` / `web` / `tma` / `auto` (автосписание) / `stars`.
- `Payment.status`: `pending` / `succeeded` / `failed` / `refunded` / `cancelled`.
  Пока висит `pending`, новый счёт выставить нельзя — поэтому есть кнопка отмены и джоб автоотмены.
- Telegram Stars: `telegram_payment_charge_id` хранится verbatim — нужен для `refundStarPayment`.

### Фоновые задачи (`tgbot/services/scheduler.py`, TZ Europe/Moscow)

| Джоб | Когда | Зачем |
|---|---|---|
| `auto_renew_subscriptions` | 12:00 | Автосписание по сохранённой карте |
| `check_subscriptions` | 12:49 | Напоминания об окончании и отключение просроченных |
| `lifecycle_renewal_reminders` | 10:00 | Lifecycle: напоминание о продлении |
| `lifecycle_winback` | 10:15 | Lifecycle: возврат ушедших |
| `lifecycle_activation_drip` | 10:30 | Lifecycle: дожим неактивированных |
| `award_referral_leaderboard` | 1-го числа, 09:00 | Приз победителю реферального лидерборда |
| `sync_device_limits` | каждый час в :20 | Сверка лимитов устройств (см. ниже) |
| `cancel_stale_payments` | каждые 15 мин | Гасит `pending` старше 60 мин (`PENDING_PAYMENT_TTL_MINUTES`) |

### Режимы бота

- **Polling** (`USE_WEBHOOK=False`) — локальная разработка, внешний домен не нужен.
- **Webhook** (`USE_WEBHOOK=True`) — прод, нужен `DOMAIN` + SSL.

### Админы и пользователи

Админские Telegram ID — в `ADMINS`. Админам доступны рассылки, тарифы, промокоды, каналы,
статистика и цены слотов устройств (`/admin`). Команды меню выставляются персонально
каждому админу в `register_commands()`.

## Docker Services

`docker-compose.yml`, префикс `flask_`:
- `flask_bot` — бот + приём вебхуков ЮKassa (8081), образ `flask_bot`, код смонтирован volume'ом
- `flask_site` — веб-кабинет (8000), тот же образ
- `nginx` — `flask_nginx`, 80 + 8443, плюс `127.0.0.1:8080/9443` для cover-сайта ноды
- `postgres` — `flask_postgres`, 5432 только на localhost, healthcheck `pg_isready`

## Дополнительные устройства (докупка слотов)

Базовый лимит устройств входит в тариф, каждое следующее продаётся отдельно и
стоит фиксированную сумму **в месяц**.

| Правило | Как реализовано |
|---|---|
| Цена при покупке/продлении | `тариф + цена_слота × месяцев × слотов` (`device_pricing.build_checkout`); месяцев = `round(duration_days/30)`, минимум 1 |
| Докупка в середине периода | проратация по остатку дней, но не дешевле месячной цены (`prorated_slot_cost`) — иначе слот на годовом тарифе стоил бы одну месячную плату |
| Срок жизни слотов | до `User.subscription_end_date`; бонусные/реферальные дни продлевают их бесплатно |
| Промокоды | скидка применяется только к тарифу, слоты идут по полной |
| Триал | докупка недоступна, пока нет ни одного платежа (`is_first_payment_made`) |
| Telegram Stars | слотами не оплачиваются: `pre_checkout` отклоняет платёж, если `extra_devices > 0` (иначе продление продлило бы и неоплаченные слоты) |
| Передумал со способом оплаты | счёт отменяется кнопкой «❌ Отменить счёт» (бот — `slots_cancel_invoice`, веб/TMA — `POST /payment/cancel-pending`): пока счёт висит, второй создать нельзя, а YooKassa отменяет его сама только через ~30 мин |
| Потолок | `max_extra_devices` (дефолт +5), правится из админки бота |
| Настройки | ключи `extra_device_price` / `base_device_limit` / `max_extra_devices` в `app_settings` + `SettingsRepository` с кэшем 60 с; экран — «📱 Доп. устройства» в `/admin` |

**Данные:** `User.extra_devices`, `Payment.kind`, `Payment.extra_devices`,
`Payment.tariff_id` — nullable. Миграция — `python3 fix_db.py`.

**Панель:** лимит пишется как АБСОЛЮТНОЕ `hwidDeviceLimit = база + слоты`
(`RemnawaveClient.update_user(hwid_device_limit=...)`). Пока поле NULL, на
пользователя действует глобальный `fallbackDeviceLimit` из
subscription-settings; как только записано персональное число, глобальная
настройка для него больше не работает.

**Две вещи, которые обязан делать джоб `sync_device_limits`:**

1. Довести лимит до состояния БД, если PATCH в панель не прошёл в момент оплаты
   (панель падала — инцидент 23.07). Деньги списаны, слот обязан появиться.
2. Удалить устройства сверх лимита, начиная с самых давно не заходивших.
   Remnawave проверяет лимит **только при регистрации нового hwid** — уже
   записанные устройства переживают снижение лимита, поэтому без чистки схема
   «купил 5 слотов на месяц, привязал 10 устройств, перестал платить» работала бы
   вечно.
