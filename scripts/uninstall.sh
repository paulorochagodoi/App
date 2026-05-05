#!/usr/bin/env bash
# Uninstall BabyMonitor — removes all installed files, configs, and services.
# Run as root. Works on both Camera Pi and Monitor Pi.
set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

info()    { echo -e "${GREEN}[babymonitor]${NC} $*"; }
warning() { echo -e "${YELLOW}[babymonitor]${NC} $*"; }
error()   { echo -e "${RED}[babymonitor]${NC} $*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    error "Este script deve ser executado como root (sudo bash uninstall.sh)"
    exit 1
fi

echo ""
echo "=== BabyMonitor — Desinstalação ==="
echo ""
warning "Este script irá remover:"
echo "  • Serviços systemd (babymonitor-camera, babymonitor-monitor, babymonitor-kiosk)"
echo "  • Arquivos instalados em /opt/babymonitor/"
echo "  • Configurações em /etc/babymonitor/"
echo "  • Segmentos HLS em /tmp/hls/"
echo ""
read -r -p "Continuar? [s/N] " CONFIRM
if [[ ! "${CONFIRM}" =~ ^[sS]$ ]]; then
    echo "Cancelado."
    exit 0
fi

echo ""

# ── Serviços systemd ──────────────────────────────────────────────────────────

SERVICES=(babymonitor-camera babymonitor-monitor babymonitor-kiosk)

for svc in "${SERVICES[@]}"; do
    unit_file="/etc/systemd/system/${svc}.service"
    if systemctl list-unit-files "${svc}.service" &>/dev/null && \
       systemctl list-unit-files "${svc}.service" | grep -q "${svc}"; then
        info "Parando e desativando ${svc}..."
        systemctl stop    "${svc}" 2>/dev/null || true
        systemctl disable "${svc}" 2>/dev/null || true
    fi
    if [ -f "${unit_file}" ]; then
        rm -f "${unit_file}"
        info "Removido: ${unit_file}"
    fi
done

systemctl daemon-reload
info "systemd recarregado."

# ── Gravações (confirmação separada) ─────────────────────────────────────────

RECORDINGS_DIR="/opt/babymonitor/recordings"
if [ -d "${RECORDINGS_DIR}" ] && [ -n "$(ls -A "${RECORDINGS_DIR}" 2>/dev/null)" ]; then
    echo ""
    warning "Foram encontradas gravações em ${RECORDINGS_DIR}."
    read -r -p "Remover gravações de vídeo? [s/N] " CONFIRM_REC
    if [[ "${CONFIRM_REC}" =~ ^[sS]$ ]]; then
        rm -rf "${RECORDINGS_DIR}"
        info "Gravações removidas."
    else
        info "Gravações mantidas em ${RECORDINGS_DIR}."
    fi
fi

# ── Arquivos instalados ───────────────────────────────────────────────────────

INSTALL_DIR="/opt/babymonitor"
if [ -d "${INSTALL_DIR}" ]; then
    rm -rf "${INSTALL_DIR}"
    info "Removido: ${INSTALL_DIR}"
fi

# ── Configurações ─────────────────────────────────────────────────────────────

CONFIG_DIR="/etc/babymonitor"
if [ -d "${CONFIG_DIR}" ]; then
    rm -rf "${CONFIG_DIR}"
    info "Removido: ${CONFIG_DIR}"
fi

# ── HLS temporário ───────────────────────────────────────────────────────────

HLS_DIR="/tmp/hls"
if [ -d "${HLS_DIR}" ]; then
    rm -rf "${HLS_DIR}"
    info "Removido: ${HLS_DIR}"
fi

# ── Logs do systemd (journal) ─────────────────────────────────────────────────

if command -v journalctl &>/dev/null; then
    for svc in "${SERVICES[@]}"; do
        journalctl --rotate --vacuum-time=1s --unit="${svc}" &>/dev/null || true
    done
    info "Entradas de log do journal limpas."
fi

echo ""
echo "=== Desinstalação concluída ==="
info "O BabyMonitor foi completamente removido deste dispositivo."
echo ""
echo "Os pacotes do sistema instalados pelas dependências (GStreamer, PortAudio,"
echo "Chromium, etc.) NÃO foram removidos. Para removê-los manualmente:"
echo ""
echo "  # Pi Câmera:"
echo "  sudo apt-get remove --purge python3-pyaudio portaudio19-dev \\"
echo "      gstreamer1.0-libcamera gstreamer1.0-plugins-bad \\"
echo "      gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \\"
echo "      gstreamer1.0-libav avahi-daemon avahi-utils"
echo ""
echo "  # Pi Monitor:"
echo "  sudo apt-get remove --purge chromium-browser openbox xorg \\"
echo "      avahi-daemon avahi-utils"
echo ""
echo "  sudo apt-get autoremove"
