#!/bin/bash
# Сборка PDF-руководства менеджера: docs/manager-guide.pdf
#
#   ./tools/manager_guide/build.sh
#
# Что нужно:
#   * зависимости проекта в python3 (как для тестов; .env не нужен);
#   * playwright в отдельном окружении — по умолчанию tools/manager_guide/.venv
#     (python3 -m venv tools/manager_guide/.venv && tools/manager_guide/.venv/bin/pip install playwright),
#     или путь к его python в PW_PYTHON;
#   * установленный Google Chrome (playwright берёт его, браузеры не скачиваются).
#
# Скрипт поднимает стенд (stand.py) с демо-данными на SQLite, снимает экраны сайта и бота
# (capture.py, тексты — content.py) и печатает PDF. Перезапускать после правок интерфейса.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/tools/manager_guide"
PW_PYTHON="${PW_PYTHON:-$HERE/.venv/bin/python}"
PORT="${PORT:-8765}"
WORK="$(mktemp -d)"
trap 'kill "${STAND_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

if [ ! -x "$PW_PYTHON" ]; then
    echo "Нет python с playwright: $PW_PYTHON" >&2
    echo "  python3 -m venv $HERE/.venv && $HERE/.venv/bin/pip install playwright" >&2
    exit 1
fi

# TZ=UTC — как в контейнере: время в чеках и на экранах переводится в МСК один раз.
cd "$ROOT"
TZ=UTC python3 "$HERE/stand.py" --port "$PORT" --out "$WORK" > "$WORK/stand.log" 2>&1 &
STAND_PID=$!
for _ in $(seq 1 60); do
    grep -q STAND_READY "$WORK/stand.log" 2>/dev/null && break
    if ! kill -0 "$STAND_PID" 2>/dev/null; then cat "$WORK/stand.log" >&2; exit 1; fi
    sleep 0.5
done
grep -q STAND_READY "$WORK/stand.log" || { cat "$WORK/stand.log" >&2; exit 1; }

cd "$HERE"
TZ=UTC "$PW_PYTHON" capture.py --base "http://127.0.0.1:$PORT" --data "$WORK" --out "$ROOT/docs/manager-guide.pdf"
