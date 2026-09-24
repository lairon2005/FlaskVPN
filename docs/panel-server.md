# Панель FlaskVPN: `13.143.165.30` (ssh `FlaskVPN_Remnawave_Panel`)

Собрана 19.09.2026 как копия программной части server31 (панель VacVPN).
Ubuntu 24.04, 2 vCPU, 1.9 ГБ RAM + 2 ГБ swap, AS197283 PowerRDP, физически
Франкфурт (гео-база при этом отдаёт **US, Utah** — стриминги считают выход
американским).

На одном хосте живут **панель, выдача подписок, subhub и боевая нода** —
ровно как на server31.

```
443/tcp  ── remnanode (REALITY Vision, self-steal cdn.flaskvpn.ru)
              └── не-VPN SNI → unix /dev/shm/nginx.sock (PROXY-protocol) → Caddy
                    ├── panel.flaskvpn.ru → 127.0.0.1:3000  (за cookie-gate)
                    ├── sub.flaskvpn.ru   → 127.0.0.1:8088  (subhub → :3010)
                    ├── hub.flaskvpn.ru   → 127.0.0.1:8088  (админка subhub, /admin)
                    └── cdn.flaskvpn.ru   → /var/www/html   (заглушка + decoy)
443/udp  ── remnanode (Hysteria2, self-signed на www.dhl.com)
8444/tcp ── remnanode (XHTTP/REALITY, чужой стил www.dhl.com)
80/tcp   ── Caddy (ACME HTTP-01 + редиректы)
9443/tcp ── Caddy → панель БЕЗ cookie-gate, ufw пускает только 13.143.232.78
```

## Чем отличается от server31

| | server31 (VacVPN) | этот хост (FlaskVPN) |
|---|---|---|
| backend | 2.8.1 (`backend:2`) | **3.4.4** (`backend:latest`) |
| легаси-ссылки Marzban | включены | не нужны, выключены |
| API для бота | через cookie-gate на 443 | отдельный порт 9443, ufw на IP бота |
| стил на 443 | свой домен + заглушка | то же (`cdn.flaskvpn.ru`) |
| стил на 8444 / HY2 | www.lufthansa.com | www.dhl.com |

## Что сломала панель 3.4.4 в коде бота

Всё вылечено в `remnawave/client.py` (`_normalize_user`, `_panel_user_id`),
тесты — `tests/test_remnawave_v3_compat.py`:

| Что | 2.8.x | 3.4.4 |
|---|---|---|
| идентификатор пользователя | `uuid` (строка) | `id` (**число** 1, 2, 3…) |
| тело `PATCH /api/users` | `{"uuid": …}` | `{"id": <число>}` |
| тело `hwid/devices/delete[-all]` | `{"userUuid": …}` | `{"userId": <число>}` |
| брендинг подписки | `profileTitle` / `supportLink` | `customResponseHeaders` |
| админский JWT в API | работал | нужен заголовок `x-remnawave-client-type: browser` |
| API-токены | вечные | обязательный `expiresInDays` (выданы на 3650 дней) |
| `JWT_AUTH_LIFETIME` | минуты | **часы**, допустимо 12–168 |
| обязательный env | — | добавился `APP_SECRET` |

Идентификатор в БД бота — колонка `String(36)`, поэтому клиент переводит
число в строку на входе и обратно в число на выходе. Оба конца зафиксированы
тестами: строка в теле запроса даёт HTTP 400, int в varchar роняет asyncpg.

## MTU — проверять на любой новой машине PowerRDP

Интерфейс заявляет 1500, реальный PMTU пути — **1476** (GRE-подложка).
Симптом: TLS встаёт, тело ответа не приходит, `curl` висит до таймаута.
Лечится `ip link set ens3 mtu 1476`, закреплено
`/etc/systemd/network/10-ens3-mtu.link` (по MAC) + `ens3-mtu.service`.
Проверка: `ping -M do -s 1448 1.1.1.1` проходит, `-s 1449` — нет.

## Эксплуатация

```bash
# состояние
docker ps                      # 7 контейнеров: панель, db, redis, caddy, subpage, node, subhub
/root/node_antiflood_firewall.sh status

# правка Caddyfile: reload НЕ работает (admin off), только рестарт
docker run --rm -v /opt/remnawave/Caddyfile:/etc/caddy/Caddyfile:ro caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile   # env подставить!
docker restart remnawave-caddy

# откат subhub из тракта подписок — одна строка в Caddyfile:
# reverse_proxy {$SUBHUB_URL}  →  reverse_proxy {$SUB_BACKEND_URL}
```

Бэкапы: `/root/backup-flaskvpn.sh` по крону в 04:17 — `pg_dump` панели в
`/root/rw-backups/`, SQLite subhub в `/root/subhub-backups/`, 14 копий.

Секреты хоста (админ панели, API-токены, ключи REALITY, пароль админки
subhub, ссылка cookie-gate) лежат в `/root/.flaskvpn-panel-creds`, режим 600.

## Сквозной тест ноды

Проверять только с чужого быстрого клиента и обязательно выбрасывать из
конфига `routing`/`direct`/`block`, иначе тест соврёт при мёртвом туннеле.
Замеры 19.09.2026 с nl-2 (клиент сам качает 1.5 Гбит/с):

| транспорт | скорость | exit-IP |
|---|---|---|
| Vision 443/tcp | 524 Мбит/с | 13.143.165.30 |
| XHTTP 8444 | 456 Мбит/с | 13.143.165.30 |
| HY2 443/udp | 236 Мбит/с | 13.143.165.30 |

Сам хост качает 620–680 Мбит/с. Мерить с server31 нельзя — он сам отдаёт
единицы Мбит/с.
