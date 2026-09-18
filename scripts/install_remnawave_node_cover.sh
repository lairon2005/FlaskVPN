#!/usr/bin/env bash

if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "$0" ]]; then
    printf '[ERROR] Не запускайте этот скрипт через source или точку. Используйте: sudo bash %s [параметры]\n' "${BASH_SOURCE[0]}" >&2
    return 1
fi

set -Eeuo pipefail

SCRIPT_VERSION="1.2.1"
INSTALL_DIR="/opt/remnawave-node-cover"
CONTAINER_NAME="remnawave-node-cover"
CADDY_IMAGE="caddy:2.11.4-alpine"
DEFAULT_FALLBACK_PORT="8080"
DEFAULT_REALITY_TARGET_PORT="8443"

DOMAIN=""
EMAIL=""
FALLBACK_PORT="$DEFAULT_FALLBACK_PORT"
REALITY_TARGET_PORT="$DEFAULT_REALITY_TARGET_PORT"
TEMPLATE_SOURCE="random"
ASSUME_YES=false
REFRESH_TEMPLATE=false
SKIP_DNS_CHECK=false
PREVIOUS_DOMAIN=""

if [[ -t 1 ]]; then
    COLOR_RED=$'\033[1;31m'
    COLOR_GREEN=$'\033[1;32m'
    COLOR_YELLOW=$'\033[1;33m'
    COLOR_RESET=$'\033[0m'
else
    COLOR_RED=""
    COLOR_GREEN=""
    COLOR_YELLOW=""
    COLOR_RESET=""
fi

info() {
    printf '%s[INFO]%s %s\n' "$COLOR_GREEN" "$COLOR_RESET" "$*"
}

warn() {
    printf '%s[WARN]%s %s\n' "$COLOR_YELLOW" "$COLOR_RESET" "$*" >&2
}

die() {
    printf '%s[ERROR]%s %s\n' "$COLOR_RED" "$COLOR_RESET" "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Установка отдельного сайта-заглушки и TLS-сертификата на Remnawave Node.

Использование:
  sudo $0 [параметры]

Параметры:
  --domain DOMAIN          Поддомен ноды, например node1.example.com
  --email EMAIL            Email для Let's Encrypt
  --fallback-port PORT     Локальный HTTP-порт для Xray fallback (по умолчанию: 8080)
  --reality-port PORT      Локальный TLS target для Xray REALITY (по умолчанию: 8443)
  --template-source NAME   random, simple, sni или nothing (по умолчанию: random)
  --refresh-template       Заменить уже установленную заглушку новым шаблоном
  --skip-dns-check         Не проверять соответствие A-записи публичному IPv4 сервера
  --yes, -y                Не задавать вопросы; параметры domain и email обязательны
  --version                Показать версию
  --help, -h               Показать справку

Скрипт не изменяет конфигурацию Remnawave Node и Xray.
EOF
}

require_value() {
    local option="$1"
    local value="${2:-}"
    [[ -n "$value" ]] || die "Для $option требуется значение."
}

parse_args() {
    while (($# > 0)); do
        case "$1" in
            --domain)
                require_value "$1" "${2:-}"
                DOMAIN="$2"
                shift 2
                ;;
            --domain=*)
                DOMAIN="${1#*=}"
                shift
                ;;
            --email)
                require_value "$1" "${2:-}"
                EMAIL="$2"
                shift 2
                ;;
            --email=*)
                EMAIL="${1#*=}"
                shift
                ;;
            --fallback-port)
                require_value "$1" "${2:-}"
                FALLBACK_PORT="$2"
                shift 2
                ;;
            --fallback-port=*)
                FALLBACK_PORT="${1#*=}"
                shift
                ;;
            --reality-port)
                require_value "$1" "${2:-}"
                REALITY_TARGET_PORT="$2"
                shift 2
                ;;
            --reality-port=*)
                REALITY_TARGET_PORT="${1#*=}"
                shift
                ;;
            --template-source)
                require_value "$1" "${2:-}"
                TEMPLATE_SOURCE="$2"
                shift 2
                ;;
            --template-source=*)
                TEMPLATE_SOURCE="${1#*=}"
                shift
                ;;
            --refresh-template)
                REFRESH_TEMPLATE=true
                shift
                ;;
            --skip-dns-check)
                SKIP_DNS_CHECK=true
                shift
                ;;
            --yes|-y)
                ASSUME_YES=true
                shift
                ;;
            --version)
                printf '%s\n' "$SCRIPT_VERSION"
                exit 0
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                die "Неизвестный параметр: $1"
                ;;
        esac
    done
}

