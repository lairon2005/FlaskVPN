# Сайт-заглушка и TLS на Remnawave Node

Скрипт `scripts/install_remnawave_node_cover.sh` устанавливает на отдельной
Remnawave Node:

- случайный статический сайт-заглушку;
- Caddy в отдельном Docker Compose-стеке;
- сертификат Let's Encrypt через HTTP-01;
- автоматическое продление сертификата Certbot;
- локальный HTTP endpoint для обычного Xray TLS fallback;
- локальный HTTPS endpoint для Xray REALITY target.

Скрипт не устанавливает Remnawave Node, не меняет `/opt/remnanode` и не создаёт
Xray Config Profile.

## DNS

Для каждой ноды создайте отдельную A-запись:

```text
node1.example.com -> публичный IPv4 первой ноды
node2.example.com -> публичный IPv4 второй ноды
```

Для Cloudflare используйте режим **DNS only** (серое облако). Cloudflare Proxy
не нужен для прямого VLESS/TCP/TLS-трафика и без специально подготовленного
транспорта может помешать подключению к Xray.

До запуска должны выполняться условия:

- домен уже указывает на публичный IPv4 ноды;
- входящий TCP/80 доступен из интернета;
- Docker и `docker compose` уже установлены вместе с Remnawave Node;
- порт 80 и выбранные локальные fallback/REALITY-порты свободны.

Скрипт принципиально не изменяет UFW/iptables и не перезапускает сетевые службы,
чтобы не оборвать активное SSH-подключение. Если UFW активен, сначала убедитесь,
что разрешён фактический SSH-порт, и только после этого отдельно откройте TCP/80.

Для длительной установки рекомендуется использовать `tmux`:

```bash
sudo apt-get install -y tmux
tmux new -s node-cover
```

После запуска скрипта отсоединиться от `tmux` можно сочетанием `Ctrl+B`, затем
`D`; вернуться — командой `tmux attach -t node-cover`.

## Установка

Интерактивный запуск:

```bash
sudo bash ./scripts/install_remnawave_node_cover.sh
```

Не запускайте установщик через `source script.sh` или `. script.sh`. В таком
режиме настройки `set -e` применяются к текущему login shell, поэтому штатная
ошибка установщика может завершить всю SSH-сессию. Версия 1.1.1 и новее явно
отклоняет запуск через `source` до изменения параметров shell.

Автоматический запуск:

```bash
sudo bash ./scripts/install_remnawave_node_cover.sh \
  --domain node1.example.com \
  --email admin@example.com \
  --yes
```

Другой локальный порт для fallback:

```bash
sudo bash ./scripts/install_remnawave_node_cover.sh \
  --domain node1.example.com \
  --email admin@example.com \
  --fallback-port 9080 \
  --yes
```

Другой локальный TLS-порт для REALITY target:

```bash
sudo bash ./scripts/install_remnawave_node_cover.sh \
  --domain node1.example.com \
  --email admin@example.com \
  --reality-port 9443 \
  --yes
```

При первом запуске случайно выбирается один из источников, использованных в
`eGamesAPI/remnawave-reverse-proxy`:

- `eGamesAPI/simple-web-templates`;
- `distillium/sni-templates`;
- `prettyleaf/nothing-sni`.

Повторный запуск сохраняет существующий сайт. Для выбора нового случайного
шаблона добавьте `--refresh-template`. Предыдущая версия сайта сохраняется в
`/opt/remnawave-node-cover/backups`.

## Получившаяся схема

```text
Интернет :80
    -> Caddy
       -> сайт-заглушка
       -> /.well-known/acme-challenge/* для Certbot

Xray fallback
    -> 127.0.0.1:8080
       -> Caddy
          -> тот же сайт-заглушка

Xray REALITY target
    -> 127.0.0.1:8443 (TLS)
       -> Caddy с сертификатом домена ноды
          -> тот же сайт-заглушка
```

Оба локальных порта привязаны только к loopback-интерфейсу и недоступны напрямую
из интернета.

После настройки TLS inbound публичный порт 443 должен занимать Xray. Xray
завершает TLS, а неподходящий под протокол запрос передаёт на локальный Caddy как
обычный HTTP. Поэтому для fallback используется `127.0.0.1:8080`, а не HTTPS.

Минимальная часть `settings` для VLESS inbound обычно выглядит так (это не
полный Config Profile):

```json
{
  "decryption": "none",
  "fallbacks": [
    {
      "dest": 8080,
      "xver": 0
    }
  ]
}
```

Если при установке указан другой `--fallback-port`, такое же значение нужно
использовать в Xray-профиле.

Для REALITY Xray передаёт не прошедший аутентификацию TLS-трафик в `target` без
завершения TLS. Поэтому REALITY должен указывать на локальный HTTPS endpoint, а
не на HTTP-порт `8080`:

```json
{
  "network": "tcp",
  "security": "reality",
  "realitySettings": {
    "target": "127.0.0.1:8443",
    "xver": 0,
    "serverNames": ["node1.example.com"],
    "privateKey": "REPLACE_WITH_REALITY_PRIVATE_KEY",
    "shortIds": ["REPLACE_WITH_16_HEX_CHARS"]
  }
}
```

