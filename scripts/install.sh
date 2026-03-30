#!/usr/bin/env bash
# install.sh – Install or update the Pi Mirror Remote Management Daemon
#
# Run as root (or with sudo) on the Raspberry Pi:
#   sudo bash scripts/install.sh
#
# The script is idempotent: safe to run multiple times for updates.

set -euo pipefail

INSTALL_DIR="/opt/pi-mirror"
CONFIG_DIR="/etc/pi-mirror"
SERVICE_FILE="/etc/systemd/system/pi-mirror-daemon.service"
SERVICE_USER="pi-mirror"
LOG_FILE="/var/log/pi-mirror-daemon.log"

# Resolve repository root (directory containing this script's parent)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*" >&2; }
error() { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        error "This script must be run as root.  Try: sudo bash scripts/install.sh"
    fi
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

create_service_user() {
    if id "${SERVICE_USER}" &>/dev/null; then
        info "Service user '${SERVICE_USER}' already exists."
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin \
                --groups gpio,video "${SERVICE_USER}"
        info "Created system user '${SERVICE_USER}'."
    fi
}

install_python_deps() {
    info "Installing Python dependencies …"
    # RPi.GPIO is the only runtime dep; it's usually pre-installed on Raspberry Pi OS.
    if ! python3 -m pip install --quiet RPi.GPIO 2>/dev/null; then
        warn "pip install RPi.GPIO failed. If RPi.GPIO is not already installed the"
        warn "PIR sensor will be unavailable (display will stay on permanently)."
        warn "Install manually: sudo pip3 install RPi.GPIO"
    fi
}

create_directories() {
    install -d -m 755 "${INSTALL_DIR}"
    install -d -m 750 -o root -g "${SERVICE_USER}" "${CONFIG_DIR}"
    info "Directories ready: ${INSTALL_DIR}  ${CONFIG_DIR}"
}

setup_tmpfiles() {
    echo "d /run/pi-mirror 0750 ${SERVICE_USER} ${SERVICE_USER} -" \
        > /etc/tmpfiles.d/pi-mirror.conf
    # Create the directory for the current boot if it doesn't already exist
    systemd-tmpfiles --create /etc/tmpfiles.d/pi-mirror.conf 2>/dev/null || \
        install -d -m 750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" /run/pi-mirror
    info "Runtime directory /run/pi-mirror configured."
}

copy_files() {
    # Python source files
    for f in mirror_daemon.py mirror_cmd.py pir_display.py; do
        install -m 644 -o root -g root "${REPO_ROOT}/${f}" "${INSTALL_DIR}/${f}"
    done

    # Default config (do NOT overwrite existing – preserves user customisations)
    if [[ ! -f "${CONFIG_DIR}/mirror.conf" ]]; then
        install -m 640 -o root -g "${SERVICE_USER}" \
                "${REPO_ROOT}/config/mirror.conf" "${CONFIG_DIR}/mirror.conf"
        info "Installed default config to ${CONFIG_DIR}/mirror.conf"
    else
        info "Existing config preserved at ${CONFIG_DIR}/mirror.conf"
    fi

    info "Python files installed to ${INSTALL_DIR}"
}

setup_log_file() {
    touch "${LOG_FILE}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_FILE}"
    chmod 640 "${LOG_FILE}"
    info "Log file prepared: ${LOG_FILE}"
}

install_service() {
    install -m 644 -o root -g root \
            "${REPO_ROOT}/systemd/pi-mirror-daemon.service" "${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl enable pi-mirror-daemon.service
    info "Systemd unit installed and enabled."
}

setup_sudoers() {
    local sudoers_file="/etc/sudoers.d/pi-mirror"
    cat > "${sudoers_file}" <<EOF
# Allow pi-mirror service to reboot or shut down without a password
${SERVICE_USER} ALL=(root) NOPASSWD: /sbin/reboot, /sbin/shutdown
EOF
    chmod 440 "${sudoers_file}"
    info "Sudoers rule written to ${sudoers_file}"
}

setup_authorized_keys() {
    # Guide the operator; we don't write keys automatically.
    cat <<EOF

---- SSH Authorized-Keys Setup (manual step) --------------------------------
To allow a remote host to issue commands via SSH, add a line like this to
~${SERVICE_USER}/.ssh/authorized_keys  (or to the relevant user's file):

  command="python3 ${INSTALL_DIR}/mirror_cmd.py --user <username>",no-pty,no-agent-forwarding,no-X11-forwarding <PUBLIC_KEY>

Replace <username> with the logical name of the remote caller (used for
admin privilege checks) and <PUBLIC_KEY> with the caller's public key.

Admin usernames must also be listed in ${CONFIG_DIR}/mirror.conf under
  admin_users = <username>
-----------------------------------------------------------------------------

EOF
}

start_service() {
    if systemctl is-active --quiet pi-mirror-daemon.service; then
        systemctl restart pi-mirror-daemon.service
        info "Service restarted."
    else
        systemctl start pi-mirror-daemon.service
        info "Service started."
    fi
    systemctl status pi-mirror-daemon.service --no-pager || true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

require_root
create_service_user
install_python_deps
create_directories
setup_tmpfiles
copy_files
setup_log_file
install_service
setup_sudoers
setup_authorized_keys
start_service

info "Pi Mirror Daemon installation complete."
