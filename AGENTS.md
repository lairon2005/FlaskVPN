# AGENTS.md
ОБЩАЙСЯ СО МНОЙ НА РУССКОМ
Speek russian!

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Submodule Docs
- **`webapp/CLAUDE.md`** — детальная документация web-dashboard: все маршруты, JWT, шаблоны, CSS/JS паттерны, деплой

## Project Overview

**FlaskVPN** — Telegram VPN subscription bot with web dashboard and payment processing. Integrates:
- **Telegram Bot** (aiogram 3.x) — user registration, subscriptions, payments
- **Web Dashboard** (FastAPI) — user portal with email auth
- **Remnawave** — external VPN backend panel (X-Ray proxy), managed via `remnawave/client.py`
- **YooKassa** — Russian payment gateway
- **PostgreSQL** (16) — user, tariff, promo code, channel data

## Running the Project

```bash
# One-time setup
cp env.dist .env
openssl req -x509 -newkey rsa:2048 -nodes -keyout privkey.pem -out fullchain.pem -days 365 -subj "/CN=localhost"

# Build and start all services (Docker)
./refresh.sh    # builds image `flask_bot`, runs docker compose up

# Development (polling mode, no Docker)
# Set USE_WEBHOOK=False in .env
python3 -m bot

# Web dashboard only
uvicorn webapp.main:app --reload
```

## Environment Variables (`.env`)

Key variables from `env.dist`:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | From @BotFather |
| `ADMIN` | Telegram ID(s) of admins, comma-separated |
| `USE_WEBHOOK` | `False` for dev (polling), `True` for prod |
| `DOMAIN` | Bot webhook domain (must be non-empty, use `localhost` for dev) |
| `SERVER_URL` | Webhook path, e.g. `/webhook` |
| `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` | Payment credentials |
| `DB_NAME/USER/PASSWORD/HOST/PORT` | PostgreSQL connection (`DB_HOST=postgres` in Docker) |
| `REMNAWAVE_API_URL` / `REMNAWAVE_API_TOKEN` | Remnawave panel API endpoint + auth token |
| `REMNAWAVE_DEFAULT_SQUAD_UUID` | Default squad assigned to new users |
| `REMNAWAVE_PROXY_URL` | Optional HTTP/SOCKS5 proxy for Remnawave API calls |
| `REMNAWAVE_SUB_HOST` | Public hostname for subscription links (used by nginx `/sub/` redirect) |
| `CERT_FULLCHAIN_PATH` / `CERT_KEY_PATH` | Absolute paths to SSL certs (required for Docker volume mounts) |

## Architecture

```
nginx (host: 80 → HTTP, 8443 → HTTPS)
  ├── /yookassa  → flask_bot (8081)   — Telegram bot + YooKassa webhooks
  ├── /sub/*     → 302 redirect     — legacy links → REMNAWAVE_SUB_HOST
  ├── /import    → static HTML      — deeplink redirect page
  └── /          → flask_site (8000)  — FastAPI web dashboard

Remnawave is an external panel (REMNAWAVE_API_URL) — not part of this Docker stack.
All services share PostgreSQL on Docker network.
```

### Data flow

1. User sends Telegram command → `bot.py` dispatcher → `tgbot/handlers/` → `database/requests.py` (SQLAlchemy) → PostgreSQL
2. Payment: bot creates YooKassa payment → YooKassa POSTs webhook to bot port 8081 → `webhook_handlers.py` → subscription extended in Remnawave
3. Remnawave calls go through `remnawave/client.py` (`RemnawaveClient` — httpx-based, optional proxy support)
4. Remnawave squads/inbounds are detected dynamically via API — no hardcoded protocol names

### Key files

| File | Role |
|---|---|
| `bot.py` | Entry point — polling vs webhook mode, scheduler start |
| `loader.py` | Initializes bot instance, config, logger, Remnawave client (`remnawave_client`) |
| `config.py` | Typed config dataclasses loaded from `.env` |
| `db.py` | SQLAlchemy models: `User`, `Tariff`, `PromoCode`, `Channel` |
| `database/requests.py` | All async DB CRUD operations |
| `remnawave/client.py` | `RemnawaveClient` — wraps Remnawave API (add/modify/delete users, get squads/config profiles) |
| `tgbot/handlers/` | Routers split into `user/`, `admin/`, `support.py`, `webhook_handlers.py` |
| `tgbot/services/` | Service layer (subscription, payment, referral, promo, profile, support, user, admin stats) |
| `tgbot/services/scheduler.py` | APScheduler jobs — subscription expiry checks |
| `webapp/main.py` | FastAPI app with Jinja2 templates |
| `webapp/routers/` | `auth.py` (JWT login/register), `dashboard.py`, `payment.py` |

### Bot modes

- **Polling** (`USE_WEBHOOK=False`): for local dev, no external domain needed
- **Webhook** (`USE_WEBHOOK=True`): for production, requires `DOMAIN` + SSL

### Admin vs regular users

Admin Telegram IDs are set via `ADMIN` env var. Admins get access to:
- Broadcast to all users
- Manage tariffs, promo codes, channels
- View user statistics

Web dashboard users register separately with email (stored with negative user IDs to avoid collision with Telegram IDs).

## Docker Services

Defined in `docker-compose.yml` (all prefixed `flask_`):
- `flask_bot` — main bot container (image: `flask_bot`, port 8081)
- `flask_site` — web dashboard container (image: `flask_bot`, port 8000)
- `nginx` — reverse proxy (container: `flask_nginx`, ports 80 + 8443)
- `postgres` — database (container: `flask_postgres`, port 5432 localhost-only, healthcheck with `pg_isready`)

Logs: `docker logs flask_bot` / `docker logs flask_site`

Remnawave is **not** a service in this compose file — it's an external panel reached via `REMNAWAVE_API_URL` (optionally through `REMNAWAVE_PROXY_URL`). Marzban has been fully decommissioned (container, compose service, and nginx routes removed).

## Refactoring Plan

The codebase is being restructured from flat handlers into a layered architecture: Handler → Service → Repository → Database. Service files in `tgbot/services/` are being built out but handlers have not yet been migrated to use them.
