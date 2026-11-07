#!/usr/bin/env bash
# VPS Discord Bot — One-Command Installer
# Usage: bash <(curl -s https://raw.githubusercontent.com/YOUR_REPO/main/install.sh)
#
# This script:
#   1. Checks system requirements
#   2. Installs system packages (Python, LXD, openssh-client)
#   3. Clones/downloads the bot files
#   4. Installs Python dependencies
#   5. Launches python3 setup.py

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
WARN='\033[0;33m'
ERR='\033[0;31m'
RST='\033[0m'

REPO_URL="https://github.com/YOUR_USERNAME/vps-discord-bot"
RAW_BASE="https://raw.githubusercontent.com/YOUR_USERNAME/vps-discord-bot/main"
INSTALL_DIR="/opt/vps-discord-bot"

FILES=(
    "bot.py"
    "setup.py"
    "vps.py"
    "monitor.py"
    "queue_manager.py"
    "requirements.txt"
)

p()   { echo -e "${1}${RST}"; }
ok()  { echo -e "${GREEN}  ✓  ${1}${RST}"; }
info(){ echo -e "${CYAN}  →  ${1}${RST}"; }
warn(){ echo -e "${WARN}  !  ${1}${RST}"; }
die() { echo -e "${ERR}  ✗  ${1}${RST}"; exit 1; }

banner() {
    echo ""
    echo -e "${CYAN}$(printf '─%.0s' {1..54})${RST}"
    echo -e "${CYAN}  ${1}${RST}"
    echo -e "${CYAN}$(printf '─%.0s' {1..54})${RST}"
    echo ""
}

# ── Root check ────────────────────────────────────────────────────────────────
banner "VPS Discord Bot — Installer"

if [ "$(id -u)" -ne 0 ]; then
    die "This installer must be run as root. Try: sudo bash <(curl -s ...)"
fi
ok "Running as root"

# ── OS check ─────────────────────────────────────────────────────────────────
if ! grep -qiE 'ubuntu|debian' /etc/os-release 2>/dev/null; then
    warn "This installer is tested on Ubuntu 22.04 LTS. Other distros may require adjustments."
fi

OS_NAME=$(grep -oP '(?<=^NAME=).+' /etc/os-release | tr -d '"' || echo "Unknown")
OS_VER=$(grep -oP '(?<=^VERSION_ID=).+' /etc/os-release | tr -d '"' || echo "?")
info "Detected OS: ${OS_NAME} ${OS_VER}"

# ── Package installation ──────────────────────────────────────────────────────
banner "Installing System Packages"

apt-get update -qq
info "Installing Python 3, pip, git, curl, openssh-client..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    openssh-client \
    openssh-server \
    wget \
    snapd \
    2>/dev/null
ok "System packages installed"

# ── LXD ──────────────────────────────────────────────────────────────────────
banner "Setting Up LXD"

if command -v lxc &>/dev/null; then
    ok "LXD already installed ($(lxc --version 2>/dev/null || echo 'unknown version'))"
else
    info "Installing LXD via snap..."
    snap install lxd
    ok "LXD installed"
fi

# Initialize LXD if not already done
if ! lxc profile show default &>/dev/null 2>&1; then
    info "Initializing LXD (auto mode)..."
    lxd init --auto
    ok "LXD initialized"
else
    # Check if storage pool exists
    if ! lxc storage show zfs-new &>/dev/null 2>&1; then
        info "Creating ZFS storage pool..."
        DISK_FREE=$(df / | awk 'NR==2{print int($4/1024/1024)}')
        POOL_SIZE=$((DISK_FREE > 100 ? DISK_FREE - 20 : DISK_FREE / 2))
        POOL_SIZE="${POOL_SIZE}GB"
        info "Allocating ${POOL_SIZE} for ZFS pool..."
        lxc storage create zfs-new zfs size="${POOL_SIZE}" 2>/dev/null || \
            lxc storage create zfs-new dir 2>/dev/null || \
            warn "Could not create ZFS pool — storage pool may already exist under another name"
    else
        ok "ZFS storage pool already exists"
    fi
fi

# Pull Ubuntu 22.04 image
if ! lxc image list | grep -q "ubuntu/22.04" 2>/dev/null; then
    info "Pulling Ubuntu 22.04 LXD image (this may take a few minutes)..."
    lxc image copy ubuntu:22.04 local: --alias ubuntu/22.04 || \
        warn "Image pull failed — ensure your server has internet access"
else
    ok "Ubuntu 22.04 image already cached"
fi

# Enable IP forwarding
info "Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf || \
    echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
ok "IP forwarding enabled"

# ── Create install directory ──────────────────────────────────────────────────
banner "Installing Bot Files"

mkdir -p "${INSTALL_DIR}"
info "Install directory: ${INSTALL_DIR}"

# ── Download bot files ────────────────────────────────────────────────────────
# If git is available and REPO_URL is configured, use git clone
# Otherwise fall back to downloading individual raw files

if git ls-remote "${REPO_URL}" HEAD &>/dev/null 2>&1; then
    info "Cloning repository..."
    git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}_tmp"
    cp -r "${INSTALL_DIR}_tmp/." "${INSTALL_DIR}/"
    rm -rf "${INSTALL_DIR}_tmp"
    ok "Repository cloned"
else
    info "Downloading bot files from ${RAW_BASE}..."
    for f in "${FILES[@]}"; do
        info "  Downloading ${f}..."
        curl -fsSL "${RAW_BASE}/${f}" -o "${INSTALL_DIR}/${f}" || \
            die "Failed to download ${f}. Check REPO_URL in this script."
    done
    ok "Files downloaded"
fi

cd "${INSTALL_DIR}"

# ── Python dependencies ───────────────────────────────────────────────────────
banner "Installing Python Dependencies"

info "Upgrading pip..."
python3 -m pip install --upgrade pip -q

info "Installing bot requirements..."
python3 -m pip install -r requirements.txt -q
ok "Python dependencies installed"

# ── Verify LXD Python binding ─────────────────────────────────────────────────
if ! python3 -c "import pylxd" 2>/dev/null; then
    info "Installing pylxd..."
    python3 -m pip install pylxd -q
    ok "pylxd installed"
else
    ok "pylxd already available"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
banner "Installation Complete"

ok "Bot files installed to: ${INSTALL_DIR}"
ok "LXD configured and ready"
ok "Python dependencies installed"

echo ""
p "${CYAN}$(printf '─%.0s' {1..54})"
p "${BOLD}  Running first-time setup wizard...${RST}"
p "${CYAN}$(printf '─%.0s' {1..54})${RST}"
echo ""

# ── Launch setup ──────────────────────────────────────────────────────────────
cd "${INSTALL_DIR}"
python3 setup.py

echo ""
ok "All done! Your VPS Discord Bot is configured."
p "${CYAN}  Bot dir: ${INSTALL_DIR}${RST}"
p "${CYAN}  Start manually: cd ${INSTALL_DIR} && python3 bot.py${RST}"
p "${CYAN}  Service status: systemctl status vps-discord-bot${RST}"
p "${CYAN}  Service logs:   journalctl -u vps-discord-bot -f${RST}"
echo ""
