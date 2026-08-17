#!/usr/bin/env bash

set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$ROOT_DIR/.securitycam"
CONFIG_FILE="$CONFIG_DIR/config.json"
VENV_DIR="$ROOT_DIR/.venv"
SERVICE_NAME="securitycameranetwork"
SERVICE_FILE="${SECURITYCAM_SERVICE_FILE:-/etc/systemd/system/$SERVICE_NAME.service}"
CADDY_FILE="${SECURITYCAM_CADDY_FILE:-/etc/caddy/Caddyfile}"
CADDY_FRAGMENT="${SECURITYCAM_CADDY_FRAGMENT:-/etc/caddy/Caddyfile.d/securitycameranetwork.caddy}"
CADDY_KEYRING="${SECURITYCAM_CADDY_KEYRING:-/usr/share/keyrings/caddy-stable-archive-keyring.gpg}"
CADDY_SOURCE="${SECURITYCAM_CADDY_SOURCE:-/etc/apt/sources.list.d/caddy-stable.list}"

info() { printf '  %s\n' "$*"; }
ok() { printf '\n[OK] %s\n' "$*"; }
fail() {
    if [[ "${TRANSACTION_ACTIVE:-false}" == true && -z "${FAILURE_REASON:-}" ]]; then
        FAILURE_REASON="${CURRENT_STAGE_REASON:-$*}"
    fi
    printf '\n[ERROR] %s\n' "$*" >&2
    exit 1
}

