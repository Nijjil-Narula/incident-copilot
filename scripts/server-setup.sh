#!/usr/bin/env bash
# scripts/server-setup.sh
# Idempotent server provisioning script for Ubuntu 24.04 LTS (ARM64 / aarch64)
# Target: Oracle Cloud Always Free VM (VM.Standard.A1.Flex, 2 OCPU, 12 GB RAM)
#
# Usage (run as root or with sudo):
#   sudo ./scripts/server-setup.sh

set -euo pipefail

echo "=========================================================="
echo " Starting Incident Copilot Host Provisioning (ARM64)     "
echo "=========================================================="

# ── 1. System Package Updates & Prerequisites ──────────────────────────────
echo "--> Updating package lists and installing base tools..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    ufw \
    unattended-upgrades \
    gzip

# ── 2. Configure Automatic Security Updates ────────────────────────────────
echo "--> Configuring unattended-upgrades for automatic security updates..."
cat <<'EOF' > /etc/apt/apt.conf.d/20auto-upgrades
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable unattended-upgrades --now

# ── 3. Configure 2 GB Swap Space (if none exists) ──────────────────────────
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
    echo "--> No active swap found. Creating 2 GB swapfile..."
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    if ! grep -qs '/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    echo "--> 2 GB swap successfully configured."
else
    echo "--> Swap space is already configured. Skipping swap creation."
fi

# ── 4. Install Docker Engine & Compose Plugin from Official Docker Apt Repo 
echo "--> Installing official Docker repository for Ubuntu ARM64..."
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
fi

ARCH="$(dpkg --print-architecture)"
CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y --no-install-recommends \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

systemctl enable docker --now

# ── 5. Create Non-Root Deploy User ─────────────────────────────────────────
DEPLOY_USER="deploy"
if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
    echo "--> Creating non-root deploy user: ${DEPLOY_USER}..."
    adduser --disabled-password --gecos "Incident Copilot Deploy User" "${DEPLOY_USER}"
else
    echo "--> Deploy user '${DEPLOY_USER}' already exists."
fi

# Add deploy user to docker group
usermod -aG docker "${DEPLOY_USER}"

# ── 6. Application Directory Setup ─────────────────────────────────────────
APP_DIR="/opt/incident-copilot"
echo "--> Setting up application directory at ${APP_DIR}..."
mkdir -p "${APP_DIR}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}"

# ── 7. Firewall Rules (UFW & Oracle Cloud OCI Notes) ────────────────────────
# IMPORTANT ORACLE CLOUD FIREWALL NOTES:
# 1. Oracle Cloud Ubuntu images ship with default iptables rules in /etc/iptables/rules.v4
#    that may reject non-SSH traffic before UFW rules execute. You must ensure incoming
#    traffic on ports 80 and 443 is permitted in iptables or flush blocking reject rules.
# 2. You MUST ALSO add Ingress Rules in your Oracle Cloud VCN Security List:
#    - Source: 0.0.0.0/0, IP Protocol: TCP, Destination Port: 80
#    - Source: 0.0.0.0/0, IP Protocol: TCP, Destination Port: 443
# 3. CRITICAL: By default, Docker manipulates iptables directly, which BYPASSES UFW filters.
#    This is why only Caddy publishes ports on the host (80 and 443), while API, UI,
#    Postgres, and Redis remain completely unexposed to the host network interface.

echo "--> Configuring UFW firewall (permitting 22, 80, 443 only)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw allow 80/tcp comment "HTTP (Caddy ACME challenge)"
ufw allow 443/tcp comment "HTTPS (Caddy)"
ufw --force enable

echo "=========================================================="
echo " Host provisioning complete!                              "
echo " Next steps:                                              "
echo " 1. Clone repository into ${APP_DIR} as user '${DEPLOY_USER}'."
echo " 2. Copy .env.example to .env and configure secrets.      "
echo " 3. Run 'chmod 600 ${APP_DIR}/.env' to protect credentials. "
echo " 4. Ensure Oracle OCI Security List opens ports 80 & 443. "
echo "=========================================================="