prompt_if_missing() {
    if [[ -z "$DOMAIN" ]]; then
        $ASSUME_YES && die "В режиме --yes необходимо указать --domain."
        read -r -p "Поддомен этой ноды: " DOMAIN
    fi

    if [[ -z "$EMAIL" ]]; then
        $ASSUME_YES && die "В режиме --yes необходимо указать --email."
        read -r -p "Email для Let's Encrypt: " EMAIL
    fi
}

validate_input() {
    DOMAIN="${DOMAIN,,}"
    TEMPLATE_SOURCE="${TEMPLATE_SOURCE,,}"

    [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] \
        || die "Некорректный домен: $DOMAIN"
    [[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] \
        || die "Некорректный email: $EMAIL"
    [[ "$FALLBACK_PORT" =~ ^[0-9]+$ ]] \
        || die "Fallback-порт должен быть числом."
    ((FALLBACK_PORT >= 1024 && FALLBACK_PORT <= 65535)) \
        || die "Fallback-порт должен находиться в диапазоне 1024-65535."
    [[ "$FALLBACK_PORT" != "2222" ]] \
        || die "Порт 2222 обычно занят API Remnawave Node. Выберите другой fallback-порт."
    [[ "$REALITY_TARGET_PORT" =~ ^[0-9]+$ ]] \
        || die "REALITY target-порт должен быть числом."
    ((REALITY_TARGET_PORT >= 1024 && REALITY_TARGET_PORT <= 65535)) \
        || die "REALITY target-порт должен находиться в диапазоне 1024-65535."
    [[ "$REALITY_TARGET_PORT" != "2222" ]] \
        || die "Порт 2222 обычно занят API Remnawave Node. Выберите другой REALITY target-порт."
    [[ "$REALITY_TARGET_PORT" != "$FALLBACK_PORT" ]] \
        || die "HTTP fallback и TLS target REALITY должны использовать разные порты."

    case "$TEMPLATE_SOURCE" in
        random|simple|sni|nothing) ;;
        *) die "Неизвестный источник шаблона: $TEMPLATE_SOURCE" ;;
    esac
}

confirm_installation() {
    $ASSUME_YES && return 0

    printf '\nБудет установлено:\n'
    printf '  Домен:              %s\n' "$DOMAIN"
    printf '  Caddy HTTP:         0.0.0.0:80\n'
    printf '  Xray HTTP fallback: 127.0.0.1:%s\n' "$FALLBACK_PORT"
    printf '  REALITY TLS target: 127.0.0.1:%s\n' "$REALITY_TARGET_PORT"
    printf '  Каталог:            %s\n' "$INSTALL_DIR"
    printf '  Источник шаблона:   %s\n\n' "$TEMPLATE_SOURCE"

    local answer
    read -r -p "Продолжить? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]$ ]] || exit 0
}

require_supported_system() {
    ((EUID == 0)) || die "Запустите скрипт с правами root: sudo $0"
    [[ -r /etc/os-release ]] || die "Не удалось определить операционную систему."

    # shellcheck disable=SC1091
    source /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) ;;
        *) die "Поддерживаются Ubuntu и Debian. Обнаружено: ${PRETTY_NAME:-unknown}" ;;
    esac
}

install_dependencies() {
    local missing=false
    local command_name
    for command_name in curl unzip certbot dig ss openssl; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            missing=true
            break
        fi
    done

    if $missing; then
        info "Устанавливаю системные зависимости..."
        apt-get update -y
        DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y \
            ca-certificates curl unzip certbot dnsutils iproute2 openssl
    fi
}

require_docker() {
    command -v docker >/dev/null 2>&1 \
        || die "Docker не найден. Сначала установите Remnawave Node по официальной инструкции."
    docker compose version >/dev/null 2>&1 \
        || die "Не найден Docker Compose plugin (команда 'docker compose')."
    docker info >/dev/null 2>&1 \
        || die "Docker daemon недоступен."
}