run_root() {
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -v || fail "Administrator access was not granted."
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

is_debian_family() {
    local release_file="${1:-${SECURITYCAM_OS_RELEASE:-/etc/os-release}}"
    [[ -r "$release_file" ]] || return 1
    grep -Eiq '^(ID|ID_LIKE)=.*(debian|ubuntu)' "$release_file"
}

venv_available() {
    local python_bin="${1:-python3}" probe_dir probe_python result=0
    probe_dir="$(mktemp -d)" || return 1
    "$python_bin" -m venv "$probe_dir/venv" >/dev/null 2>&1 || result=$?
    probe_python="$probe_dir/venv/bin/python"
    [[ -x "$probe_python" ]] || probe_python="$probe_dir/venv/Scripts/python.exe"
    if ((result == 0)); then
        [[ -x "$probe_python" ]] \
            && "$probe_python" -c 'import ensurepip' >/dev/null 2>&1 \
            && "$probe_python" -m pip --version >/dev/null 2>&1 \
            || result=1
    fi
    rm -rf -- "$probe_dir"
    return "$result"
}

versioned_venv_package() {
    python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")'
}

install_debian_package() {
    local package="$1" fallback="${2:-}" release_file="${3:-/etc/os-release}"
    is_debian_family "$release_file" || fail "$package is required. Install the equivalent package for your distribution, then rerun ./setup.sh."
    command -v apt-get >/dev/null 2>&1 || fail "apt-get is unavailable. Install $package manually, then rerun ./setup.sh."

    printf '\nThe installer needs to install:\n  %s\n\n' "$package"
    if ! confirm "Install required packages now?" Y; then
        fail "Installation declined. Install $package for your system, then rerun ./setup.sh."
    fi

    info "Updating package information..."
    run_root apt-get update || fail "apt-get update failed. Check the output above and your network connection."
    info "Installing $package..."
    if run_root apt-get install -y "$package"; then
        ok "$package installed"
        return
    fi

    [[ -n "$fallback" && "$fallback" != "$package" ]] || fail "Could not install $package. Check the package-manager output above."
    info "$package was unavailable; trying $fallback for the active Python version..."
    run_root apt-get install -y "$fallback" || fail "Could not install $package or $fallback. Install Python venv support manually, then rerun ./setup.sh."
    ok "$fallback installed"
}

check_system_requirements() {
    local version fallback
    printf '\nChecking system requirements...\n\n'

    if ! command -v python3 >/dev/null 2>&1; then
        printf '[MISSING] Python 3\n'
        install_debian_package python3
    fi

    version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" || fail "Python 3 could not be started."
    python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || fail "Python 3.10 or newer is required; found Python $version."
    printf '[OK] Python %s\n' "$version"

    if venv_available python3; then
        printf '[OK] Python virtual environment support\n'
        return
    fi

    printf '[MISSING] Python virtual environment support\n'
    fallback="$(versioned_venv_package)"
    install_debian_package python3-venv "$fallback"
    venv_available python3 || fail "Python virtual environments still cannot be created after package installation. Try installing $fallback manually."
    printf '[OK] Python virtual environment support\n'
}

venv_is_valid() {
    [[ -x "$VENV_DIR/bin/python" ]] \
        && "$VENV_DIR/bin/python" -c 'import ensurepip, sys; raise SystemExit(sys.prefix == sys.base_prefix)' >/dev/null 2>&1 \
        && "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1
}

prepare_virtualenv() {
    if venv_is_valid; then
        ok "Reusing existing Python environment"
        return
    fi

    if [[ -e "$VENV_DIR" ]]; then
        [[ "$VENV_DIR" == "$ROOT_DIR/.venv" && "$VENV_DIR" != "/.venv" ]] || fail "Refusing to remove unexpected environment path: $VENV_DIR"
        info "Found incomplete Python environment from a previous setup attempt."
        info "Recreating .venv..."
        rm -rf -- "$VENV_DIR" || fail "Could not remove the incomplete environment at $VENV_DIR."
    else
        info "Creating Python environment..."
    fi

    python3 -m venv "$VENV_DIR" || fail "Could not create $VENV_DIR even though venv support passed its preflight check."
    venv_is_valid || fail "The new Python environment is incomplete: $VENV_DIR"
    ok "Python environment created"
}

install_python_app() {
    local pip_log
    prepare_virtualenv
    info "Installing Python dependencies..."
    if [[ "${SECURITYCAM_VERBOSE:-0}" == 1 ]]; then
        "$VENV_DIR/bin/python" -m pip install --upgrade pip || fail "Could not update pip inside .venv."
        "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/server/requirements.txt" || fail "Dependency installation failed."
    else
        pip_log="$(mktemp)" || fail "Could not create a temporary pip log."
        if ! "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade pip >"$pip_log" 2>&1 \
            || ! "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --quiet -r "$ROOT_DIR/server/requirements.txt" >>"$pip_log" 2>&1; then
            printf '\nPython dependency installation output:\n' >&2
            cat "$pip_log" >&2
            rm -f "$pip_log"
            fail "Dependency installation failed."
        fi
        rm -f "$pip_log"
    fi
    ok "Python dependencies installed"
}

write_config() {
    local output_file="${1:-$CONFIG_FILE}"
    mkdir -p "$(dirname "$output_file")" || fail "Could not create the configuration directory."
    CONFIG_HOST="$host" CONFIG_PORT="$port" CONFIG_RECORDINGS="$recordings_path" \
    CONFIG_STORAGE="$storage_limit" CONFIG_HTTPS="$https" CONFIG_BOOT="$start_on_boot" \
    "$VENV_DIR/bin/python" - "$output_file" <<'PY'
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

systemd_escape_path() {
    python3 - "$1" <<'PY'
import sys

path = sys.argv[1]
if not path.startswith("/") or "\0" in path or "\n" in path or "\r" in path:
    raise SystemExit(1)

safe = b"/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
result = []
for byte in path.encode("utf-8"):
    if byte == ord("%"):
        result.append("%%")
    elif byte in safe:
        result.append(chr(byte))
    else:
        result.append(f"\\x{byte:02x}")
print("".join(result))
PY
}

write_systemd_unit() {
    local output_file="$1" config_path="$2" install_user install_group root_path config_value python_path server_path
    install_user="${SUDO_USER:-$(id -un)}"
    install_group="$(id -gn "$install_user")"
    root_path="$(systemd_escape_path "$ROOT_DIR")" || fail "The repository path cannot be represented safely in a systemd service."
    config_value="$(systemd_escape_path "$config_path")" || fail "The configuration path cannot be represented safely in a systemd service."
    python_path="$(systemd_escape_path "$VENV_DIR/bin/python")" || fail "The Python path cannot be represented safely in a systemd service."
    server_path="$(systemd_escape_path "$ROOT_DIR/server")" || fail "The server path cannot be represented safely in a systemd service."
    cat >"$output_file" <<EOF
[Unit]
Description=Security Camera Network
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$install_user
Group=$install_group
WorkingDirectory=$root_path
Environment=SECURITYCAM_CONFIG=$config_value
ExecStart=$python_path -m uvicorn app.main:app --app-dir $server_path --host $host --port $port
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

install_caddy_official() {
    local repo_tmp key_tmp source_tmp prerequisites
    if command -v caddy >/dev/null 2>&1 && caddy version >/dev/null 2>&1; then
        CADDY_INSTALL_COMPLETED=true
        printf '[OK] Caddy %s\n' "$(caddy version)"
        return
    fi

    is_debian_family || fail "Caddy is missing. Automatic installation is supported only on Debian/Ubuntu; install Caddy for your distribution and retry."
    command -v apt-get >/dev/null 2>&1 || fail "apt-get is unavailable; Caddy could not be installed."
    printf '\nHTTPS requires Caddy from the official Caddy Debian repository.\n'
    confirm "Install Caddy now?" Y || fail "HTTPS setup cancelled because Caddy is required."

    prerequisites=(debian-keyring debian-archive-keyring apt-transport-https curl)
    command -v gpg >/dev/null 2>&1 || prerequisites+=(gnupg)
    info "Installing Caddy repository prerequisites..."
    run_root apt-get update || fail "Could not update package information before installing Caddy prerequisites."
    run_root apt-get install -y "${prerequisites[@]}" || fail "Could not install the prerequisites for Caddy's official repository."

    if [[ ! -s "$CADDY_KEYRING" || ! -s "$CADDY_SOURCE" ]] \
        || ! grep -qF 'dl.cloudsmith.io/public/caddy/stable' "$CADDY_SOURCE" \
        || ! gpg --show-keys "$CADDY_KEYRING" >/dev/null 2>&1; then
        repo_tmp="$(mktemp -d)" || fail "Could not create temporary files for the Caddy repository."
        key_tmp="$repo_tmp/caddy-key.gpg"
        source_tmp="$repo_tmp/caddy-stable.list"
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor >"$key_tmp" \
            || { rm -rf -- "$repo_tmp"; fail "Could not download or verify the official Caddy signing key."; }
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' -o "$source_tmp" \
            || { rm -rf -- "$repo_tmp"; fail "Could not download the official Caddy apt source."; }
        [[ -s "$key_tmp" && -s "$source_tmp" ]] \
            && grep -qF 'dl.cloudsmith.io/public/caddy/stable' "$source_tmp" \
            && gpg --show-keys "$key_tmp" >/dev/null 2>&1 \
            || { rm -rf -- "$repo_tmp"; fail "The downloaded Caddy repository files were empty or invalid."; }
        run_root install -m 0644 "$key_tmp" "$CADDY_KEYRING" \
            || { rm -rf -- "$repo_tmp"; fail "Could not install the Caddy signing key."; }
        run_root install -m 0644 "$source_tmp" "$CADDY_SOURCE" \
            || { rm -rf -- "$repo_tmp"; fail "Could not install the Caddy apt source."; }
        rm -rf -- "$repo_tmp"
    fi

    run_root chmod o+r "$CADDY_KEYRING" "$CADDY_SOURCE" || fail "Could not set readable permissions on the Caddy repository files."
    info "Installing Caddy..."
    run_root apt-get update || fail "The official Caddy repository could not be refreshed."
    run_root apt-get install -y caddy || fail "Caddy installation failed. Review the apt output above, then retry HTTPS setup."
    command -v caddy >/dev/null 2>&1 && caddy version >/dev/null 2>&1 \
        || fail "Caddy was installed but 'caddy version' failed."
    CADDY_INSTALL_COMPLETED=true
    ok "Caddy $(caddy version) installed"
}

write_caddy_fragment() {
    local output_file="$1"
    cat >"$output_file" <<EOF
https://$lan_ip {
    tls internal
    reverse_proxy 127.0.0.1:$port
}
EOF
}

config_value_from() {
    python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as file:
    value = json.load(file).get(sys.argv[2], "")
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

config_address() {
    local source_file="$1" scheme config_port
    [[ -f "$source_file" ]] || return 1
    [[ "$(config_value_from "$source_file" https)" == true ]] && scheme=https || scheme=http
    config_port="$(config_value_from "$source_file" port)"
    if [[ "$scheme" == https ]]; then
        printf 'https://%s' "$lan_ip"
    else
        printf 'http://%s:%s' "$lan_ip" "$config_port"
    fi
}

TRANSACTION_ACTIVE=false
TRANSACTION_DIR=""
PENDING_CONFIG=""
PREVIOUS_CONFIG_EXISTS=false
PREVIOUS_SERVICE_EXISTS=false
PREVIOUS_SERVICE_ENABLED=false
PREVIOUS_RUNNING=false
PREVIOUS_CADDY_FILE_EXISTS=false
PREVIOUS_CADDY_FRAGMENT_EXISTS=false
PREVIOUS_CADDY_ACTIVE=false
PREVIOUS_CADDY_KEY_EXISTS=false
PREVIOUS_CADDY_SOURCE_EXISTS=false
CADDY_INSTALL_COMPLETED=false
PREVIOUS_ADDRESS=""
CUTOVER_STARTED=false
CURRENT_STAGE_REASON=""
FAILURE_REASON=""

begin_transaction() {
    mkdir -p "$CONFIG_DIR" || fail "Could not create $CONFIG_DIR."
    TRANSACTION_DIR="$(mktemp -d "$CONFIG_DIR/transaction.XXXXXX")" || fail "Could not create a setup transaction."
    PENDING_CONFIG="$TRANSACTION_DIR/config.pending.json"

    if [[ -f "$CONFIG_FILE" ]]; then
        PREVIOUS_CONFIG_EXISTS=true
        cp "$CONFIG_FILE" "$TRANSACTION_DIR/config.previous.json" || fail "Could not preserve the current configuration."
        PREVIOUS_ADDRESS="$(config_address "$CONFIG_FILE")"
    fi
    if [[ -f "$SERVICE_FILE" ]]; then
        PREVIOUS_SERVICE_EXISTS=true
        run_root cat "$SERVICE_FILE" >"$TRANSACTION_DIR/service.previous" || fail "Could not preserve the current systemd service."
        systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null && PREVIOUS_SERVICE_ENABLED=true
    fi
    if [[ -f "$CADDY_FILE" ]]; then
        PREVIOUS_CADDY_FILE_EXISTS=true
        run_root cat "$CADDY_FILE" >"$TRANSACTION_DIR/Caddyfile.previous" || fail "Could not preserve the current Caddyfile."
    fi
    if [[ -f "$CADDY_FRAGMENT" ]]; then
        PREVIOUS_CADDY_FRAGMENT_EXISTS=true
        run_root cat "$CADDY_FRAGMENT" >"$TRANSACTION_DIR/caddy-fragment.previous" || fail "Could not preserve the current Caddy configuration."
    fi
    if [[ -f "$CADDY_KEYRING" ]]; then
        PREVIOUS_CADDY_KEY_EXISTS=true
        run_root cat "$CADDY_KEYRING" >"$TRANSACTION_DIR/caddy-key.previous" || fail "Could not preserve the current Caddy signing key."
    fi
    if [[ -f "$CADDY_SOURCE" ]]; then
        PREVIOUS_CADDY_SOURCE_EXISTS=true
        run_root cat "$CADDY_SOURCE" >"$TRANSACTION_DIR/caddy-source.previous" || fail "Could not preserve the current Caddy apt source."
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy 2>/dev/null; then
        PREVIOUS_CADDY_ACTIVE=true
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        PREVIOUS_RUNNING=true
    elif [[ "$PREVIOUS_CONFIG_EXISTS" == true ]] \
        && SECURITYCAM_CONFIG="$CONFIG_FILE" "$ROOT_DIR/securitycam" status 2>/dev/null | grep -q 'Status:  Running'; then
        PREVIOUS_RUNNING=true
    fi

    TRANSACTION_ACTIVE=true
    trap rollback_transaction EXIT
}

restore_caddy_files() {
    if [[ "$PREVIOUS_CADDY_FILE_EXISTS" == true ]]; then
        run_root install -m 0644 "$TRANSACTION_DIR/Caddyfile.previous" "$CADDY_FILE" || true
    elif [[ -f "$TRANSACTION_DIR/Caddyfile.installed-default" ]]; then
        run_root install -m 0644 "$TRANSACTION_DIR/Caddyfile.installed-default" "$CADDY_FILE" || true
    fi
    if [[ "$PREVIOUS_CADDY_FRAGMENT_EXISTS" == true ]]; then
        run_root mkdir -p "$(dirname "$CADDY_FRAGMENT")" || true
        run_root install -m 0644 "$TRANSACTION_DIR/caddy-fragment.previous" "$CADDY_FRAGMENT" || true
    else
        run_root rm -f "$CADDY_FRAGMENT" || true
    fi
    if [[ "$CADDY_INSTALL_COMPLETED" == false ]]; then
        if [[ "$PREVIOUS_CADDY_KEY_EXISTS" == true ]]; then
            run_root install -m 0644 "$TRANSACTION_DIR/caddy-key.previous" "$CADDY_KEYRING" || true
        else
            run_root rm -f "$CADDY_KEYRING" || true
        fi
        if [[ "$PREVIOUS_CADDY_SOURCE_EXISTS" == true ]]; then
            run_root install -m 0644 "$TRANSACTION_DIR/caddy-source.previous" "$CADDY_SOURCE" || true
        else
            run_root rm -f "$CADDY_SOURCE" || true
        fi
    fi
}

rollback_transaction() {
    local status=$? restored_running=false
    trap - EXIT
    [[ "$TRANSACTION_ACTIVE" == true ]] || exit "$status"
    set +e

    if [[ "$CUTOVER_STARTED" == true ]]; then
        command -v systemctl >/dev/null 2>&1 && run_root systemctl stop "$SERVICE_NAME" >/dev/null 2>&1
        if [[ -f "$ROOT_DIR/.securitycam/server.pid" ]]; then
            SECURITYCAM_CONFIG="${PENDING_CONFIG:-$CONFIG_FILE}" "$ROOT_DIR/securitycam" stop >/dev/null 2>&1
        fi
    fi

    if [[ "$PREVIOUS_CONFIG_EXISTS" == true ]]; then
        cp "$TRANSACTION_DIR/config.previous.json" "$CONFIG_FILE"
    else
        rm -f "$CONFIG_FILE"
    fi

    if [[ "$PREVIOUS_SERVICE_EXISTS" == true ]]; then
        run_root install -m 0644 "$TRANSACTION_DIR/service.previous" "$SERVICE_FILE"
    else
        run_root rm -f "$SERVICE_FILE"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        run_root systemctl daemon-reload >/dev/null 2>&1
        if [[ "$PREVIOUS_SERVICE_ENABLED" == true ]]; then
            run_root systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
        else
            run_root systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
        fi
    fi

    restore_caddy_files
    if command -v systemctl >/dev/null 2>&1; then
        if [[ "$PREVIOUS_CADDY_ACTIVE" == true ]]; then
            run_root systemctl restart caddy >/dev/null 2>&1
        else
            run_root systemctl stop caddy >/dev/null 2>&1
        fi
    fi

    if [[ "$PREVIOUS_RUNNING" == true && "$PREVIOUS_CONFIG_EXISTS" == true ]]; then
        SECURITYCAM_CONFIG="$CONFIG_FILE" "$ROOT_DIR/securitycam" start >/dev/null 2>&1
        if SECURITYCAM_CONFIG="$CONFIG_FILE" "$ROOT_DIR/securitycam" status 2>/dev/null | grep -q 'Status:  Running'; then
            restored_running=true
        fi
    fi

    rm -rf -- "$TRANSACTION_DIR"
    if [[ "$PREVIOUS_CONFIG_EXISTS" == true ]]; then
        printf '\nYour previous configuration has been restored.\n' >&2
        if [[ -n "$FAILURE_REASON" ]]; then
            printf '\nReason:\n  %s\n\nYour existing server configuration remains unchanged.\n' "$FAILURE_REASON" >&2
        fi
        if [[ "$PREVIOUS_RUNNING" == true && "$restored_running" == true ]]; then
            printf '\nSecurity Camera Network is still available at:\n%s\n' "$PREVIOUS_ADDRESS" >&2
        elif [[ "$PREVIOUS_RUNNING" == true ]]; then
            printf 'The previous configuration was restored, but the server could not be restarted automatically. Run ./securitycam start.\n' >&2
        fi
    else
        printf '\nSetup failed before a configuration was activated.\n' >&2
    fi
    exit "$status"
}

prepare_caddy_candidate() {
    local candidate_dir="$TRANSACTION_DIR/caddy" candidate_file="$TRANSACTION_DIR/caddy/Caddyfile"
    mkdir -p "$candidate_dir/Caddyfile.d" || fail "Could not prepare the Caddy configuration."
    if [[ -f "$CADDY_FILE" ]]; then
        if [[ "$PREVIOUS_CADDY_FILE_EXISTS" == false && ! -f "$TRANSACTION_DIR/Caddyfile.installed-default" ]]; then
            run_root cat "$CADDY_FILE" >"$TRANSACTION_DIR/Caddyfile.installed-default" \
                || fail "Could not preserve Caddy's package configuration."
        fi
        run_root cat "$CADDY_FILE" >"$candidate_file" || fail "Could not read the current Caddyfile."
    else
        : >"$candidate_file"
    fi
    if [[ -d "$(dirname "$CADDY_FRAGMENT")" ]]; then
        run_root cp -a "$(dirname "$CADDY_FRAGMENT")/." "$candidate_dir/Caddyfile.d/" \
            || fail "Could not copy the current Caddy configuration."
        run_root chown -R "$(id -u):$(id -g)" "$candidate_dir" \
            || fail "Could not prepare writable Caddy validation files."
    fi
    run_root rm -f "$candidate_dir/Caddyfile.d/$(basename "$CADDY_FRAGMENT")"
    if ! grep -qF 'import Caddyfile.d/*.caddy' "$candidate_file"; then
        printf '\nimport Caddyfile.d/*.caddy\n' >>"$candidate_file"
    fi
    if [[ "$https" == true ]]; then
        write_caddy_fragment "$candidate_dir/Caddyfile.d/$(basename "$CADDY_FRAGMENT")"
    fi
    if ! (cd "$candidate_dir" && caddy validate --config Caddyfile --adapter caddyfile); then
        fail "The generated Caddy configuration is invalid; it was not applied."
    fi
    ok "Caddy configuration validated"
}

apply_caddy_candidate() {
    local candidate_dir="$TRANSACTION_DIR/caddy"
    [[ -d "$candidate_dir" ]] || return
    run_root mkdir -p "$(dirname "$CADDY_FRAGMENT")" || fail "Could not create the Caddy configuration directory."
    run_root install -m 0644 "$candidate_dir/Caddyfile" "$CADDY_FILE" || fail "Could not apply the Caddyfile."
    if [[ "$https" == true ]]; then
        run_root install -m 0644 "$candidate_dir/Caddyfile.d/$(basename "$CADDY_FRAGMENT")" "$CADDY_FRAGMENT" \
            || fail "Could not apply the Security Camera Network Caddy configuration."
        run_root systemctl enable --now caddy >/dev/null || fail "The official Caddy service could not be started."
    else
        run_root rm -f "$CADDY_FRAGMENT" || fail "Could not remove the previous HTTPS configuration."
    fi
    run_root systemctl reload caddy || fail "Caddy could not reload the validated configuration."
}

validate_systemd_unit() {
    local unit_file="$1" config_path="$2" quiet="${3:-false}" verify_log relevant_log unit_name
    local root_path config_value python_path server_path expected_exec
    verify_log="$TRANSACTION_DIR/systemd-verify.log"
    relevant_log="$TRANSACTION_DIR/systemd-verify-relevant.log"
    unit_name="$(basename "$unit_file")"

    [[ "$quiet" == true ]] || info "Validating service configuration..."
    [[ "$ROOT_DIR" == /* && -d "$ROOT_DIR" ]] || fail "The generated Security Camera Network service is invalid."
    [[ "$config_path" == /* && -f "$config_path" ]] || fail "The generated Security Camera Network service is invalid."
    [[ "$VENV_DIR/bin/python" == /* && -x "$VENV_DIR/bin/python" ]] || fail "The generated Security Camera Network service is invalid."
    [[ "$ROOT_DIR/server" == /* && -d "$ROOT_DIR/server" ]] || fail "The generated Security Camera Network service is invalid."

    root_path="$(systemd_escape_path "$ROOT_DIR")" || fail "The generated Security Camera Network service is invalid."
    config_value="$(systemd_escape_path "$config_path")" || fail "The generated Security Camera Network service is invalid."
    python_path="$(systemd_escape_path "$VENV_DIR/bin/python")" || fail "The generated Security Camera Network service is invalid."
    server_path="$(systemd_escape_path "$ROOT_DIR/server")" || fail "The generated Security Camera Network service is invalid."
    expected_exec="ExecStart=$python_path -m uvicorn app.main:app --app-dir $server_path --host $host --port $port"
    grep -Fxq "WorkingDirectory=$root_path" "$unit_file" \
        && grep -Fxq "Environment=SECURITYCAM_CONFIG=$config_value" "$unit_file" \
        && grep -Fxq "$expected_exec" "$unit_file" \
        || fail "The generated Security Camera Network service is invalid."

    if command -v systemd-analyze >/dev/null 2>&1; then
        if ! systemd-analyze verify "$unit_file" >"$verify_log" 2>&1; then
            awk -v name="$unit_name" -v service="$SERVICE_NAME" '
                { line[NR] = $0 }
                END {
                    for (i = 1; i <= NR; i++) {
                        if (index(line[i], name) || index(line[i], service)) {
                            keep[i] = 1
                            if (line[i-1] ~ /(WorkingDirectory=|ExecStart=|Environment=|not absolute|not executable|fatal error|bad unit file)/) keep[i-1] = 1
                            if (line[i+1] ~ /(WorkingDirectory=|ExecStart=|Environment=|not absolute|not executable|fatal error|bad unit file)/) keep[i+1] = 1
                        }
                    }
                    for (i = 1; i <= NR; i++) if (keep[i]) print line[i]
                }
            ' "$verify_log" >"$relevant_log"
            if [[ -s "$relevant_log" ]]; then
                if [[ "${SECURITYCAM_VERBOSE:-0}" == 1 ]]; then
                    cat "$verify_log" >&2
                else
                    cat "$relevant_log" >&2
                fi
                fail "The generated Security Camera Network service is invalid."
            fi
            [[ -s "$verify_log" ]] || fail "systemd-analyze could not validate the generated Security Camera Network service."
        fi
        [[ "${SECURITYCAM_VERBOSE:-0}" != 1 || ! -s "$verify_log" ]] || cat "$verify_log" >&2
    fi
    [[ "$quiet" == true ]] || ok "Service configuration valid"
}

wait_for_health() {
    local attempts=20
    while ((attempts > 0)); do
        if "$VENV_DIR/bin/python" - "$port" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=1) as response:
    raise SystemExit(response.status != 200 or json.load(response).get("status") != "online")
PY
        then
            if [[ "$https" != true ]] || "$VENV_DIR/bin/python" - "$lan_ip" <<'PY' >/dev/null 2>&1
import json, ssl, sys, urllib.request
context = ssl._create_unverified_context()
with urllib.request.urlopen(f"https://{sys.argv[1]}/health", context=context, timeout=1) as response:
    raise SystemExit(response.status != 200 or json.load(response).get("status") != "online")
PY
            then
                ok "Local health check passed"
                return
            fi
        fi
        sleep 0.5
        attempts=$((attempts - 1))
    done
    fail "The new configuration failed its local health check."
}

finish_transaction() {
    local final_unit="$TRANSACTION_DIR/securitycameranetwork.final.service"
    mv -f "$PENDING_CONFIG" "$CONFIG_FILE" || fail "Could not activate the validated configuration."
    if [[ "$start_on_boot" == true ]]; then
        write_systemd_unit "$final_unit" "$CONFIG_FILE"
        validate_systemd_unit "$final_unit" "$CONFIG_FILE" true
        run_root install -m 0644 "$final_unit" "$SERVICE_FILE" || fail "Could not finalize the systemd service."
        run_root systemctl daemon-reload || fail "systemd could not load the finalized service."
    fi
    TRANSACTION_ACTIVE=false
    trap - EXIT
    rm -rf -- "$TRANSACTION_DIR"
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

if [[ ${EUID:-$(id -u)} -eq 0 && -n "${SUDO_USER:-}" ]]; then
    fail "Run ./setup.sh as your normal user, not with sudo. Setup will request administrator access when needed."
fi

printf '\n========================================\n'
printf '  Security Camera Network Setup\n'
printf '========================================\n\n'

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

check_system_requirements

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

if [[ "$start_on_boot" == true ]]; then
    command -v systemctl >/dev/null 2>&1 || fail "systemd is required for start-on-boot but is unavailable. Choose no or install systemd, then rerun setup."
    printf '[OK] systemd\n'
fi

install_python_app

mkdir -p "$recordings_path" || fail "The recordings directory could not be created: $recordings_path"
begin_transaction
CURRENT_STAGE_REASON="Candidate application configuration is invalid."
write_config "$PENDING_CONFIG"

(
    cd "$ROOT_DIR/server" && SECURITYCAM_CONFIG="$PENDING_CONFIG" "$VENV_DIR/bin/python" -c 'from app.main import app'
) || fail "FastAPI application validation failed."

if [[ "$start_on_boot" == true ]]; then
    CURRENT_STAGE_REASON="Invalid systemd service configuration."
    write_systemd_unit "$TRANSACTION_DIR/securitycameranetwork.pending.service" "$PENDING_CONFIG"
    validate_systemd_unit "$TRANSACTION_DIR/securitycameranetwork.pending.service" "$PENDING_CONFIG"
fi

if [[ "$https" == true ]]; then
    CURRENT_STAGE_REASON="Caddy could not be installed."
    [[ "$lan_ip" != "LAN-IP-NOT-DETECTED" ]] || fail "HTTPS setup needs a detected LAN IP."
    install_caddy_official
    CURRENT_STAGE_REASON="Invalid Caddy configuration."
    prepare_caddy_candidate
elif [[ -f "$CADDY_FRAGMENT" ]]; then
    CURRENT_STAGE_REASON="The existing Caddy configuration could not be updated safely."
    command -v caddy >/dev/null 2>&1 && caddy version >/dev/null 2>&1 \
        || fail "The existing HTTPS configuration cannot be removed safely because Caddy is unavailable."
    prepare_caddy_candidate
fi

previous_port=""
CURRENT_STAGE_REASON="The requested server port is unavailable."
[[ "$PREVIOUS_CONFIG_EXISTS" == false ]] || previous_port="$(config_value_from "$CONFIG_FILE" port)"
if command -v ss >/dev/null 2>&1 \
    && ss -H -ltn "sport = :$port" 2>/dev/null | grep -q . \
    && [[ "$PREVIOUS_RUNNING" == false || "$port" != "$previous_port" ]]; then
    fail "Port $port is already in use. The proposed configuration was not applied."
fi

CUTOVER_STARTED=true
CURRENT_STAGE_REASON="The previous service could not be stopped cleanly."
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    run_root systemctl stop "$SERVICE_NAME" || fail "Could not stop the current server for reconfiguration."
elif [[ "$PREVIOUS_RUNNING" == true && "$PREVIOUS_CONFIG_EXISTS" == true ]]; then
    SECURITYCAM_CONFIG="$CONFIG_FILE" "$ROOT_DIR/securitycam" stop || fail "Could not stop the current server for reconfiguration."
fi

if [[ "$start_on_boot" == true ]]; then
    CURRENT_STAGE_REASON="The staged systemd service could not be started."
    run_root install -m 0644 "$TRANSACTION_DIR/securitycameranetwork.pending.service" "$SERVICE_FILE" \
        || fail "Could not stage the systemd service."
    run_root systemctl daemon-reload || fail "systemd could not load the staged service."
    run_root systemctl enable "$SERVICE_NAME" >/dev/null || fail "Could not enable the staged service."
    run_root systemctl start "$SERVICE_NAME" || fail "The staged FastAPI service could not start."
else
    if [[ -f "$SERVICE_FILE" ]]; then
        run_root systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
        run_root rm -f "$SERVICE_FILE" || fail "Could not remove the previous systemd service."
        run_root systemctl daemon-reload || fail "systemd could not apply the service change."
    fi
    SECURITYCAM_CONFIG="$PENDING_CONFIG" "$ROOT_DIR/securitycam" start \
        || fail "The staged FastAPI server could not start."
fi

CURRENT_STAGE_REASON="The validated Caddy configuration could not be applied."
apply_caddy_candidate
CURRENT_STAGE_REASON="The new configuration failed its local health check."
wait_for_health
CURRENT_STAGE_REASON="The validated configuration could not be finalized."
finish_transaction

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
