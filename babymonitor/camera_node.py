"""Entry point for the Camera Pi."""
from __future__ import annotations
import asyncio
import os
import signal
import sys
import uvicorn

from babymonitor.common.config import load_camera_config
from babymonitor.common.constants import ConnectionState
from babymonitor.common.logger import get_logger
from babymonitor.network.state_machine import CameraFSM
from babymonitor.network.mdns import MDNSAdvertiser
from babymonitor.streaming.camera_stream import CameraStream
from babymonitor.camera.api_server import create_app

log = get_logger(__name__)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/babymonitor/camera.yaml"
    os.environ.setdefault("CAMERA_CONFIG", config_path)

    cfg = load_camera_config(config_path)
    log.info("Camera node starting — AP: %s", cfg.ap.ssid)

    stream = CameraStream(cfg.streaming)
    stream.start()

    app = create_app(cfg, stream)

    mdns_advertiser: MDNSAdvertiser | None = None

    def on_state_change(state: ConnectionState, detail: str) -> None:
        nonlocal mdns_advertiser
        if state == ConnectionState.STREAMING and mdns_advertiser is None:
            mdns_advertiser = MDNSAdvertiser(detail or cfg.ap.ip)
            mdns_advertiser.start()

    fsm = CameraFSM(
        ap_ssid=cfg.ap.ssid,
        ap_password=cfg.ap.password,
        fallback_ssid=cfg.fallback_wifi.ssid,
        fallback_password=cfg.fallback_wifi.password,
        on_state_change=on_state_change,
    )
    fsm.start()

    uv_config = uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port,
                               loop="asyncio", access_log=False)
    server = uvicorn.Server(uv_config)

    def _shutdown(signum: int, frame) -> None:
        log.info("Shutdown signal received (signal %d)", signum)
        server.should_exit = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        asyncio.run(server.serve())
    finally:
        fsm.stop()
        if mdns_advertiser:
            mdns_advertiser.stop()
        stream.stop()
        log.info("Camera node stopped")


if __name__ == "__main__":
    main()