Если указан другой `--reality-port`, его же следует записать в `target`.

Основные пути:

```text
/opt/remnawave-node-cover/docker-compose.yml
/opt/remnawave-node-cover/Caddyfile
/opt/remnawave-node-cover/www/
/opt/remnawave-node-cover/certs/fullchain.pem
/opt/remnawave-node-cover/certs/privkey.key
```

## Сертификаты и Remnawave

Официальная документация Remnawave рекомендует хранить сертификаты на сервере
панели: каталог с сертификатами монтируется в backend панели, после чего панель
передаёт сертификат выбранной ноде вместе с Xray-конфигурацией.

Для отдельных сертификатов каждой ноды можно создать на панели структуру:

```text
/opt/remnawave/nginx/nodes/node1.example.com/fullchain.pem
/opt/remnawave/nginx/nodes/node1.example.com/privkey.key
/opt/remnawave/nginx/nodes/node2.example.com/fullchain.pem
/opt/remnawave/nginx/nodes/node2.example.com/privkey.key
```

Затем смонтировать родительский каталог в backend панели:

```yaml
services:
  remnawave:
    volumes:
      - /opt/remnawave/nginx/nodes:/var/lib/remnawave/configs/xray/ssl/nodes:ro
```

Для профиля первой ноды пути TLS будут выглядеть так:

```json
{
  "security": "tls",
  "tlsSettings": {
    "certificates": [
      {
        "keyFile": "/var/lib/remnawave/configs/xray/ssl/nodes/node1.example.com/privkey.key",
        "certificateFile": "/var/lib/remnawave/configs/xray/ssl/nodes/node1.example.com/fullchain.pem"
      }
    ]
  }
}
```

Скопировать сертификаты с ноды на панель можно вручную через `scp`, через
Ansible или отдельную ограниченную задачу синхронизации. Приватный ключ следует
передавать только по защищённому каналу и хранить с правами `0600`.

Скрипт намеренно не настраивает такую синхронизацию: адрес панели, SSH-пользователь
и модель доступа отличаются между установками. После обновления сертификата на
ноде файлы в `/opt/remnawave-node-cover/certs` заменяются автоматически, но копию
на панели также нужно обновить и повторно применить профиль/перезапустить Xray.

## Проверка

Проверка HTTP-сайта и Caddy:

```bash
curl -I http://node1.example.com
docker logs --tail 100 remnawave-node-cover
```

Проверка сертификата:

```bash
sudo certbot certificates --cert-name node1.example.com
sudo openssl x509 \
  -in /opt/remnawave-node-cover/certs/fullchain.pem \
  -noout -subject -issuer -dates
```

Проверка локального REALITY target:

```bash
curl --resolve node1.example.com:8443:127.0.0.1 \
  --cacert /opt/remnawave-node-cover/certs/fullchain.pem \
  https://node1.example.com:8443/
```

Тест автоматического продления без выпуска нового сертификата:

```bash
sudo certbot renew --dry-run
```

После создания Xray TLS inbound проверьте HTTPS:

```bash
curl -I https://node1.example.com
```

## Что изменяет скрипт

- создаёт `/opt/remnawave-node-cover`;
- запускает контейнер `remnawave-node-cover`;
- устанавливает недостающие пакеты Certbot/DNS/архиватора;
- проверяет UFW и предупреждает о закрытом TCP/80, но не меняет firewall;
- создаёт lineage Certbot в `/etc/letsencrypt`;
- добавляет deploy-hook Certbot, копирующий обновлённый сертификат в стабильные
  пути `/opt/remnawave-node-cover/certs` и перезапускающий Caddy для загрузки
  обновлённого сертификата.

Скрипт не открывает 443, не редактирует firewall для API ноды и не трогает
`/opt/remnanode/docker-compose.yml`.

## Если Docker Hub не загружается

Ошибка вида:

```text
lookup registry-1.docker.io on 127.0.0.53:53: i/o timeout
```

означает сбой локального DNS `systemd-resolved`, а не ошибку Caddyfile. Скрипт
повторяет загрузку четыре раза и очищает DNS-кеш между попытками. Если это не
помогло, проверьте resolver:

```bash
resolvectl status
resolvectl query registry-1.docker.io
sudo systemctl restart systemd-resolved
getent ahosts registry-1.docker.io
sudo docker pull caddy:2.11.4-alpine
```

После успешного `docker pull` повторно запустите установщик с теми же аргументами.
Существующая заглушка повторно скачиваться не будет.

## Источники

- [Официальная установка Remnawave Node](https://docs.rw/install/remnawave-node/)
- [eGamesAPI/remnawave-reverse-proxy](https://github.com/eGamesAPI/remnawave-reverse-proxy)
- [Node-модуль Caddy исходного проекта](https://github.com/eGamesAPI/remnawave-reverse-proxy/blob/main/src/caddy/install_node.sh)
