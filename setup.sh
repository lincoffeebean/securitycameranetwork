#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$ROOT_DIR/.securitycam"
CONFIG_FILE="$CONFIG_DIR/config.json"
VENV_DIR="$ROOT_DIR/.venv"
SERVICE_NAME="securitycameranetwork"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

info() { printf '  %s\n' "$*"; }
ok() { printf '\n[OK] %s\n' "$*"; }
fail() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }

if [[ ${EUID:-$(id -u)} -eq 0 && -n "${SUDO_USER:-}" ]]; then
    fail "Run ./setup.sh as your normal user, not with sudo. Setup will request administrator access when needed."
fi

run_root() {
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail "This step needs administrator access, but sudo is unavailable."
    fi
}

prompt() {
    local message="$1" default="$2" answer
    read -r -p "$message [$default]: " answer
    printf '%s' "${answer:-$default}"
}

confirm() {
    local message="$1" default="${2:-Y}" answer
    read -r -p "$message [$default/n] " answer
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy]$ ]]
}

config_get() {
    python3 - "$CONFIG_FILE" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as file:
    value = json.load(file).get(sys.argv[2], "")
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

detect_lan_ip() {
    local address=""
    if command -v ip >/dev/null 2>&1; then
        address="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
    fi
    if [[ -z "$address" ]] && command -v hostname >/dev/null 2>&1; then
        address="$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/ {print; exit}')"
    fi
    printf '%s' "$address"
}

ensure_python() {
    if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
        info "Installing Python 3.10+ and virtual-environment support..."
        command -v apt-get >/dev/null 2>&1 || fail "Python 3.10+ is required. Install it and run setup again."
        run_root apt-get update || fail "Could not update Ubuntu package lists."
        run_root apt-get install -y python3 python3-venv python3-pip || fail "Could not install Python."
    fi

    if ! python3 -m venv --help >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || fail "Python venv support is required."
        run_root apt-get install -y python3-venv || fail "Could not install python3-venv."
    fi
}

install_python_app() {
    info "Creating Python environment..."
    python3 -m venv "$VENV_DIR" || fail "Could not create $VENV_DIR."
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null || fail "Could not update pip."
    "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/server/requirements.txt" || fail "Dependency installation failed."
}

write_config() {
    mkdir -p "$CONFIG_DIR" || fail "Could not create $CONFIG_DIR."
    CONFIG_HOST="$host" CONFIG_PORT="$port" CONFIG_RECORDINGS="$recordings_path" \
    CONFIG_STORAGE="$storage_limit" CONFIG_HTTPS="$https" CONFIG_BOOT="$start_on_boot" \
    "$VENV_DIR/bin/python" - "$CONFIG_FILE" <<'PY'
import json, os, sys
config = {
    "host": os.environ["CONFIG_HOST"],
    "port": int(os.environ["CONFIG_PORT"]),
    "recordings_path": os.environ["CONFIG_RECORDINGS"],
    "max_storage_gb": None if os.environ["CONFIG_STORAGE"] == "unlimited" else float(os.environ["CONFIG_STORAGE"]),
    "https": os.environ["CONFIG_HTTPS"] == "true",
    "start_on_boot": os.environ["CONFIG_BOOT"] == "true",
}
with open(sys.argv[1], "w", encoding="utf-8") as file:
    json.dump(config, file, indent=2)
    file.write("\n")
PY
}

install_systemd_service() {
    command -v systemctl >/dev/null 2>&1 || fail "systemd is unavailable; choose no for start-on-boot."
    local install_user install_group unit
    install_user="${SUDO_USER:-$(id -un)}"
    install_group="$(id -gn "$install_user")"
    unit="[Unit]
Description=Security Camera Network
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$install_user
Group=$install_group
WorkingDirectory="$ROOT_DIR"
Environment="SECURITYCAM_CONFIG=$CONFIG_FILE"
ExecStart="$ROOT_DIR/securitycam" run
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"
    printf '%s\n' "$unit" | run_root tee "$SERVICE_FILE" >/dev/null || fail "Could not install the systemd service."
    run_root systemctl daemon-reload || fail "systemd daemon-reload failed."
    run_root systemctl enable "$SERVICE_NAME" >/dev/null || fail "Could not enable the service."
}

configure_https() {
    if ! command -v caddy >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || fail "Caddy is required for HTTPS. Install Caddy and rerun setup."
        info "Installing Caddy..."
        run_root apt-get update || fail "Could not update Ubuntu package lists."
        run_root apt-get install -y caddy || fail "Could not install Caddy."
    fi
    local caddy_config caddy_fragment
    caddy_config="https://$lan_ip {
    tls internal
    reverse_proxy 127.0.0.1:$port
}"
    caddy_fragment="/etc/caddy/Caddyfile.d/securitycameranetwork.caddy"
    run_root mkdir -p /etc/caddy/Caddyfile.d
    printf '%s\n' "$caddy_config" | run_root tee "$caddy_fragment" >/dev/null || fail "Could not write the Caddy configuration."
    if ! run_root grep -qF 'import Caddyfile.d/*.caddy' /etc/caddy/Caddyfile; then
        printf '\nimport Caddyfile.d/*.caddy\n' | run_root tee -a /etc/caddy/Caddyfile >/dev/null
    fi
    run_root systemctl enable --now caddy >/dev/null || fail "Could not start Caddy."
    run_root systemctl reload caddy || fail "Could not reload Caddy."
}

