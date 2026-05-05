#!/usr/bin/env bash
# Setup script for the Camera Raspberry Pi
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/babymonitor"
CONFIG_DIR="/etc/babymonitor"

echo "=== BabyMonitor — Camera Pi setup ==="

# System packages
apt-get update -q
apt-get install -y \
    python3-pip \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    python3-picamera2 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-libcamera \
    libportaudio2 \
    portaudio19-dev \
    libasound2-dev \
    python3-pyaudio \
    network-manager \
    avahi-daemon \
    avahi-utils

# Python dependencies (pyaudio is already installed via apt above;
# --no-build-isolation lets pip find the system portaudio headers if it needs to compile)
pip3 install --break-system-packages \
    --extra-index-url https://pypi.org/simple \
    -r "${APP_DIR}/requirements.txt"

# Download hls.js if placeholder is present
HLS_FILE="${APP_DIR}/web/hls.min.js"
if grep -q 'placeholder' "${HLS_FILE}"; then
    echo "Downloading hls.js..."
    curl -fsSL "https://cdn.jsdelivr.net/npm/hls.js@latest/dist/hls.min.js" \
        -o "${HLS_FILE}" || echo "WARNING: Could not download hls.js, kiosk will use stub"
fi

# Install app
mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}"
cp -r "${APP_DIR}/babymonitor" "${INSTALL_DIR}/"
cp -r "${APP_DIR}/web" "${INSTALL_DIR}/"

# Config (don't overwrite existing)
if [ ! -f "${CONFIG_DIR}/camera.yaml" ]; then
    cp "${APP_DIR}/config/camera.yaml" "${CONFIG_DIR}/camera.yaml"
    echo "Config installed at ${CONFIG_DIR}/camera.yaml — edit WiFi credentials!"
fi

# Recordings directory
mkdir -p /opt/babymonitor/recordings
mkdir -p /tmp/hls

# Systemd
cp "${APP_DIR}/systemd/babymonitor-camera.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable babymonitor-camera
systemctl restart babymonitor-camera

echo ""
echo "=== Camera Pi setup complete ==="
echo "Edit ${CONFIG_DIR}/camera.yaml to configure fallback WiFi."
echo "Service status: systemctl status babymonitor-camera"
