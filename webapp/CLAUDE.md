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
- **Tailwind CSS v4** (standalone-бинарник, без npm) — **все** страницы: лендинг,
  юр. документы, вход/регистрация, кабинет и Mini App. Источник
  `static/css/src/app.css`, сборка `./tools/tailwind.sh`, результат
  `static/css/app.css` коммитится.
- **Иконки** — SVG-спрайт `templates/_icons.html` (Lucide). Bootstrap и Font Awesome
  из проекта удалены полностью, как и `styles.css`, `tma.css`, `base_legacy.html`.
- Оформление — дофаминовый бренд-бук FLASK (кремовая бумага `#F7F1E8`, синий `#2457C5`,
  оранжевый акцент `#FF7627`) — см. `docs/brand.md`.

> Один CSS на всё приложение: правка токена в `@theme` меняет и сайт, и Mini App.
> Не добавляй второй файл стилей — TMA-специфика живёт секцией `.tma-*`
> в том же `src/app.css`.

Запуск: `uvicorn webapp.main:app --reload` (порт 8000). В Docker — сервис `flask_site`.

---

## Структура файлов

```
webapp/
├── main.py              # FastAPI app, lifespan, ProxyHeadersMiddleware, маршрут "/"
├── templating.py        # ЕДИНСТВЕННЫЙ Jinja2Templates + фильтр timestamp_to_date + глобалы site/now_year/has_static
├── dependencies.py      # get_current_user(request) → User | None  (JWT из cookie)
├── core/
│   ├── security.py      # get_password_hash(), verify_password(), create_access_token()
│   ├── site.py          # SiteInfo: домен, ссылки на бота/поддержку, реквизиты из .env
│   └── mail.py          # send_verification_email(), send_reset_code(), MailSendError
├── routers/
│   ├── auth.py          # /login /register /verify-email /logout /forgot-password /reset-password
│   ├── dashboard.py     # /profile/ …
│   ├── legal.py         # /offer /privacy /refund
│   └── payment.py       # /payment/create /payment/device-slots /payment/cancel-pending /payment/validate-promo /payment/apply-bonus-promo
├── static/
│   ├── css/src/app.css  # ИСХОДНИК Tailwind: @theme-токены бренда + дофаминовые компоненты
│   ├── css/app.css      # СБОРКА (коммитится, руками не править)
│   ├── fonts/matcha-world.woff2  # фирменный леттеринг (латиница, @font-face с unicode-range)
│   ├── img/collage/     # ретро-коллаж лендинга, рисуется только при наличии файлов (README внутри)
│   ├── js/site.js       # мобильное меню + модалки <dialog> (сайт)
│   ├── js/scripts.js    # payment, promo, copy, OS detection, toast (сайт)
│   └── js/tma.js        # Telegram SDK, MainButton, шторка «Ещё», Stars (Mini App)
└── templates/
    ├── _icons.html       # SVG-спрайт (Lucide) + макрос icon(name, classes)
    ├── base.html         # Layout сайта: SEO/OG-метатеги, шапка, подвал с юр. ссылками
    ├── auth_base.html    # Обёртка экранов авторизации (карточка + alert'ы)
    ├── index.html        # Лендинг: hero, сценарии, как это работает, фичи, тарифы, CTA
    ├── dashboard.html    # Кабинет: статус-блок, ключ, устройства, оплата, рефералка, платежи
    ├── login.html / register.html / verify_email.html
    ├── forgot_password.html / reset_password.html
    ├── legal/_doc.html   # обёртка юр. документов + блок реквизитов
    ├── legal/{offer,privacy,refund}.html
    └── tma/              # Mini App: base, index, tariffs, devices, import,
                          #   referral, history, support, _auth_splash
```

### Шаблоны: три базы

| База | Кто наследует |
|---|---|
| `base.html` | лендинг, юр. документы, кабинет |
| `auth_base.html` (наследует `base.html`) | вход, регистрация, код из письма, сброс пароля |
| `tma/base.html` | все экраны Mini App (свой таб-бар, без шапки и подвала сайта) |

Все три подключают один и тот же `app.css` и один спрайт иконок.

### Иерархия кабинета

`dashboard.html` намеренно неровный по весу: сверху единственный доминирующий
блок состояния подписки с одной главной кнопкой, затем ключ и подключение,
устройства, и только потом — второстепенное (способ оплаты, рефералка, история)
в две колонки. Раньше это были восемь одинаковых карточек подряд, где срок
подписки весил столько же, сколько история платежей.

Часть веток `/profile` отдаёт шаблон с урезанным контекстом (ошибка активации
триала присылает только `user`/`error`/`tariffs`). Обращение к атрибуту
неопределённой переменной в Jinja — исключение, поэтому в начале шаблона стоит
блок `{% set ... | default(...) %}`; **не убирай его**, добавляя новые поля.

### Модальные окна

Bootstrap JS больше нет — окна на нативном `<dialog class="modal">`:
кнопка `data-modal-open="<id>"`, закрытие `data-modal-close`, клик по затемнению
и Esc обрабатываются в `site.js::initModals`. Фокус-трап и инертность фона
даёт сам `<dialog>`.

### Сборка CSS

```bash
./tools/tailwind.sh            # разовая сборка (minify)
./tools/tailwind.sh --watch    # пересборка при правке шаблонов
python3 tools/make_og.py       # перегенерировать og-обложку 1200×630
python3 tools/make_collage.py  # пересобрать коллажную картинку лендинга из исходника
```

Иконки добавляются символом в `templates/_icons.html` и вставляются макросом
`{{ ic.icon('name', 'w-5 h-5') }}`. JS, которому надо сменить иконку на лету
(определение ОС), переписывает `href` у вложенного `<use>` — см.
`scripts.js::detectOSAndSetLink`.

