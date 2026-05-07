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
    xorg \
    openbox \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gtk-3.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-gtk3

# Python dependencies
pip3 install --break-system-packages pyyaml zeroconf websockets

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
