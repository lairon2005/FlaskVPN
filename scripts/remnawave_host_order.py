#!/usr/bin/env python3
"""Порядок хостов подписки и авто-выбор сервера (docs/subscription-hosts.md).

Запускается НА ХОСТЕ ПАНЕЛИ (root, docker):

    host_order.py            # dry run: показывает итоговый порядок
    host_order.py --apply    # бэкап -> запись в БД -> restart remnawave
    host_order.py --revert   # вернуть порядок и теги, убрать авто-хосты и шаблон

Что делает:
1. Раскладывает хосты по группам по слову в названии: «Основной» -> MAIN,
   «Запасной» -> RESERVE, «Турбо» -> TURBO. Группе ставит тег.
2. Порядок в подписке: три авто-хоста (основные, запасные, турбо), затем все основные,
   все запасные, все турбо. Внутри группы порядок прежний (по view_position).
3. На каждую группу заводит «виртуальный» хост `🌐 Авто (…)` — клон первого хоста
   группы со своим XRAY_JSON-шаблоном `Auto-Select`. Шаблон секцией
   `remnawave.injectHosts` подтягивает в конфиг все хосты с тем же тегом как
   outbound'ы `proxy`, `proxy-2`, …, а `observatory` + `leastPing` выбирает лучший.
   В остальных форматах подписки (base64, clash, …) авто-хосты скрыты: там нет
   балансировщиков, и это был бы дубль одного сервера.

Новая нода с хостами по тем же названиям попадёт в нужную группу простым
повторным `--apply`. Правки идут прямо в БД: API вырезает из шаблона секции
remnawave/observatory/balancers, а панель кеширует шаблоны.
"""
import datetime
import glob
import json
import os
import subprocess
import sys

DB = ["docker", "exec", "-i", "remnawave-db", "psql", "-U", "postgres", "-d", "postgres", "-At"]
BACKUPS = "/root/rw-backups"
AUTO_TEMPLATE = "Auto-Select"
# Метки группы — цвета бренда FLASK (docs/brand.md): синий — основной, голубой — второй,
# оранжевый — акцент; «искра»-астериск — фирменный элемент, им помечен авто-выбор.
AUTO_EMOJI = "✴️"
LEGACY_EMOJI = ("🔶", "🔸", "⚡", "🌐")  # оформление, скопированное с vacvpn
NON_JSON = ["XRAY_BASE64", "MIHOMO", "STASH", "CLASH", "SINGBOX"]

# (тег, слово в названии хоста, эмодзи, название группы в авто-хосте)
GROUPS = [
    ("MAIN", "Основной", "🔵", "Основные"),
    ("RESERVE", "Запасной", "🔹", "Запасные"),
    ("TURBO", "Турбо", "🟠", "Турбо"),
]


def psql(sql):
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
    """Литерал для PostgreSQL через dollar-quoting."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    assert "$q$" not in text
    return "$q$" + text + "$q$"


def load_hosts():
    raw = psql("select coalesce(json_agg(to_jsonb(h) order by view_position), '[]') from hosts h")
    return json.loads(raw)


def load_template(name):
    raw = psql("select coalesce(json_agg(json_build_object('uuid', uuid, 'json', template_json)), "
               "'[]') from subscription_templates "
               "where template_type = 'XRAY_JSON' and name = %s" % q(name))
    rows = json.loads(raw)
    return rows[0] if rows else None


def is_auto(host, auto_uuid):
    return bool(auto_uuid) and host["xray_json_template_uuid"] == auto_uuid


def auto_remark(title):
    return "%s Авто (%s)" % (AUTO_EMOJI, title)


def host_remark(host, word, emoji):
    """Название с меткой группы: старую метку (vacvpn или прежнюю) убираем, новую ставим перед словом."""
    name = host["remark"]
    for old in LEGACY_EMOJI + tuple(g[2] for g in GROUPS):
        name = name.replace(old + " ", "").replace(old, "")
    return name.replace(word, "%s %s" % (emoji, word), 1)


def build_auto_template(default_json):
    """Копия Default: хосты группы -> outbound'ы proxy*, лучший по пингу, остальное в баланс."""
    tpl = json.loads(json.dumps(default_json))
    tpl["remnawave"] = {"injectHosts": [{
        "selector": {"type": "sameTagAsRecipient"},
        "selectFrom": "ALL",
        "tagPrefix": "proxy",
    }]}
    tpl["observatory"] = {
        "subjectSelector": ["proxy"],
        "probeUrl": "https://www.gstatic.com/generate_204",
        "probeInterval": "60s",
        "enableConcurrency": True,
    }
    routing = tpl.setdefault("routing", {})
    routing["balancers"] = [{
        "tag": "auto",
        "selector": ["proxy"],
        "strategy": {"type": "leastPing"},
        "fallbackTag": "proxy",
    }]
    rules = [r for r in routing.get("rules", []) if r.get("ruleTag") != "AUTO-SELECT"]
    rules.append({"type": "field", "ruleTag": "AUTO-SELECT", "network": "tcp,udp",
                  "balancerTag": "auto"})
    routing["rules"] = rules
    return tpl


