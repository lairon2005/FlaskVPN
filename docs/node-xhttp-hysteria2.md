# node-eu-1: добавление инбаундов Hysteria2 и XHTTP за Cloudflare

Передаточная записка по работам 31.07–01.08.2026 на хосте `node-eu-1`. Всё
описанное проверено на живом сервере; там, где вывод получен рассуждением, а не
замером, это отмечено явно.

---

## 1. Что за хост

Хост **203.0.113.40** (PowerRDP), Ubuntu 24.04, 4 vCPU, 3.8 ГБ RAM, BBR+fq
включены. Домен проекта — `flaskvpn.ru`.

**Это НЕ тот же хост, что `panel-server`.** Тот — `panel.flaskvpn.ru` (203.0.113.20),
другой проект, там Caddy. Здесь nginx 1.28. Оба у одного провайдера в одной /24,
то есть блокировка подсети убьёт сразу оба проекта.

### Состав стека

Всё в одном `/opt/remnawave/docker-compose.yml`:

| Контейнер | Роль |
|---|---|
| `remnawave` | панель, backend 2.8.1, `127.0.0.1:3000` |
| `remnawave-db` | PostgreSQL 18.3 |
| `remnawave-redis` | valkey |
| `remnawave-nginx` | host mode, слушает **только** unix-сокет `/dev/shm/nginx.sock` |
| `remnawave-subscription-page` | `127.0.0.1:3010` |
| `remnanode` | **нода**, host mode, Xray 26.6.27 |

Плюс отдельный стек `/opt/flaskvpn` (сайт на 8000, pg 16.4, redis).

⚠️ **Нода лежит в том же compose, что и панель.** Любой `docker compose down` в
`/opt/remnawave` роняет VPN целиком.

⚠️ **У `remnanode` выставлен `XRAY_JSON_STRICT=true`** — неизвестное поле в
конфиге означает, что ядро не стартует. Валидация до применения обязательна.

### Кто держит порты

```
443/tcp   rw-core (нода), VLESS+REALITY, self-steal SNI node.flaskvpn.ru
          не-VPN SNI → unix-сокет nginx с PROXY protocol (xver: 1)
443/udp   rw-core, Hysteria2
2053/tcp  rw-core, VLESS+XHTTP+TLS (за Cloudflare)
8443/tcp  rw-core, VLESS+XHTTP+TLS+VLESS Encryption — ВЫКЛЮЧЕН в подписке
80/tcp    ACME и HTTP-редирект
```

Конфиг Xray нода тянет по абстрактному сокету `@rwint-...`, **файла на диске нет**.
Смотреть только в БД:

```bash
docker exec remnawave-db psql -U "$PU" -d "$PD" -t -A \
  -c "SELECT jsonb_pretty(config) FROM config_profiles;"
```

---

## 2. Текущее состояние (на 01.08.2026)

### Инбаунды в профиле `StealConfig` (`7e873634-2cb7-41e0-8246-d885e187ad8b`)

| Тег | Протокол | Порт origin | Host-запись | Статус |
|---|---|---|---|---|
| `Steal` | VLESS/REALITY/Vision | 443/tcp | `Steal` | работает |
| `HY2` | Hysteria2 | 443/udp | `HY2` | работает |
| `XHTTP2` | VLESS/XHTTP/TLS | 2053 | `CDN` → клиент на **443** | работает |
| `XHTTP` | то же + VLESS Encryption | 8443 | `CDN-enc-off` | **выключен** |

Все четыре привязаны к ноде `Steal` и скваду `Default-Squad`. В подписке видны
три (`CDN-enc-off` скрыт).

### Почему `XHTTP` выключен

**Happ не умеет VLESS Encryption** (проверено на Happ 5.2.0/iOS): профиль не
подключается, пинг `n/a`. Доказано A/B — два инбаунда, идентичные во всём, кроме
`decryption`; оба исправно работали при проверке ядром Xray 26.6.27, то есть дело
в клиенте. Инбаунд оставлен в конфиге — включить, когда клиент обновит ядро.

**Цена:** Cloudflare терминирует TLS и видит проксируемый трафик. Для HTTPS-сайтов
это домены и объёмы, для HTTP и DNS — содержимое. Если понадобится защита от
самого CF при живой совместимости, единственный путь — протокол со своим
шифрованием поверх того же xhttp (**Shadowsocks-2022**; VLESS и Trojan своего
шифрования не имеют вообще). Xray и панель 2.8.1 такую связку поддерживают —
проверено по коду, вживую не пробовалось.

