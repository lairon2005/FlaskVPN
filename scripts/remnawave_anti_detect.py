#!/usr/bin/env python3
"""Защита от обнаружения VPN российскими приложениями (docs/anti-vpn-detection.md).

Запускается НА ХОСТЕ ПАНЕЛИ (root, docker):

    anti_detect.py            # dry run: показывает, что изменится
    anti_detect.py --apply    # бэкап -> запись в БД -> restart remnawave
    anti_detect.py --revert   # вернуть шаблон и заголовки из последнего бэкапа

Что делает:
1. В XRAY_JSON-шаблоны добавляет два правила с outboundTag=direct сразу после
   правила bittorrent: ANTI-DETECT-IPCHECK (сервисы определения IP) и
   ANTI-DETECT-RU-APPS (домены российских приложений и их SDK). Только
   доменные правила — без geoip/geosite.
2. В customResponseHeaders подписки добавляет `socks-auth-mode: auto`
   (пароль на локальный SOCKS; понимает Happ, остальные клиенты игнорируют).

Правки идут прямо в БД панели: шаблоны в API режутся, а панель кеширует их,
поэтому после записи нужен `docker restart remnawave` (API молчит ~20 с).
Скрипт идемпотентен: старые ANTI-DETECT-правила перед вставкой удаляются.
"""
import datetime
import glob
import json
import os
import subprocess
import sys

DB = ["docker", "exec", "-i", "remnawave-db", "psql", "-U", "postgres", "-d", "postgres", "-At"]
BACKUPS = "/root/rw-backups"
HEADERS = {"socks-auth-mode": "auto"}

IPCHECK = ["domain:ipify.org", "full:checkip.amazonaws.com", "domain:ifconfig.me"]

RU_APPS = [
    # Яндекс (+AppMetrica живёт под yandex.net)
    "domain:yandex.ru", "domain:ya.ru", "domain:yandex.net", "domain:yandex.com",
    "domain:yastatic.net", "domain:yandexcloud.net", "domain:yandex.cloud",
    "domain:naydex.net", "domain:yandexadexchange.net", "domain:yandex.by",
    "domain:yandex.kz", "domain:yandex.uz", "domain:kinopoisk.ru", "domain:auto.ru",
    # VK / Mail.ru / OK / MAX / MyTracker / RuStore / Дзен
    "domain:vk.com", "domain:vk.ru", "domain:vk.me", "domain:vk.cc", "domain:vk.link",
    "domain:vk.company", "domain:vkuser.net", "domain:vkuseraudio.net",
    "domain:vkuservideo.net", "domain:userapi.com", "domain:vk-cdn.net",
    "domain:vk-portal.net", "domain:vkvideo.ru", "domain:mycdn.me", "domain:ok.ru",
    "domain:okcdn.ru", "domain:mail.ru", "domain:imgsmail.ru", "domain:my.com",
    "domain:mradx.net", "domain:mytracker.ru", "domain:max.ru", "domain:oneme.ru",
    "domain:rustore.ru", "domain:dzen.ru",
    # Сбер и сервисы
    "domain:sber.ru", "domain:sberbank.ru", "domain:sberbank.com",
    "domain:sbermarket.ru", "domain:kuper.ru", "domain:megamarket.ru",
    "domain:samokat.ru", "domain:sberdevices.ru", "domain:okko.tv", "domain:zvuk.com",
    # Банки и НСПК
    "domain:tbank.ru", "domain:tinkoff.ru", "domain:tcsbank.ru", "domain:vtb.ru",
    "domain:alfabank.ru", "domain:gazprombank.ru", "domain:raiffeisen.ru",
    "domain:mtsbank.ru", "domain:pochtabank.ru", "domain:sovcombank.ru",
    "domain:nspk.ru", "domain:cbr.ru",
    # Маркетплейсы и сервисы
    "domain:wildberries.ru", "domain:wb.ru", "domain:wbbasket.ru", "domain:wbstatic.net",
    "domain:rwb.ru", "domain:ozon.ru", "domain:ozone.ru", "domain:avito.ru",
    "domain:avito.st", "domain:2gis.ru", "domain:2gis.com", "domain:hh.ru",
    "domain:hhcdn.ru", "domain:rutube.ru",
    # Госсервисы, почта, операторы
    "domain:gosuslugi.ru", "domain:gov.ru", "domain:mos.ru", "domain:pochta.ru",
    "domain:mts.ru", "domain:kion.ru", "domain:megafon.ru", "domain:beeline.ru",
    "domain:t2.ru", "domain:tele2.ru",
]

