#!/usr/bin/env bash

set -eu
export MSYS_NO_PATHCONV=1

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../setup.sh
source "$REPO_DIR/setup.sh"

# Git Bash cannot resolve the Windows primary group; Linux resolves both normally.
id() {
    [[ "$1" == -un ]] && printf 'tester\n' || printf 'tester\n'
}

assert_equal() {
    [[ "$1" == "$2" ]] || { printf 'expected <%s>, got <%s>\n' "$2" "$1" >&2; exit 1; }
}

assert_equal "$(systemd_escape_path /home/test/securitycameranetwork)" "/home/test/securitycameranetwork"
assert_equal "$(systemd_escape_path /opt/securitycameranetwork)" "/opt/securitycameranetwork"
assert_equal "$(systemd_escape_path '/home/test user/security camera network')" '/home/test\x20user/security\x20camera\x20network'

test_dir="$(mktemp -d)"
trap 'rm -rf -- "$test_dir"' EXIT
ROOT_DIR="$test_dir/security camera network"
CONFIG_DIR="$ROOT_DIR/.securitycam"
VENV_DIR="$ROOT_DIR/.venv"
TRANSACTION_DIR="$CONFIG_DIR/transaction.test"
config_path="$TRANSACTION_DIR/config.pending.json"
unit_file="$TRANSACTION_DIR/securitycameranetwork.pending.service"
host=127.0.0.1
port=8000
mkdir -p "$ROOT_DIR/server" "$VENV_DIR/bin" "$TRANSACTION_DIR"
printf '#!/bin/sh\n' >"$VENV_DIR/bin/python"
chmod +x "$VENV_DIR/bin/python"
printf '{"recordings_path":"%s"}\n' "$ROOT_DIR/recordings with spaces" >"$config_path"

write_systemd_unit "$unit_file" "$config_path"
escaped_root="$(systemd_escape_path "$ROOT_DIR")"
grep -Fxq "WorkingDirectory=$escaped_root" "$unit_file"
grep -Fxq "ExecStart=$(systemd_escape_path "$VENV_DIR/bin/python") -m uvicorn app.main:app --app-dir $(systemd_escape_path "$ROOT_DIR/server") --host 127.0.0.1 --port 8000" "$unit_file"
! grep -Eq '^(WorkingDirectory|ExecStart|Environment)="' "$unit_file"

fake_bin="$test_dir/bin"
mkdir "$fake_bin"
cat >"$fake_bin/systemd-analyze" <<'SH'
#!/bin/sh
printf "/lib/systemd/system/snapd.service: Unknown key name 'RestartMode'.\n" >&2
if [ "${CANDIDATE_ERROR:-0}" = 1 ]; then
    printf '%s: Unit configuration has fatal error.\n' "$(basename "$2")" >&2
fi
exit 1
SH
chmod +x "$fake_bin/systemd-analyze"
normal_output="$(PATH="$fake_bin:$PATH" validate_systemd_unit "$unit_file" "$config_path" true 2>&1)"
[[ -z "$normal_output" ]] || { printf 'unrelated systemd warnings leaked: %s\n' "$normal_output" >&2; exit 1; }
set +e
candidate_output="$(CANDIDATE_ERROR=1 PATH="$fake_bin:$PATH" validate_systemd_unit "$unit_file" "$config_path" true 2>&1)"
candidate_status=$?
set -e
if [[ $candidate_status -eq 0 ]]; then
    printf 'candidate-specific systemd validation errors must fail\n' >&2
    exit 1
fi
[[ "$candidate_output" == *securitycameranetwork.pending.service* ]]
[[ "$candidate_output" != *snapd.service* ]]

rollback_dir="$test_dir/rollback"
TRANSACTION_DIR="$rollback_dir/transaction"
CONFIG_FILE="$rollback_dir/config.json"
SERVICE_FILE="$rollback_dir/securitycameranetwork.service"
CADDY_FILE="$rollback_dir/Caddyfile"
CADDY_FRAGMENT="$rollback_dir/securitycameranetwork.caddy"
CADDY_KEYRING="$rollback_dir/caddy.gpg"
CADDY_SOURCE="$rollback_dir/caddy.list"
mkdir -p "$TRANSACTION_DIR"
printf 'previous\n' >"$TRANSACTION_DIR/config.previous.json"
printf 'candidate\n' >"$CONFIG_FILE"
TRANSACTION_ACTIVE=true
CUTOVER_STARTED=false
PREVIOUS_CONFIG_EXISTS=true
PREVIOUS_SERVICE_EXISTS=false
PREVIOUS_SERVICE_ENABLED=false
PREVIOUS_RUNNING=false
PREVIOUS_CADDY_FILE_EXISTS=false
PREVIOUS_CADDY_FRAGMENT_EXISTS=false
PREVIOUS_CADDY_ACTIVE=false
PREVIOUS_CADDY_KEY_EXISTS=false
PREVIOUS_CADDY_SOURCE_EXISTS=false
CADDY_INSTALL_COMPLETED=true
FAILURE_REASON="Invalid systemd service configuration."
run_root() { "$@"; }
set +e
rollback_output="$(false; rollback_transaction 2>&1)"
rollback_status=$?
set -e
[[ $rollback_status -ne 0 ]]
[[ "$(cat "$CONFIG_FILE")" == previous ]]
[[ "$rollback_output" == *"Reason:"*"Invalid systemd service configuration."* ]]
[[ "$rollback_output" != *"Caddy could not be restored"* ]]

printf 'setup systemd checks passed\n'