---

## 3. Порядок добавления инбаунда — рабочая последовательность

Именно в этом порядке; каждый шаг ловит свой класс ошибок.

### 3.1. Сначала отделить сеть от Xray

Поднять **временный TLS-сервер** на целевом порту с боевым сертификатом и сходить
на него снаружи. Это отсекает проблемы DNS / Cloudflare / ufw / SSL-mode от
проблем конфига Xray.

```bash
curl -sS -D- https://<домен>:<порт>/
# server: cloudflare + HTTP/2  = путь живой, режим SSL не Flexible
```

### 3.2. Валидация конфига ядром

```bash
docker cp /root/new.json remnanode:/tmp/new.json
docker exec remnanode xray run -test -c /tmp/new.json
```

Обязательно при `XRAY_JSON_STRICT=true`. Если ядро не найдёт файл сертификата,
оно не стартует — **вместе с ним лягут все остальные инбаунды**, они в одном
процессе.

### 3.3. Применить конфиг

```
PATCH http://127.0.0.1:3000/api/config-profiles
{"uuid": "<profile-uuid>", "config": {...весь xray-конфиг...}}
```

### 3.4. Привязать к ноде и скваду — ОБЯЗАТЕЛЬНО ВРУЧНУЮ

**Панель НЕ делает этого сама.** Без привязки порт вообще не поднимется.

```
PATCH /api/nodes
{"uuid": "<node>", "configProfile": {
    "activeConfigProfileUuid": "<profile>",
    "activeInbounds": ["<uuid>", "<uuid>", ...]}}

PATCH /api/internal-squads
{"uuid": "<squad>", "inbounds": ["<uuid>", "<uuid>", ...]}
```

⚠️ Оба принимают **полный список** uuid, а не дельту. Передашь только новый —
остальные отвяжутся.

Проверять: таблицы `config_profile_inbounds_to_nodes`, `internal_squad_inbounds`
и `ss -tulnp`.

### 3.5. Host-запись

```
POST /api/hosts
{"remark": "...", "address": "...", "port": 443, "path": "/...",
 "sni": "...", "host": "...", "alpn": "h2", "fingerprint": "chrome",
 "isDisabled": false, "securityLayer": "DEFAULT",
 "inbound": {"configProfileUuid": "<profile>", "configProfileInboundUuid": "<inbound>"}}
```

Без неё инбаунд работает, но **в подписку не попадает**.
Обновление — `PATCH /api/hosts` с `{"uuid": ..., <поля>}`, удаление —
`DELETE /api/hosts/<uuid>`.

### 3.6. Сквозной тест — единственный, которому можно верить

Взять конфиг из подписки, **выкинуть `freedom`/`blackhole` и весь `routing`**,
оставить один outbound, поднять `xray run` и сходить курлом через socks:

```bash
curl --socks5-hostname 127.0.0.1:10811 https://www.cloudflare.com/cdn-cgi/trace
# ip=<IP сервера> colo=... — значит трафик реально шёл туннелем
```

Без выпиливания `routing` клиент молча уйдёт через `direct`, и тест соврёт.

---

## 4. Доступ к API панели

Токен лежит **открытым текстом** в `/opt/remnawave/docker-compose.yml`, переменная
`REMNAWAVE_API_TOKEN` (не в `.env`!). Учесть при ротации.

`ProxyCheckMiddleware` рвёт соединение, пока не отправлены **все четыре** заголовка:

```bash
curl "http://127.0.0.1:3000/api/..." \
  -H "Authorization: Bearer $TOKEN" \
  -H "Host: panel.flaskvpn.ru" \
  -H "X-Forwarded-Proto: https" \
  -H "X-Forwarded-For: 127.0.0.1"
```

То же самое относится к `subscription-page` на `:3010` — без них покажется, что
апстрим лежит.

---

## 5. Cloudflare

### Порты

CF проксирует HTTPS только на **443, 2053, 2083, 2087, 2096, 8443** и ходит к
origin на том же порту, что запросил клиент.

### Origin Rule — как отдать клиенту 443, когда origin слушает другое

Нужен, потому что 443 на origin занят REALITY.

