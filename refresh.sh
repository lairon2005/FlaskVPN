#!/bin/bash
set -euo pipefail

# Образ общий для бота и сайта. Остальные сервисы намеренно не собираем и
# не пересоздаём: действующий Marzban и PostgreSQL должны продолжать работу.
docker compose build flask_bot

docker compose up -d --no-deps --force-recreate flask_bot flask_site

# Одноразовый контейнер nginx рендерит template в общий bind mount и проверяет
# результат. Зависимости не запускаются и действующий nginx не прерывается.
docker compose run --rm --no-deps -T nginx nginx -t

# Действующий nginx перечитывает уже проверенную конфигурацию. Сам контейнер,
# Marzban и PostgreSQL не пересоздаются и не перезапускаются.
docker compose exec -T nginx nginx -t
docker compose exec -T nginx nginx -s reload
