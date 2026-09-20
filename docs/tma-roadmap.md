# Roadmap: Telegram Mini App (TMA) для FlaskVPN

Цель — адаптировать существующий веб-дашборд (`webapp/`) под Telegram Mini App с полным функционалом сайта, онбордингом и поддержкой.

**Зафиксированные решения:**
- **Функционал:** всё как на сайте (профиль, ключ, тарифы, промокоды, рефералка, история платежей, триал) + онбординг/импорт + поддержка
- **Авторизация:** только Telegram `initData` (HMAC от `BOT_TOKEN`), без email/пароля; веб-аккаунты живут отдельно
- **Фронтенд:** отдельные Jinja2-шаблоны поверх текущего CSS + Telegram WebApp JS SDK (без SPA-сборки)
- **Оплата:** YooKassa (текущий флоу) + Telegram Stars

**Ключевое упрощение:** `webapp/routers/payment.py` и `dashboard.py` уже работают через общий сервисный слой `tgbot/services/*`, а `get_current_user` проверяет только JWT. Значит, TMA-авторизация сводится к обмену `initData → тот же JWT`, после чего **все существующие API-роуты работают без изменений**.

---

## Фаза 0 — Каркас и регистрация (0.5 дня)

1. **BotFather:** создать Mini App (`/newapp`) → получить прямую ссылку `https://t.me/<bot>/<app>`; настроить Menu Button на URL приложения.
2. **URL приложения:** `https://{DOMAIN}:8443/tma/` — новый префикс в `flask_site`.
3. **nginx** (`etc/nginx/templates/default.conf.template`): маршрут `/tma` → `flask_site:8000` (по аналогии с `/`).
4. **Роутер:** `webapp/routers/tma.py` (prefix `/tma`), пустая страница-заглушка `templates/tma/index.html`, подключить в `webapp/main.py`.

**Результат:** Mini App открывается из бота и показывает заглушку.

---

## Фаза 1 — Авторизация через initData (1 день)

1. **Валидация initData** — `webapp/core/tma_auth.py`:
   - HMAC-SHA256 проверка: `secret = HMAC_SHA256("WebAppData", BOT_TOKEN)`, сверка `hash` из query-string;
   - проверка свежести `auth_date` (окно ~1 час, защита от replay);
   - парсинг `user` (Telegram ID, имя, username).
2. **Эндпоинт `POST /tma/auth`:** принимает `initData` строкой → валидирует → find-or-create юзера по Telegram ID (переиспользовать логику регистрации из `tgbot/handlers/user/start.py` / user_service — юзер мог открыть TMA, ни разу не нажав /start) → выдаёт **тот же JWT** через `create_access_token()`.
3. **Доставка токена:** cookie `access_token` (как на сайте) **плюс** поддержка заголовка `Authorization: Bearer` в `webapp/dependencies.py::get_current_user` — на случай урезанных cookie в webview Telegram Desktop. Токен также хранить в JS-памяти страницы.
4. **Рефералка при первом входе:** параметр `startapp={ref_id}` из `initDataUnsafe.start_param` → передавать в `/tma/auth` → начислять реферала через `referral_service` (как в боте).
5. **Тесты:** `tests/test_tma_auth.py` — валидный/подделанный hash, просроченный `auth_date`, `start_param`.

**Результат:** открытие TMA = автоматический вход, юзер создаётся в БД при необходимости.

---

## Фаза 2 — UI: экраны Mini App (2–3 дня)

1. **`templates/tma/base.html`:** без navbar/footer, подключает `https://telegram.org/js/telegram-web-app.js`; `Telegram.WebApp.ready()` + `expand()`; тема сайта остаётся общей с вебом — сейчас это светлая тема бренд-бука FLASK (`--bg-body: #F7F1E8`, акцент `#2457C5`, см. `docs/brand.md`); на `themeParams` клиента не завязываемся, вместо этого красим хром Telegram через `setHeaderColor` / `setBackgroundColor` / `setBottomBarColor` в `tma.js`.
2. **Экраны** (SSR как на сайте, данные из тех же сервисов):
   - **Главная `/tma/`** — статус подписки (`status-badge`), дата окончания, кнопка продления; адаптация `dashboard.html`;
   - **Ключ и импорт `/tma/import`** — VPN-ключ (`copy-field`), определение ОС, кнопка «Добавить в Happ» через `build_import_url()`/`build_deeplink()` из `utils/url.py` (percent-encoding уже починен), ссылки на приложения (iOS/macOS id6783623643, Android com.happproxy), пошаговая инструкция — перенос логики `etc/nginx/static/import.html`;
   - **Тарифы `/tma/tariffs`** — карточки тарифов, промокод (`/payment/validate-promo` работает как есть), кнопка оплаты через **MainButton**;
   - **Рефералка** — вместо веб-ссылки шарить `https://t.me/<bot>/<app>?startapp={user_id}` через `Telegram.WebApp.shareURL` / `switchInlineQuery`;
   - **История платежей** — таблица `.data-table` как на сайте;
   - **Триал** — кнопка активации (`POST /profile/activate_trial` работает как есть).
