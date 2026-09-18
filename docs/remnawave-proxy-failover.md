# Резервный маршрут до панели через прокси (failover)

Бот ходит к Remnawave-панели по двум маршрутам по очереди:

1. **Прямой** — как раньше: `bot → panel:443`, с ретраями GET (4 попытки, бюджет ~8 c).
2. **Через прокси** — включается автоматически, только если задан
   `REMNAWAVE_PROXY_URL` и прямой маршрут **не смог достучаться** до панели.

Реализация: [`remnawave/client.py`](../remnawave/client.py) (`RemnawaveClient._request`
/ `_request_via_route` / `_may_failover`). Если `REMNAWAVE_PROXY_URL` пуст —
поведение не меняется, работает только прямой маршрут.

## Когда происходит переключение

| Тип запроса | Ошибка прямого маршрута | Уходим на прокси? |
|---|---|---|
| `GET` (чтение) | любая транспортная ошибка / исчерпан временный статус 429/5xx | **Да** |
| `POST/PATCH/DELETE` (запись) | `ConnectTimeout` / `ConnectError` / `PoolTimeout` (соединение не установилось) | **Да** |
| `POST/PATCH/DELETE` (запись) | `ReadTimeout` / `ReadError` (ответ потерян после отправки) | **Нет** |

Логика в `_may_failover`: изменяющий запрос уводим на прокси только когда
**точно** ничего не применилось на панели (соединение не поднялось). Если ответ
потерян уже после отправки запроса — панель могла продлить подписку/списать
трафик, поэтому повтор через прокси **не** делаем, чтобы не задвоить операцию.

## Что это чинит, а что нет

**Помогает**, когда флапает сам путь `bot → panel` (фильтрация/маршрутизация по
IP бота, перегрузка фронт-слоя панели). Если прокси стоит на самом
panel-сервере, финальный хоп к `:443` идёт с локального адреса панели и минует
фильтрацию, привязанную к IP бота.

**Не помогает**, если у панели зависла VM целиком (ядро отвечает на ping/SYN, но
userspace мёртв) — тогда недоступен и прокси на том же сервере. Такое лечится
только перезагрузкой VM.

> Максимально надёжный вариант — поднять прокси на **третьем** сервере с хорошим
> маршрутом до панели. Ниже — вариант «прокси на самом panel-сервере», как проще
> всего для текущей инфраструктуры.

---

## Вариант через Xray-инбаунд на ноде (без SSH к серверу панели)

Подходит, когда к самому серверу панели нет SSH-доступа: SOCKS поднимается как
обычный **SOCKS-инбаунд Xray** на ноде, а конфиг правится из панели (Config
Profile). Затем `REMNAWAVE_PROXY_URL` указывает на этот инбаунд.

### ⚠️ Главное — на какой ноде

Фолловер помогает, только если у ноды с этим инбаундом **хороший маршрут до
панели**. Нельзя брать ноду, которая стоит **на бот-сервере** (bot-server,
`/opt/remnanode`): тогда хоп `нода → панель` пойдёт по тому же сбойному пути
`bot → panel`, что и прямой маршрут, и пользы не будет. Нужна нода на **другом**
сервере, у которого связь с панелью стабильнее, чем у бота.

### 1. Добавить SOCKS-инбаунд в Config Profile

Config Profile в Remnawave — это по сути сырой Xray-конфиг. Важно: Remnawave
**не принимает `protocol: "socks"`** (валидатор разрешает
`dokodemo-door, http, hysteria, mixed, shadowsocks, trojan, tun, tunnel, vless,
wireguard`). SOCKS живёт под именем **`mixed`** — в доке Remnawave это
«mixed(socks)», один порт принимает и SOCKS5, и HTTP. Схема `settings` — обычная
socks-овая. Для mixed «user management недоступен» — это ожидаемо, аккаунт задаём
руками прямо в JSON.

Добавь в массив `inbounds` профиля, назначенного нужной ноде:

```jsonc
{
  "tag": "socks-admin",
  "listen": "0.0.0.0",
  "port": 18443,               // свободный порт на ноде
  "protocol": "mixed",         // НЕ "socks" — Remnawave не примет
  "settings": {
    "auth": "password",         // без auth = открытый релей, обязательно password
    "accounts": [
      { "user": "rwproxy", "pass": "PROXY_PASS" }
    ],
    "udp": false                // боту нужен только TCP (HTTPS к API)
  },
  "sniffing": { "enabled": false }
}
```

`mixed` говорит по SOCKS5, поэтому в `.env` остаётся `socks5://...`. Если mixed
почему-то не подходит — в списке разрешённых есть и `http` (тогда инбаунд
`"protocol": "http"` с `settings.accounts`, а в `.env` — `http://...`; httpx
поддерживает оба).

Убедись, что в `routing` есть правило, уводящее этот инбаунд в прямой выход
(`freedom`/`direct`), иначе Xray может задропать его трафик:

```jsonc
{ "type": "field", "inboundTag": ["socks-admin"], "outboundTag": "direct" }
```

### 2. Не отдавать инбаунд пользователям

Инбаунд `socks-admin` — служебный. **Не добавляй** его тег ни в один
Internal Squad, чтобы он не попал в подписки VPN-клиентов. Он всё равно будет
слушать на ноде, но обычным юзерам не выдастся.

### 3. Открыть порт только боту

На сервере ноды (или в фаерволе хостера) разреши `port = 18443` **только** с IP
бот-сервера (`203.0.113.10`) и закрой для всех остальных — иначе это открытый
SOCKS для абузов. Если Xray в Docker — правило вешай на `DOCKER-USER` (Docker
обходит `ufw`):

```bash
sudo iptables -I DOCKER-USER -p tcp --dport 18443 ! -s 203.0.113.10 -j DROP
```

### 4. Ссылка прокси в .env бота

```dotenv
REMNAWAVE_PROXY_URL=socks5://rwproxy:PROXY_PASS@NODE_IP:18443
```

`NODE_IP` — публичный IP выбранной ноды. Перезапусти бота. Проверка — как в
разделе «Проверка» ниже, но целься в `NODE_IP:18443`.

> Если панель не даёт сохранить `socks`-инбаунд (строгая валидация UI) — тогда
> откатывайся на отдельный SOCKS-контейнер/Dante на сервере с хорошим маршрутом.

---

## Настройка SOCKS5-прокси на panel-сервере (Dante)

Выполняется **на сервере панели** (там, где `panel.flaskvpn.ru`). Порт SOCKS
откроем только для IP бот-сервера, вход — по логину/паролю, а исходящие
соединения ограничим самой панелью (чтобы прокси не стал открытым релеем).

Ниже — Debian/Ubuntu. Замени плейсхолдеры:

- `BOT_IP` — внешний IP бот-сервера (bot-server = `203.0.113.10`)
- `PANEL_IP` — IP самого panel-сервера (`203.0.113.20`)
- `PROXY_PORT` — любой свободный высокий порт, напр. `18443` (не 1080 — его сканят)
- `PROXY_PASS` — сгенерируй пароль **только из букв и цифр** (без `@ : / #`, иначе
  сломается URL прокси)

### 1. Пользователь для аутентификации

Dante-метод `username` проверяет пароль по системным учёткам. Заводим
служебного пользователя без права входа в систему:

```bash
sudo useradd -r -s /usr/sbin/nologin rwproxy
echo 'rwproxy:PROXY_PASS' | sudo chpasswd
```

### 2. Установка Dante

```bash
sudo apt-get update && sudo apt-get install -y dante-server
```

Узнай имя внешнего интерфейса (понадобится в конфиге):

```bash
ip route get 1.1.1.1 | grep -oP 'dev \K\S+'   # обычно eth0 / ens3
```

### 3. Конфиг `/etc/danted.conf`