def plan(hosts, auto_uuid):
    """Итоговый порядок: список (host|None, group, remark). host=None -> авто-хост создаётся."""
    real = [h for h in hosts if not is_auto(h, auto_uuid)]
    autos = {tuple(h["tags"]): h for h in hosts if is_auto(h, auto_uuid)}
    autos_first = []
    groups = []
    claimed = set()
    for tag, word, emoji, title in GROUPS:
        members = [h for h in real if word in h["remark"]]
        if not members:
            continue
        claimed.update(h["uuid"] for h in members)
        autos_first.append({"auto": True, "tag": tag, "title": title, "emoji": emoji,
                            "host": autos.get((tag,)), "source": members[0]})
        groups.extend({"auto": False, "tag": tag, "host": h,
                       "remark": host_remark(h, word, emoji)} for h in members)
    ordered = autos_first + groups
    leftover = [h for h in real if h["uuid"] not in claimed]
    if leftover:
        print("не попали ни в одну группу (останутся в конце):",
              ", ".join(h["remark"] for h in leftover))
        ordered.extend({"auto": False, "tag": None, "host": h, "remark": h["remark"]}
                       for h in leftover)
    return ordered


def merged_tags(host, tag):
    groups = {g[0] for g in GROUPS}
    return [t for t in host["tags"] if t not in groups] + ([tag] if tag else [])


