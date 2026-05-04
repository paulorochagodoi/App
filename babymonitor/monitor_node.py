"""Entry point for the Monitor Pi.

Manages WiFi connection FSM and writes the camera URL to
/etc/babymonitor/kiosk_url so babymonitor-kiosk.service can read it
and launch Chromium pointing at the right address.
"""
from __future__ import annotations
import signal
import subprocess
import sys
import threading

from babymonitor.common.config import load_monitor_config
from babymonitor.common.logger import get_logger
from babymonitor.network.state_machine import MonitorFSM

log = get_logger(__name__)


def restart_kiosk(url: str) -> None:
    log.info("Restarting kiosk → %s", url)
    try:
        subprocess.run(
            ["systemctl", "restart", "babymonitor-kiosk"],
            check=False,
            timeout=10,
        )
    except Exception as e:
        log.warning("Could not restart kiosk service: %s", e)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/babymonitor/monitor.yaml"
    cfg = load_monitor_config(config_path)
    log.info("Monitor node starting")

    fsm = MonitorFSM(
        camera_ap_ssid=cfg.camera_ap.ssid,
        camera_ap_password=cfg.camera_ap.password,
        fallback_ssid=cfg.fallback_wifi.ssid,
        fallback_password=cfg.fallback_wifi.password,
        kiosk_url_file=cfg.kiosk_url_file,
        default_camera_url=cfg.default_camera_url,
        on_camera_url=restart_kiosk,
    )
    fsm.start()

    stop_event = threading.Event()

    def _shutdown(signum: int, frame) -> None:
        log.info("Shutdown signal received (signal %d)", signum)
        fsm.stop()
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    stop_event.wait()
    fsm.join(timeout=5)
    log.info("Monitor node stopped")


if __name__ == "__main__":
    main()