pull_caddy_image() {
    if docker image inspect "$CADDY_IMAGE" >/dev/null 2>&1; then
        info "Образ $CADDY_IMAGE уже загружен."
        return 0
    fi

    local attempt
    local max_attempts=4
    for ((attempt = 1; attempt <= max_attempts; attempt++)); do
        info "Загружаю образ $CADDY_IMAGE (попытка $attempt/$max_attempts)..."
        if docker pull "$CADDY_IMAGE"; then
            return 0
        fi

        warn "Docker не смог загрузить образ. Проверяю DNS перед повторной попыткой."
        if command -v resolvectl >/dev/null 2>&1; then
            resolvectl flush-caches >/dev/null 2>&1 || true
        fi

        if ((attempt < max_attempts)); then
            sleep $((attempt * 5))
        fi
    done

    if ! getent ahosts registry-1.docker.io >/dev/null 2>&1; then
        warn "Системный DNS не разрешает registry-1.docker.io."
        warn "Проверьте: resolvectl status"
        warn "Безопасная первая попытка восстановления: systemctl restart systemd-resolved"
    fi

    die "Не удалось загрузить $CADDY_IMAGE. После восстановления DNS повторно запустите скрипт — уже скачанный шаблон сохранится."
}

dns_problem() {
    warn "$1"
    if $ASSUME_YES; then
        die "Исправьте DNS или явно добавьте --skip-dns-check."
    fi

    local answer
    read -r -p "Продолжить несмотря на предупреждение? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]$ ]] || exit 1
}