Глобал `has_static('img/...')` проверяет наличие файла в `webapp/static` на рендере.
Коллаж бренда сейчас на месте, но проверка остаётся: картинки заливают через
`docker cp` мимо рестарта контейнера, и до их появления hero рисует SVG-глобус
вместо битого `<img>`. Размеры картинок продублированы в `width`/`height` у
`<img>` в `index.html` — меняешь картинку, меняй и их, иначе браузер зарезервирует
место не по той пропорции.

Бинарник скачивается в `tools/bin/` (gitignored) при первом запуске — npm,
`package.json` и `node_modules` в проекте не появляются, `Dockerfile` не меняется.
**Собранный `app.css` обязан быть закоммичен**: контейнер Tailwind не запускает.

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

### `legal.py`
| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/offer` | Публичная оферта |
| GET | `/privacy` | Политика конфиденциальности |
| GET | `/refund` | Оплата и возврат |

Тексты параметризованы реквизитами из `site` (`LEGAL_*` в `.env`). Пока реквизиты
пусты, `site.has_legal` = False и страница помечается плашкой «Черновик».

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

**Функции:** `send_verification_email(email, code)`, `send_reset_code(email, code)`.
Обе собирают HTML через `_brand_code_email()` — таблицы + инлайн-цвета бренда
(почтовые клиенты режут `<style>` и веб-шрифты).

> ⚠️ Яндекс SMTP блокирует соединения с VPS-IP. Нужен transactional-провайдер (Resend, SendGrid и т.п.).

---

## Security (`core/security.py`)

```python
get_password_hash(password: str) → str       # Argon2
verify_password(plain, hashed) → bool         # Argon2 constant-time
create_access_token(data: dict, expires_delta?) → str  # JWT HS256
```

---

## CSS (`static/css/src/app.css` → `static/css/app.css`)

Оформление — бренд-бук FLASK, подробности в `docs/brand.md`. Токены объявлены
в `@theme` (Tailwind v4 CSS-first), поэтому доступны и как CSS-переменные
(`var(--color-blue)`), и как утилиты (`bg-blue`, `text-orange`).

```css
--color-blue    #2457C5   основной, логотип, кнопки
--color-sky     #73B9F5   фоновые формы, полутон
--color-orange  #FF7627   акценты, «искры», активная вкладка TMA
--color-cream   #F7F1E8   фон
--color-paper   #FFFCF6   карточки
--color-ink     #17203A   текст
--color-subtle  #4A5878   вторичный текст (6.3:1 на кремовом)
--color-muted   #616D88   третичный текст (4.6:1 — минимум AA)
--font-lettering  Matcha World (ТОЛЬКО латиница) → Nunito
--font-display    Nunito 800/900    --font-body Inter    --font-mono JetBrains Mono
```

**Компоненты (`@layer components`):**
- `.btn-pop` / `.btn-pop-outline` / `.btn-pop-orange` / `.btn-loading` — кнопки с печатным офсетом
- `.sticker` (+ `.sticker-hover`) — карточка-стикер; `.panel` / `.panel-dashed` — вложенные блоки
- `.input` / `.input-wrap` + `.input-icon` / `.field-label` / `.code-input` / `.strength-track`
- `.alert` + `-error` / `-success` / `-warn` / `-info`
- `.copy-field` — поле с кнопкой копирования
- `.badge` + `-active` / `-expired` / `-pending` / `-info` / `-muted`, `.dot` — статусы
- `.data-table` — таблица платежей (табличные цифры)
- `.modal` / `.modal-head` / `.modal-body` / `.modal-close` — нативный `<dialog>`
- `.stepper-btn` — ± счётчика устройств, `.avatar`
- `.wordmark` / `.slogan` / `.eyebrow` / `.marker-underline` / `.halftone` / `.blob` / `.tape` — графика бренда
- `.doc` — типографика юридических страниц
- `.tma-*` — таб-бар, шторка «Ещё», карточки тарифов и сплэш Mini App
- `.animate-in` — появление секции; `opacity: 0` вешается только при `html.js`

Зерно бумаги — `body::before` (SVG-шум, `mix-blend-mode: multiply`, `opacity .11`).
В Mini App отключено (`.tma-body::before { content: none }`) — на слабых телефонах
fixed-слой с блендом роняет плавность прокрутки.

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
| `LEGAL_NAME` | — | Продавец в оферте и подвале («ИП …») |
| `LEGAL_INN` | — | ИНН |
| `LEGAL_OGRNIP` | — | ОГРНИП (опционально) |
| `LEGAL_ADDRESS` | — | Адрес (опционально) |
| `LEGAL_EMAIL` | — | Почта для претензий и возвратов |
| `TG_BOT_USERNAME` | — | Из него строятся ссылки на бота и поддержку в подвале |

Также используется `config.webhook.domain` для построения subscription URL (`https://domain/sub/...`).

---

## Деплой

```bash
# Обновить файл в контейнере без пересборки:
docker cp webapp/templates/foo.html flask_site:/usr/src/app/webapp/templates/foo.html
docker cp webapp/static/css/app.css flask_site:/usr/src/app/webapp/static/css/app.css

# Перезапуск (если изменился Python-код):
docker compose restart flask_site

# Полная пересборка (если изменился Dockerfile или зависимости):
./refresh.sh
```

> ⚠️ `docker compose restart` НЕ подхватывает изменения исходников — они скопированы в образ. Используй `docker cp` для быстрого обновления шаблонов/статики. Для `.py` файлов — `restart`.