def backup(hosts, auto_uuid):
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = "%s/host-order.pre.%s.json" % (BACKUPS, stamp)
    state = [{"uuid": h["uuid"], "view_position": h["view_position"], "tags": h["tags"],
              "remark": h["remark"]}
             for h in hosts if not is_auto(h, auto_uuid)]
    with open(path, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    dump = "%s/hosts_templates.pre-host-order.%s.sql" % (BACKUPS, stamp)
    with open(dump, "w") as f:
        subprocess.run(["docker", "exec", "remnawave-db", "pg_dump", "-U", "postgres",
                        "-t", "hosts", "-t", "subscription_templates", "postgres"],
                       stdout=f, check=True)
    print("бэкап:", path, "и", dump)


def restart():
    print("перезапускаю remnawave (~20 с без API)…")
    subprocess.run(["docker", "restart", "remnawave"], check=True, capture_output=True)


def apply_plan(ordered, default_tpl):
    tpl = build_auto_template(default_tpl["json"])
    sql = ["begin;",
           "insert into subscription_templates (template_type, name, template_json) "
           "values ('XRAY_JSON', %s, %s::jsonb) "
           "on conflict (template_type, name) do update set template_json = excluded.template_json, "
           "updated_at = now();" % (q(AUTO_TEMPLATE), q(tpl))]
    tpl_uuid = "(select uuid from subscription_templates where template_type = 'XRAY_JSON' "\
               "and name = %s)" % q(AUTO_TEMPLATE)
    for pos, item in enumerate(ordered, start=1):
        if item["auto"]:
            remark = auto_remark(item["title"])
            host = item["host"]
            if host is None:
                # клон первого хоста группы: вход берётся оттуда, сам он как outbound не используется
                sql.append(
                    "insert into hosts (view_position, remark, address, port, path, sni, host, alpn, "
                    "fingerprint, is_disabled, security_layer, xhttp_extra_params, "
                    "config_profile_inbound_uuid, config_profile_uuid, server_description, mux_params, "
                    "sockopt_params, is_hidden, override_sni_from_address, vless_route_id, mihomo_x25519, "
                    "shuffle_host, xray_json_template_uuid, keep_sni_blank, exclude_from_subscription_types, "
                    "final_mask, pinned_peer_cert_sha256, verify_peer_cert_by_name, mihomo_ip_version, "
                    "tags, mapper, internal_squads_mode) "
                    "select %d, %s, address, port, path, sni, host, alpn, fingerprint, false, "
                    "security_layer, xhttp_extra_params, config_profile_inbound_uuid, config_profile_uuid, "
                    "%s, mux_params, sockopt_params, false, override_sni_from_address, vless_route_id, "
                    "mihomo_x25519, shuffle_host, %s, keep_sni_blank, %s::text[], final_mask, "
                    "pinned_peer_cert_sha256, verify_peer_cert_by_name, mihomo_ip_version, "
                    "ARRAY['%s']::text[], mapper, internal_squads_mode "
                    "from hosts where uuid = '%s';"
                    % (pos, q(remark), q("Лучший сервер автоматически"), tpl_uuid,
                       q("{" + ",".join(NON_JSON) + "}"), item["tag"], item["source"]["uuid"]))
            else:
                sql.append("update hosts set view_position = %d, remark = %s, xray_json_template_uuid = %s, "
                           "exclude_from_subscription_types = %s::text[], is_disabled = false "
                           "where uuid = '%s';"
                           % (pos, q(remark), tpl_uuid, q("{" + ",".join(NON_JSON) + "}"),
                              host["uuid"]))
        else:
            host = item["host"]
            tags = "{" + ",".join(merged_tags(host, item["tag"])) + "}"
            sql.append("update hosts set view_position = %d, tags = %s::text[], remark = %s "
                       "where uuid = '%s';" % (pos, q(tags), q(item["remark"]), host["uuid"]))
    sql.append("commit;")
    psql_file("\n".join(sql))
    print("запись в БД выполнена")
    restart()


def revert():
    files = sorted(glob.glob(BACKUPS + "/host-order.pre.*.json"))
    if not files:
        sys.exit("бэкапов нет")
    state = json.load(open(files[-1]))
    print("откат из", files[-1])
    sql = ["begin;",
           "delete from hosts where xray_json_template_uuid = (select uuid from subscription_templates "
           "where template_type = 'XRAY_JSON' and name = %s);" % q(AUTO_TEMPLATE),
           "delete from subscription_templates where template_type = 'XRAY_JSON' and name = %s;"
           % q(AUTO_TEMPLATE)]
    for h in state:
        tags = "{" + ",".join(h["tags"]) + "}"
        remark = "remark = %s, " % q(h["remark"]) if "remark" in h else ""
        sql.append("update hosts set %sview_position = %d, tags = %s::text[] where uuid = '%s';"
                   % (remark, h["view_position"], q(tags), h["uuid"]))
    sql.append("commit;")
    psql_file("\n".join(sql))
    restart()


def main():
    if "--revert" in sys.argv:
        revert()
        return
    hosts = load_hosts()
    default_tpl = load_template("Default")
    if not default_tpl:
        sys.exit("нет XRAY_JSON-шаблона Default")
    auto_row = load_template(AUTO_TEMPLATE)
    auto_uuid = auto_row["uuid"] if auto_row else None
    ordered = plan(hosts, auto_uuid)
    print("порядок в подписке:")
    for pos, item in enumerate(ordered, start=1):
        if item["auto"]:
            mark = "NEW " if item["host"] is None else "keep"
            print("%3d  %s [%s] %s" % (pos, mark, item["tag"],
                                       auto_remark(item["title"])))
        else:
            print("%3d       [%s] %s" % (pos, item["tag"] or "-", item["remark"]))
    if "--apply" not in sys.argv:
        print("\n--- dry run, для записи добавь --apply ---")
        return
    backup(hosts, auto_uuid)
    apply_plan(ordered, default_tpl)
    print("готово; проверь выдачу: curl -s -A 'INCY/1' https://sub.flaskvpn.ru/<short_uuid>/json "
          "| python3 -c \"import sys,json;print([c['remarks'] for c in json.load(sys.stdin)])\"")


main()