> Зона → **Rules** → **Origin Rules** → Create rule
> Фильтр: `Hostname` **equals** `<домен>` → выражение `(http.host eq "<домен>")`
> **Destination Port** → `Rewrite to` → `<порт origin>`
> Host Header / SNI / DNS Record — оставить **Preserve**

Есть на free-плане. Заводится **в каждой зоне отдельно**.

**Накладных расходов не вносит** — замерено: 443 через Origin Rule и порт origin
напрямую дают одинаковые ~270–310 Мбит/с.

### 🔴 Порт решает всё на мобильном

Профиль на **2053 на сотовой сети не работает вообще**, на Wi-Fi работает.
После перевода клиента на **443** работает везде. Операторы режут нестандартные
порты. **Клиентам отдавать только 443**, прочие порты CF годятся исключительно
как origin-порт за Origin Rule.

Имя домена при этом ни при чём: `cloud.flaskvpn.ru` открывается с той же сотовой
сети, хотя содержит `vpn`.

### Как проверить домен за 30 секунд, до всякой работы

Завести проксируемую A-запись и открыть **с телефона на сотовой сети**:

```
https://<домен>/cdn-cgi/trace     ← проверяемый
https://www.cloudflare.com/cdn-cgi/trace   ← контроль
```

Этот путь Cloudflare обслуживает сам, до origin запрос не идёт — сертификат и
инбаунд не нужны. Отделяет «режут это имя» от «режут CF целиком» и от «проблема
в клиенте».

### ufw: пускать на origin-порт только Cloudflare

```bash
for net in $(curl -fsS https://www.cloudflare.com/ips-v4) \
           $(curl -fsS https://www.cloudflare.com/ips-v6); do
  ufw allow proto tcp from "$net" to any port 2053 comment 'XHTTP via Cloudflare'
done
```

20 правил (14 v4 + 6 v6). Иначе origin отвечает сканерам напрямую и его находят.

---

## 6. XHTTP: факты из исходников Xray 26.6.27

### `mode` — серверная сторона работает как whitelist

`transport/internet/splithttp/hub.go`: при `mode: packet-up` сервер отвергает
stream-up и stream-one. Панель отдаёт клиентам **тот же** режим, что стоит в
инбаунде (`resolve-proxy-config.service.js`: `mode: settings?.mode ?? 'auto'`) —
host-запись его переопределить **не может**, в отличие от `path` и `host`.

### Замер режимов через Cloudflare (25 МБ, с самого сервера)

| Режим | Результат |
|---|---|
| `packet-up` | **285 Мбит/с**, отклик 239 мс |
| `stream-up` | **не работает** |
| `stream-one` | **не работает** |
| без туннеля | 566 Мбит/с (потолок канала) |

**`packet-up` — единственный рабочий режим за Cloudflare**, потому что CF
буферизует тело запроса и не отдаёт его потоком. Крутить тут нечего. Транспорт
сам по себе быстрый: если у клиента медленно, причина в его пути до CF.

⚠️ `mode: auto` в инбаунде **ломает клиентов**: по H2 клиент выберет stream-one,
который CF не пропускает.

### Проверка `Host` — точное совпадение одной строки

`IsValidHTTPHost` (`transport/internet/internet.go`) сравнивает с **одной**
строкой, списка не понимает. При пустом `host` проверка отключается целиком
(`len(h.host) > 0`).

**Отсюда приём переезда без даунтайма:** один инбаунд может обслуживать несколько
доменов — в `tlsSettings.certificates` кладём несколько пар (Xray выбирает по
SNI), а `host` в `xhttpSettings` убираем, иначе второй домен будет отбит. Клиент
подставит Host сам из своего адреса; в host-записи Remnawave поле `host` задаётся
каждому домену своё. Плата — теряется слабая анти-пробинг-проверка.

### Полный список полей `xhttpSettings`

`infra/conf/transport_internet.go`, `type SplitHTTPConfig`: `host`, `path`, `mode`,
`headers`, `xPadding*`, `uplinkHTTPMethod`, `sessionID*`, `seq*`, `uplinkData*`,
`noGRPCHeader`, `noSSEHeader`, `sc*`, `serverMaxHeaderBytes`, `xmux`,
`downloadSettings`, `extra`. Поле `extra` в host-записи Remnawave — это
`xhttp_extra_params` (jsonb).

