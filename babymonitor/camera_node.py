"""Entry point for the Camera Pi."""
from __future__ import annotations
import asyncio
import os
import signal
import sys
import threading
import uvicorn

from babymonitor.common.config import load_camera_config
from babymonitor.common.constants import ConnectionState
from babymonitor.common.logger import get_logger
from babymonitor.network.state_machine import CameraFSM
from babymonitor.network.mdns import MDNSAdvertiser
from babymonitor.streaming.camera_stream import CameraStream
from babymonitor.camera.recorder import Recorder
from babymonitor.camera.cry_detector import CryDetector
from babymonitor.camera.api_server import create_app

log = get_logger(__name__)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/babymonitor/camera.yaml"
    os.environ.setdefault("CAMERA_CONFIG", config_path)

    cfg = load_camera_config(config_path)
    log.info("Camera node starting — AP: %s", cfg.ap.ssid)

    # GStreamer pipeline
    stream = CameraStream(cfg.streaming, cfg.recordings)
    stream.start()

    # Recorder (wraps stream)
    recorder = Recorder(
        stream,
        cfg.recordings.output_dir,
        max_recordings=cfg.recordings.max_recordings,
        min_free_mb=cfg.recordings.min_free_mb,
    )

    # Cry detector (created before app so health endpoint can check it)
    cry_detector = CryDetector(
        sample_rate=cfg.cry_detector.sample_rate,
        chunk_size=cfg.cry_detector.chunk_size,
        threshold=cfg.cry_detector.threshold,
        silence_timeout=cfg.cry_detector.silence_timeout,
    )

    # FastAPI app
    app = create_app(cfg, recorder, cry_detector)

    # Async bridge: detector callbacks → asyncio loop
    loop = asyncio.new_event_loop()

    def on_cry(confidence: float) -> None:
        if not recorder.is_recording():
            recorder.start()
        asyncio.run_coroutine_threadsafe(
            app.state.broadcast_alert(confidence), loop
        )

    def on_silence() -> None:
        if recorder.is_recording():
            recorder.stop()

    # Calibrate ambient noise before starting detection
    if cfg.cry_detector.calibrate_on_start:
        cry_detector.calibrate()

    cry_detector.start(on_cry, on_silence)

    # Network FSM
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

    # uvicorn server
    uv_config = uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        loop="asyncio",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)

    def _shutdown(signum: int, frame) -> None:
        log.info("Shutdown signal received (signal %d)", signum)
        server.should_exit = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(server.serve())
    finally:
        log.info("Shutting down components...")
        fsm.stop()
        cry_detector.stop()
        if recorder.is_recording():
            recorder.stop()
        if mdns_advertiser:
            mdns_advertiser.stop()
        stream.stop()
        log.info("Camera node stopped")


if __name__ == "__main__":
    main()
