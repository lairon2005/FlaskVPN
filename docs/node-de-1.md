# Нода de-1: `150.241.66.227` (ssh `FlaskVPN_node_de`)

Введена 30.09.2026. Германия, Ubuntu 24.04, 2 ГБ RAM, 40 ГБ диск, хост `my-server-1.tihost.com`.
Чистый сервер, без соседей. Введена тем же способом, что и [pl-1](./node-pl-1.md).

```
443/tcp   rw-core   DE1-VISION  VLESS/REALITY/Vision, стил www.dhl.com
443/udp   rw-core   DE1-HY2     Hysteria2, self-signed CN=www.dhl.com
8444/tcp  rw-core   DE1-XHTTP   VLESS/XHTTP/REALITY, стил www.dhl.com
2222/tcp  rw-node   API ноды — iptables пускает только панель 13.143.165.30
```

MTU интерфейса уже 1476 (PMTU проходит `-s 1448`), трогать не нужно.
BBR + fq — `/etc/sysctl.d/99-bbr.conf`. Self-steal не делали (как на pl-1).

## Что где лежит

| Что | Где |
|---|---|
| compose ноды | `/opt/remnanode/docker-compose.yml`, `SECRET_KEY` в `.env` рядом (600) |
| сертификат HY2 | `/etc/letsencrypt/selfsigned/de1/` (10 лет) |
| анти-флуд | `/root/node_antiflood_firewall.sh`, правила переживают ребут через `antiflood-restore.service` |
| iptables до работ | `/root/deploy-backups/iptables.*.bak` |

На панели:

| Что | Значение |
|---|---|
| нода | `de-1`, uuid `7c35fb28-ed07-4214-89f7-a569f612e8c8`, страна DE |
| профиль | `De1Config`, uuid `c19b0e87-63c1-4096-bd0f-b5844921c369`; routing скопирован из `Fl1Config` |
| сквад | инбаунды добавлены в `FlaskVPN` (теперь 9) |
| хосты | `🇩🇪 🔵 Основной (Германия 2)`, `🔹 Запасной`, `🟠 Турбо` |
| профиль целиком (с приватными ключами) | `/root/rw-backups/de1_profile.json` |
| скрипт ввода | `/root/rw-backups/de1_setup.py` (`gen` / `apply <sha256 cert HY2>`; fingerprint — hex без двоеточий) |
| дамп БД до работ | `/root/rw-backups/before-de1_*.sql` |

Проверено: нода `connected`, xray v26.7.28 поднят, порты 443/tcp, 443/udp, 8444 слушают,
с панели 443/8444/2222 открыты, `xray tls ping www.dhl.com` с ноды успешен.
Сквозной тест скорости по подписке не делался.

## Эксплуатация

```bash
cd /opt/remnanode && docker compose pull && docker compose up -d   # обновить
/root/node_antiflood_firewall.sh status                            # анти-флуд
```

Откат: на панели удалить хосты, ноду и профиль `De1Config` (сквад вернуть к 6 инбаундам),
на ноде `cd /opt/remnanode && docker compose down`.
