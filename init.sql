-- Таблица пользователей
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    is_paid BOOLEAN DEFAULT FALSE,
    paid_until TIMESTAMP,
    referral_code TEXT,               -- Уникальный реферальный код (на свой аккаунт)
    referred_by BIGINT                -- telegram_id того, кто пригласил
);

-- Таблица платежей
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    yookassa_id TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT now()
);