3. **`static/js/tma.js`:** адаптация `scripts.js` — `initPayment` (см. Фазу 3), promo, copy (+`HapticFeedback`), навигация через **BackButton**; toast заменить на `Telegram.WebApp.showAlert`/оставить свой.
4. **Deeplink-роутинг:** `startapp=tariffs|import|support` → открытие нужного экрана (для кнопок из бота).

**Результат:** полнофункциональный дашборд внутри Telegram.

---

## Фаза 3 — Оплата (2–3 дня)

### 3.1 YooKassa (переиспользование, ~0.5 дня)
1. `POST /payment/create` работает как есть; в metadata `source: "tma"` (новое значение) для аналитики.
2. Открытие `payment_url` через `Telegram.WebApp.openLink()` (внешний браузер).
3. **`return_url`** — не `/profile/`, а ссылка обратно в TMA: `https://t.me/<bot>/<app>?startapp=paid` (юзер возвращается в Telegram, а не в браузер).
4. Подтверждение — существующий YooKassa-вебхук (`tgbot/handlers/webhook_handlers.py`) + уведомление в чат ботом; TMA при повторном открытии показывает актуальный статус. Опционально — поллинг статуса платежа на экране «ожидание оплаты».

### 3.2 Telegram Stars (новая логика, ~2 дня)
1. **Цены в Stars:** поле `price_stars` в `Tariff` (миграция через `fix_db.py`-паттерн) или конвертация RUB→XTR по фикс. курсу в конфиге.
2. **`POST /tma/payment/stars-invoice`:** бэкенд вызывает Bot API `createInvoiceLink` (currency `XTR`, без provider_token) → возвращает `invoice_url`.
3. **Фронт:** `Telegram.WebApp.openInvoice(url, callback)` — оплата в один тап, по `status === "paid"` обновить экран.
4. **Бот:** обработчики `pre_checkout_query` (валидация: тариф существует, нет дублей) и `successful_payment` → запись платежа (`source='stars'`, `telegram_payment_charge_id` для рефандов) → `subscription_service.extend()` → продление в Remnawave.
5. **Учёт:** пометить Stars-платежи в админ-статистике (`admin_stats_service`) — доход в XTR отдельно от RUB.
6. **Тесты:** `tests/test_stars_payment.py` — pre_checkout валидация, зачисление, идемпотентность по `charge_id`.

**Результат:** два способа оплаты; Stars — не покидая Telegram.

---

## Фаза 4 — Поддержка и интеграция с ботом (1 день)

1. **Экран `/tma/support`:** форма обращения → тот же сервис, что `tgbot/handlers/support.py` (сообщение админам с Telegram ID юзера); ответ приходит в чат с ботом.
2. **Кнопки в боте** (`tgbot/keyboards/inline.py`): `InlineKeyboardButton(web_app=WebAppInfo(url=...))` — «Открыть кабинет», «Оплатить» (→ `?startapp=tariffs`), «Подключить VPN» (→ `?startapp=import`) в /start и меню.
3. **Рассылки/уведомления** об истечении подписки (`scheduler.py`) — добавить кнопку «Продлить» с web_app.

**Результат:** TMA встроен во все точки контакта с юзером.

---

## Фаза 5 — Деплой, QA, полировка (1 день)

1. **Деплой:** rsync на bot-server → `./refresh.sh` (пересоздаёт `flask_bot`/`flask_site`); nginx-конфиг — через template, **не** заливать `etc/nginx/conf/default.conf`.
2. **QA-матрица:** iOS / Android / Telegram Desktop / macOS — auth, cookie vs Bearer, openLink, openInvoice, deeplink Happ, BackButton/MainButton.
3. **Безопасность:** заголовки для webview (не отдавать `X-Frame-Options: DENY` на `/tma`), rate-limit на `/tma/auth`.
4. **Аналитика:** `source='tma'` в платежах — сравнение конверсии сайт vs TMA vs бот.

---

## Порядок и оценка

| Фаза | Что | Оценка | Зависимости |
|---|---|---|---|
| 0 | Каркас, BotFather, nginx | 0.5 дня | — |
| 1 | initData-авторизация | 1 день | 0 |
| 2 | UI-экраны | 2–3 дня | 1 |
| 3.1 | YooKassa в TMA | 0.5 дня | 2 |
| 3.2 | Telegram Stars | 2 дня | 2 |
| 4 | Поддержка + кнопки бота | 1 день | 2 |
| 5 | Деплой + QA | 1 день | всё |

**MVP (0 → 1 → 2 → 3.1 → 5): ~5 дней.** Stars (3.2) и поддержка (4) — вторая итерация, не блокируют запуск.

## Статус реализации (на 25.07.2026)

Фазы 0–4 реализованы и проверены, тесты: **142 passed**. Не закоммичено.

