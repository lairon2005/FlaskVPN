# CLAUDE.md — webapp

FastAPI web dashboard для VPN-сервиса. Регистрация по email, JWT-авторизация, профиль, оплата.

---

## Стек

- **FastAPI** + Jinja2 (SSR-шаблоны)
- **SQLAlchemy async** (через общий `db.py` и `database/repositories/`)
- **python-jose** — JWT (HS256, httponly cookie)
- **passlib[argon2]** — хэширование паролей
- **fastapi-mail** — SMTP email
- **YooKassa** — приём платежей
- **Bootstrap 5** + кастомный CSS (dark theme, бирюзовый акцент `#06b6d4`)

Запуск: `uvicorn webapp.main:app --reload` (порт 8000). В Docker — сервис `flask_site`.

---

## Структура файлов

```
webapp/
├── main.py              # FastAPI app, lifespan, ProxyHeadersMiddleware, jinja2_filter timestamp_to_date
├── dependencies.py      # get_current_user(request) → User | None  (JWT из cookie)
├── core/
│   ├── security.py      # get_password_hash(), verify_password(), create_access_token()
│   └── mail.py          # send_verification_email(), send_reset_code(), MailSendError
├── routers/
│   ├── auth.py          # /login /register /verify-email /logout /forgot-password /reset-password
│   ├── dashboard.py     # /profile/ /profile/activate_trial
│   └── payment.py       # /payment/create /payment/device-slots /payment/cancel-pending /payment/validate-promo /payment/apply-bonus-promo
├── static/
│   ├── css/styles.css   # 826 строк: CSS variables, компоненты, анимации
│   └── js/scripts.js    # 285 строк: payment, promo, copy, OS detection, toast
└── templates/
    ├── base.html         # Layout: navbar (логин/не логин), footer, toast container
    ├── index.html        # Лендинг: hero SVG-глобус, features, тарифы
    ├── login.html        # Форма входа
    ├── register.html     # Форма регистрации + strength bar
    ├── verify_email.html # Ввод 6-значного кода из email
    ├── forgot_password.html
    ├── reset_password.html
    └── dashboard.html    # Профиль, VPN-ключ, реферальная ссылка, история платежей
```

---

## Все маршруты

### `auth.py`
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/login` | Страница входа |
| GET | `/register` | Страница регистрации (`?ref=` для реферала) |
| GET | `/logout` | Удалить cookie, редирект на `/login` |
| GET | `/forgot-password` | Форма сброса пароля |
| GET | `/reset-password` | Форма ввода кода + нового пароля |
| POST | `/register` | Валидация → генерация кода → email → render verify_email.html с JWT |
| POST | `/verify-email` | Проверить 6-значный код из JWT, создать User в БД, выдать cookie |
| POST | `/resend-code` | Повторная отправка кода (rate-limit 2 мин), обновить JWT |
| POST | `/login` | Проверить email+password, выдать access_token cookie |
| POST | `/forgot-password` | Сгенерировать reset_code → сохранить в БД → email |
| POST | `/reset-password` | Проверить reset_code, сохранить новый пароль |

### `dashboard.py` (prefix `/profile`)
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/profile/` | Профиль: Marzban данные, subscription link, реферальная статистика, платежи |
| POST | `/profile/activate_trial` | Активировать пробный период (3 дня) |
| POST | `/profile/devices/delete` | Форма `key` → отвязать HWID-устройство (пара к `hwidDeviceLimit`) |
| POST | `/profile/key/revoke` | Перевыпустить ключ подписки → редирект `/profile/?key=<код>` (PRG: после POST-рендера F5 перевыпускал бы ключ повторно) |

Шаблон получает `slot_quote` (результат `device_slot_service.quote`) и
`device_settings` — из них рисуются блок докупки устройств в карточке
«Мои устройства» и счётчик доп. устройств над тарифами.

