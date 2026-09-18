#!/usr/bin/env python3
"""Ставит анти-флуд routing в config-profile Remnawave.

    antiflood.py <profile-name> [--apply] [--sniffing-only]

Без --apply только показывает, что изменится. Бэкап уходит в /root/rw-backups/.

--sniffing-only включает sniffing на инбаундах и НЕ трогает routing: нужен для
живых нод, где жёсткий UDP-whitelist сломал бы звонки и игры пользователям,
но детект bittorrent на HY2 включить всё равно надо.
"""
import json, subprocess, sys, datetime, os

RULES = [
    {"type": "field", "ruleTag": "BLOCK_PRIVATE_IP",
     "ip": ["geoip:private"], "outboundTag": "BLOCK"},
    {"type": "field", "ruleTag": "BLOCK_PRIVATE_DOMAIN",
     "domain": ["geosite:private"], "outboundTag": "BLOCK"},
    {"type": "field", "ruleTag": "ABUSE_BITTORRENT",
     "protocol": ["bittorrent"], "outboundTag": "BLOCK"},
    {"type": "field", "ruleTag": "ABUSE_TORRENT_PORTS",
     "port": "6881-6999", "outboundTag": "BLOCK"},
    {"type": "field", "ruleTag": "ABUSE_SMTP",
     "network": "tcp", "port": "25", "outboundTag": "BLOCK"},
    {"type": "field", "ruleTag": "ABUSE_PROXY_SDK", "outboundTag": "BLOCK",
     "domain": ["domain:packetsdk.io", "domain:packetsdk.com", "domain:packetsdk.net",
                "domain:packetsdk.xyz", "domain:hexsdk.com", "domain:castarsdk.com",
                "domain:earnsdk.io", "domain:ipidea.io", "domain:ip2world.com",
                "domain:lunaproxy.com", "domain:piaproxy.com", "domain:pyproxy.com",
                "domain:360proxy.com", "domain:922proxy.com", "domain:abcproxy.com",
                "domain:cherryproxy.com", "domain:tabproxy.com"]},
    # Порты классической UDP-амплификации. При включённом whitelist ниже это
    # дублирующий слой — оставлен, чтобы пережить смягчение whitelist.
    {"type": "field", "ruleTag": "ABUSE_UDP_AMPLIFICATION", "network": "udp",
     "port": "17,19,69,111,123,137-139,161-162,389,520,623,1434,1900,3283,3702,"
             "5093,5351,5353,11211,27015-27030,32414,33848,37810,47808",
     "outboundTag": "BLOCK"},
    # Жёсткий whitelist: наружу по UDP можно только DNS, QUIC/HTTP-3 и STUN.
    {"type": "field", "ruleTag": "UDP_ALLOW", "network": "udp",
     "port": "53,443,3478,19302-19309", "outboundTag": "DIRECT"},
    {"type": "field", "ruleTag": "UDP_DENY_REST", "network": "udp",
     "outboundTag": "BLOCK"},
]


def call(method, path, payload=None):
    cmd = ["python3", "/root/rwapi.py", method, path]
    if payload is not None:
        tmp = "/tmp/_af_payload.json"
        json.dump(payload, open(tmp, "w"))
        cmd.append(tmp)
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    head, body = out.split("\n", 1)
    return int(head.split()[1]), json.loads(body)


def main():
    name = sys.argv[1]
    apply_it = "--apply" in sys.argv
    sniffing_only = "--sniffing-only" in sys.argv

    _, data = call("GET", "/api/config-profiles")
    resp = data["response"]
    items = resp.get("configProfiles") or resp.get("items") or []
    prof = next((p for p in items if p["name"] == name), None)
    if not prof:
        sys.exit("профиль %s не найден" % name)

    cfg = prof["config"]
    os.makedirs("/root/rw-backups", exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = "/root/rw-backups/%s_%s.json" % (name, stamp)
    json.dump(cfg, open(bak, "w"), indent=2, ensure_ascii=False)
    print("бэкап:", bak)

    old = (cfg.get("routing") or {}).get("rules", [])

    if sniffing_only:
        print("routing не трогаем, правил остаётся: %d" % len(old))
    else:
        print("было правил: %d, станет: %d" % (len(old), len(RULES)))
        cfg["routing"] = {"domainStrategy": "IPIfNonMatch", "rules": RULES}

    # HY2-инбаунды идут без sniffing => детект по protocol на них не работает.
    touched = False
    for inb in cfg.get("inbounds", []):
        if not (inb.get("sniffing") or {}).get("enabled"):
            inb["sniffing"] = {"enabled": True,
                               "destOverride": ["http", "tls", "quic"],
                               "routeOnly": False}
            print("включаю sniffing:", inb.get("tag"))
            touched = True
    if not touched:
        print("sniffing уже включён на всех инбаундах")

    if not apply_it:
        print("\n--- dry run, для записи добавь --apply ---")
        return

    status, out = call("PATCH", "/api/config-profiles",
                       {"uuid": prof["uuid"], "config": cfg})
    print("PATCH HTTP", status)
    if status >= 300:
        print(json.dumps(out, indent=2, ensure_ascii=False)[:2000])
        sys.exit(1)

    _, chk = call("GET", "/api/config-profiles/" + prof["uuid"])
    live = chk["response"]["config"]
    tags = [r.get("ruleTag") or "—" for r in (live.get("routing") or {}).get("rules", [])]
    sniff = {i["tag"]: (i.get("sniffing") or {}).get("enabled") for i in live["inbounds"]}
    print("проверка routing:", tags)
    print("проверка sniffing:", sniff)


main()