check_dns() {
    $SKIP_DNS_CHECK && {
        warn "Проверка DNS отключена параметром --skip-dns-check."
        return 0
    }

    local public_ipv4
    local -a dns_ipv4=()
    public_ipv4="$(curl -4fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
    mapfile -t dns_ipv4 < <(dig +short A "$DOMAIN" | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | sort -u)

    ((${#dns_ipv4[@]} > 0)) \
        || dns_problem "У $DOMAIN не найдена A-запись. Создайте её до выпуска сертификата."

    if [[ -n "$public_ipv4" ]]; then
        local found=false
        local dns_ip
        for dns_ip in "${dns_ipv4[@]}"; do
            [[ "$dns_ip" == "$public_ipv4" ]] && found=true
        done
        $found || dns_problem \
            "A-запись $DOMAIN (${dns_ipv4[*]}) не совпадает с публичным IP сервера ($public_ipv4). Проверьте DNS и отключите Cloudflare Proxy."
    else
        warn "Не удалось определить публичный IPv4; проверено только наличие A-записи."
    fi
}

read_previous_setting() {
    local key="$1"
    local state_file="$INSTALL_DIR/installation.env"
    [[ -r "$state_file" ]] || return 0
    awk -F= -v requested_key="$key" '$1 == requested_key {print substr($0, index($0, "=") + 1); exit}' "$state_file"
}

is_managed_caddy_running() {
    docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME" || return 1

    local compose_working_dir
    compose_working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER_NAME" 2>/dev/null || true)"
    [[ "$compose_working_dir" == "$INSTALL_DIR" ]]
}

check_ports() {
    if ss -H -ltn | awk '{print $4}' | grep -Eq '(^|:|\])80$'; then
        is_managed_caddy_running \
            || die "Порт 80 уже занят. Освободите его для Caddy и проверки Let's Encrypt HTTP-01."
    fi

    if ss -H -ltn | awk '{print $4}' | grep -Eq "(^|:|\\])${FALLBACK_PORT}$"; then
        local previous_fallback_port
        previous_fallback_port="$(read_previous_setting FALLBACK_PORT)"
        if ! is_managed_caddy_running || [[ "$previous_fallback_port" != "$FALLBACK_PORT" ]]; then
            die "Локальный fallback-порт $FALLBACK_PORT уже занят. Укажите другой через --fallback-port."
        fi
    fi

    if ss -H -ltn | awk '{print $4}' | grep -Eq "(^|:|\\])${REALITY_TARGET_PORT}$"; then
        local previous_reality_target_port
        previous_reality_target_port="$(read_previous_setting REALITY_TARGET_PORT)"
        if ! is_managed_caddy_running \
            || { [[ -n "$previous_reality_target_port" ]] \
                && [[ "$previous_reality_target_port" != "$REALITY_TARGET_PORT" ]]; }; then
            die "Локальный REALITY target-порт $REALITY_TARGET_PORT уже занят. Укажите другой через --reality-port."
        fi
    fi
}

remove_stale_renewal_hook() {
    if [[ -n "$PREVIOUS_DOMAIN" && "$PREVIOUS_DOMAIN" != "$DOMAIN" ]]; then
        if [[ "$PREVIOUS_DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
            rm -f "/etc/letsencrypt/renewal-hooks/deploy/50-remnawave-node-cover-${PREVIOUS_DOMAIN//./-}.sh"
            warn "Домен установки изменён с $PREVIOUS_DOMAIN на $DOMAIN; старый deploy-hook удалён."
        else
            warn "Предыдущее значение DOMAIN некорректно; deploy-hook не изменялся."
        fi
    fi
}

capture_previous_state() {
    PREVIOUS_DOMAIN="$(read_previous_setting DOMAIN)"
}

check_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        if ! ufw status 2>/dev/null | grep -Eq '^80(/tcp)?[[:space:]]+ALLOW'; then
            warn "UFW активен, но разрешающее правило TCP/80 не найдено."
            warn "Скрипт намеренно не меняет firewall, чтобы не оборвать текущую SSH-сессию."
            warn "После проверки SSH-правил откройте порт вручную: ufw allow 80/tcp"
        else
            info "UFW: разрешающее правило для TCP/80 уже существует."
        fi
    fi
}

prepare_directories() {
    mkdir -p \
        "$INSTALL_DIR/www" \
        "$INSTALL_DIR/acme/.well-known/acme-challenge" \
        "$INSTALL_DIR/certs" \
        "$INSTALL_DIR/backups"
    chmod 755 "$INSTALL_DIR" "$INSTALL_DIR/www" "$INSTALL_DIR/acme"
}

choose_template_source() {
    if [[ "$TEMPLATE_SOURCE" != "random" ]]; then
        printf '%s\n' "$TEMPLATE_SOURCE"
        return 0
    fi

    local -a sources=(simple sni nothing)
    printf '%s\n' "${sources[RANDOM % ${#sources[@]}]}"
}

install_random_template() (
    local selected_source
    local archive_url
    local temp_dir
    local source_root
    local staging_dir
    local selected_file
    local selected_dir
    local timestamp
    local -a candidates=()

    selected_source="$(choose_template_source)"
    case "$selected_source" in
        simple)
            archive_url="https://github.com/eGamesAPI/simple-web-templates/archive/refs/heads/main.zip"
            ;;
        sni)
            archive_url="https://github.com/distillium/sni-templates/archive/refs/heads/main.zip"
            ;;
        nothing)
            archive_url="https://github.com/prettyleaf/nothing-sni/archive/refs/heads/main.zip"
            ;;
    esac

    temp_dir="$(mktemp -d)"
    staging_dir="$temp_dir/staging"
    mkdir -p "$staging_dir"
    trap 'rm -rf -- "${temp_dir:-}"' EXIT

    info "Скачиваю случайный шаблон из источника '$selected_source'..."
    curl -fL --retry 3 --connect-timeout 15 --max-time 600 --limit-rate 4M \
        -o "$temp_dir/template.zip" "$archive_url"
    unzip -q "$temp_dir/template.zip" -d "$temp_dir/unpacked"
    source_root="$(find "$temp_dir/unpacked" -mindepth 1 -maxdepth 1 -type d -print -quit)"
    [[ -n "$source_root" ]] || die "Архив шаблонов имеет неожиданную структуру."

    if [[ "$selected_source" == "nothing" ]]; then
        mapfile -d '' -t candidates < <(find "$source_root" -maxdepth 2 -type f -name '*.html' -print0)
        ((${#candidates[@]} > 0)) || die "В архиве не найдены HTML-шаблоны."
        selected_file="${candidates[RANDOM % ${#candidates[@]}]}"
        cp "$selected_file" "$staging_dir/index.html"
    else
        mapfile -d '' -t candidates < <(find "$source_root" -mindepth 2 -maxdepth 5 -type f -iname 'index.html' -print0)
        ((${#candidates[@]} > 0)) || die "В архиве не найдены каталоги с index.html."
        selected_file="${candidates[RANDOM % ${#candidates[@]}]}"
        selected_dir="$(dirname "$selected_file")"
        cp -a "$selected_dir/." "$staging_dir/"
    fi

    [[ -s "$staging_dir/index.html" ]] || die "Выбранный шаблон не содержит непустой index.html."

    if [[ -n "$(find "$INSTALL_DIR/www" -mindepth 1 -print -quit)" ]]; then
        timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
        tar -czf "$INSTALL_DIR/backups/site-$timestamp.tar.gz" -C "$INSTALL_DIR/www" .
        find "$INSTALL_DIR/www" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    fi

    cp -a "$staging_dir/." "$INSTALL_DIR/www/"
    printf '%s\n' "$selected_source" > "$INSTALL_DIR/template-source"
    info "Установлена заглушка из источника '$selected_source'."
)

ensure_template() {
    if [[ ! -s "$INSTALL_DIR/www/index.html" ]] || $REFRESH_TEMPLATE; then
        install_random_template
    else
        info "Существующая заглушка сохранена. Для замены используйте --refresh-template."
    fi
}

write_caddy_config() {
    cat > "$INSTALL_DIR/Caddyfile" <<EOF
{
    admin off
    auto_https off
    servers {
        protocols h1 h2
    }
}

(cover_site) {
    root * /srv
    try_files {path} /index.html
    encode zstd gzip
    header {
        X-Robots-Tag "noindex, nofollow, noarchive, nosnippet, noimageindex"
        -Server
    }
    file_server
}

http://$DOMAIN {
    handle /.well-known/acme-challenge/* {
        root * /var/www/acme
        file_server
    }

    handle {
        import cover_site
    }
}

http://:$FALLBACK_PORT {
    bind 127.0.0.1
    import cover_site
}
EOF

    if [[ -s "$INSTALL_DIR/certs/fullchain.pem" \
        && -s "$INSTALL_DIR/certs/privkey.key" ]]; then
        cat >> "$INSTALL_DIR/Caddyfile" <<EOF

https://$DOMAIN:$REALITY_TARGET_PORT {
    bind 127.0.0.1
    tls /certs/fullchain.pem /certs/privkey.key
    import cover_site
}
EOF
    fi

    cat > "$INSTALL_DIR/docker-compose.yml" <<EOF
services:
  caddy:
    image: $CADDY_IMAGE
    container_name: $CONTAINER_NAME
    hostname: $CONTAINER_NAME
    restart: unless-stopped
    network_mode: host
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE
    security_opt:
      - no-new-privileges:true
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./www:/srv:ro
      - ./acme:/var/www/acme:ro
      - ./certs:/certs:ro
      - caddy_data:/data
      - caddy_config:/config
    tmpfs:
      - /tmp:size=16m,mode=1777
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"

volumes:
  caddy_data:
  caddy_config:
EOF

    cat > "$INSTALL_DIR/installation.env" <<EOF
DOMAIN=$DOMAIN
EMAIL=$EMAIL
FALLBACK_PORT=$FALLBACK_PORT
REALITY_TARGET_PORT=$REALITY_TARGET_PORT
TEMPLATE_SOURCE=$TEMPLATE_SOURCE
SCRIPT_VERSION=$SCRIPT_VERSION
EOF
    chmod 600 "$INSTALL_DIR/installation.env"
}

start_caddy() {
    info "Проверяю и запускаю Caddy..."
    docker compose -f "$INSTALL_DIR/docker-compose.yml" config -q
    docker run --rm \
        --network none \
        --cap-drop ALL \
        --cap-add NET_BIND_SERVICE \
        --security-opt no-new-privileges:true \
        --volume "$INSTALL_DIR/Caddyfile:/etc/caddy/Caddyfile:ro" \
        --volume "$INSTALL_DIR/certs:/certs:ro" \
        "$CADDY_IMAGE" \
        caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    docker compose -f "$INSTALL_DIR/docker-compose.yml" up -d --force-recreate caddy

    local challenge_token
    challenge_token="node-cover-$(openssl rand -hex 8)"
    printf '%s' "$challenge_token" > "$INSTALL_DIR/acme/.well-known/acme-challenge/$challenge_token"

    local attempt
    for attempt in {1..15}; do
        if curl -fsS --max-time 3 -H "Host: $DOMAIN" \
            "http://127.0.0.1/.well-known/acme-challenge/$challenge_token" 2>/dev/null \
            | grep -Fxq "$challenge_token"; then
            rm -f "$INSTALL_DIR/acme/.well-known/acme-challenge/$challenge_token"
            return 0
        fi
        sleep 1
    done

    rm -f "$INSTALL_DIR/acme/.well-known/acme-challenge/$challenge_token"
    docker logs --tail 50 "$CONTAINER_NAME" >&2 || true
    die "Caddy не прошёл локальную проверку HTTP-01."
}

verify_reality_tls_target() {
    local attempt
    info "Проверяю локальный TLS target REALITY на 127.0.0.1:$REALITY_TARGET_PORT..."
    for attempt in {1..15}; do
        if curl -fsS --max-time 3 \
            --resolve "$DOMAIN:$REALITY_TARGET_PORT:127.0.0.1" \
            --cacert "$INSTALL_DIR/certs/fullchain.pem" \
            "https://$DOMAIN:$REALITY_TARGET_PORT/" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    docker logs --tail 50 "$CONTAINER_NAME" >&2 || true
    die "Caddy не прошёл локальную TLS-проверку на порту $REALITY_TARGET_PORT."
}

issue_certificate() {
    info "Получаю или обновляю сертификат Let's Encrypt для $DOMAIN..."
    certbot certonly \
        --webroot \
        --webroot-path "$INSTALL_DIR/acme" \
        --domain "$DOMAIN" \
        --cert-name "$DOMAIN" \
        --email "$EMAIL" \
        --agree-tos \
        --non-interactive \
        --keep-until-expiring \
        --preferred-challenges http

    [[ -s "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]] \
        || die "Certbot завершился без файла fullchain.pem."
    [[ -s "/etc/letsencrypt/live/$DOMAIN/privkey.pem" ]] \
        || die "Certbot завершился без файла privkey.pem."
}

deploy_certificate() {
    install -o root -g root -m 0644 \
        "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
        "$INSTALL_DIR/certs/fullchain.pem"
    install -o root -g root -m 0600 \
        "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
        "$INSTALL_DIR/certs/privkey.key"
}

install_renewal_hook() {
    local hook_dir="/etc/letsencrypt/renewal-hooks/deploy"
    local hook_path="$hook_dir/50-remnawave-node-cover-${DOMAIN//./-}.sh"
    mkdir -p "$hook_dir"

    cat > "$hook_path" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_LINEAGE="/etc/letsencrypt/live/$DOMAIN"
TARGET_DIR="$INSTALL_DIR/certs"

[[ "\${RENEWED_LINEAGE:-}" == "\$EXPECTED_LINEAGE" ]] || exit 0
install -o root -g root -m 0644 "\$RENEWED_LINEAGE/fullchain.pem" "\$TARGET_DIR/fullchain.pem"
install -o root -g root -m 0600 "\$RENEWED_LINEAGE/privkey.pem" "\$TARGET_DIR/privkey.key"
docker compose -f "$INSTALL_DIR/docker-compose.yml" restart caddy >/dev/null
logger -t remnawave-node-cover "TLS certificate for $DOMAIN was renewed and copied to \$TARGET_DIR"
EOF
    chmod 750 "$hook_path"

    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files certbot.timer >/dev/null 2>&1; then
        systemctl enable --now certbot.timer >/dev/null 2>&1 \
            || warn "Не удалось включить certbot.timer; проверьте автоматическое продление вручную."
    else
        warn "certbot.timer не найден; настройте периодический запуск 'certbot renew'."
    fi
}

print_result() {
    cat <<EOF

${COLOR_GREEN}Готово.${COLOR_RESET}

Сайт-заглушка:
  HTTP:                 http://$DOMAIN
  Xray fallback:        127.0.0.1:$FALLBACK_PORT (обычный HTTP без TLS)
  REALITY TLS target:   127.0.0.1:$REALITY_TARGET_PORT

Стабильные пути сертификата на ноде:
  Сертификат:           $INSTALL_DIR/certs/fullchain.pem
  Приватный ключ:       $INSTALL_DIR/certs/privkey.key

Управление Caddy:
  Логи:                 docker logs $CONTAINER_NAME
  Перезапуск:           docker compose -f $INSTALL_DIR/docker-compose.yml restart

Важно: скрипт не изменял /opt/remnanode/docker-compose.yml и Xray-конфигурацию.
Для официальной схемы Remnawave сертификаты нужно также разместить/синхронизировать
на сервер панели и смонтировать в backend панели. Подробности находятся в:
  docs/remnawave-node-cover.md
EOF
}

main() {
    parse_args "$@"
    prompt_if_missing
    validate_input
    confirm_installation
    require_supported_system
    install_dependencies
    require_docker
    check_dns
    check_ports
    check_firewall
    pull_caddy_image
    prepare_directories
    capture_previous_state
    ensure_template
    write_caddy_config
    start_caddy
    issue_certificate
    deploy_certificate
    write_caddy_config
    start_caddy
    verify_reality_tls_target
    remove_stale_renewal_hook
    install_renewal_hook
    print_result
}

main "$@"