### `payment.py` (prefix `/payment`)
| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/payment/create` | JSON: `{tariff_name, price, promo_code?, extra_devices?}` → YooKassa → `{payment_url}`. `extra_devices` — доп. устройства к тарифу; количество режется по серверному потолку, скидка на них не действует, в чек уходят отдельной позицией (54-ФЗ) |
| POST | `/payment/device-slots` | JSON: `{slots, source?}` → счёт на докупку устройств (`kind='devices'`, без тарифа). Цену считает сервер по остатку срока подписки; 400 — докупка недоступна (нет подписки, триал, потолок), 409 — уже есть неоплаченный счёт |
| POST | `/payment/cancel-pending` | Без тела → локально помечает висящий счёт `cancelled`, чтобы можно было выставить новый (например, сменить карту на СБП). YooKassa сама отменяет счёт только через ~30 мин, а `Payment.cancel` для `capture=True` недоступен; 404 — счёта нет |
| POST | `/payment/validate-promo` | JSON: `{code}` → `{valid, type, discount_percent/bonus_days}` |
| POST | `/payment/apply-bonus-promo` | JSON: `{code}` → применить бонус-дни сразу |

### `main.py`
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | Авторизованным → `/profile/`, анонимным → `index.html` с тарифами |

---

## Аутентификация (JWT)

**Cookie:** `access_token` = `"Bearer {JWT}"`, `httponly=True`
**Алгоритм:** HS256, ключ: `SECRET_KEY` из env
**Срок:** 7 дней (`ACCESS_TOKEN_EXPIRE_MINUTES = 60*24*7`)

### Два типа токенов

**1. Pending Registration** (`type: "registration"`, 1 час)
Хранит: `email, full_name, password_hash, code, code_expire, attempts`
Живёт между `POST /register` и `POST /verify-email` — данные не в БД.

**2. Access Token** (7 дней)
Хранит: `sub` (user_id как строка), `email`, `exp`
`get_current_user` проверяет `type != "registration"` перед запросом в БД.

### ID пользователей
- Telegram-пользователи: **положительные** int (Telegram ID)
- Веб-пользователи: **отрицательные** int (от -1 до -1 000 000 000)

---

## Email (`core/mail.py`)

```python
MAIL_TIMEOUT = 15  # секунд, asyncio.wait_for()
```

Порт 465 → `MAIL_SSL_TLS=True`; порт 587 → `MAIL_STARTTLS=True` (определяется автоматически).

**Env vars:** `MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM, MAIL_FROM_NAME`

**Ошибки:** `MailSendError` — содержит безопасное сообщение для пользователя; техническая ошибка только в логах.

**Функции:** `send_verification_email(email, code)`, `send_reset_code(email, code)`

> ⚠️ Яндекс SMTP блокирует соединения с VPS-IP. Нужен transactional-провайдер (Resend, SendGrid и т.п.).

---

## Security (`core/security.py`)

```python
get_password_hash(password: str) → str       # Argon2
verify_password(plain, hashed) → bool         # Argon2 constant-time
create_access_token(data: dict, expires_delta?) → str  # JWT HS256
```

---

## CSS (`static/css/styles.css`)

**CSS-переменные:**
```css
--primary:        #06b6d4   /* бирюзовый */
--bg-body:        #0e0e0e   /* основной фон */
--bg-card:        #141414   /* карточки */
--text-primary:   #ffffff
--text-secondary: #a0a0a0
```

**Ключевые классы:**
- `.btn-accent` / `.btn-accent-outline` / `.btn-loading` — кнопки
- `.glass-card` / `.section-card` — карточки
- `.copy-field` — поле с кнопкой копирования
- `.status-badge`, `.status-badge-active`, `.status-badge-expired` — статусы
- `.alert-custom.alert-error` / `.alert-success` — сообщения об ошибках/успехе
- `.tariff-card.popular` — выделенный тариф
- `.animate-in`, `.animate-in-delay-{1-4}` — staggered анимация при загрузке
- `.hero-visual` / `.hero-svg` — SVG-глобус в hero секции
- `.hv-*` — классы анимаций SVG-глобуса (кольца, ноды, линии, щит)
- `.data-table` — таблица истории платежей

---

## JavaScript (`static/js/scripts.js`)

| Функция | Назначение |
|---------|------------|
| `showToast(message, type)` | Уведомление (top-right, 3 сек) |
| `initPayment(name, price, btn)` | POST `/payment/create` (+ `extra_devices`), редирект на payment_url. На 409 предлагает отменить висящий счёт (`offerCancelPending`) и повторяет запрос один раз |
| `initSlotSelector()` | Ставит счётчик доп. устройств в уже оплаченное количество — иначе продление без касания счётчика сняло бы слоты |
| `updateSlotCount(delta)` | Степпер доп. устройств: пересчёт цены во всех карточках тарифов (`цена × месяцев × слотов`) |
| `updateBuySlots(delta)` / `buyDeviceSlots(btn)` | Докупка устройств в карточке «Мои устройства» → POST `/payment/device-slots`; на 409 — тот же `offerCancelPending` |
| `applyPromo()` | Валидация промокода, пересчёт цен |
| `updateTariffPrices(discount%)` | Обновить цены на всех `.tariff-price` |
| `copyLink()` | Скопировать `#subLink` |
| `copyRefLink()` | Скопировать реферальную ссылку |
| `initRefLink()` | Построить ref URL: `window.location.origin + /register?ref={data-ref}` |
| `detectOSAndSetLink()` | Определить OS → подставить ссылку на скачивание |
| `initPasswordStrength()` | Strength bar при вводе пароля |
| `initFormLoading()` | `.btn-loading` на submit кнопки при отправке форм |

