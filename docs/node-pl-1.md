# Нода pl-1: `31.77.59.199` (ssh `expressvpn_node_pol`)

Введена 25.09.2026. Польша, astravps, Ubuntu 26.04, 1 vCPU, 2 ГБ RAM.
Главная особенность: **на хосте уже работает marzban-node другого проекта**
(ExpressVPN, мастер Marzban — `expressvpn_node_neth`, 13.143.232.16), и
Remnawave-нода стоит рядом с ним на свободных портах. Обе в `network_mode: host`.

```
443/tcp   rw-core   PL1-VISION  VLESS/REALITY/Vision, стил www.dhl.com
443/udp   rw-core   PL1-HY2     Hysteria2, self-signed CN=www.dhl.com
8444/tcp  rw-core   PL1-XHTTP   VLESS/XHTTP/REALITY, стил www.dhl.com
2222/tcp  rw-node   API ноды — iptables пускает только панель 13.143.165.30

2053/tcp  Marzban xray   Trojan TLS        ┐
8443/tcp  Marzban xray   VLESS REALITY     │ чужое, не трогать
62050     marzban-node   REST для мастера  │
62051     Marzban xray   API               ┘
```

## ⚠️ Соседство с Marzban

- Инбаунды marzban-node задаёт **мастер** на neth. Если там добавят инбаунд на
  443 или 8444, упадёт тот, кто стартует вторым. Порты, занятые Marzban, в
  `Pl1Config` использовать нельзя, и наоборот.
- Анти-флуд (`/root/node_antiflood_firewall.sh`) висит на OUTPUT и режет pps
  **для всего хоста**, включая трафик Marzban. Это сделано намеренно: абузу
  хостер выставляет серверу, а не проекту.
- BBR + fq включены на весь хост (`/etc/sysctl.d/99-bbr.conf`).
- Self-steal не делали: DNS `flaskvpn.ru` в Cloudflare, а Caddy-заглушке нужен
  был бы порт 80 и A-запись. Все три инбаунда маскируются под `www.dhl.com`
  (проверено `xray tls ping` с самой ноды: TLS 1.3, X25519MLKEM768).

## Что где лежит

| Что | Где |
|---|---|
| compose ноды | `/opt/remnanode/docker-compose.yml`, `SECRET_KEY` в `.env` рядом (600) |
| сертификат HY2 | `/etc/letsencrypt/selfsigned/pl1/` (10 лет, SHA-256 в host-записи как `pinnedPeerCertSha256`) |
| правила 2222 | `iptables INPUT`, переживают ребут через `antiflood-restore.service` |
| бэкап iptables до работ | `/root/deploy-backups/iptables.*.bak` |

На панели:

| Что | Значение |
|---|---|
| нода | `pl-1`, uuid `08a1249a-7187-4d47-af66-d8015d589c7f`, страна PL |
| профиль | `Pl1Config`, uuid `353f6fec-e77c-4e3b-9990-c5824358e4c1`; routing скопирован из `Fl1Config` (9 правил анти-флуда) |
| сквад | инбаунды добавлены в `FlaskVPN` (теперь 6: 3 от fl-1 + 3 от pl-1) |
| хосты | `🇵🇱 🔶 Основной`, `🇵🇱 🔸 Запасной`, `🇵🇱 ⚡ Турбо` (Польша) |
| ключи | `PL1_*` в `/root/.flaskvpn-panel-creds` |
| профиль целиком (с приватными ключами) | `/root/rw-backups/pl1_profile.json` |
| скрипт ввода | `/root/rw-backups/pl1_setup.py` (`gen` / `apply`, идемпотентный) |
| дамп таблиц до работ | `/root/rw-backups/before-pl1_*.sql` |

## Сквозной тест (25.09.2026)

С хоста панели, по JSON-подписке живого пользователя, routing вырезан:

| транспорт | скорость | exit-IP |
|---|---|---|
| Vision 443/tcp | 292 Мбит/с | 31.77.59.199 |
| XHTTP 8444 | 389 Мбит/с | 31.77.59.199 |
| HY2 443/udp | 257 Мбит/с | 31.77.59.199 |

Подписку для теста отдают только с заголовком `x-hwid` (иначе «App not
supported»). Бери HWID, **уже привязанный** к пользователю
(`hwid_user_devices`), иначе тест займёт ему слот устройства.

Проверять закрытость 2222 с Mac бесполезно, если на нём включён VPN-клиент в
TUN-режиме: он сам принимает любое TCP-соединение, и «открыт» даже заведомо
пустой порт. Проверять со стороннего сервера.

## Эксплуатация

```bash
# обновить ноду
cd /opt/remnanode && docker compose pull && docker compose up -d

# кто держит порты
ss -tulnp | grep -E ':(443|8444|2222|2053|8443|6205[01]) '

# сработал ли анти-флуд
/root/node_antiflood_firewall.sh status
```

Откат целиком: на панели удалить хосты, ноду и профиль `Pl1Config` (сквад
FlaskVPN вернуть к трём инбаундам fl-1), на ноде
`cd /opt/remnanode && docker compose down`. Marzban это не затрагивает.
