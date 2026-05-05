#!/usr/bin/env bash
# Setup script for the Monitor Raspberry Pi
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/babymonitor"
CONFIG_DIR="/etc/babymonitor"

echo "=== BabyMonitor — Monitor Pi setup ==="

# System packages
apt-get update -q
apt-get install -y \
    python3-pip \
    network-manager \
    avahi-daemon \
    avahi-utils \
    chromium-browser \
    xorg \
    openbox

# Python dependencies (monitor only needs FSM)
pip3 install --break-system-packages pyyaml zeroconf

# Install app (only the modules needed by monitor_node)
mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}"
cp -r "${APP_DIR}/babymonitor" "${INSTALL_DIR}/"

# Config
if [ ! -f "${CONFIG_DIR}/monitor.yaml" ]; then
    cp "${APP_DIR}/config/monitor.yaml" "${CONFIG_DIR}/monitor.yaml"
    echo "Config installed at ${CONFIG_DIR}/monitor.yaml"
fi

# Systemd services
cp "${APP_DIR}/systemd/babymonitor-monitor.service" /etc/systemd/system/
cp "${APP_DIR}/systemd/babymonitor-kiosk.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable babymonitor-monitor
systemctl enable babymonitor-kiosk
systemctl restart babymonitor-monitor

echo ""
echo "=== Monitor Pi setup complete ==="
echo "The kiosk will start automatically once the FSM connects to the camera."
echo "Service status: systemctl status babymonitor-monitor babymonitor-kiosk"
