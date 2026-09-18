# Настройка новой Remnawave Node

Эта инструкция описывает полный ввод новой ноды в эксплуатацию:

1. подготовка DNS и сервера;
2. создание ноды в Remnawave Panel;
3. запуск официального контейнера `remnanode`;
4. установка Caddy, сайта-заглушки и сертификата Let's Encrypt;
5. подключение заглушки к Xray REALITY;
6. итоговая проверка и обслуживание.

Инструкция рассчитана на Ubuntu/Debian, установленный Remnawave Panel и
отдельный сервер ноды с правами `root` или `sudo`.

> Скрипт заглушки не устанавливает Remnawave Node, не меняет
> `/opt/remnanode/docker-compose.yml` и не редактирует Xray Config Profile.

## 1. Подготовить данные

Для каждой ноды нужны:

- отдельный поддомен, например `node-de.example.com`;
- публичный IPv4 сервера ноды;
- SSH-доступ к серверу;
- публичный IPv4 сервера Remnawave Panel;
- email для уведомлений Let's Encrypt.

Для команд с рабочего компьютера удобно определить переменные:

```bash
NODE_SSH=server144
NODE_DOMAIN=node-de.example.com
LE_EMAIL=admin@example.com
```

Значение `NODE_SSH` может быть SSH-алиасом из `~/.ssh/config`, IP-адресом или
именем вида `root@203.0.113.10`.

## 2. Настроить DNS

Создайте A-запись, указывающую непосредственно на IPv4 ноды:

```text
node-de.example.com -> 203.0.113.10
```

Если DNS управляется через Cloudflare, выберите **DNS only** — серое облако.
Обычный Cloudflare Proxy не проксирует произвольный VLESS/REALITY TCP-трафик.

Проверьте DNS и внешний адрес сервера:

```bash
dig +short A "$NODE_DOMAIN"
ssh "$NODE_SSH" 'curl -4fsS https://api.ipify.org; echo'
```

Оба результата должны содержать один и тот же IPv4. Перед выпуском сертификата
дождитесь обновления DNS у публичных резолверов:

```bash
dig +short A "$NODE_DOMAIN" @1.1.1.1
dig +short A "$NODE_DOMAIN" @8.8.8.8
```

Для этой схемы AAAA-запись не нужна. Если она существует, IPv6 должен вести на
эту же ноду и принимать TCP/80 и TCP/443; иначе удалите ошибочную AAAA-запись.

## 3. Создать ноду в Remnawave Panel

В панели откройте `Nodes` → `Management` и нажмите `+`:

1. укажите название и адрес новой ноды;
2. задайте `Node Port`, обычно `2222`;
3. сохраните сгенерированный секрет;
4. нажмите `Copy docker-compose.yml`;
5. пока не завершайте мастер — Config Profile выбирается после запуска ноды.

В поле адреса можно использовать домен или IP. Для клиентов удобнее домен, но
он должен разрешаться непосредственно в IP ноды. Значение `serverNames`/SNI в
REALITY-профиле должно совпадать с доменом сертификата заглушки.

## 4. Установить Remnawave Node

Если Docker ещё не установлен, используйте команду из официальной инструкции:

```bash
sudo curl -fsSL https://get.docker.com | sh
```

Создайте каталог ноды:

```bash
sudo mkdir -p /opt/remnanode
cd /opt/remnanode
```

Создайте `/opt/remnanode/docker-compose.yml` и вставьте содержимое, скопированное
из панели:

```bash
sudo nano /opt/remnanode/docker-compose.yml
```

Не используйте пример секретного ключа из документации. Compose-файл должен
содержать уникальный `SECRET_KEY`, сгенерированный панелью для этой ноды.

Проверьте и запустите контейнер:

```bash
cd /opt/remnanode
sudo docker compose config -q
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail 100
```

Ожидаемый контейнер — `remnanode`, а API ноды обычно слушает TCP/2222:

```bash
sudo ss -ltnp | grep ':2222 '
```

Вернитесь в мастер Remnawave Panel, нажмите `Next`, выберите Config Profile и
завершите создание ноды. Статус ноды в панели должен стать подключённым.

## 5. Настроить firewall

Нужные входящие порты:

| Порт | Назначение | Откуда разрешать |
|---|---|---|
| SSH-порт | администрирование | только доверенные адреса, если возможно |
| TCP/80 | сайт и HTTP-01 Let's Encrypt | из интернета |
| TCP/443 | пользовательский Xray inbound | из интернета |
| `NODE_PORT`, обычно TCP/2222 | связь панели с нодой | только IP панели |

Порты `8080` и `8443` открывать не нужно: Caddy привязывает их только к
`127.0.0.1`.

Скрипт заглушки намеренно не меняет UFW/iptables, чтобы не оборвать SSH-сессию.
Если UFW уже активен, сначала убедитесь, что фактический SSH-порт разрешён. Затем
можно добавить правила, подставив реальный адрес панели:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow from PANEL_PUBLIC_IP to any port 2222 proto tcp
sudo ufw status numbered
```

Не включайте UFW вслепую на удалённом сервере. Сначала проверьте отдельное правило
для SSH. Если для TCP/2222 уже существует общее разрешающее правило, удаляйте его
только после проверки доступа ноды со стороны панели.

## 6. Установить сайт-заглушку и сертификат

На рабочем компьютере скопируйте установщик на ноду:

```bash
scp scripts/install_remnawave_node_cover.sh \
  "$NODE_SSH:/tmp/install_remnawave_node_cover.sh"
