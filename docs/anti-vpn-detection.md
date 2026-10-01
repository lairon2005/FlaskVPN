# Защита от обнаружения VPN российскими приложениями

Применено к панели FlaskVPN **01.10.2026**. Источник методики — `docs/anti-vpn-detection.md`
проекта vacvpn (применено там 26.09.2026). Идея: российские приложения видят, что на
телефоне включён VPN, и присылают адрес, с которого пришёл запрос. Если запрос шёл
через туннель, это адрес ноды — и он уходит в блокировку. Задача — чтобы приложения
видели **реальный адрес пользователя**, а не адрес ноды. Сам факт VPN на телефоне
скрыть со стороны сервиса нельзя.

| Канал утечки | Чем закрыт | Статус |
|---|---|---|
| Запросы российских приложений к своему бэкенду и SDK (AppMetrica, MyTracker) | правило `ANTI-DETECT-RU-APPS` → `direct` | **сделано** |
| Сервисы определения IP (`ipify.org`, `checkip.amazonaws.com`, `ifconfig.me`) | правило `ANTI-DETECT-IPCHECK` → `direct` | **сделано** |
| Локальный SOCKS `127.0.0.1:10808` без пароля | заголовок `socks-auth-mode: auto` | **сделано, но только для Happ** |
| Локальный HTTP `127.0.0.1:10809` без пароля | `http-auth-mode: auto` | не сделано |
| Флаг VPN в системе (`TRANSPORT_VPN`, `tun0`) | исключение приложений из VPN на Android | не сделано, решение владельца |

## Что на панели

Панель 3.4.4 (`panel-server.md`), один XRAY_JSON-шаблон `Default`
(`bfb82159-de4d-45c6-bcba-0681b5a46b7c`). В отличие от vacvpn, шаблона `Auto-Select` нет.

- В `routing.rules` сразу после правила `bittorrent` добавлены `ANTI-DETECT-IPCHECK`
  (3 домена) и `ANTI-DETECT-RU-APPS` (85 доменов: Яндекс/AppMetrica, VK/Mail.ru/OK/MAX/
  MyTracker/RuStore/Дзен, Сбер, банки и НСПК, WB, Ozon, Авито, 2ГИС, hh, Rutube,
  Госуслуги, операторы связи). Оба с `outboundTag: direct`.
- Только доменные правила, без `geoip:`/`geosite:` — клиенты на iOS нарезают геофайлы
  по профилю маршрутизации, а не по JSON, и неизвестный тег может уронить ядро.
- Список адресный, а не весь `.ru`: заблокированные сайты в зоне `.ru` должны остаться в
  туннеле.
- В `subscription_settings.custom_response_headers` добавлен `socks-auth-mode: auto`;
  прежние ключи (`support-url`, `profile-title`, …) сохранены.

Цена: пользователь, который проверяет IP на `ifconfig.me` или ipify, увидит домашний
адрес и может решить, что VPN не работает. Для проверки подходят `2ip.io`, `whoer.net`,
`cloudflare.com/cdn-cgi/trace`. Бонус: банки и госсервисы, режущие хостинговые адреса,
начинают открываться.

## Наш клиент — INCY, а не Happ

Инструкция писалась под Happ. У нас пользователям рекомендован INCY, и это меняет
часть мер:

- **Маршрутизация** работает в любом клиенте, читающем Xray JSON, — главная мера
  действует и в INCY.
- **`socks-auth-mode`** — параметр Happ. В документации INCY его нет, значит INCY
  заголовок, скорее всего, игнорирует: локальный SOCKS у пользователей INCY остаётся
  без пароля. Заголовок оставлен ради тех, кто подписку открывает в Happ. **Не
  проверялось на устройстве.**
- **Исключение приложений из VPN на Android** у INCY устроено иначе и, по его
  документации, **не требует Provider ID**: `per-app-proxy-enable: 1`,
  `per-app-proxy-mode: bypass`, `per-app-proxy-list: <пакеты>` (значения Happ —
  `on|bypass` — INCY не понимает). Это самая сильная мера для Android: трафик приложения
  вообще не попадает в туннель. Не включено — навязывает пользователям обход банков и
  маркетплейсов; см. «Что осталось». Перед включением проверить на устройстве, добавляет
  ли список к выбору пользователя или затирает его.