**Глобальное состояние:** `activePromo` — применённый промокод с discount_percent;
`selectedSlots` — выбранное количество доп. устройств на экране тарифов.

**DOMContentLoaded:** вызывает все `init*` функции.

---

## Паттерны шаблонов

**Ошибка в форме:**
```html
{% if error %}<div class="alert-custom alert-error">{{ error }}</div>{% endif %}
```

**Успех (после редиректа):**
```html
{% if message %}<div class="alert-custom alert-success">{{ message }}</div>{% endif %}
```

**JSON API ответы:**
```json
// success
{"payment_url": "...", "valid": true, "type": "discount", "discount_percent": 15}
// error
{"detail": "Unauthorized"}  {"error": "Invalid promo code"}
```

---

## Переменные окружения (webapp-специфичные)

| Переменная | Дефолт | Где используется |
|-----------|--------|-----------------|
| `SECRET_KEY` | `"CHANGE_THIS..."` | JWT подпись |
| `MAIL_SERVER` | `smtp.gmail.com` | SMTP хост |
| `MAIL_PORT` | `587` | SMTP порт |
| `MAIL_USERNAME` | `""` | SMTP логин |
| `MAIL_PASSWORD` | `""` | SMTP пароль |
| `MAIL_FROM` | `noreply@flaskvpn.ru` | Отправитель |
| `MAIL_FROM_NAME` | `FlaskVPN` | Имя отправителя |

Также используется `config.webhook.domain` для построения subscription URL (`https://domain:8443/sub/...`).

---

## Деплой

```bash
# Обновить файл в контейнере без пересборки:
docker cp webapp/templates/foo.html flask_site:/usr/src/app/webapp/templates/foo.html
docker cp webapp/static/css/styles.css flask_site:/usr/src/app/webapp/static/css/styles.css

# Перезапуск (если изменился Python-код):
docker compose restart flask_site

# Полная пересборка (если изменился Dockerfile или зависимости):
./refresh.sh
```

> ⚠️ `docker compose restart` НЕ подхватывает изменения исходников — они скопированы в образ. Используй `docker cp` для быстрого обновления шаблонов/статики. Для `.py` файлов — `restart`.
