#!/bin/bash
# Сборка Tailwind через standalone-бинарник — без npm, node_modules и package.json.
# Бинарник скачивается в tools/bin/ (gitignored) при первом запуске, собранный
# CSS коммитится в репозиторий: образ flask_bot остаётся чисто питоновским,
# Dockerfile и refresh.sh не меняются.
#
#   ./tools/tailwind.sh          — одна сборка (minify)
#   ./tools/tailwind.sh --watch  — пересборка при правке шаблонов
set -euo pipefail

TAILWIND_VERSION="v4.3.3"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/tools/bin/tailwindcss"
SRC="$ROOT/webapp/static/css/src/app.css"
OUT="$ROOT/webapp/static/css/app.css"

# Имя ассета в релизе tailwindlabs/tailwindcss зависит от платформы.
detect_asset() {
    local os arch
    case "$(uname -s)" in
        Darwin) os="macos" ;;
        Linux)  os="linux" ;;
        *) echo "Неизвестная ОС: $(uname -s)" >&2; exit 1 ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) arch="arm64" ;;
        x86_64|amd64)  arch="x64" ;;
        *) echo "Неизвестная архитектура: $(uname -m)" >&2; exit 1 ;;
    esac
    echo "tailwindcss-${os}-${arch}"
}

if [ ! -x "$BIN" ]; then
    asset="$(detect_asset)"
    echo "Скачиваю Tailwind ${TAILWIND_VERSION} (${asset})…"
    mkdir -p "$(dirname "$BIN")"
    curl -fsSL -o "$BIN" \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
    chmod +x "$BIN"
fi

if [ "${1:-}" = "--watch" ]; then
    exec "$BIN" --input "$SRC" --output "$OUT" --watch
fi

"$BIN" --input "$SRC" --output "$OUT" --minify
echo "Собрано: webapp/static/css/app.css ($(wc -c < "$OUT" | tr -d ' ') байт)"