Пакеты из инструкции vacvpn:

```text
ru.sberbankmobile,com.idamob.tinkoff.android,ru.vtb24.mobilebanking.android,ru.alfabank.mobile.android,ru.rostel,ru.yandex.searchplugin,com.yandex.browser,ru.yandex.yandexmaps,ru.yandex.music,com.vkontakte.android,ru.ok.android,ru.oneme.app,ru.mail.mailapp,ru.ozon.app.android,com.wildberries.ru,com.avito.android,ru.dublgis.dgismobile,ru.mts.mymts,ru.vk.store,ru.hh.android
```

## Что осталось

1. Проверить на реальном INCY (Android + iOS): открываются ли банки/Госуслуги, `2ip.io`
   показывает домашний IP, `cloudflare.com/cdn-cgi/trace` — адрес ноды, запрещённый
   сайт в `.ru` (например `grani.ru`) идёт через туннель.
2. Решить про `per-app-proxy-*` для INCY (выше).
3. `http-auth-mode: auto` — только после проверки, что Happ Desktop в режиме «Прокси» и
   раздача по LAN не ломаются.
4. **Отдельный выходной адрес на нодах** — закрывает то, что маршрутизацией не закрыть
   (приложения, проверяющие доступность заблокированных ресурсов, и клиенты без наших
   шаблонов). Инфраструктурная задача, по ней у vacvpn `xray-node-config.md` §4.2.
5. Инструкции в боте (`tgbot/handlers/user/instruction.py`) не менялись. Совет
   пользователям на Android: исключить банки, Госуслуги, VK, Яндекс, MAX, маркетплейсы
   из VPN в настройках приложения.

Обновление у пользователей — при следующем обновлении подписки. `profile-update-interval`
у нас **12 часов** (у vacvpn — 1), так что правила доедут не мгновенно.

## Как дополнять список и откатывать

Всё делает `scripts/remnawave_anti_detect.py`, запускается **на хосте панели**
(`scp` → `/root/anti_detect.py`):

```bash
python3 /root/anti_detect.py            # dry run
python3 /root/anti_detect.py --apply    # бэкап → запись в БД → docker restart remnawave
python3 /root/anti_detect.py --revert   # вернуть шаблон и заголовки из последнего бэкапа
```

Новые домены — в `RU_APPS` скрипта и повторный `--apply` (идемпотентно: старые
`ANTI-DETECT-*` правила удаляются перед вставкой). Запись идёт прямо в БД, потому что
панель кеширует шаблоны; рестарт `remnawave` гасит API на ~20 с, ноды и туннели
продолжают работать.

Бэкап перед применением 01.10.2026 — в `/root/rw-backups/`:
`anti-detect.pre.20261001_000843.json` (шаблоны и заголовки) и
`subscription_tables.pre-anti-detect.20261001_000843.sql` (дамп обеих таблиц).

**Проверка выдачи** (`short_uuid` — из таблицы `users`):

```bash
curl -s -A 'INCY/1.0' https://sub.flaskvpn.ru/<short_uuid>/json | grep -o 'ANTI-DETECT-[A-Z-]*'
curl -sI -A 'INCY/1.0' https://sub.flaskvpn.ru/<short_uuid> | grep -i socks-auth
```

Базовый URL с выдуманным User-Agent отдаёт заглушку «App not supported» — это поведение
панели 3.x для неизвестных клиентов, проверять нужно через `/json`.

## Источники

- [Happ: управление приложением](https://www.happ.su/main/ru/dev-docs/app-management)
- [INCY: Application management](https://incy.gitbook.io/docs/docs-en/app-management.en)
- [Хабр: MAX обращается к иностранным сервисам определения IP](https://habr.com/ru/articles/1006394/)
- [RKS Global: как российские приложения ищут VPN](https://rks.global/ru/research/vpn-detection/)
