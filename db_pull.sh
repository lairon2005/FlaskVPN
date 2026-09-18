#!/usr/bin/env bash
# db_pull.sh — копирует базу данных с удалённого сервера в локальный Docker
#
# Использование:
#   ./db_pull.sh root@1.2.3.4
#   ./db_pull.sh root@1.2.3.4 /opt/FlaskVPN   # если проект в другом каталоге
#
# Что делает:
#   1. SSH на сервер → читает .env → docker exec pg_dump → стримит дамп сюда
#   2. Локально: пересоздаёт БД → восстанавливает дамп через docker exec psql

set -euo pipefail

# ── Аргументы ───────────────────────────────────────────────────────────────
REMOTE="${1:-}"
REMOTE_PATH="${2:-/root/FlaskVPN}"

if [ -z "$REMOTE" ]; then
  echo "Использование: $0 user@host [путь_к_проекту_на_сервере]"
  echo "Пример:        $0 root@1.2.3.4"
  exit 1
fi

# ── Читаем локальный .env ────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo "❌ Файл .env не найден. Запустите скрипт из корня проекта."
  exit 1
fi

# Парсим нужные переменные (не делаем source, чтобы не загрязнять окружение)
_parse_env() { grep -E "^${1}=" .env | head -1 | cut -d= -f2- | tr -d "'" | tr -d '"'; }

LOCAL_USER=$(_parse_env DB_USER)
LOCAL_DB=$(_parse_env DB_NAME)
LOCAL_PASS=$(_parse_env DB_PASSWORD)

if [ -z "$LOCAL_USER" ] || [ -z "$LOCAL_DB" ]; then
  echo "❌ Не удалось прочитать DB_USER / DB_NAME из .env"
  exit 1
fi

echo "═══════════════════════════════════════════════════════"
echo "  DB Pull: $REMOTE → localhost"
echo "  Локальная БД: $LOCAL_DB (пользователь: $LOCAL_USER)"
echo "═══════════════════════════════════════════════════════"

# ── Шаг 1: закрываем активные соединения к локальной БД ─────────────────────
echo ""
echo "1️⃣  Закрываю активные соединения к локальной БД..."
PGPASSWORD="$LOCAL_PASS" docker exec -i -e PGPASSWORD="$LOCAL_PASS" flask_postgres \
  psql -U "$LOCAL_USER" -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$LOCAL_DB' AND pid <> pg_backend_pid();" \
  > /dev/null 2>&1 || true

# ── Шаг 2: пересоздаём локальную БД ─────────────────────────────────────────
echo "2️⃣  Пересоздаю локальную БД..."
docker exec -i flask_postgres psql -U "$LOCAL_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"$LOCAL_DB\";" \
  -c "CREATE DATABASE \"$LOCAL_DB\";" > /dev/null

# ── Шаг 3: дамп с сервера и восстановление ───────────────────────────────────
echo "3️⃣  Дампирую с $REMOTE и восстанавливаю локально..."
echo "    (это может занять время в зависимости от размера БД...)"
echo ""

ssh -o BatchMode=no -o ConnectTimeout=10 "$REMOTE" "
  set -e
  # Читаем переменные из .env на сервере
  REMOTE_USER=\$(grep -E '^DB_USER=' $REMOTE_PATH/.env | head -1 | cut -d= -f2- | tr -d \\\"\\')
  REMOTE_DB=\$(grep -E '^DB_NAME=' $REMOTE_PATH/.env | head -1 | cut -d= -f2- | tr -d \\\"\\')
  REMOTE_PASS=\$(grep -E '^DB_PASSWORD=' $REMOTE_PATH/.env | head -1 | cut -d= -f2- | tr -d \\\"\\')

  if [ -z \"\$REMOTE_USER\" ] || [ -z \"\$REMOTE_DB\" ]; then
    echo '❌ Не удалось прочитать DB_USER / DB_NAME из .env на сервере' >&2
    exit 1
  fi

  echo \"    Серверная БД: \$REMOTE_DB (пользователь: \$REMOTE_USER)\" >&2

  # Дамп: схема + данные, без owner/acl (переносимо между пользователями)
  docker exec -e PGPASSWORD=\"\$REMOTE_PASS\" flask_postgres \
    pg_dump -U \"\$REMOTE_USER\" -d \"\$REMOTE_DB\" \
    --no-owner --no-acl --no-privileges \
    --verbose 2>/dev/null
" | docker exec -i flask_postgres psql -U "$LOCAL_USER" -d "$LOCAL_DB" -q

echo ""
echo "✅ Готово! База '$LOCAL_DB' восстановлена из $REMOTE"
echo ""
echo "Следующий шаг — примените миграции (на случай если схема новее дампа):"
echo "  python3 fix_db.py"