```conf
logoutput: /var/log/danted.log

# Слушаем SOCKS на всех интерфейсах; доступ ограничим фаерволом и ACL ниже.
internal: 0.0.0.0 port = PROXY_PORT
# Исходящие соединения (к панели) — через внешний интерфейс из шага 2.
external: eth0

# Аутентификация по логину/паролю (системный пользователь rwproxy).
socksmethod: username
user.privileged: root
user.unprivileged: nobody

# --- Кто может подключиться к прокси: только бот-сервер ---
client pass {
    from: BOT_IP/32 to: 0.0.0.0/0
    log: connect disconnect error
}
client block {
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: connect error
}

# --- Куда прокси может ходить: только сама панель (:443) ---
socks pass {
    from: 0.0.0.0/0 to: PANEL_IP/32 port = 443
    protocol: tcp
    socksmethod: username
    log: connect disconnect error
}
socks block {
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: connect error
}
```

### 4. Запуск

```bash
sudo systemctl enable --now danted
sudo systemctl status danted --no-pager
```

### 5. Фаервол — открыть порт только боту

```bash
# ufw:
sudo ufw allow from BOT_IP to any port PROXY_PORT proto tcp
# или iptables:
sudo iptables -A INPUT -p tcp --dport PROXY_PORT -s BOT_IP -j ACCEPT
sudo iptables -A INPUT -p tcp --dport PROXY_PORT -j DROP
```

---

## Прописать прокси боту

На бот-сервере в `.env`:

```dotenv
REMNAWAVE_PROXY_URL=socks5://rwproxy:PROXY_PASS@PANEL_IP:PROXY_PORT
```

`socks5://` достаточно: httpx резолвит DNS на стороне прокси (как `socks5h`),
отдельная схема не нужна. Перезапусти бота (`./refresh.sh` или
`docker compose restart flask_bot`).

## Проверка

С **бот-сервера** (bot-server) — прокси должен доставать панель:

```bash
curl -sv -x socks5h://rwproxy:PROXY_PASS@PANEL_IP:PROXY_PORT \
     https://panel.flaskvpn.ru/api/... -o /dev/null
```

С любого другого IP то же подключение должно **отклоняться** фаерволом/ACL.

В логах бота при срабатывании фолловера появится:

```
[remnawave] route 'direct' unreachable (ConnectTimeout); failing over: method=GET path=/api/...
```

## Проще: SOCKS контейнером на том же сервере (рекомендуется)

Панель уже крутится в Docker, поэтому не нужно ставить пакеты на хост — прокси
поднимается одним контейнером. Образ [`serjs/go-socks5-proxy`](https://github.com/serjs/socks5-server)
умеет и логин/пароль, и ограничение egress по FQDN (`ALLOWED_DEST_FQDN`), так что
по безопасности не уступает Dante:

```bash
docker run -d --name rwproxy --restart unless-stopped \
  -p PROXY_PORT:1080 \
  -e PROXY_USER=rwproxy \
  -e PROXY_PASSWORD=PROXY_PASS \
  -e ALLOWED_DEST_FQDN='^panel\.flaskvpn\.ru$' \
  serjs/go-socks5-proxy
```

`ALLOWED_DEST_FQDN` — регэксп разрешённых назначений: прокси сможет ходить только
на панель и не станет открытым релеем даже при утечке пароля.

И **обязательно** ограничь порт фаерволом только для `BOT_IP` (см. шаг 5 выше) —
Docker сам пробрасывает порт в обход `ufw`, так что правило лучше вешать на
`iptables`/`DOCKER-USER`:

```bash
sudo iptables -I DOCKER-USER -p tcp --dport PROXY_PORT ! -s BOT_IP -j DROP
```

`.env` — тот же `REMNAWAVE_PROXY_URL=socks5://rwproxy:PROXY_PASS@PANEL_IP:PROXY_PORT`.

> Примечание: сама **Remnawave-панель встроенного SOCKS не имеет** — это
> control-plane, а не прокси. SOCKS умеет Xray, но он живёт на **нодах** (одна
> нода вообще на бот-сервере), поэтому маршрут через неё всё равно шёл бы по тому
> же сбойному пути `bot → panel` и не помог бы. Нужен отдельный слушатель именно
> на panel-сервере — контейнер выше и есть самый лёгкий способ.
