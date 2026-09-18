# fix_db.py — миграция: приводит БД к текущей версии db.py
import asyncio
from sqlalchemy import text
from db import async_engine


async def fix_database():
    print("🔄 Подключение к базе данных...")
    async with async_engine.begin() as conn:

        # ── 1. Таблица users: старые колонки (могут уже существовать) ───────
        print("\n📋 Таблица users — старые поля...")
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255) UNIQUE;"
        ))
        print("  ✅ email")

        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);"
        ))
        print("  ✅ password_hash")

        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_code VARCHAR(10);"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_code_expire TIMESTAMP WITHOUT TIME ZONE;"
        ))
        print("  ✅ reset_code / reset_code_expire")

        # ── 2. Таблица users: новые колонки верификации email ────────────────
        print("\n📋 Таблица users — колонки верификации email...")
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_code VARCHAR(10);"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_code_expire TIMESTAMP WITHOUT TIME ZONE;"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified BOOLEAN NOT NULL DEFAULT FALSE;"
        ))
        print("  ✅ verification_code / verification_code_expire / is_email_verified")

        # ── 3. Таблица payments ──────────────────────────────────────────────
        print("\n📋 Создаю таблицу payments...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS payments (
                id                  BIGSERIAL PRIMARY KEY,
                yookassa_payment_id VARCHAR NOT NULL UNIQUE,
                user_id             BIGINT NOT NULL REFERENCES users(user_id),
                tariff_id           BIGINT NOT NULL REFERENCES tariffs(id),
                original_amount     FLOAT NOT NULL,
                final_amount        FLOAT NOT NULL,
                promo_code          VARCHAR,
                discount_percent    INTEGER NOT NULL DEFAULT 0,
                status              VARCHAR NOT NULL DEFAULT 'pending',
                source              VARCHAR NOT NULL DEFAULT 'bot',
                created_at          TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                completed_at        TIMESTAMP WITHOUT TIME ZONE
            );
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_payments_yookassa_payment_id ON payments(yookassa_payment_id);"
        ))
        print("  ✅ payments")

        # ── 4. Таблица user_payment_methods (сохранённые карты / автоплатёж) ──
        print("\n📋 Создаю таблицу user_payment_methods...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_payment_methods (
                id                          BIGSERIAL PRIMARY KEY,
                user_id                     BIGINT NOT NULL UNIQUE
                    REFERENCES users(user_id) ON DELETE CASCADE,
                yookassa_payment_method_id  VARCHAR NOT NULL UNIQUE,
                card_last4                  VARCHAR(4),
                card_type                   VARCHAR,
                auto_renew_enabled          BOOLEAN NOT NULL DEFAULT TRUE,
                renew_tariff_id             BIGINT REFERENCES tariffs(id),
                fail_count                  INTEGER NOT NULL DEFAULT 0,
                created_at                  TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
            );
        """))
        print("  ✅ user_payment_methods")

        # ── 7. Индексы для когортных метрик (дашборд "Когорты / метрики") ────
        # Ускоряют: подзапрос "первый успешный платёж пользователя",
        # выборку когорты по subscription_end_date, подсчёт реф-стартов.
        print("\n📋 Создаю индексы для когортных метрик...")
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_payments_user_id_status ON payments(user_id, status);"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_payments_completed_at ON payments(completed_at);"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_payments_tariff_id ON payments(tariff_id);"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_users_subscription_end_date ON users(subscription_end_date);"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_users_referrer_id ON users(referrer_id);"
        ))
        print("  ✅ индексы payments/users для когортных метрик")

        # ── 8. Таблица lifecycle_messages (трекер касаний lifecycle-рассылок) ─
        print("\n📋 Создаю таблицу lifecycle_messages...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS lifecycle_messages (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                series     VARCHAR(32) NOT NULL,
                step       VARCHAR(64) NOT NULL,
                sent_at    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_lifecycle_user_series_step UNIQUE (user_id, series, step)
            );
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_lifecycle_messages_user_series "
            "ON lifecycle_messages(user_id, series);"
        ))
        print("  ✅ lifecycle_messages")

        # ── 9. Таблица tariffs: квота трафика / лоялти-цена / подсветка (Тарифы 2.0) ─
        print("\n📋 Таблица tariffs — квота трафика, лоялти-цена, подсветка (Тарифы 2.0)...")
        await conn.execute(text(
            "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS data_limit_gb INTEGER;"
        ))
        await conn.execute(text(
            "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS loyalty_price DOUBLE PRECISION;"
        ))
        await conn.execute(text(
            "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS is_highlighted BOOLEAN NOT NULL DEFAULT FALSE;"
        ))
        print("  ✅ data_limit_gb / loyalty_price / is_highlighted")

        # ── 10. Таблица users: миграция Marzban → Remnawave ──────────────────
        # marzban_username → vpn_username (переименование, идемпотентно —
        # если колонка уже переименована, IF EXISTS/IF NOT EXISTS не даст упасть).
        print("\n📋 Таблица users — миграция Marzban → Remnawave...")
        await conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='users' AND column_name='marzban_username'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='users' AND column_name='vpn_username'
                ) THEN
                    ALTER TABLE users RENAME COLUMN marzban_username TO vpn_username;
                END IF;
            END $$;
        """))
        print("  ✅ marzban_username → vpn_username")

        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_username VARCHAR;"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS remnawave_uuid VARCHAR(36);"
        ))
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'users_vpn_username_key'
                ) THEN
                    BEGIN
                        ALTER TABLE users ADD CONSTRAINT users_vpn_username_key UNIQUE (vpn_username);
                    EXCEPTION WHEN duplicate_table OR unique_violation THEN
                        NULL;
                    END;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'users_remnawave_uuid_key'
                ) THEN
                    BEGIN
                        ALTER TABLE users ADD CONSTRAINT users_remnawave_uuid_key UNIQUE (remnawave_uuid);
                    EXCEPTION WHEN duplicate_table OR unique_violation THEN
                        NULL;
                    END;
                END IF;
            END $$;
        """))
        print("  ✅ vpn_username / remnawave_uuid")

        # ── 11. Таблица users: флаг активности для рассылок ──────────────────
        # is_active=False → пользователь заблокировал бота; джобы рассылок его
        # пропускают (см. scheduler.send_reminder / _send_lifecycle_message).
        # Существующие строки получают TRUE — все считаются активными.
        print("\n📋 Таблица users — флаг активности (is_active)...")
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;"
        ))
        print("  ✅ is_active")

        # ── 12. Таблица tariffs: цена в Telegram Stars (Mini App, фаза 3.2) ──
        # NULL = оплата Stars для тарифа недоступна (кнопка "⭐" скрыта в tariffs.html).
        print("\n📋 Таблица tariffs — price_stars (оплата Telegram Stars)...")
        await conn.execute(text(
            "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS price_stars INTEGER;"
        ))
        print("  ✅ price_stars")

        # ── 13. Таблица payments: telegram_payment_charge_id (Stars-платежи) ──
        # Нужен verbatim для рефандов через Bot API refundStarPayment. UNIQUE —
        # тот же механизм идемпотентности, что и yookassa_payment_id.
        print("\n📋 Таблица payments — telegram_payment_charge_id (Stars-платежи)...")
        await conn.execute(text(
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS telegram_payment_charge_id VARCHAR;"
        ))
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'payments_telegram_payment_charge_id_key'
                ) THEN
                    BEGIN
                        ALTER TABLE payments ADD CONSTRAINT payments_telegram_payment_charge_id_key
                            UNIQUE (telegram_payment_charge_id);
                    EXCEPTION WHEN duplicate_table OR unique_violation THEN
                        NULL;
                    END;
                END IF;
            END $$;
        """))
        print("  ✅ telegram_payment_charge_id")

        # ── 14. Таблица used_promo_codes: UNIQUE(user_id, promo_code_id) ─────
        # Закрывает TOCTOU-гонку между PromoCodeService.validate() и use():
        # конкурентные апдейты от Telegram могли оба пройти has_user_used() до
        # коммита первой строки и погасить один промокод дважды (security review
        # 2026-07-26). PromoCodeRepository.try_claim() полагается на этот индекс
        # через INSERT ... ON CONFLICT DO NOTHING.
        #
        # В проде уже могли накопиться дубли (user_id, promo_code_id) от прошлых
        # двойных погашений — без дедупа ADD CONSTRAINT упадёт на существующих
        # данных. Сначала оставляем в каждой группе строку с минимальным id,
        # остальные удаляем, и только потом добавляем ограничение.
        print("\n📋 Таблица used_promo_codes — UNIQUE(user_id, promo_code_id)...")
        await conn.execute(text("""
            DELETE FROM used_promo_codes upc
            USING used_promo_codes dup
            WHERE upc.user_id = dup.user_id
              AND upc.promo_code_id = dup.promo_code_id
              AND upc.id > dup.id;
        """))
        print("  ✅ дубли (user_id, promo_code_id) удалены")

        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_used_promo_user_code'
                ) THEN
                    BEGIN
                        ALTER TABLE used_promo_codes ADD CONSTRAINT uq_used_promo_user_code
                            UNIQUE (user_id, promo_code_id);
                    EXCEPTION WHEN duplicate_table OR unique_violation THEN
                        NULL;
                    END;
                END IF;
            END $$;
        """))
        print("  ✅ uq_used_promo_user_code")

        # ── 15. Докупка доп. устройств ───────────────────────────────────────
        # users.extra_devices — сколько слотов сверх базового лимита оплачено;
        # payments.kind/extra_devices — чтобы вебхук знал, за что пришли деньги
        # (kind='devices' не продлевает подписку и приходит без тарифа);
        # app_settings — цена слота и лимиты, редактируемые из админки бота.
        print("\n📋 Докупка доп. устройств...")
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS extra_devices INTEGER NOT NULL DEFAULT 0;"
        ))
        print("  ✅ users.extra_devices")

        await conn.execute(text(
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'subscription';"
        ))
        await conn.execute(text(
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS extra_devices INTEGER NOT NULL DEFAULT 0;"
        ))
        print("  ✅ payments.kind / payments.extra_devices")

        # Платёж за одни только слоты приходит без тарифа — снимаем NOT NULL.
        await conn.execute(text(
            "ALTER TABLE payments ALTER COLUMN tariff_id DROP NOT NULL;"
        ))
        print("  ✅ payments.tariff_id → NULL допустим")

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key        VARCHAR(64) PRIMARY KEY,
                value      VARCHAR(255) NOT NULL,
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
            );
        """))
        print("  ✅ app_settings")

    print("\n🎉 Миграция завершена успешно!")


if __name__ == "__main__":
    asyncio.run(fix_database())
