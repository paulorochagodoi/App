#!/usr/bin/env bash
# Setup script for the Camera Raspberry Pi
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/babymonitor"
CONFIG_DIR="/etc/babymonitor"

GST_MIN_MAJOR=1
GST_MIN_MINOR=22   # audio pads on hlssink2 require >= 1.22

echo "=== BabyMonitor — Camera Pi setup ==="

# ── OS version check ──────────────────────────────────────────────────────────
OS_CODENAME="$(lsb_release -cs 2>/dev/null || echo unknown)"
echo "Detected OS: $(lsb_release -ds 2>/dev/null || echo unknown) (${OS_CODENAME})"

if [ "${OS_CODENAME}" = "bullseye" ]; then
    echo ""
    echo "WARNING: Raspberry Pi OS Bullseye ships GStreamer 1.18."
    echo "         GStreamer >= ${GST_MIN_MAJOR}.${GST_MIN_MINOR} is required for audio in the stream."
    echo "         Adding Debian Bullseye backports and attempting to install GStreamer 1.22..."
    echo ""
    # Add backports only if not already present
    BACKPORTS_LIST="/etc/apt/sources.list.d/bullseye-backports.list"
    if [ ! -f "${BACKPORTS_LIST}" ]; then
        echo "deb http://deb.debian.org/debian bullseye-backports main" \
            > "${BACKPORTS_LIST}"
    fi
    apt-get update -q
    # Try to pull GStreamer from backports; fall through if unavailable
    apt-get install -y -t bullseye-backports \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        2>/dev/null && echo "GStreamer from backports installed." \
        || echo "WARNING: GStreamer 1.22 not available in backports — stream will work without audio. Upgrade to Raspberry Pi OS Bookworm for full audio support."
elif [ "${OS_CODENAME}" = "bookworm" ]; then
    echo "Raspberry Pi OS Bookworm detected — GStreamer 1.22 available in default repos."
else
    echo "Unrecognised OS codename '${OS_CODENAME}'. Proceeding with default package repos."
fi

# ── System packages ───────────────────────────────────────────────────────────
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

# ── GStreamer version verification ────────────────────────────────────────────
GST_VERSION="$(gst-launch-1.0 --version 2>/dev/null \
    | grep -oP 'GStreamer \K[0-9]+\.[0-9]+' | head -1 || echo "0.0")"
GST_MAJOR="${GST_VERSION%%.*}"
GST_MINOR="${GST_VERSION#*.}"

echo ""
echo "Installed GStreamer version: ${GST_VERSION}"

if [ "${GST_MAJOR}" -gt "${GST_MIN_MAJOR}" ] || \
   { [ "${GST_MAJOR}" -eq "${GST_MIN_MAJOR}" ] && [ "${GST_MINOR}" -ge "${GST_MIN_MINOR}" ]; }; then
    echo "GStreamer ${GST_VERSION} >= ${GST_MIN_MAJOR}.${GST_MIN_MINOR} — audio stream enabled."
else
    echo "WARNING: GStreamer ${GST_VERSION} < ${GST_MIN_MAJOR}.${GST_MIN_MINOR}."
    echo "         The stream will work but WITHOUT audio."
    echo "         To enable audio, upgrade to Raspberry Pi OS Bookworm:"
    echo "           https://www.raspberrypi.com/software/"
fi
echo ""

# ── Python dependencies ───────────────────────────────────────────────────────
# pyaudio is installed via apt above; --no-build-isolation lets pip find the
# system portaudio headers when it needs to compile
pip3 install --break-system-packages \
    --extra-index-url https://pypi.org/simple \
    -r "${APP_DIR}/requirements.txt"

# ── Download hls.js if placeholder is present ─────────────────────────────────
HLS_FILE="${APP_DIR}/web/hls.min.js"
if grep -q 'placeholder' "${HLS_FILE}"; then
    echo "Downloading hls.js..."
    curl -fsSL "https://cdn.jsdelivr.net/npm/hls.js@latest/dist/hls.min.js" \
        -o "${HLS_FILE}" || echo "WARNING: Could not download hls.js, kiosk will use stub"
fi

# ── Install app ───────────────────────────────────────────────────────────────
mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}"
cp -r "${APP_DIR}/babymonitor" "${INSTALL_DIR}/"
cp -r "${APP_DIR}/web" "${INSTALL_DIR}/"

# Config (don't overwrite existing)
if [ ! -f "${CONFIG_DIR}/camera.yaml" ]; then
    cp "${APP_DIR}/config/camera.yaml" "${CONFIG_DIR}/camera.yaml"
    echo "Config installed at ${CONFIG_DIR}/camera.yaml — edit WiFi credentials!"
fi

# ── Runtime directories ───────────────────────────────────────────────────────
mkdir -p /opt/babymonitor/recordings
mkdir -p /tmp/hls

# ── Systemd ───────────────────────────────────────────────────────────────────
cp "${APP_DIR}/systemd/babymonitor-camera.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable babymonitor-camera
systemctl restart babymonitor-camera

echo ""
echo "=== Camera Pi setup complete ==="
echo "Edit ${CONFIG_DIR}/camera.yaml to configure fallback WiFi."
echo "Service status: systemctl status babymonitor-camera"