```

Подключитесь к серверу и установите постоянную копию скрипта:

```bash
ssh "$NODE_SSH"
sudo install -o root -g root -m 0755 \
  /tmp/install_remnawave_node_cover.sh \
  /usr/local/sbin/install-remnawave-node-cover
```

Задайте значения уже на сервере ноды:

```bash
NODE_DOMAIN=node-de.example.com
LE_EMAIL=admin@example.com
INSTALL_UNIT="remnawave-node-cover-install-$(date +%s)"
```

Запустите установку как transient systemd unit. Процесс продолжит работу, даже
если SSH-соединение оборвётся:

```bash
sudo systemd-run \
  --unit="$INSTALL_UNIT" \
  --collect \
  /usr/local/sbin/install-remnawave-node-cover \
  --domain "$NODE_DOMAIN" \
  --email "$LE_EMAIL" \
  --reality-port 8443 \
  --yes
```

Следите за журналом:

```bash
sudo journalctl -fu "$INSTALL_UNIT"
```

`Ctrl+C` закрывает только просмотр журнала и не останавливает установку. После
повторного SSH-подключения статус можно проверить так:

```bash
sudo systemctl show "$INSTALL_UNIT" \
  -p ActiveState -p SubState -p Result -p ExecMainStatus
sudo journalctl -u "$INSTALL_UNIT" -n 100 --no-pager
```

Успешный результат содержит `Result=success` и `ExecMainStatus=0`.

По умолчанию установщик:

- случайно выбирает шаблон из `simple`, `sni` или `nothing`;
- запускает Caddy в контейнере `remnawave-node-cover`;
- публикует заглушку на TCP/80;
- создаёт HTTP fallback `127.0.0.1:8080`;
- создаёт TLS target REALITY `127.0.0.1:8443`;
- выпускает сертификат Let's Encrypt через HTTP-01;
- включает автоматическое обновление сертификата через Certbot.

Подробнее обо всех параметрах установщика: [remnawave-node-cover.md](./remnawave-node-cover.md).

## 7. Проверить заглушку и TLS

На рабочем компьютере:

```bash
curl -I "http://$NODE_DOMAIN"
```

На сервере ноды:

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
sudo ss -ltnp | grep -E ':80 |:443 |:8080 |:8443 '
sudo docker logs --tail 100 remnawave-node-cover
```

Проверка локального HTTPS target для REALITY:

```bash
curl -I \
  --resolve "$NODE_DOMAIN:8443:127.0.0.1" \
  --cacert /opt/remnawave-node-cover/certs/fullchain.pem \
  "https://$NODE_DOMAIN:8443/"
```

Ожидается `HTTP/2 200`. Проверьте сертификат и таймер продления:

```bash
sudo openssl x509 \
  -in /opt/remnawave-node-cover/certs/fullchain.pem \
  -noout -subject -issuer -dates
sudo systemctl is-enabled certbot.timer
sudo systemctl is-active certbot.timer
```

Основные файлы:

```text
/opt/remnawave-node-cover/docker-compose.yml
/opt/remnawave-node-cover/Caddyfile
/opt/remnawave-node-cover/www/
/opt/remnawave-node-cover/certs/fullchain.pem
/opt/remnawave-node-cover/certs/privkey.key
```

## 8. Настроить Xray REALITY

Публичный TCP/443 должен занимать Xray, а не Caddy. В Config Profile ноды
настройте VLESS TCP REALITY inbound на порт `443` и укажите локальный TLS target:

```json
{
  "tag": "VLESS_TCP_REALITY",
  "port": 443,
  "protocol": "vless",
  "settings": {
    "clients": [],
    "decryption": "none"
  },
  "streamSettings": {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
      "show": false,
      "target": "127.0.0.1:8443",
      "xver": 0,
      "serverNames": [
        "node-de.example.com"
      ],
      "privateKey": "",
      "shortIds": [
        "0123456789abcdef"
      ]
    }
  }
}
```

Замените домен и `shortIds` на значения конкретной ноды. Если версия Xray или
валидатор профиля использует старое имя поля, замените `target` на `dest`:

```json
"dest": "127.0.0.1:8443"
```

Не указывайте для REALITY `127.0.0.1:8080`: это обычный HTTP endpoint без TLS.
Значение `xver` должно оставаться `0`, потому что локальный Caddy не ожидает
PROXY protocol. Поле `privateKey` оставляйте пустым только если ключами управляет
Remnawave; иначе используйте ключ, созданный для этого профиля.

После применения Config Profile проверьте, что Xray занял TCP/443:

```bash
sudo ss -ltnp | grep ':443 '
sudo docker logs --tail 100 remnanode
```

В клиентском хосте/профиле:

- адрес сервера — домен ноды либо её IP;
- порт — `443`;
- SNI/serverName — домен ноды;
- public key и short ID — от REALITY-профиля этой ноды.

Если соединение работает по IP, но не по домену, проблема обычно находится в
A/AAAA-записях, включённом Cloudflare Proxy или DNS-кеше клиента. Адрес может
быть IP, но SNI для этой схемы всё равно должен оставаться доменным именем.

## 9. TLS-транспорты и Trojan

Согласно официальной архитектуре Remnawave, сертификаты для обычного TLS
транспорта читает backend панели и затем передаёт ноде вместе с Xray-конфигом.
Поэтому наличие сертификата только в `/opt/remnawave-node-cover/certs` на ноде
недостаточно для Trojan+TLS или VLESS+TLS.

Для REALITY сертификат в Xray-профиле не требуется: сертификат нашей заглушки
используется локальным Caddy как настоящий TLS target.

Если нужен Trojan+TLS, скопируйте сертификат и приватный ключ на сервер панели,
смонтируйте каталог в backend Remnawave и используйте внутренние пути контейнера
в `tlsSettings.certificates`. Порядок и пример путей находятся в разделе
«Сертификаты и Remnawave» файла [remnawave-node-cover.md](./remnawave-node-cover.md#сертификаты-и-remnawave).

## 10. Обслуживание

Обновить Remnawave Node:

```bash
cd /opt/remnanode
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Проверить журнал ноды и Caddy:

```bash
sudo docker logs --tail 100 remnanode
sudo docker logs --tail 100 remnawave-node-cover
```

Проверить Certbot без выпуска нового сертификата:

```bash
sudo certbot renew --dry-run
```

Заменить сайт новым случайным шаблоном:

```bash
sudo /usr/local/sbin/install-remnawave-node-cover \
  --domain "$NODE_DOMAIN" \
  --email "$LE_EMAIL" \
  --refresh-template \
  --yes
```

Повторный обычный запуск безопасно сохраняет существующую заглушку и сертификат.
Резервные копии заменённых шаблонов находятся в
`/opt/remnawave-node-cover/backups`.

## 11. Частые ошибки

### SSH-сессия закрывается во время установки

Не запускайте скрипт через `source` или `. script.sh`. Используйте transient
systemd unit из раздела 6 либо отдельную сессию `tmux`. Установка продолжится
после обрыва SSH.

### `exec: "validate": executable file not found`

Запущена старая версия установщика, вызывающая подкоманду Caddy без имени
исполняемого файла. Обновите `/usr/local/sbin/install-remnawave-node-cover`
текущим файлом `scripts/install_remnawave_node_cover.sh` и повторите установку.

### Docker Hub не разрешается через DNS

Для ошибки `lookup registry-1.docker.io ... i/o timeout` проверьте resolver:

```bash
resolvectl status
resolvectl query registry-1.docker.io
sudo systemctl restart systemd-resolved
getent ahosts registry-1.docker.io
sudo docker pull caddy:2.11.4-alpine
```

После успешного `docker pull` повторите установку с теми же параметрами.

### Certbot не может выпустить сертификат

Проверьте:

```bash
dig +short A "$NODE_DOMAIN"
sudo ss -ltnp | grep ':80 '
sudo ufw status
curl -I "http://$NODE_DOMAIN/.well-known/acme-challenge/test"
```

Ответ `404` на последнюю команду допустим: он подтверждает, что запрос доходит до
Caddy. Таймаут означает проблему с DNS, маршрутизацией или firewall.

### Порт 443 уже занят

Определите процесс:

```bash
sudo ss -ltnp | grep ':443 '
sudo docker ps --format 'table {{.Names}}\t{{.Ports}}'
```

На рабочей ноде TCP/443 обычно занимает Xray внутри `remnanode`. Не запускайте
на публичном 443 второй Caddy/nginx и не останавливайте Xray только ради повторного
выпуска сертификата: Certbot в нашей схеме использует TCP/80.

## Итоговый чек-лист

- [ ] A-запись домена указывает на публичный IPv4 ноды.
- [ ] Cloudflare Proxy отключён, либо используется осознанно подготовленная схема.
- [ ] `remnanode` запущен и подключён к панели.
- [ ] `NODE_PORT` разрешён только с IP панели.
- [ ] TCP/80 и TCP/443 доступны из интернета.
- [ ] `http://DOMAIN` отвечает `200`.
- [ ] `127.0.0.1:8080` и `127.0.0.1:8443` слушают только loopback.
- [ ] Сертификат действителен, `certbot.timer` включён и активен.
- [ ] REALITY target/dest указывает на `127.0.0.1:8443`, `xver` равен `0`.
- [ ] Xray слушает публичный TCP/443.
- [ ] В клиенте SNI совпадает с доменом ноды.

## Источники

- [Официальная установка Remnawave Node](https://docs.rw/install/remnawave-node/)
- [Локальная документация сайта-заглушки](./remnawave-node-cover.md)
- [eGamesAPI/remnawave-reverse-proxy](https://github.com/eGamesAPI/remnawave-reverse-proxy)
