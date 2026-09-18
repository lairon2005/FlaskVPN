#!/usr/bin/env bash
# Лимит исходящих пакетов на ноде Remnawave — страховка от абузы хостера.
#
# Xray умеет только запрещать классы трафика (см. routing в Config Profile),
# но НЕ умеет ограничивать пакеты в секунду. Потолок ставится здесь, в ядре.
#
#   ./node_antiflood_firewall.sh install   — поставить правила + сохранить
#   ./node_antiflood_firewall.sh status    — счётчики срабатываний
#   ./node_antiflood_firewall.sh remove    — снять
#
# Пороги подобраны под ноду ~200 Мбит/с. На гигабитной площадке подними
# GLOBAL_PPS и UDP_GLOBAL_PPS пропорционально.

set -euo pipefail

CHAIN=ANTIFLOOD

UDP_PER_DST_PPS=3000     # UDP на одну цель: хватает видеозвонку и игре
UDP_PER_DST_BURST=6000
UDP_GLOBAL_PPS=25000     # суммарный исходящий UDP
SYN_PER_DST_PPS=300      # новые TCP-соединения на одну цель
GLOBAL_PPS=80000         # аварийный потолок по всем пакетам (хостер ругался на 106k)

need_root() { [ "$(id -u)" = 0 ] || { echo "нужен root"; exit 1; }; }

build_chain() {
    iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"

    # 1. UDP: пер-целевой лимит. Флуд всегда бьёт в одну жертву.
    iptables -A "$CHAIN" -p udp -m hashlimit \
        --hashlimit-name af_udp_dst --hashlimit-mode dstip \
        --hashlimit-above "${UDP_PER_DST_PPS}/sec" --hashlimit-burst "$UDP_PER_DST_BURST" \
        --hashlimit-htable-expire 10000 -j DROP

    # 2. UDP: суммарный потолок на случай флуда веером по многим целям.
    iptables -A "$CHAIN" -p udp -m hashlimit \
        --hashlimit-name af_udp_all --hashlimit-mode srcip \
        --hashlimit-above "${UDP_GLOBAL_PPS}/sec" --hashlimit-burst "$UDP_GLOBAL_PPS" \
        --hashlimit-htable-expire 10000 -j DROP

    # 3. TCP SYN-флуд: лимитируем только новые соединения, не поток данных,
    #    иначе порежем ACK'и при обычном скачивании.
    iptables -A "$CHAIN" -p tcp --syn -m hashlimit \
        --hashlimit-name af_syn_dst --hashlimit-mode dstip \
        --hashlimit-above "${SYN_PER_DST_PPS}/sec" --hashlimit-burst "$SYN_PER_DST_PPS" \
        --hashlimit-htable-expire 10000 -j DROP

    # 4. Аварийный потолок по всему исходящему.
    iptables -A "$CHAIN" -m hashlimit \
        --hashlimit-name af_global --hashlimit-mode srcip \
        --hashlimit-above "${GLOBAL_PPS}/sec" --hashlimit-burst "$GLOBAL_PPS" \
        --hashlimit-htable-expire 10000 -j DROP

    # 5. Спам с ноды — мгновенная абуза, режем безусловно.
    iptables -A "$CHAIN" -p tcp --dport 25 -j DROP

    iptables -A "$CHAIN" -j RETURN
}

hook() {
    # Xray с network_mode: host выходит через OUTPUT; в bridge-режиме
    # трафик контейнера идёт через FORWARD/DOCKER-USER. Вешаем на все три,
    # лишнее просто не сматчится.
    for c in OUTPUT FORWARD; do
        iptables -C "$c" -j "$CHAIN" 2>/dev/null || iptables -I "$c" 1 -j "$CHAIN"
    done
    if iptables -L DOCKER-USER -n >/dev/null 2>&1; then
        iptables -C DOCKER-USER -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER 1 -j "$CHAIN"
    fi
}

unhook() {
    for c in OUTPUT FORWARD DOCKER-USER; do
        while iptables -C "$c" -j "$CHAIN" 2>/dev/null; do
            iptables -D "$c" -j "$CHAIN"
        done
    done
}

persist() {
    if command -v netfilter-persistent >/dev/null 2>&1; then
        netfilter-persistent save
    else
        mkdir -p /etc/iptables
        iptables-save > /etc/iptables/rules.v4
        cat > /etc/systemd/system/antiflood-restore.service <<'UNIT'
[Unit]
Description=Restore antiflood iptables rules
After=network-pre.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/sbin/iptables-restore --noflush /etc/iptables/rules.v4
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
        systemctl daemon-reload
        systemctl enable antiflood-restore.service
    fi
}

case "${1:-}" in
    install)
        need_root
        build_chain
        hook
        persist
        echo "готово. правила активны и переживут перезагрузку."
        iptables -L "$CHAIN" -n -v
        ;;
    status)
        need_root
        iptables -L "$CHAIN" -n -v
        echo
        echo "ненулевой pkts в строках DROP = флуд поймали и обрезали"
        ;;
    remove)
        need_root
        unhook
        iptables -F "$CHAIN" 2>/dev/null || true
        iptables -X "$CHAIN" 2>/dev/null || true
        echo "снято"
        ;;
    *)
        sed -n '2,16p' "$0"
        exit 1
        ;;
esac
