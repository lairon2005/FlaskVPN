# 🧪 FlaskVPN

Telegram-бот и веб-кабинет для продажи VPN-подписок. Пользователи покупают тариф
в боте или на сайте, деньги принимает ЮKassa, доступы выдаёт внешняя панель
**Remnawave** (X-Ray). Всё поднимается одним `docker compose`.

---

## 🚀 Возможности

* **Telegram-бот** (aiogram 3.x) — регистрация, тарифы, оплата, пробный период,
  промокоды, реферальная программа, поддержка, «Мои устройства», перевыпуск ключа.
* **Telegram Mini App** — те же экраны внутри Telegram (`UI_MODE=tma`).
* **Веб-кабинет** (FastAPI + Jinja2) — регистрация по email, JWT в httponly-cookie,
  профиль, история платежей, докупка устройств.
* **Оплата** — ЮKassa (в том числе автосписания) и Telegram Stars.
* **Доп. устройства** — слоты сверх тарифа с помесячной ценой и проратацией.
* **Админка в боте** — тарифы, промокоды, рассылки, каналы, статистика, цены слотов.

## 📦 Стек

Python 3.11 · aiogram 3 · FastAPI · SQLAlchemy (async) · PostgreSQL 16 ·
APScheduler · Docker Compose · nginx · Remnawave API · ЮKassa

---

## 🔧 Установка

### 1. Клонирование

```bash
git clone https://github.com/lairon2005/FlaskVPN.git
cd FlaskVPN
```

### 2. Файл окружения

```bash
cp env.dist .env
```

Минимум, что нужно заполнить:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `ADMIN` | Telegram ID администратора (через запятую — несколько) |
| `DOMAIN` | домен сервиса (`localhost` для локальной разработки) |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | доступы к PostgreSQL (`DB_HOST=postgres` в Docker) |
| `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` | магазин ЮKassa |
| `REMNAWAVE_API_URL` / `REMNAWAVE_API_TOKEN` | адрес и токен панели Remnawave |
| `REMNAWAVE_DEFAULT_SQUAD_UUID` | сквад, в который попадают новые пользователи |
| `REMNAWAVE_SUB_HOST` | публичный хост страницы подписки (`sub.flaskvpn.ru`) |
| `CERT_FULLCHAIN_PATH` / `CERT_KEY_PATH` | абсолютные пути к сертификатам (монтируются в nginx) |

### 3. Сертификаты

Локально — самоподписанные:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout privkey.pem -out fullchain.pem -days 365 -subj "/CN=localhost"
```

На проде — Let's Encrypt:

```bash
certbot certonly --standalone -d flaskvpn.ru -d sub.flaskvpn.ru
```

Пути из `/etc/letsencrypt/live/<домен>/` пропишите в `CERT_FULLCHAIN_PATH` и `CERT_KEY_PATH`.

### 4. Запуск

```bash
./refresh.sh
```

Скрипт собирает образ `flask_bot`, пересоздаёт контейнеры бота и сайта и
перечитывает конфиг nginx, не трогая PostgreSQL.

### 5. Миграции

После обновления схемы:

```bash
python3 fix_db.py
```

---

## 🧭 Локальная разработка

```bash
# Бот в режиме polling (в .env: USE_WEBHOOK=False)
python3 -m bot

# Только веб-кабинет
uvicorn webapp.main:app --reload
```

## 🐳 Сервисы Docker

| Контейнер | Назначение | Порт |
|---|---|---|
| `flask_bot` | Telegram-бот + приём вебхуков ЮKassa | 8081 |
| `flask_site` | веб-кабинет FastAPI | 8000 |
| `flask_nginx` | реверс-прокси, TLS | 80 / 8443 |
| `flask_postgres` | PostgreSQL 16 | 5432 (только localhost) |

Логи: `docker logs flask_bot`, `docker logs flask_site`.

> Remnawave в этот compose не входит — это отдельная панель, доступная по
> `REMNAWAVE_API_URL`. Если разворачиваете стек рядом с другим VPN-проектом на
> том же хосте, разведите порты 80/8443/8000/8081 или вынесите на отдельный сервер.

## 📚 Документация

* `CLAUDE.md` — архитектура проекта и договорённости по коду
* `docs/remnawave-node-setup.md` — ввод новой ноды в эксплуатацию
* `docs/remnawave-node-cover.md` — сайт-заглушка и сертификат для REALITY
* `docs/node-xhttp-hysteria2.md` — инбаунды XHTTP и Hysteria2, работа за Cloudflare
* `docs/node-antiflood.md` — защита от абуз хостера (pps-лимиты, bittorrent)
* `docs/remnawave-proxy-failover.md` — резервный маршрут до панели через SOCKS5
* `docs/tma-roadmap.md` — Telegram Mini App

## ✅ Проверка

Отправьте боту `/start` — он должен ответить главным меню и выдать ключ после
активации пробного периода или оплаты.