printf '\n========================================\n'
printf '  Security Camera Network Setup\n'
printf '========================================\n\n'

ensure_python

repair=false
if [[ -f "$CONFIG_FILE" ]]; then
    printf 'Security Camera Network is already configured.\n\n'
    printf '  1. Reconfigure\n  2. Repair installation\n  3. Exit\n\n'
    read -r -p '> ' existing_choice
    case "${existing_choice:-3}" in
        1) ;;
        2) repair=true ;;
        *) exit 0 ;;
    esac
fi

lan_ip="$(detect_lan_ip)"
[[ -n "$lan_ip" ]] || lan_ip="LAN-IP-NOT-DETECTED"

if [[ "$repair" == true ]]; then
    host="$(config_get host)"
    port="$(config_get port)"
    recordings_path="$(config_get recordings_path)"
    storage_limit="$(config_get max_storage_gb)"
    [[ -n "$storage_limit" && "$storage_limit" != "None" ]] || storage_limit="unlimited"
    https="$(config_get https)"
    start_on_boot="$(config_get start_on_boot)"
else
    info "System: $(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-Linux}")"
    info "LAN address: $lan_ip"
    printf '\nHow would you like to run the server?\n\n'
    printf '  1. Simple LAN setup\n  2. LAN + HTTPS\n  3. Advanced/custom setup\n\n'
    read -r -p '> ' mode
    mode="${mode:-1}"

    default_port=8000
    [[ -f "$CONFIG_FILE" ]] && default_port="$(config_get port)"
    while true; do
        port="$(prompt 'Server port' "$default_port")"
        [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) && break
        info "Enter a port from 1 to 65535."
    done

    default_recordings="$ROOT_DIR/recordings"
    [[ -f "$CONFIG_FILE" ]] && default_recordings="$(config_get recordings_path)"
    recordings_path="$(prompt 'Recordings location' "$default_recordings")"
    [[ "$recordings_path" = /* ]] || recordings_path="$ROOT_DIR/$recordings_path"

    printf '\nRecording storage limit:\n\n  1. Unlimited\n  2. 10 GB\n  3. 50 GB\n  4. Custom\n\n'
    read -r -p '> ' storage_choice
    case "${storage_choice:-1}" in
        2) storage_limit=10 ;;
        3) storage_limit=50 ;;
        4)
            while true; do
                storage_limit="$(prompt 'Maximum storage in GB' '10')"
                [[ "$storage_limit" =~ ^[0-9]+([.][0-9]+)?$ ]] && break
                info "Enter a positive number."
            done
            ;;
        *) storage_limit=unlimited ;;
    esac

    if confirm "Start Security Camera Network automatically when this computer boots?" Y; then
        start_on_boot=true
    else
        start_on_boot=false
    fi
    [[ "$mode" == 2 ]] && https=true || https=false
    [[ "$https" == true ]] && host=127.0.0.1 || host=0.0.0.0
fi

install_python_app

if [[ -f "$CONFIG_FILE" ]]; then
    "$ROOT_DIR/securitycam" stop >/dev/null 2>&1 || true
fi
if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .; then
    fail "Port $port is already in use. Run ./setup.sh and choose another port."
fi

mkdir -p "$recordings_path" || fail "The recordings directory could not be created: $recordings_path"
write_config

(
    cd "$ROOT_DIR/server" && SECURITYCAM_CONFIG="$CONFIG_FILE" "$VENV_DIR/bin/python" -c 'from app.main import app'
) || fail "FastAPI application validation failed."

if [[ "$start_on_boot" == true ]]; then
    install_systemd_service
elif [[ -f "$SERVICE_FILE" ]]; then
    run_root systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    run_root rm -f "$SERVICE_FILE"
    run_root systemctl daemon-reload
fi

if [[ "$https" == true ]]; then
    [[ "$lan_ip" != "LAN-IP-NOT-DETECTED" ]] || fail "HTTPS setup needs a detected LAN IP."
    configure_https
elif [[ -f /etc/caddy/Caddyfile.d/securitycameranetwork.caddy ]]; then
    run_root rm -f /etc/caddy/Caddyfile.d/securitycameranetwork.caddy
    run_root systemctl reload caddy >/dev/null 2>&1 || true
fi

"$ROOT_DIR/securitycam" restart || fail "The server could not start. Check: ./securitycam logs"

if command -v ufw >/dev/null 2>&1 && run_root ufw status 2>/dev/null | grep -q '^Status: active'; then
    if [[ "$https" == true ]]; then
        run_root ufw allow 443/tcp >/dev/null
    else
        run_root ufw allow "$port/tcp" >/dev/null
    fi
fi

if [[ "$https" == true ]]; then
    dashboard="https://$lan_ip"
else
    dashboard="http://$lan_ip:$port"
fi

ok "Security Camera Network is running"
printf '\nDashboard:\n%s\n\nCamera:\n%s/camera\n\n' "$dashboard" "$dashboard"
printf 'Any device on the same LAN/Wi-Fi can open this address.\n'
[[ "$lan_ip" != "LAN-IP-NOT-DETECTED" ]] || printf 'LAN detection failed; run "ip -4 address" and use this computer\x27s private address.\n'
[[ "$https" == false ]] || printf 'HTTPS uses Caddy\x27s local CA; client devices must trust its root certificate.\n'
[[ "$storage_limit" == "unlimited" ]] || printf 'Storage limit is saved but automatic deletion is not enabled; monitor recordings with ./securitycam recordings.\n'