RULES = [
    {"type": "field", "ruleTag": "ANTI-DETECT-IPCHECK", "domain": IPCHECK,
     "outboundTag": "direct"},
    {"type": "field", "ruleTag": "ANTI-DETECT-RU-APPS", "domain": RU_APPS,
     "outboundTag": "direct"},
]


def psql(sql, stdin_extra=None):
    res = subprocess.run(DB + ["-c", sql], capture_output=True, text=True)
    if res.returncode:
        sys.exit("psql: " + res.stderr.strip())
    return res.stdout.strip()


def psql_file(sql_text):
    res = subprocess.run(DB, input=sql_text, capture_output=True, text=True)
    if res.returncode:
        sys.exit("psql: " + res.stderr.strip())
    return res.stdout.strip()


def q(value):
    """Литерал для PostgreSQL через dollar-quoting (в JSON нет `$q$`)."""
    text = json.dumps(value, ensure_ascii=False)
    assert "$q$" not in text
    return "$q$" + text + "$q$"


def load_templates():
    raw = psql("select coalesce(json_agg(json_build_object('uuid', uuid, 'name', name, "
               "'json', template_json)), '[]') from subscription_templates "
               "where template_type = 'XRAY_JSON'")
    return json.loads(raw)


def load_headers():
    return json.loads(psql("select coalesce(custom_response_headers, '{}') "
                           "from subscription_settings"))


def with_rules(tpl):
    """Копия шаблона: ANTI-DETECT-правила стоят сразу после bittorrent."""
    tpl = json.loads(json.dumps(tpl))
    routing = tpl.setdefault("routing", {})
    rules = [r for r in routing.get("rules", [])
             if not str(r.get("ruleTag", "")).startswith("ANTI-DETECT")]
    pos = next((i + 1 for i, r in enumerate(rules) if "bittorrent" in r.get("protocol", [])), 0)
    routing["rules"] = rules[:pos] + RULES + rules[pos:]
    return tpl


def backup(templates, headers):
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = "%s/anti-detect.pre.%s.json" % (BACKUPS, stamp)
    with open(path, "w") as f:
        json.dump({"templates": templates, "headers": headers}, f, ensure_ascii=False, indent=2)
    dump = "%s/subscription_tables.pre-anti-detect.%s.sql" % (BACKUPS, stamp)
    with open(dump, "w") as f:
        subprocess.run(["docker", "exec", "remnawave-db", "pg_dump", "-U", "postgres",
                        "-t", "subscription_templates", "-t", "subscription_settings",
                        "postgres"], stdout=f, check=True)
    print("бэкап:", path, "и", dump)


def write(templates, headers):
    sql = ["begin;"]
    for t in templates:
        sql.append("update subscription_templates set template_json = %s::jsonb, "
                   "updated_at = now() where uuid = '%s';" % (q(t["json"]), t["uuid"]))
    sql.append("update subscription_settings set custom_response_headers = %s::jsonb, "
               "updated_at = now();" % q(headers))
    sql.append("commit;")
    psql_file("\n".join(sql))
    print("запись в БД выполнена, перезапускаю remnawave (~20 с без API)…")
    subprocess.run(["docker", "restart", "remnawave"], check=True, capture_output=True)


def main():
    apply_it = "--apply" in sys.argv
    templates = load_templates()
    headers = load_headers()

    if "--revert" in sys.argv:
        files = sorted(glob.glob(BACKUPS + "/anti-detect.pre.*.json"))
        if not files:
            sys.exit("бэкапов нет")
        data = json.load(open(files[-1]))
        print("откат из", files[-1])
        write(data["templates"], data["headers"])
        return

    new_templates = [{"uuid": t["uuid"], "name": t["name"], "json": with_rules(t["json"])}
                     for t in templates]
    new_headers = {**headers, **HEADERS}

    for old, new in zip(templates, new_templates):
        before = len(old["json"].get("routing", {}).get("rules", []))
        after = len(new["json"]["routing"]["rules"])
        print("шаблон %s (%s): правил %d -> %d" % (new["name"], new["uuid"], before, after))
    print("заголовки:", json.dumps(headers, ensure_ascii=False), "->",
          json.dumps(new_headers, ensure_ascii=False))

    if not apply_it:
        print("\n--- dry run, для записи добавь --apply ---")
        return
    backup(templates, headers)
    write(new_templates, new_headers)
    print("готово; проверь выдачу: curl -s -A 'INCY/1' <sub-url> | grep -c ANTI-DETECT")


main()