### ⚠️ XHTTP+REALITY с локальным dest не работает

Известный баг (issue #5923, closed not planned). Поэтому за CDN идём через
обычный TLS-сертификат, а не steal-oneself.

---

## 7. VLESS Encryption

Панель **сама выводит** клиентский `encryption` из серверного `decryption`:
`common/helpers/xray-config/resolve-public-key.js` → `resolveEncryptionFromDecryption()`
(пропускает `none`). Отдельного поля в `hosts` нет и не нужно.

Генерация ключей:

```bash
docker exec remnanode xray vlessenc
```

В выводе два блока: первый — X25519, **второй — ML-KEM-768 (post-quantum)**.
Приватная строка `decryption` короткая (~117 симв.), публичная `encryption` —
~1610 симв., в подписку влезает нормально.

⚠️ **Happ это не понимает** — см. раздел 2.

---

## 8. Hysteria2 в Xray

```json
{
  "tag": "HY2", "listen": "0.0.0.0", "port": 443, "protocol": "hysteria",
  "settings": {"version": 2, "clients": []},
  "streamSettings": {
    "network": "hysteria", "security": "tls",
    "tlsSettings": {"serverName": "node.flaskvpn.ru", "certificates": [{
        "certificateFile": ".../fullchain.pem", "keyFile": ".../privkey.pem",
        "oneTimeLoading": false}]},
    "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60,
        "masquerade": {"type": "file", "dir": "/var/www/html"}}
  }
}
```

- `masquerade` — анти-пробинг: при провале аутентификации отдаёт статику вместо
  явного отказа. Типы: `404` / `file` / `proxy` / `string`.
- Поля `congestion`, `up`, `down`, `udphop` в этой версии **не функциональны** —
  только warning «moved to finalmask/quicParams».
- Реализация Hysteria2 в Xray молодая (~4 месяца). Держать как резерв, не как
  основной транспорт. Совместимость с Happ на реальном клиенте подтверждена
  (отклик 208 мс), но нагрузкой не проверялась.

---

## 9. Сертификаты

### Выпуск за оранжевой тучкой — только DNS-01

HTTP-01 за проксированием не проходит. Плагин `dns-cloudflare` установлен.

```bash
certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d <домен> --cert-name <домен> --non-interactive --agree-tos
```

Токен CF нужен с правами **`Zone:DNS:Edit` + `Zone:Zone:Read`** (второе — чтобы
плагин нашёл `zone_id`). Файл кредов обязан быть `600`.

### 🔴 Токен нельзя отзывать после выпуска

`certbot` записывает authenticator в `/etc/letsencrypt/renewal/<домен>.conf` и
использует его при **каждом продлении**. Отзыв = сертификат тихо протухнет через
~60 дней. Ротация только через перезапись файла новым токеном с теми же правами.

### Автоперечитывание без рестарта

`oneTimeLoading: false` (дефолт при `certificateFile`/`keyFile`) — Xray сам
перечитывает сертификаты с диска. Рестарт ноды при продлении не нужен.

### Монтирование в ноду

В `/opt/remnawave/docker-compose.yml` у сервиса `remnanode`:

```yaml
volumes:
  - /dev/shm:/dev/shm:rw
  - /etc/letsencrypt:/etc/letsencrypt:ro
  - /var/www/html:/var/www/html:ro
```

### Продление: разряженная мина

Был root-crontab, который каждое воскресенье делал
`certbot renew && cd /opt/remnawave && docker compose down && docker compose up`.
Проблемы: `certbot renew` возвращает 0 и когда ничего не обновилось → полный
down/up **каждую неделю** независимо от сертов; нода в том же compose →
еженедельный даунтайм VPN; `up` **без `-d`** → cron-процесс висит прикреплённым,
а его смерть = штатная остановка всего стека без автоподъёма.

**Cron удалён**, заменён на штатные хуки certbot:

`/etc/letsencrypt/renewal-hooks/deploy/10-flag-renewed.sh`
```sh
#!/bin/sh
touch /run/remnawave-cert-renewed
```

`/etc/letsencrypt/renewal-hooks/post/50-reload-remnawave.sh`
```sh
#!/bin/sh
[ -f /run/remnawave-cert-renewed ] || exit 0
rm -f /run/remnawave-cert-renewed
cd /opt/remnawave || exit 1
docker compose up -d --force-recreate remnawave-nginx
```

Пересоздание, а не reload: compose монтирует файлы по симлинку из `live/`, и
Docker резолвит его в момент старта контейнера. Deploy-хук вызывается на каждый
продлённый домен, post-хук — один раз за прогон, поэтому реальное действие в нём.
Продление выполняет встроенный `certbot.timer` (2 раза в сутки).

---

## 10. Грабли

**`pkill -f <шаблон>` по ssh убивает собственную сессию** — шаблон присутствует в
argv удалённого шелла. Писать `pkill -f "tls[_]probe.py"`: регулярка с классом не
совпадёт сама с собой.

**Перезапуск контейнеров рвёт установленные TCP-соединения** (Docker перекраивает
iptables NAT). SSH отваливается с кодом **255** ровно в момент применения
конфига — это не поломка. Применение и проверку разносить по отдельным вызовам.

Побочный эффект: `certbot renew --dry-run`, запущенный параллельно с
пересозданием контейнеров, падает с «Connection reset by peer» на http-01. Это
артефакт тайминга, а не проблема конфигурации.

**`.env` панели ломает `source`** — строка вида `...80]...` парсится shell'ом как
команда. Читать через
`grep -E '^KEY=' .env | cut -d= -f2- | tr -d '"'`.

**Сложные скрипты с кавычками не писать через heredoc по ssh** — вложенные
кавычки ломают here-document. Писать локально и `scp`.

**Имена колонок в БД проверять, а не угадывать.** Реальные: `si.inbound_uuid`
(не `config_profile_inbound_uuid`), `nodes.is_connected` (колонки `is_node_online`
не существует). Смотреть `information_schema.columns`.

---

## 11. 🔴 Методическая ошибка, стоившая часа работы

После смены порта пользователь сказал «на мобильном всё ещё нет» — я принял это
за опровержение версии с портом. На самом деле **в клиенте лежала старая подписка
со старым портом**. Дальше я достроил вывод «режут по имени домена» из результата,
которого мне не присылали (пришёл ответ только по новому домену), и объявил его
подтверждённым. Итог: зря выпущен сертификат и заведён лишний домен в чужой зоне,
потом всё откатывалось.

**Правила, выведенные отсюда:**

1. Перед любым выводом из клиентского теста — **убедиться, что подписка
   обновлена**. Спрашивать явно, а не предполагать.
2. **Неполученный результат ≠ отрицательный результат.** Если из трёх запрошенных
   проверок пришла одна — это одна проверка, а не три.
3. Не объявлять гипотезу подтверждённой, пока не отработал разделяющий тест.

Второй случай того же рода: применив конфиг, я проверил состояние через 6 секунд,
не увидел порта и заявил, что панель не отдала инбаунд ноде. На деле нода ещё
перезапускала ядро. **Ждать явно, а не мерить один раз сразу.**

---

## 12. Что осталось незакрытым

- **Скорость CDN у клиента.** На Wi-Fi рвано, хотя транспорт даёт 285 Мбит/с с
  сервера. Причина, скорее всего, в маршруте провайдера до Cloudflare — с
  сервера это не воспроизводится и не чинится настройками. Если подтвердится,
  CDN остаётся аварийным резервом, а за скоростью надо идти во второй
  REALITY-эндпоинт на отдельном IP.
- **`CDN-enc-off`** — включить, когда Happ научится VLESS Encryption.
- **Shadowsocks-2022 поверх XHTTP** — единственный способ закрыть видимость
  трафика для Cloudflare при живой совместимости. По коду поддерживается и
  ядром, и панелью; вживую не пробовалось.
- **`node.flaskvpn.ru` — CNAME на апекс**, то есть маскировочный домен ноды
  публично указывает на домен VPN-сервиса. Стоит развязать.
- **Маскировочный сайт** в `/var/www/html` — скачанный бесплатный шаблон с
  `<title>Page_3d6412a5</title>`. Палевно.

---

## 13. Бэкапы

Всё в `/root/deploy-backups/`, с датой в имени: `config_profiles.*.sql`
(`pg_dump -t config_profiles --data-only`), `docker-compose.yml.*.bak`,
`nginx.conf.*.bak`, `crontab.*.bak`, `ufw.*.bak`.

Откат конфига Xray — восстановить `config_profiles` из дампа и сделать
`PATCH /api/config-profiles` тем, что было.