| Фаза | Статус | Ключевые файлы |
|---|---|---|
| 0 | ✅ | `webapp/routers/tma.py`, `etc/nginx/templates/default.conf.template` (`location /tma`) |
| 1 | ✅ | `webapp/core/tma_auth.py`, `POST /tma/auth`, Bearer-фолбэк в `webapp/dependencies.py`, `tests/test_tma_auth.py` |
| 2 | ✅ | `webapp/templates/tma/*` (7 экранов), `webapp/static/js/tma.js` |
| 3.1 | ✅ | `PaymentRequest.source` (`web`/`tma`), `return_url` → `t.me/<bot>/<app>?startapp=paid` |
| 3.2 | ✅ | `Tariff.price_stars`, `Payment.telegram_payment_charge_id`, `POST /tma/payment/stars-invoice`, `tgbot/handlers/user/stars_payment.py`, `tests/test_stars_payment.py` |
| 4 | ✅ | `GET/POST /tma/support`, `webapp/core/support.py`, web_app-кнопки в `inline.py` + `scheduler.py`, `tests/test_tma_support.py` |
| 5 | ⬜ | не начата — BotFather, деплой, QA-матрица |

### Переключатель режима интерфейса (`UI_MODE`)

Глобальный флаг в `.env`, действует на всех пользователей сразу, применяется при перезапуске `flask_bot`:

| `UI_MODE` | Главное меню | Экран ключей | Напоминание о продлении |
|---|---|---|---|
| `bot` (дефолт) | callback-кнопки, сценарии внутри бота | без кнопки Mini App | клавиатура тарифов |
| `tma` | те же пункты, но `web_app` на `/tma/tariffs`, `/tma/import`, `/tma/referral`, `/tma/support` | + «📲 Открыть в приложении» | + «🚀 Продлить в приложении» |

Пункты «🌟 Бесплатная подписка», «📧 Привязать Email» и «🎁 +3 дня за подписку на канал» остаются callback-кнопками в **обоих** режимах: триал и channel-gate требуют проверки подписки на каналы через Bot API, привязка email — FSM бота. Из Mini App эти сценарии не выполнить.

**Защита от локального домена.** При `DOMAIN=localhost` (dev/polling) режим `tma` автоматически деградирует в `bot` — см. `tgbot/keyboards/inline.py::tma_mode_enabled`. Telegram отвергает `web_app`-кнопку с локальным хостом ошибкой `BUTTON_TYPE_INVALID`, причём отклоняется **вся клавиатура**, то есть пользователь получил бы не «меню без Mini App», а вообще никакого меню.

### Обязательно перед деплоем (иначе 500 / тихая деградация)

1. **`python3 fix_db.py`** — добавляет `tariffs.price_stars`, `payments.telegram_payment_charge_id` (UNIQUE), `users.is_active`. Без миграции упадёт любой платёж.
2. **`TG_BOT_USERNAME`** и **`TMA_APP_NAME`** в `.env` прода. Без них: YooKassa `return_url` молча откатывается на `/profile/` (юзер зависает в браузере), а реферальная ссылка на экране `/tma/referral` не показывается.
2a. **`UI_MODE=tma`** — иначе бот останется в классическом режиме и Mini App будет доступен только по прямой ссылке. Раскатывать имеет смысл после того, как QA-матрица (ниже) пройдена: откат — смена флага и перезапуск `flask_bot`, без миграций.
3. **`price_stars`** проставить у тарифов вручную — пока `NULL`, кнопка «⭐» скрыта и Stars-оплата недоступна.
4. `SUPPORT_CHAT_ID` уже используется ботом — форма `/tma/support` пишет в те же топики.

### Проверить на QA (фаза 5)

- **web_app-кнопки на порту 8443** — Telegram требует HTTPS; порт нестандартный, проверить открытие на iOS/Android/Desktop/macOS.
- Rate-limit поддержки (`webapp/core/support.py`) — in-memory, свой на каждый процесс `flask_site`; при масштабировании в несколько воркеров лимит станет мягче.

## Риски и нюансы

- **Cookie в webview:** Telegram Desktop иногда режет cookies — поэтому обязателен фолбэк на `Authorization: Bearer` (Фаза 1.3).
- **Возврат из YooKassa:** оплата уходит во внешний браузер; юзер должен вернуться по `t.me`-ссылке, иначе «повис» в браузере — обязательно проверить на iOS.
- **Юзер без /start:** TMA можно открыть по прямой ссылке — find-or-create в `/tma/auth` обязателен, иначе 500 на первом же экране.
- **Stars-рефанды:** хранить `telegram_payment_charge_id`; рефанд только через Bot API `refundStarPayment`.
- **Двойные аккаунты:** юзер с веб-аккаунтом (отрицательный ID) и Telegram-аккаунтом — это два разных юзера с разными подписками; по решению — не линкуем, но поддержка должна уметь это объяснять.
- **Pending-платежи:** лимит «один неоплаченный счёт / 30 мин» (`has_pending_payment`) общий для сайта, бота и TMA — при оплате Stars его учитывать не нужно (инвойс живёт отдельно).
