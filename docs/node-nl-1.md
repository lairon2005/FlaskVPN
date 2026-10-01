# Нода nl-1: `150.251.143.190` / `shop.flaskvpn.ru` (ssh `FlaskVPN_node_neth`)

Введена 01.10.2026. Нидерланды (Амстердам), astravps (AS219095), Ubuntu 26.04, 1 vCPU,
2 ГБ RAM, 30 ГБ диск, хост `astra--900131-preset1.astravps.co`. Чистый сервер, без соседей.

**Главное отличие от [pl-1](./node-pl-1.md) и [de-1](./node-de-1.md): это self-steal.**
pl-1/de-1 маскируются под чужой `www.dhl.com`; здесь REALITY отдаёт наблюдателю
**собственный** сайт-заглушку с настоящим сертификатом Let's Encrypt на `shop.flaskvpn.ru`.
Схема — из [remnawave-node-setup.md](./remnawave-node-setup.md) / [remnawave-node-cover.md](./remnawave-node-cover.md).

```
80/tcp    Caddy     сайт-заглушка + ACME HTTP-01 (контейнер remnawave-node-cover)
443/tcp   rw-core   NL1-VISION  VLESS/REALITY/Vision, target 127.0.0.1:8443, SNI shop.flaskvpn.ru
443/udp   rw-core   NL1-HY2     Hysteria2, настоящий LE-сертификат shop.flaskvpn.ru
8444/tcp  rw-core   NL1-XHTTP   VLESS/XHTTP/REALITY, target 127.0.0.1:8443, SNI shop.flaskvpn.ru
2222/tcp  rw-node   API ноды — iptables пускает только панель 13.143.165.30
127.0.0.1:8443  Caddy  TLS-target REALITY (сертификат домена)   ┐ только loopback,
127.0.0.1:8080  Caddy  HTTP-fallback                            ┘ наружу не торчат
```

MTU интерфейса 1500, PMTU проходит `-s 1472` — трогать не нужно (в отличие от PowerRDP).
BBR + fq — `/etc/sysctl.d/99-bbr.conf`.

## Как работает маскировка

Зонд, пришедший на 443 или 8444 с `SNI=shop.flaskvpn.ru`, проксируется REALITY на локальный
Caddy и получает настоящий сайт: сертификат LE (TLS 1.3, verify OK), `200`, ~25 КБ.
Проверено с стороннего сервера. С чужим SNI и без SNI Caddy просто закрывает соединение
(сертификата под них нет) — это штатное поведение схемы, а не поломка.

Клиентские хосты ходят по **IP** (как на остальных нодах), SNI — `shop.flaskvpn.ru`.
Поэтому A-запись должна оставаться на `150.251.143.190`, **DNS only** (без проксирования
Cloudflare), и закрывать/менять домен нельзя: на нём держится и сертификат, и маскировка.

HY2 использует тот же настоящий сертификат (`/etc/letsencrypt/live/shop.flaskvpn.ru/`,
каталог `/etc/letsencrypt` смонтирован в `remnanode` как `:ro`), поэтому в host-записи
**нет** `pinnedPeerCertSha256` — в отличие от pl-1/de-1 с самоподписанным.

## Что где лежит

| Что | Где |
|---|---|
| compose ноды | `/opt/remnanode/docker-compose.yml`, `SECRET_KEY` в `.env` рядом (600) |
| заглушка + Caddy | `/opt/remnawave-node-cover/` (`Caddyfile`, `www/`, `certs/`), контейнер `remnawave-node-cover` |
| установщик заглушки | `/usr/local/sbin/install-remnawave-node-cover` (копия `scripts/install_remnawave_node_cover.sh`) |
| сертификат | `/etc/letsencrypt/live/shop.flaskvpn.ru/`, до 30.12.2026, продлевает `certbot.timer` |
| хуки продления | `/etc/letsencrypt/renewal-hooks/deploy/50-…` (копия в `certs/` + рестарт Caddy), `60-…` (рестарт `remnanode`, чтобы HY2 взяла новый сертификат) |
| анти-флуд | `/root/node_antiflood_firewall.sh`, правила и 2222 переживают ребут через `antiflood-restore.service` |
| iptables до работ | `/root/deploy-backups/iptables.*.bak` |

На панели:

| Что | Значение |
|---|---|
| нода | `nl-1`, uuid `53699b4f-e484-470b-ba3b-5d1353dbd083`, страна NL |
| профиль | `Nl1Config`, uuid `948a7cee-318b-4f65-b4d1-af04213a3329`; routing скопирован из `Fl1Config` (анти-флуд) |
| сквад | инбаунды добавлены в `FlaskVPN` (теперь 12) |
| хосты | `🇳🇱 🔵 Основной (Нидерланды)`, `🔹 Запасной`, `🟠 Турбо` |
| профиль целиком (с приватными ключами) | `/root/rw-backups/nl1_profile.json` (600) |
| публичные ключи REALITY, shortId, путь XHTTP | `/root/rw-backups/nl1_meta.json` |
| скрипт ввода | `/root/rw-backups/nl1_setup.py` (`gen` / `apply`, идемпотентный; self-steal-версия `de1_setup.py`) |
| сквозной тест | `/root/rw-backups/nl1_e2e.py` |
| дамп БД до работ | `/root/rw-backups/before-nl1_20261001_102632.sql` |

## Сквозной тест (01.10.2026)

С хоста панели (Франкфурт), по JSON-подписке живого пользователя, routing вырезан,
источник — `proof.ovh.net/files/100Mb.dat`:

| транспорт | скорость | exit-IP |
|---|---|---|
| Vision 443/tcp | 347 Мбит/с | 150.251.143.190 |
| XHTTP 8444 | 253 Мбит/с | 150.251.143.190 |
| HY2 443/udp | 129 Мбит/с | 150.251.143.190 |

Нода `connected`, xray v26.7.28, certbot `renew --dry-run` проходит. Реального продления
(и срабатывания хука `60-…`) ещё не было — первое ожидается около 30.11.2026, после него
стоит убедиться, что HY2 жива.

## Эксплуатация

```bash
cd /opt/remnanode && docker compose pull && docker compose up -d   # обновить ноду
docker logs --tail 50 remnawave-node-cover                         # Caddy
certbot certificates --cert-name shop.flaskvpn.ru                  # срок сертификата
/root/node_antiflood_firewall.sh status                            # анти-флуд
```

Сменить внешний вид заглушки (новый случайный шаблон):

```bash
/usr/local/sbin/install-remnawave-node-cover --domain shop.flaskvpn.ru \
  --email <email> --refresh-template --yes
```

Откат: на панели удалить хосты, ноду `nl-1` и профиль `Nl1Config` (сквад вернуть к 9
инбаундам), на ноде `cd /opt/remnanode && docker compose down`.

## Нюансы

- Контактный email Let's Encrypt — личный адрес владельца проекта. Если появится
  служебная почта, поменять: `certbot update_account --email <новый>`.
- `sshd` на ноде — `PasswordAuthentication yes` и `PermitRootLogin yes` (дефолт хостера,
  так же на de-1). Вход по ключу работает, пароль не отключали.
- Порты `80`, `443`, `8444` открыты всем, `2222` — только панели. Других правил INPUT нет,
  `ufw` не установлен.
